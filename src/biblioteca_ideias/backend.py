from __future__ import annotations

from pathlib import Path

import html
import os
import re

from PySide6.QtCore import QObject, Property, QLockFile, QStandardPaths, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication

from .storage import COLUMNS, INITIAL_BODY, MarkdownStore, StorageError
from .git_sync import STATE_ERROR, STATE_LOCAL, SyncCoordinator
from .bootstrap import BootstrapCoordinator, BootstrapError, validate_existing_vault


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
    readyChanged = Signal()
    githubEnabledChanged = Signal()

    def __init__(
        self,
        vault: Path | None = None,
        parent: QObject | None = None,
        sync_coordinator: SyncCoordinator | None = None,
    ):
        super().__init__(parent)
        self._notice = "Preparando a Biblioteca de Ideias…"
        vault_path = vault or default_vault_path()
        self._vault_path = vault_path
        vault_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = QLockFile(str(vault_path.with_suffix(".lock")))
        self._lock.setStaleLockTime(30_000)
        if not self._lock.tryLock(0):
            raise StorageError("A Biblioteca de Ideias já está aberta para este cofre.")
        self._state_path = default_state_path() if vault is None else vault_path.parent / ".state-test"
        self._disabled_marker = self._state_path / "github-disabled"
        self._integration_enabled = not self._disabled_marker.exists()
        self._store: MarkdownStore | None = None
        self._ideas: list[dict[str, str]] = []
        self._sync_state = STATE_LOCAL
        self._auth_status = "GitHub CLI não autenticado"
        self._sync: SyncCoordinator | None = None
        self._bootstrap: BootstrapCoordinator | None = None
        if vault is not None:
            self._initialize_store(sync_coordinator, auto_sync=False)
        elif vault_path.exists():
            try:
                validate_existing_vault(vault_path)
            except BootstrapError as exc:
                self._notice = str(exc)
            else:
                self._initialize_store(None, auto_sync=self._integration_enabled)
        else:
            self._bootstrap = BootstrapCoordinator(vault_path, self)
            self._bootstrap.stateChanged.connect(self._on_bootstrap_state)
            self._bootstrap.progressChanged.connect(self._on_bootstrap_progress)
            self._bootstrap.authChanged.connect(self._on_bootstrap_auth)
            self._bootstrap.ready.connect(self._on_bootstrap_ready, Qt.ConnectionType.QueuedConnection)
            if self._integration_enabled:
                self._bootstrap.inspect_or_bootstrap()
            else:
                self._notice = "Integração GitHub desativada neste aplicativo."

    def _initialize_store(
        self,
        sync_coordinator: SyncCoordinator | None,
        *,
        auto_sync: bool,
    ) -> None:
        self._store = MarkdownStore(self._vault_path, state_dir=self._state_path)
        self._sync = sync_coordinator or SyncCoordinator(self._vault_path, auto_sync=auto_sync)
        self._sync.stateChanged.connect(self._on_sync_state)
        self._sync.authChanged.connect(self._on_auth_status)
        self._sync.repositoryUpdated.connect(
            self._reload_after_fast_forward, Qt.ConnectionType.QueuedConnection
        )
        self.reload()
        self.readyChanged.emit()

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
        return str(self._vault_path)

    @Property(bool, notify=readyChanged)
    def ready(self) -> bool:
        return self._store is not None

    @Property(bool, notify=githubEnabledChanged)
    def githubEnabled(self) -> bool:
        return self._integration_enabled

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
        if self._store is None:
            self._set_notice("Conecte o GitHub para instalar o cofre privado existente.")
            return
        try:
            self._ideas = [idea.as_dict() for idea in self._store.list_ideas()]
            self.ideasChanged.emit()
            self._set_notice("Cofre recarregado com segurança.")
        except StorageError as exc:
            self._set_notice(str(exc))

    @Slot(str, str, result=bool)
    def createIdea(self, title: str, body: str) -> bool:
        if self._store is None:
            self._set_notice("O cofre ainda não está disponível.")
            return False
        try:
            created = self._store.create(title, body)
            self.reload()
            self._set_notice("Ideia criada em Entrada.")
            relative = created.path.relative_to(self._store.vault).as_posix()
            if self._sync is not None and self._integration_enabled:
                self._sync.request_save((relative,), f"ideia: criar {created.title}")
            return True
        except StorageError as exc:
            self._set_notice(str(exc))
            return False

    @Slot(str, str, str, str, str, result=bool)
    def saveIdea(self, idea_id: str, title: str, body: str, status: str, expected_hash: str) -> bool:
        if self._store is None:
            self._set_notice("O cofre ainda não está disponível.")
            return False
        try:
            current = {idea.idea_id: idea for idea in self._store.list_ideas()}[idea_id]
            saved = self._store.save_idea(idea_id, title, body, status, expected_hash)
            self.reload()
            self._set_notice("Ideia salva.")
            paths = (
                current.path.relative_to(self._store.vault).as_posix(),
                saved.path.relative_to(self._store.vault).as_posix(),
            )
            if self._sync is not None and self._integration_enabled:
                self._sync.request_save(paths, f"ideia: atualizar {saved.title}")
            return True
        except (KeyError, StorageError) as exc:
            self._set_notice(str(exc))
            return False

    @Slot()
    def syncNow(self) -> None:
        if not self._integration_enabled:
            self._set_notice("A integração GitHub está desativada neste aplicativo.")
        elif self._sync is not None:
            self._sync.request_sync()
        else:
            self._set_notice("Conecte sua conta do GitHub para instalar o cofre.")

    @Slot()
    def connectGithub(self) -> None:
        self._state_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._state_path.chmod(0o700)
        self._disabled_marker.unlink(missing_ok=True)
        self._integration_enabled = True
        self.githubEnabledChanged.emit()
        if self._bootstrap is None:
            self._bootstrap = BootstrapCoordinator(self._vault_path, self)
            self._bootstrap.stateChanged.connect(self._on_bootstrap_state)
            self._bootstrap.progressChanged.connect(self._on_bootstrap_progress)
            self._bootstrap.authChanged.connect(self._on_bootstrap_auth)
            self._bootstrap.ready.connect(self._on_bootstrap_ready, Qt.ConnectionType.QueuedConnection)
        self._bootstrap.connect_account()

    @Slot()
    def cancelGithubLogin(self) -> None:
        if self._bootstrap is not None:
            self._bootstrap.cancel_login()

    @Slot()
    def disconnectGithub(self) -> None:
        self._state_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._state_path.chmod(0o700)
        self._disabled_marker.write_text("disabled\n", encoding="utf-8")
        self._disabled_marker.chmod(0o600)
        self._integration_enabled = False
        self._auth_status = "Integração GitHub desativada neste aplicativo"
        self.githubEnabledChanged.emit()
        self.authStatusChanged.emit()
        self._set_notice("Integração desativada localmente. O login global não foi alterado.")

    @Slot(str)
    def _on_bootstrap_state(self, message: str) -> None:
        self._set_notice(message)

    @Slot(str)
    def _on_bootstrap_progress(self, message: str) -> None:
        self._set_notice(message)

    @Slot(bool, str)
    def _on_bootstrap_auth(self, authenticated: bool, username: str) -> None:
        self._on_auth_status(authenticated, username)
        if authenticated and self._sync is not None and self._integration_enabled:
            self._sync.request_sync()

    @Slot(str)
    def _on_bootstrap_ready(self, path: str) -> None:
        if Path(path) != self._vault_path or self._store is not None:
            return
        try:
            self._initialize_store(None, auto_sync=self._integration_enabled)
            self._set_notice("Cofre privado pronto para uso.")
        except StorageError as exc:
            self._set_notice(str(exc))

    def close(self) -> None:
        if self._sync is not None:
            self._sync.close()
        if self._bootstrap is not None:
            self._bootstrap.close()

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

