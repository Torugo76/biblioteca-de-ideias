from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml
from PySide6.QtCore import QSaveFile


COLUMNS: tuple[tuple[str, str], ...] = (
    ("entrada", "Entrada"),
    ("avaliando", "Avaliando"),
    ("planejada", "Planejada"),
    ("em-andamento", "Em andamento"),
    ("arquivada", "Arquivada"),
)
COLUMN_KEYS = {key for key, _ in COLUMNS}
MAX_FILE_SIZE = 2 * 1024 * 1024
MAX_FILENAME_BYTES = 240
FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
SECTION = re.compile(r"(?ms)^##\s+Resumo\s*$\n?(.*?)(?=^#{1,6}\s+|\Z)")
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*]|[\x00-\x1f]')
WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
INITIAL_BODY = """## Resumo


## Problema


## Público


## Possível solução


## Próximos passos

- [ ]
"""


class StorageError(RuntimeError):
    pass


class ExternalChangeError(StorageError):
    pass


class CollisionError(StorageError):
    pass


def enforce_private_tree(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise StorageError("O cofre não é uma pasta privada válida.")
    pending = [root]
    directories: list[Path] = []
    files: list[tuple[Path, int]] = []
    while pending:
        directory = pending.pop()
        directories.append(directory)
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise StorageError("Não foi possível verificar as permissões do cofre.") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise StorageError("Não foi possível verificar as permissões do cofre.") from exc
            if stat.S_ISLNK(mode):
                raise StorageError("O cofre contém link simbólico inseguro.")
            if stat.S_ISDIR(mode):
                pending.append(path)
            elif stat.S_ISREG(mode):
                files.append((path, mode))
            else:
                raise StorageError("O cofre contém arquivo especial inseguro.")
    try:
        for directory in directories:
            directory.chmod(0o700)
        objects_root = root / ".git" / "objects"
        for path, mode in files:
            in_objects = objects_root in path.parents
            private_mode = 0o400 if in_objects and not (mode & stat.S_IWUSR) else 0o600
            path.chmod(private_mode)
    except OSError as exc:
        raise StorageError("Não foi possível aplicar permissões privadas ao cofre.") from exc


@dataclass(frozen=True)
class Idea:
    idea_id: str
    title: str
    body: str
    status: str
    created_at: str
    updated_at: str
    content_hash: str
    path: Path

    @property
    def summary(self) -> str:
        match = SECTION.search(self.body)
        value = match.group(1).strip() if match else self.body.strip()
        return " ".join(value.split())[:240]

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.idea_id,
            "titulo": self.title,
            "resumo": self.summary,
            "corpo": self.body,
            "status": self.status,
            "criada_em": self.created_at,
            "atualizada_em": self.updated_at,
            "hash": self.content_hash,
            "arquivo": self.path.name,
        }


@dataclass(frozen=True)
class MigrationItem:
    source: Path
    destination: Path
    idea_id: str
    content_hash: str
    modified_ns: int


def title_filename(title: str) -> str:
    normalized = unicodedata.normalize("NFC", title)
    replaced = INVALID_FILENAME.sub("-", normalized).rstrip(" .")
    if replaced in {"", ".", ".."}:
        raise StorageError("O título não produz um nome de arquivo válido.")
    device_name = replaced.split(".", 1)[0].casefold()
    if device_name in WINDOWS_RESERVED:
        raise StorageError("O título corresponde a um nome reservado do Windows.")
    filename = f"{replaced}.md"
    if len(filename.encode("utf-8")) > MAX_FILENAME_BYTES:
        raise StorageError(
            f"O nome do arquivo excede o limite seguro de {MAX_FILENAME_BYTES} bytes."
        )
    return filename


def filename_key(filename: str) -> str:
    return unicodedata.normalize("NFC", filename).casefold()


class MarkdownStore:
    def __init__(self, vault: Path, state_dir: Path | None = None):
        requested = Path(os.path.abspath(os.fspath(vault.expanduser())))
        for component in (requested, *requested.parents):
            if component.is_symlink():
                raise StorageError(
                    f"O caminho do cofre não pode conter links simbólicos: {component}"
                )
        self.vault = requested.resolve(strict=False)
        default_state = self.vault.parent / ".biblioteca-de-ideias-state"
        self.state_dir = (state_dir or default_state).expanduser().resolve(strict=False)
        self._prepare_vault()
        enforce_private_tree(self.vault)
        self._prepare_state()
        self._recover_interrupted_migration()
        self._assert_no_orphan_migration_files()
        self._recover_interrupted_save()
        self._ideas_by_id: dict[str, Idea] = {}
        self._refresh_index()

    def _prepare_vault(self) -> None:
        if self.vault.exists() and self.vault.is_symlink():
            raise StorageError("O cofre não pode ser um link simbólico.")
        self.vault.mkdir(parents=True, exist_ok=True)
        os.chmod(self.vault, 0o700)
        for key, _ in COLUMNS:
            column = self.vault / key
            if column.exists() and (column.is_symlink() or not column.is_dir()):
                raise StorageError(f"A coluna {key} não é uma pasta segura.")
            column.mkdir(exist_ok=True)
            os.chmod(column, 0o700)
            keep = column / ".gitkeep"
            if not keep.exists():
                keep.touch()
            os.chmod(keep, 0o600)

    def _prepare_state(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)
        self.journal_path = self.state_dir / "save-journal.json"
        self.backup_path = self.state_dir / "save-backup.bin"
        self.migration_journal_path = self.state_dir / "migration-journal.json"

    @staticmethod
    def _digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _safe_relative_path(self, relative: str) -> Path:
        target = self.vault / relative
        try:
            target.resolve(strict=False).relative_to(self.vault)
        except ValueError as exc:
            raise StorageError("O diário contém um caminho fora do cofre.") from exc
        if target.is_symlink():
            raise StorageError("O diário aponta para um link simbólico.")
        return target

    def _recover_interrupted_save(self) -> None:
        if not self.journal_path.exists():
            return
        try:
            record = json.loads(self.journal_path.read_text(encoding="utf-8"))
            source = self._safe_relative_path(record["source"])
            target = self._safe_relative_path(record["target"])
            backup = self.backup_path.read_bytes()
            self._atomic_write(source, backup)
            if target != source and target.exists():
                target.unlink()
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise StorageError("Não foi possível recuperar uma gravação interrompida.") from exc
        self._clear_journal()

    def _write_journal(self, source: Path, target: Path, original: bytes) -> None:
        self.backup_path.write_bytes(original)
        os.chmod(self.backup_path, 0o600)
        record = {
            "version": 1,
            "source": source.relative_to(self.vault).as_posix(),
            "target": target.relative_to(self.vault).as_posix(),
        }
        data = json.dumps(record, ensure_ascii=False).encode("utf-8")
        self._atomic_write(self.journal_path, data)
        os.chmod(self.journal_path, 0o600)

    def _clear_journal(self) -> None:
        for path in (self.journal_path, self.backup_path):
            if path.exists():
                path.unlink()

    def _failure_hook(self, stage: str) -> None:
        """Ponto de injeção usado somente por testes de recuperação."""

    def _migration_failure_hook(self, stage: str, index: int) -> None:
        """Ponto de injeção usado somente por testes de migração."""

    def _migration_recovery_failure_hook(self, stage: str, index: int) -> None:
        """Ponto de injeção usado somente por testes de retomada."""

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _serialize(idea_id: str, title: str, body: str, created_at: str, updated_at: str) -> bytes:
        metadata = {
            "id": idea_id,
            "titulo": title,
            "criada_em": created_at,
            "atualizada_em": updated_at,
        }
        front = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).rstrip()
        canonical_body = body.rstrip() + "\n"
        return f"---\n{front}\n---\n\n{canonical_body}".encode("utf-8")

    @staticmethod
    def _validate_serialized_size(data: bytes) -> None:
        if len(data) > MAX_FILE_SIZE:
            raise StorageError(
                f"A ideia excede o limite de {MAX_FILE_SIZE // (1024 * 1024)} MB."
            )

    def _parse(self, path: Path, status: str) -> Idea:
        if path.is_symlink() or not path.is_file():
            raise StorageError(f"Arquivo inseguro ignorado: {path.name}")
        data = path.read_bytes()
        if len(data) > MAX_FILE_SIZE:
            raise StorageError(f"Arquivo muito grande: {path.name}")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StorageError(f"Arquivo não está em UTF-8: {path.name}") from exc
        match = FRONT_MATTER.match(text)
        if not match:
            raise StorageError(f"Markdown sem cabeçalho válido: {path.name}")
        try:
            meta = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            raise StorageError(f"Cabeçalho YAML inválido: {path.name}") from exc
        if not isinstance(meta, dict):
            raise StorageError(f"Cabeçalho inválido: {path.name}")
        required = ("id", "titulo", "criada_em", "atualizada_em")
        if any(not isinstance(meta.get(field), str) for field in required):
            raise StorageError(f"Campos obrigatórios inválidos: {path.name}")
        try:
            canonical_id = str(uuid.UUID(meta["id"]))
        except ValueError as exc:
            raise StorageError(f"UUID inválido: {path.name}") from exc
        body = match.group(2)
        if body.startswith("\n"):
            body = body[1:]
        body = body.rstrip() + "\n"
        return Idea(
            idea_id=canonical_id,
            title=meta["titulo"],
            body=body,
            status=status,
            created_at=meta["criada_em"],
            updated_at=meta["atualizada_em"],
            content_hash=self._digest(data),
            path=path,
        )

    def _scan(self) -> list[Idea]:
        ideas: list[Idea] = []
        ids: dict[str, Path] = {}
        names: dict[str, Path] = {}
        for status, _ in COLUMNS:
            column = self.vault / status
            if column.is_symlink():
                raise StorageError(f"A coluna {status} se tornou um link simbólico.")
            for path in sorted(column.glob("*.md")):
                idea = self._parse(path, status)
                if idea.idea_id in ids:
                    raise CollisionError(
                        f"UUID duplicado em {ids[idea.idea_id].name} e {path.name}."
                    )
                key = filename_key(path.name)
                if key in names:
                    raise CollisionError(
                        f"Nomes de arquivo colidem entre {names[key].name} e {path.name}."
                    )
                ids[idea.idea_id] = path
                names[key] = path
                ideas.append(idea)
        return ideas

    def _refresh_index(self) -> list[Idea]:
        ideas = self._scan()
        self._ideas_by_id = {idea.idea_id: idea for idea in ideas}
        return ideas

    def list_ideas(self) -> list[Idea]:
        return self._refresh_index()

    def _target_path(self, status: str, title: str) -> Path:
        if status not in COLUMN_KEYS:
            raise StorageError("Coluna inválida.")
        column = self.vault / status
        if column.is_symlink():
            raise StorageError("A coluna não pode ser um link simbólico.")
        target = column / title_filename(title)
        if target.is_symlink():
            raise StorageError("O arquivo da ideia não pode ser um link simbólico.")
        try:
            target.resolve(strict=False).relative_to(self.vault)
        except ValueError as exc:
            raise StorageError("O arquivo está fora do cofre.") from exc
        return target

    def _assert_name_available(self, target: Path, excluding_id: str | None = None) -> None:
        wanted = filename_key(target.name)
        for idea in self._ideas_by_id.values():
            if idea.idea_id != excluding_id and filename_key(idea.path.name) == wanted:
                raise CollisionError(f"Já existe uma ideia chamada “{target.stem}”.")

    def create(self, title: str, body: str) -> Idea:
        if not title.strip():
            raise StorageError("Informe um título.")
        self._refresh_index()
        target = self._target_path("entrada", title)
        self._assert_name_available(target)
        idea_id = str(uuid.uuid4())
        now = self._now()
        data = self._serialize(idea_id, title, body or INITIAL_BODY, now, now)
        self._validate_serialized_size(data)
        self._atomic_write(target, data)
        self._refresh_index()
        return self._ideas_by_id[idea_id]

    def save_idea(
        self,
        idea_id: str,
        title: str,
        body: str,
        status: str,
        expected_hash: str,
    ) -> Idea:
        if not title.strip():
            raise StorageError("Informe um título.")
        self._refresh_index()
        current = self._ideas_by_id.get(idea_id)
        if current is None:
            raise StorageError("Ideia não encontrada.")
        if current.content_hash != expected_hash:
            raise ExternalChangeError(
                "A ideia foi alterada por outro programa. Recarregue antes de salvar."
            )
        target = self._target_path(status, title)
        self._assert_name_available(target, excluding_id=idea_id)
        data = self._serialize(idea_id, title, body, current.created_at, self._now())
        self._validate_serialized_size(data)
        original = current.path.read_bytes()
        self._write_journal(current.path, target, original)
        try:
            self._atomic_write(target, data)
            self._failure_hook("target-written")
            if target != current.path:
                current.path.unlink()
            self._failure_hook("source-removed")
        except Exception as exc:
            self._recover_interrupted_save()
            if isinstance(exc, StorageError):
                raise
            raise StorageError("A operação falhou e a ideia original foi restaurada.") from exc
        self._clear_journal()
        self._refresh_index()
        return self._ideas_by_id[idea_id]

    def plan_title_migration(self) -> list[MigrationItem]:
        ideas = self._refresh_index()
        destinations: dict[str, Path] = {}
        plan: list[MigrationItem] = []
        for idea in ideas:
            destination = self._target_path(idea.status, idea.title)
            key = filename_key(destination.name)
            previous = destinations.get(key)
            if previous is not None and previous != idea.path:
                raise CollisionError(
                    f"A migração produziria nomes colidentes: {previous.name} e {destination.name}."
                )
            destinations[key] = idea.path
            if idea.path != destination:
                stat = idea.path.stat()
                plan.append(
                    MigrationItem(
                        source=idea.path,
                        destination=destination,
                        idea_id=idea.idea_id,
                        content_hash=idea.content_hash,
                        modified_ns=stat.st_mtime_ns,
                    )
                )
        return plan

    def apply_title_migration(self, plan: list[MigrationItem]) -> None:
        current_plan = self.plan_title_migration()
        if [(item.source, item.destination) for item in current_plan] != [
            (item.source, item.destination) for item in plan
        ]:
            raise StorageError("O cofre mudou desde o planejamento da migração.")
        records = []
        for item in plan:
            migration_nonce = uuid.uuid4().hex
            records.append(
                {
                    "source": item.source.relative_to(self.vault).as_posix(),
                    "temporary": item.source.with_name(f".migration-{migration_nonce}.tmp").relative_to(self.vault).as_posix(),
                    "destination": item.destination.relative_to(self.vault).as_posix(),
                    "rollback": item.source.with_name(f".migration-rollback-{migration_nonce}.tmp").relative_to(self.vault).as_posix(),
                    "content_hash": item.content_hash,
                }
            )
        self._write_migration_journal("planned", records)
        try:
            self._migration_failure_hook("after-planned", -1)
            self._write_migration_journal("moving-to-temporary", records)
            for index, record in enumerate(records):
                os.replace(
                    self._safe_relative_path(record["source"]),
                    self._safe_relative_path(record["temporary"]),
                )
                self._migration_failure_hook("after-temporary", index)
            self._migration_failure_hook("after-temporary-phase", -1)
            self._write_migration_journal("moving-to-destination", records)
            for index, record in enumerate(records):
                os.replace(
                    self._safe_relative_path(record["temporary"]),
                    self._safe_relative_path(record["destination"]),
                )
                self._migration_failure_hook("after-destination", index)
        except OSError as exc:
            self._recover_interrupted_migration()
            raise StorageError("A migração falhou e foi revertida.") from exc
        self._clear_migration_journal()
        self._refresh_index()

    def _write_migration_journal(self, phase: str, records: list[dict[str, str]]) -> None:
        payload = json.dumps(
            {"version": 1, "phase": phase, "items": records},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        self._atomic_write(self.migration_journal_path, payload)
        os.chmod(self.migration_journal_path, 0o600)

    def _clear_migration_journal(self) -> None:
        if self.migration_journal_path.exists():
            self.migration_journal_path.unlink()

    def _recover_interrupted_migration(self) -> None:
        if not self.migration_journal_path.exists():
            return
        try:
            journal = json.loads(self.migration_journal_path.read_text(encoding="utf-8"))
            if journal.get("version") != 1 or not isinstance(journal.get("items"), list):
                raise ValueError("versão inválida")
            records = journal["items"]
            resolved: list[tuple[dict[str, str], Path]] = []
            all_candidates = {
                self._safe_relative_path(record[field])
                for record in records
                for field in ("source", "temporary", "destination", "rollback")
            }
            existing = [path for path in all_candidates if path.exists()]
            for record in records:
                matches = [
                    path for path in existing
                    if self._digest(path.read_bytes()) == record["content_hash"]
                ]
                if len(matches) != 1:
                    raise StorageError("A recuperação não encontrou uma cópia única de uma ideia.")
                resolved.append((record, matches[0]))

            self._write_migration_journal("recovering", records)
            for index, (record, current) in enumerate(resolved):
                rollback = self._safe_relative_path(record["rollback"])
                if current != rollback:
                    os.replace(current, rollback)
                self._migration_recovery_failure_hook("after-isolation", index)
            self._migration_recovery_failure_hook("after-isolation-phase", -1)
            for index, (record, _) in enumerate(resolved):
                rollback = self._safe_relative_path(record["rollback"])
                source = self._safe_relative_path(record["source"])
                os.replace(rollback, source)
                self._migration_recovery_failure_hook("after-restore", index)
            for path in all_candidates:
                if path.exists() and path.name.startswith(".migration-"):
                    path.unlink()
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StorageError("Não foi possível recuperar a migração interrompida.") from exc
        self._clear_migration_journal()

    def _assert_no_orphan_migration_files(self) -> None:
        orphans = [
            path
            for status, _ in COLUMNS
            for path in (self.vault / status).glob(".migration-*.tmp")
        ]
        if orphans:
            raise StorageError("Foram encontrados temporários órfãos de migração.")

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        save = QSaveFile(str(path))
        save.setDirectWriteFallback(False)
        if not save.open(QSaveFile.OpenModeFlag.WriteOnly):
            raise StorageError(f"Não foi possível abrir {path.name} para gravação segura.")
        if save.write(data) != len(data):
            save.cancelWriting()
            raise StorageError(f"Não foi possível gravar {path.name} por completo.")
        if not save.commit():
            raise StorageError(f"Não foi possível concluir a gravação segura de {path.name}.")
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            raise StorageError(f"Não foi possível proteger as permissões de {path.name}.") from exc
