from __future__ import annotations

import hashlib
import os
import re
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
FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
SECTION = re.compile(r"(?ms)^##\s+Resumo\s*$\n?(.*?)(?=^#{1,6}\s+|\Z)")
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


@dataclass(frozen=True)
class Idea:
    idea_id: str
    title: str
    body: str
    status: str
    created_at: str
    updated_at: str
    content_hash: str

    @property
    def summary(self) -> str:
        match = SECTION.search(self.body)
        value = match.group(1).strip() if match else self.body.strip()
        compact = " ".join(value.split())
        return compact[:240]

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
        }


class MarkdownStore:
    def __init__(self, vault: Path):
        requested = Path(os.path.abspath(os.fspath(vault.expanduser())))
        for component in (requested, *requested.parents):
            if component.is_symlink():
                raise StorageError(
                    f"O caminho do cofre não pode conter links simbólicos: {component}"
                )
        self.vault = requested.resolve(strict=False)
        self._prepare_vault()

    def _prepare_vault(self) -> None:
        if self.vault.exists() and self.vault.is_symlink():
            raise StorageError("O cofre não pode ser um link simbólico.")
        self.vault.mkdir(parents=True, exist_ok=True)
        for key, _ in COLUMNS:
            column = self.vault / key
            if column.exists() and (column.is_symlink() or not column.is_dir()):
                raise StorageError(f"A coluna {key} não é uma pasta segura.")
            column.mkdir(exist_ok=True)
            keep = column / ".gitkeep"
            if not keep.exists():
                keep.touch()

    @staticmethod
    def _digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _path(self, status: str, idea_id: str) -> Path:
        if status not in COLUMN_KEYS:
            raise StorageError("Coluna inválida.")
        try:
            parsed = uuid.UUID(idea_id)
        except ValueError as exc:
            raise StorageError("Identificador de ideia inválido.") from exc
        filename = f"{parsed}.md"
        column = self.vault / status
        if column.is_symlink():
            raise StorageError("A coluna não pode ser um link simbólico.")
        target = column / filename
        if target.is_symlink():
            raise StorageError("O arquivo da ideia não pode ser um link simbólico.")
        try:
            target.resolve(strict=False).relative_to(self.vault)
        except ValueError as exc:
            raise StorageError("O arquivo está fora do cofre.") from exc
        return target

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
        expected = path.stem
        if meta["id"] != expected:
            raise StorageError(f"ID do arquivo não corresponde ao cabeçalho: {path.name}")
        body = match.group(2)
        if body.startswith("\n"):
            body = body[1:]
        body = body.rstrip() + "\n"
        return Idea(
            idea_id=meta["id"],
            title=meta["titulo"].strip(),
            body=body,
            status=status,
            created_at=meta["criada_em"],
            updated_at=meta["atualizada_em"],
            content_hash=self._digest(data),
        )

    def list_ideas(self) -> list[Idea]:
        ideas: list[Idea] = []
        for status, _ in COLUMNS:
            column = self.vault / status
            if column.is_symlink():
                raise StorageError(f"A coluna {status} se tornou um link simbólico.")
            for path in sorted(column.glob("*.md")):
                ideas.append(self._parse(path, status))
        return ideas

    def create(self, title: str, body: str) -> Idea:
        title = title.strip()
        if not title:
            raise StorageError("Informe um título.")
        idea_id = str(uuid.uuid4())
        now = self._now()
        path = self._path("entrada", idea_id)
        data = self._serialize(idea_id, title, body or INITIAL_BODY, now, now)
        self._validate_serialized_size(data)
        self._atomic_write(path, data)
        return self._parse(path, "entrada")

    def update(self, idea_id: str, status: str, title: str, body: str, expected_hash: str) -> Idea:
        title = title.strip()
        if not title:
            raise StorageError("Informe um título.")
        path = self._path(status, idea_id)
        current = self._parse(path, status)
        if current.content_hash != expected_hash:
            raise ExternalChangeError(
                "A ideia foi alterada por outro programa. Recarregue antes de salvar."
            )
        data = self._serialize(idea_id, title, body, current.created_at, self._now())
        self._validate_serialized_size(data)
        self._atomic_write(path, data)
        return self._parse(path, status)

    def move(self, idea_id: str, old_status: str, new_status: str, expected_hash: str) -> Idea:
        source = self._path(old_status, idea_id)
        destination = self._path(new_status, idea_id)
        current = self._parse(source, old_status)
        if current.content_hash != expected_hash:
            raise ExternalChangeError(
                "A ideia foi alterada por outro programa. Recarregue antes de mover."
            )
        if destination.exists():
            raise StorageError("Já existe uma ideia com este ID na coluna de destino.")
        try:
            os.replace(source, destination)
        except OSError as exc:
            raise StorageError(f"Não foi possível mover a ideia: {exc}") from exc
        return self._parse(destination, new_status)

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

