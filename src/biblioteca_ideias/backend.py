from __future__ import annotations

from pathlib import Path

import html
import os
import re

from PySide6.QtCore import QObject, Property, QLockFile, QStandardPaths, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication

from .storage import COLUMNS, INITIAL_BODY, MarkdownStore, StorageError
from .git_sync import STATE_ERROR, STATE_LOCAL, SyncCoordinator


APP_ID = "br.com.torugo.bibliotecadeideias"


def default_vault_path() -> Path:
    override = os.environ.get("BIBLIOTECA_IDEIAS_VAULT")
    if override:
        return Path(override)
    root = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
    return root / "cofre"


def default_state_path() -> Path:
    explicit = os.environ.get("BIBLIOTECA_IDEIAS_STATE")
    if explicit:
        return Path(explicit)
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "Torugo" / "Biblioteca de Ideias"


class Backend(QObject):
    ideasChanged = Signal()
    noticeChanged = Signal()
    syncStateChanged = Signal()
    authStatusChanged = Signal()

    def __init__(
        self,
        vault: Path | None = None,
        parent: QObject | None = None,
        sync_coordinator: SyncCoordinator | None = None,
    ):
        super().__init__(parent)
        self._notice = "Pronto. Seus dados estão somente neste computador."
        vault_path = vault or default_vault_path()
        vault_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = QLockFile(str(vault_path.with_suffix(".lock")))
        self._lock.setStaleLockTime(30_000)
        if not self._lock.tryLock(0):
            raise StorageError("A Biblioteca de Ideias já está aberta para este cofre.")
        state_path = default_state_path() if vault is None else vault_path.parent / ".state-test"
        self._store = MarkdownStore(vault_path, state_dir=state_path)
        self._ideas: list[dict[str, str]] = []
        self._sync_state = STATE_LOCAL
        self._auth_status = "GitHub CLI não autenticado"
        self._sync = sync_coordinator or SyncCoordinator(vault_path, auto_sync=vault is None)
        self._sync.stateChanged.connect(self._on_sync_state)
        self._sync.authChanged.connect(self._on_auth_status)
        self._sync.repositoryUpdated.connect(
            self._reload_after_fast_forward, Qt.ConnectionType.QueuedConnection
        )
        self.reload()

    @Property("QVariantList", notify=ideasChanged)
    def ideas(self) -> list[dict[str, str]]:
        return self._ideas

    @Property("QVariantList", constant=True)
    def columns(self) -> list[dict[str, str]]:
        return [{"chave": key, "titulo": title} for key, title in COLUMNS]

    @Property(str, notify=noticeChanged)
    def notice(self) -> str:
        return self._notice

    @Property(str, constant=True)
    def vaultPath(self) -> str:
        return str(self._store.vault)

    @Property(int, notify=ideasChanged)
    def ideaCount(self) -> int:
        return len(self._ideas)

    @Property(str, notify=syncStateChanged)
    def syncStatus(self) -> str:
        return self._sync_state

    @Property(str, notify=authStatusChanged)
    def authStatus(self) -> str:
        return self._auth_status

    @Property(str, constant=True)
    def initialBody(self) -> str:
        return INITIAL_BODY

    def _set_notice(self, message: str) -> None:
        self._notice = message
        self.noticeChanged.emit()

    @Slot(str)
    def _on_sync_state(self, state: str) -> None:
        self._sync_state = state
        self.syncStateChanged.emit()

    @Slot(bool, str)
    def _on_auth_status(self, authenticated: bool, username: str) -> None:
        if authenticated:
            self._auth_status = "Autenticado pelo GitHub CLI"
            if username:
                self._auth_status += f" como {username}"
        else:
            self._auth_status = "GitHub CLI não autenticado"
        self.authStatusChanged.emit()

    @Slot()
    def _reload_after_fast_forward(self) -> None:
        try:
            self._ideas = [idea.as_dict() for idea in self._store.list_ideas()]
            self.ideasChanged.emit()
            self._set_notice("Cofre atualizado pelo Git.")
        except StorageError as exc:
            self._set_notice(str(exc))
            self._sync_state = STATE_ERROR
            self.syncStateChanged.emit()

    @Slot()
    def reload(self) -> None:
        try:
            self._ideas = [idea.as_dict() for idea in self._store.list_ideas()]
            self.ideasChanged.emit()
            self._set_notice("Cofre recarregado com segurança.")
        except StorageError as exc:
            self._set_notice(str(exc))

    @Slot(str, str, result=bool)
    def createIdea(self, title: str, body: str) -> bool:
        try:
            created = self._store.create(title, body)
            self.reload()
            self._set_notice("Ideia criada em Entrada.")
            relative = created.path.relative_to(self._store.vault).as_posix()
            self._sync.request_save((relative,), f"ideia: criar {created.title}")
            return True
        except StorageError as exc:
            self._set_notice(str(exc))
            return False

    @Slot(str, str, str, str, str, result=bool)
    def saveIdea(self, idea_id: str, title: str, body: str, status: str, expected_hash: str) -> bool:
        try:
            current = {idea.idea_id: idea for idea in self._store.list_ideas()}[idea_id]
            saved = self._store.save_idea(idea_id, title, body, status, expected_hash)
            self.reload()
            self._set_notice("Ideia salva.")
            paths = (
                current.path.relative_to(self._store.vault).as_posix(),
                saved.path.relative_to(self._store.vault).as_posix(),
            )
            self._sync.request_save(paths, f"ideia: atualizar {saved.title}")
            return True
        except (KeyError, StorageError) as exc:
            self._set_notice(str(exc))
            return False

    @Slot()
    def syncNow(self) -> None:
        self._sync.request_sync()

    def close(self) -> None:
        self._sync.close()

    @Slot(str, result=str)
    def safePreviewMarkdown(self, markdown: str) -> str:
        image_references: set[str] = set()

        def normalized_reference(value: str) -> str:
            return re.sub(r"\s+", " ", value.strip()).casefold()

        def replace_inline(match: re.Match[str]) -> str:
            return f"[imagem: {match.group(1)}]"

        def replace_reference(match: re.Match[str]) -> str:
            alt = match.group(1)
            reference = match.group(2) or alt
            image_references.add(normalized_reference(reference))
            return f"[imagem: {alt}]"

        def replace_shortcut(match: re.Match[str]) -> str:
            alt = match.group(1)
            image_references.add(normalized_reference(alt))
            return f"[imagem: {alt}]"

        without_images = re.sub(
            r"!\[([^\]\n]*)\]\s*\(\s*(?:<[^>\n]*>|[^)\n]*?)\s*\)",
            replace_inline,
            markdown,
        )
        without_images = re.sub(
            r"!\[([^\]\n]*)\][ \t]*\[([^\]\n]*)\]",
            replace_reference,
            without_images,
        )
        without_images = re.sub(
            r"!\[([^\]\n]+)\](?![ \t]*[\[(])",
            replace_shortcut,
            without_images,
        )

        definition = re.compile(r"^[ \t]{0,3}\[([^\]\n]+)\]:[ \t]*\S.*$")
        safe_lines: list[str] = []
        for line in without_images.splitlines(keepends=True):
            match = definition.match(line.rstrip("\r\n"))
            if match and normalized_reference(match.group(1)) in image_references:
                continue
            safe_lines.append(line)
        return html.escape("".join(safe_lines), quote=False)

    @Slot()
    def copyVaultPath(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.vaultPath)
            self._set_notice("Caminho do cofre copiado.")

    @Slot()
    def openVault(self) -> None:
        if QDesktopServices.openUrl(QUrl.fromLocalFile(self.vaultPath)):
            self._set_notice("Cofre aberto no gerenciador de arquivos.")
        else:
            self._set_notice("Não foi possível abrir o cofre.")

