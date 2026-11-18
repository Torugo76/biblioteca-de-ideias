from __future__ import annotations

import difflib
import json
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from .git_sync import GitService, resolve_system_executable, safe_subprocess_environment, sanitize_output
from .storage import (
    COLUMNS, COLUMN_KEYS, FRONT_MATTER, MAX_FILE_SIZE, MarkdownStore, StorageError,
    filename_key, title_filename,
)


class ConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class SnapshotIdea:
    idea_id: str
    title: str
    created_at: str
    updated_at: str
    status: str
    path: str
    data: bytes

    @property
    def body(self) -> str:
        match = FRONT_MATTER.match(self.data.decode("utf-8"))
        assert match is not None
        return match.group(2).lstrip("\n").rstrip() + "\n"


class ConflictManager:
    def __init__(self, repo: Path, state_dir: Path, service: GitService):
        self.repo = repo.resolve()
        self.state_dir = state_dir.resolve()
        self.service = service
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir.chmod(0o700)

    def _git_bytes(self, args: tuple[str, ...], *, index: Path | None = None) -> bytes:
        git = resolve_system_executable("git")
        if not git:
            raise ConflictError("Git não está disponível para analisar o conflito.")
        environment = safe_subprocess_environment()
        if index is not None:
            environment["GIT_INDEX_FILE"] = os.fspath(index)
        completed = subprocess.run(
            [
                git, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                "-c", "core.attributesFile=/dev/null", "-c", "commit.gpgSign=false",
                *args,
            ],
            cwd=self.repo,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
            shell=False,
            umask=0o077,
        )
        if completed.returncode != 0:
            raise ConflictError("Não foi possível analisar o histórico sem alterar seus arquivos.")
        return completed.stdout

    @staticmethod
    def _parse_blob(path: str, data: bytes) -> SnapshotIdea:
        if len(data) > MAX_FILE_SIZE:
            raise ConflictError("Uma ideia no histórico excede o limite seguro.")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConflictError("Uma ideia no histórico não está em UTF-8.") from exc
        match = FRONT_MATTER.match(text)
        if not match:
            raise ConflictError("Uma ideia no histórico possui Markdown inválido.")
        try:
            metadata = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            raise ConflictError("Uma ideia no histórico possui YAML inválido.") from exc
        if not isinstance(metadata, dict):
            raise ConflictError("Uma ideia no histórico possui cabeçalho inválido.")
        required = ("id", "titulo", "criada_em", "atualizada_em")
        if any(not isinstance(metadata.get(key), str) for key in required):
            raise ConflictError("Uma ideia no histórico possui campos inválidos.")
        try:
            idea_id = str(uuid.UUID(metadata["id"]))
        except ValueError as exc:
            raise ConflictError("Uma ideia no histórico possui UUID inválido.") from exc
        parts = Path(path).parts
        if len(parts) != 2 or parts[0] not in COLUMN_KEYS or not path.endswith(".md"):
            raise ConflictError("Uma ideia no histórico está fora das colunas permitidas.")
        return SnapshotIdea(
            idea_id=idea_id,
            title=metadata["titulo"],
            created_at=metadata["criada_em"],
            updated_at=metadata["atualizada_em"],
            status=parts[0],
            path=path,
            data=data,
        )

    def _snapshot(self, commit: str) -> dict[str, SnapshotIdea]:
        paths = self._git_bytes(("ls-tree", "-r", "-z", "--name-only", commit, "--"))
        ideas: dict[str, SnapshotIdea] = {}
        names: dict[str, str] = {}
        for raw_path in paths.split(b"\0"):
            if not raw_path:
                continue
            path = raw_path.decode("utf-8", "strict")
            parts = Path(path).parts
            if len(parts) != 2 or parts[0] not in COLUMN_KEYS or not path.endswith(".md"):
                continue
            data = self._git_bytes(("show", f"{commit}:{path}"))
            idea = self._parse_blob(path, data)
            if idea.idea_id in ideas:
                raise ConflictError("O histórico contém UUID duplicado.")
            name_key = filename_key(Path(path).name)
            if name_key in names:
                raise ConflictError("O histórico contém nomes de ideia duplicados.")
            ideas[idea.idea_id] = idea
            names[name_key] = path
        return ideas

    def prepare(self, local: str, remote: str) -> str:
        for manifest_path in self.state_dir.glob("*/manifest.json"):
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (existing.get("status") != "resolved" and existing.get("local") == local
                    and existing.get("remote") == remote):
                return str(existing["id"])
        base = self.service.run(("merge-base", local, remote))
        if base.returncode != 0 or not base.stdout.strip():
            raise ConflictError("Não foi possível encontrar a base comum do conflito.")
        conflict_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        directory = self.state_dir / conflict_id
        directory.mkdir(mode=0o700)
        local_ref = f"refs/recovery/conflict-{conflict_id}-local"
        remote_ref = f"refs/recovery/conflict-{conflict_id}-remote"
        for ref, commit in ((local_ref, local), (remote_ref, remote)):
            if self.service.run(("update-ref", ref, commit)).returncode != 0:
                raise ConflictError("Não foi possível criar referências locais de recuperação.")
        snapshots = {
            "base": self._snapshot(base.stdout.strip()),
            "local": self._snapshot(local),
            "remote": self._snapshot(remote),
        }
        all_ids = sorted(set().union(*(snapshot.keys() for snapshot in snapshots.values())))
        items: list[dict[str, object]] = []
        for idea_id in all_ids:
            base_idea = snapshots["base"].get(idea_id)
            local_idea = snapshots["local"].get(idea_id)
            remote_idea = snapshots["remote"].get(idea_id)
            local_data = local_idea.data if local_idea else None
            remote_data = remote_idea.data if remote_idea else None
            if local_data == remote_data and (
                (local_idea.path if local_idea else None) == (remote_idea.path if remote_idea else None)
            ):
                continue
            for label, idea in (("base", base_idea), ("local", local_idea), ("remote", remote_idea)):
                if idea is not None:
                    snapshot_path = directory / f"{idea_id}.{label}.md"
                    snapshot_path.write_bytes(idea.data)
                    snapshot_path.chmod(0o600)
            local_lines = local_idea.body.splitlines() if local_idea else ["[excluída localmente]"]
            remote_lines = remote_idea.body.splitlines() if remote_idea else ["[excluída no GitHub]"]
            diff = sanitize_output("\n".join(
                difflib.unified_diff(local_lines, remote_lines, fromfile="Local", tofile="GitHub", lineterm="")
            ))[:4000]
            items.append(
                {
                    "id": idea_id,
                    "titulo_local": local_idea.title if local_idea else "Excluída",
                    "titulo_remoto": remote_idea.title if remote_idea else "Excluída",
                    "data_local": local_idea.updated_at if local_idea else "",
                    "data_remota": remote_idea.updated_at if remote_idea else "",
                    "caminho_local": local_idea.path if local_idea else "",
                    "caminho_remoto": remote_idea.path if remote_idea else "",
                    "diff": diff,
                }
            )
        manifest = {
            "version": 1,
            "id": conflict_id,
            "status": "pending",
            "base": base.stdout.strip(),
            "local": local,
            "remote": remote,
            "local_ref": local_ref,
            "remote_ref": remote_ref,
            "items": items,
            "choices": {},
        }
        self._write_manifest(directory, manifest)
        return conflict_id

    @staticmethod
    def _write_manifest(directory: Path, manifest: dict[str, object]) -> None:
        path = directory / "manifest.json"
        temporary = directory / ".manifest.tmp"
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)

    def _manifest(self, conflict_id: str) -> tuple[Path, dict[str, object]]:
        if not conflict_id or any(character not in "0123456789T-abcdef" for character in conflict_id):
            raise ConflictError("Identificador de conflito inválido.")
        directory = self.state_dir / conflict_id
        try:
            directory.resolve().relative_to(self.state_dir)
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ConflictError("Não foi possível abrir a recuperação do conflito.") from exc
        return directory, manifest

    def list_pending(self) -> list[dict[str, object]]:
        pending: list[dict[str, object]] = []
        for manifest_path in sorted(self.state_dir.glob("*/manifest.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if manifest.get("status") != "resolved":
                for item in manifest.get("items", []):
                    pending.append({"conflito": manifest.get("id"), **item})
        return pending

    def version(self, conflict_id: str, idea_id: str, side: str) -> dict[str, str]:
        directory, manifest = self._manifest(conflict_id)
        if side not in {"local", "remote"}:
            raise ConflictError("Versão de conflito inválida.")
        path = directory / f"{idea_id}.{side}.md"
        if not path.exists():
            return {"titulo": "", "corpo": "", "status": "entrada"}
        item = next((entry for entry in manifest.get("items", []) if entry.get("id") == idea_id), None)
        if item is None:
            raise ConflictError("A ideia não pertence a este conflito.")
        actual_path = item["caminho_local" if side == "local" else "caminho_remoto"]
        if not actual_path:
            return {"titulo": "", "corpo": "", "status": "entrada"}
        idea = self._parse_blob(str(actual_path), path.read_bytes())
        return {"titulo": idea.title, "corpo": idea.body, "status": idea.status}

    def choose(
        self,
        conflict_id: str,
        idea_id: str,
        action: str,
        *,
        title: str = "",
        body: str = "",
        status: str = "entrada",
    ) -> str:
        if action not in {"local", "remote", "both", "combined"}:
            raise ConflictError("Escolha de conflito inválida.")
        directory, manifest = self._manifest(conflict_id)
        item_ids = {item["id"] for item in manifest.get("items", [])}
        if idea_id not in item_ids:
            raise ConflictError("A ideia não pertence a este conflito.")
        if action == "combined":
            if not title.strip() or status not in COLUMN_KEYS:
                raise ConflictError("Informe título e coluna para a versão combinada.")
            title_filename(title)
        choices = manifest.setdefault("choices", {})
        assert isinstance(choices, dict)
        choices[idea_id] = {
            "action": action, "title": title, "body": body, "status": status,
        }
        self._write_manifest(directory, manifest)
        if len(choices) < len(item_ids):
            return "Escolha registrada. Ainda existem conflitos pendentes."
        self._reconcile(directory, manifest)
        return "Conflitos reconciliados e enviados com segurança."

    def _load_snapshot_idea(
        self, directory: Path, idea_id: str, side: str, manifest: dict[str, object]
    ) -> SnapshotIdea | None:
        path = directory / f"{idea_id}.{side}.md"
        if not path.exists():
            return None
        item = next(item for item in manifest["items"] if item["id"] == idea_id)  # type: ignore[index]
        actual_path = item[f"caminho_{'remoto' if side == 'remote' else 'local'}"]
        if not actual_path:
            return None
        return self._parse_blob(str(actual_path), path.read_bytes())

    def _reconcile(self, directory: Path, manifest: dict[str, object]) -> None:
        local = str(manifest["local"])
        remote = str(manifest["remote"])
        head = self.service.ref("HEAD")
        tracked_remote = self.service.ref("origin/main")
        status, records = self.service.status_records()
        previous_result = str(manifest.get("result") or "")
        if head == previous_result and tracked_remote == remote and status.returncode == 0 and not records:
            pushed = self.service.push()
            if pushed.returncode != 0:
                raise ConflictError("A resolução ficou salva localmente e o envio está pendente.")
            manifest["status"] = "resolved"
            self._write_manifest(directory, manifest)
            return
        if head != local or tracked_remote != remote or status.returncode != 0 or records:
            raise ConflictError("O repositório mudou desde a análise. As escolhas foram preservadas.")

        local_snapshot = self._snapshot(local)
        result: dict[str, SnapshotIdea] = dict(local_snapshot)
        choices = manifest["choices"]
        assert isinstance(choices, dict)
        for idea_id, choice_value in choices.items():
            choice = choice_value
            assert isinstance(choice, dict)
            local_idea = self._load_snapshot_idea(directory, idea_id, "local", manifest)
            remote_idea = self._load_snapshot_idea(directory, idea_id, "remote", manifest)
            action = choice["action"]
            if action == "local":
                selected = local_idea
            elif action == "remote":
                selected = remote_idea
            elif action == "both":
                selected = local_idea or remote_idea
                duplicate = remote_idea if local_idea and remote_idea else None
                if duplicate is not None:
                    copy_id = str(uuid.uuid4())
                    copy_title = duplicate.title + " (GitHub)"
                    data = MarkdownStore._serialize(
                        copy_id, copy_title, duplicate.body, duplicate.created_at,
                        datetime.now().astimezone().isoformat(timespec="seconds"),
                    )
                    result[copy_id] = SnapshotIdea(
                        copy_id, copy_title, duplicate.created_at, duplicate.updated_at,
                        duplicate.status, f"{duplicate.status}/{title_filename(copy_title)}", data,
                    )
            else:
                source = local_idea or remote_idea
                if source is None:
                    raise ConflictError("Não há versão para combinar.")
                title = str(choice["title"])
                status_name = str(choice["status"])
                data = MarkdownStore._serialize(
                    idea_id, title, str(choice["body"]), source.created_at,
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                )
                selected = SnapshotIdea(
                    idea_id, title, source.created_at, source.updated_at, status_name,
                    f"{status_name}/{title_filename(title)}", data,
                )
            if selected is None:
                result.pop(idea_id, None)
            else:
                canonical_path = f"{selected.status}/{title_filename(selected.title)}"
                result[idea_id] = SnapshotIdea(
                    selected.idea_id, selected.title, selected.created_at, selected.updated_at,
                    selected.status, canonical_path, selected.data,
                )

        names: dict[str, str] = {}
        for idea in result.values():
            key = filename_key(Path(idea.path).name)
            if key in names:
                raise ConflictError("As escolhas produzem títulos duplicados. Renomeie uma versão combinada.")
            names[key] = idea.idea_id

        index = directory / "resolution.index"
        index.unlink(missing_ok=True)
        self._git_bytes(("read-tree", "--empty"), index=index)
        # Preserva arquivos não-Markdown rastreados do lado local.
        tree = self._git_bytes(("ls-tree", "-r", "-z", local, "--"))
        for record in tree.split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, blob = metadata.decode().split(" ")
            path = raw_path.decode("utf-8")
            if kind != "blob" or path.endswith(".md"):
                continue
            if mode not in {"100644", "100755"}:
                raise ConflictError("O histórico contém arquivo não regular.")
            self._git_bytes(("update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}"), index=index)
        blob_dir = directory / "resolution-files"
        blob_dir.mkdir(exist_ok=True, mode=0o700)
        for idea in result.values():
            raw = blob_dir / idea.idea_id
            raw.write_bytes(idea.data)
            raw.chmod(0o600)
            blob = self._git_bytes(("hash-object", "-w", os.fspath(raw))).decode().strip()
            self._git_bytes(
                ("update-index", "--add", "--cacheinfo", f"100644,{blob},{idea.path}"),
                index=index,
            )
        tree_id = self._git_bytes(("write-tree",), index=index).decode().strip()
        commit = self._git_bytes(
            (
                "-c", "user.name=Biblioteca de Ideias",
                "-c", "user.email=Torugo76@users.noreply.github.com",
                "commit-tree", tree_id, "-p", local, "-p", remote,
                "-m", "conflito: reconciliar escolhas explícitas",
            )
        ).decode().strip()
        self.service.run(("update-ref", f"refs/recovery/conflict-{manifest['id']}-result", commit))
        manifest["result"] = commit
        self._write_manifest(directory, manifest)

        fetched = self.service.fetch()
        if fetched.returncode != 0 or self.service.ref("origin/main") != remote:
            raise ConflictError("O GitHub mudou durante a resolução. Nada foi aplicado ao checkout.")
        if self.service.ref("HEAD") != local:
            raise ConflictError("O checkout local mudou durante a resolução.")
        with self.service._network_config_guard() as guard_error:
            if guard_error:
                raise ConflictError("O repositório está ocupado; as escolhas foram preservadas.")
            advanced = self.service.run(
                ("-c", "merge.autoStash=false", "merge", "--ff-only", "--no-edit", commit)
            )
        if advanced.returncode != 0:
            raise ConflictError("Não foi possível aplicar a resolução; as cópias foram preservadas.")
        pushed = self.service.push()
        if pushed.returncode != 0:
            raise ConflictError("A resolução ficou salva localmente e o envio está pendente.")
        manifest["status"] = "resolved"
        self._write_manifest(directory, manifest)

