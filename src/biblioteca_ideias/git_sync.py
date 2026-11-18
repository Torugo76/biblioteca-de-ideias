from __future__ import annotations

import os
import json
import queue
import re
import shutil
import subprocess
import threading
from urllib.parse import unquote, unquote_plus
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from PySide6.QtCore import QObject, Signal


STATE_SYNCED = "Sincronizado"
STATE_SYNCING = "Sincronizando"
STATE_LOCAL = "Alterações locais"
STATE_PENDING = "Envio pendente"
STATE_OFFLINE = "Offline"
STATE_LOGIN = "Login necessário"
STATE_CONFLICT = "Conflito precisa de atenção"
STATE_ERROR = "Erro dados locais salvos"
EXPECTED_REPOSITORY = "Torugo76/biblioteca-de-ideias-cofre"
OFFICIAL_ORIGINS = {
    "https://github.com/Torugo76/biblioteca-de-ideias-cofre",
    "https://github.com/Torugo76/biblioteca-de-ideias-cofre.git",
    "git@github.com:Torugo76/biblioteca-de-ideias-cofre",
    "git@github.com:Torugo76/biblioteca-de-ideias-cofre.git",
    "ssh://git@github.com/Torugo76/biblioteca-de-ideias-cofre",
    "ssh://git@github.com/Torugo76/biblioteca-de-ideias-cofre.git",
}

GITHUB_TOKEN_PATTERN = re.compile(
    r"(?:gh[pousr](?:_|%5f)|github(?:_|%5f)pat(?:_|%5f))(?:[A-Za-z0-9_]|%[0-9a-f]{2})+",
    re.IGNORECASE,
)
ENCODED_TOKEN_CANDIDATE = re.compile(r"(?:[A-Za-z0-9_]|%[0-9a-f]{2}){10,}", re.IGNORECASE)
DECODED_GITHUB_TOKEN = re.compile(r"(?:gh[pousr]_\w+|github_pat_\w+)", re.IGNORECASE)
URL_CREDENTIAL_PATTERN = re.compile(r"(https?://)[^/@\s]+@", re.IGNORECASE)
QUERY_PARAMETER_PATTERN = re.compile(r"([?&#;])([^=&#;\s]+)=([^&#;\s]*)")
SENSITIVE_QUERY_KEYS = {
    "access_token", "token", "auth", "authorization", "password", "passwd",
    "secret", "client_secret", "api_key", "apikey",
}


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ValidatedDestination:
    fetch_url: str
    push_url: str
    branch: str = "main"
    repository: str = EXPECTED_REPOSITORY
    visibility: str = "PRIVATE"


def sanitize_output(value: str) -> str:
    without_credentials = GITHUB_TOKEN_PATTERN.sub("[credencial removida]", value)

    def sanitize_encoded_token(match: re.Match[str]) -> str:
        decoded = unquote(match.group(0))
        return "[credencial removida]" if DECODED_GITHUB_TOKEN.fullmatch(decoded) else match.group(0)

    without_credentials = ENCODED_TOKEN_CANDIDATE.sub(sanitize_encoded_token, without_credentials)
    without_credentials = URL_CREDENTIAL_PATTERN.sub(r"\1[credencial removida]@", without_credentials)

    def sanitize_parameter(match: re.Match[str]) -> str:
        decoded_key = unquote_plus(match.group(2)).casefold().replace("-", "_")
        if decoded_key in SENSITIVE_QUERY_KEYS:
            return f"{match.group(1)}{match.group(2)}=[credencial removida]"
        return match.group(0)

    without_credentials = QUERY_PARAMETER_PATTERN.sub(sanitize_parameter, without_credentials)
    without_ansi = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", without_credentials)
    return "".join(
        character
        for character in without_ansi
        if character in "\0\n\t" or ord(character) >= 32
    )


class GitService:
    def __init__(
        self,
        repo: Path,
        timeout: float = 20.0,
        destination_validator: Callable[[], bool] | None = None,
    ):
        self.repo = repo.resolve()
        self.timeout = timeout
        self.git = shutil.which("git")
        self._destination_validator = destination_validator
        self._validated_destination: ValidatedDestination | None = None

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "Never",
                "LC_ALL": "C",
            }
        )
        return environment

    def run(self, args: Iterable[str], timeout: float | None = None) -> CommandResult:
        if self.git is None:
            return CommandResult(127, "", "git não encontrado")
        command = [
            self.git,
            "-c", "core.hooksPath=/dev/null",
            "-c", "credential.interactive=never",
            *list(args),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=self.repo,
                env=self._environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout or self.timeout,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(124, "", "tempo limite excedido")
        except (OSError, subprocess.SubprocessError):
            return CommandResult(126, "", "falha ao iniciar operação Git")
        return CommandResult(
            completed.returncode,
            sanitize_output(completed.stdout.decode("utf-8", "replace")),
            sanitize_output(completed.stderr.decode("utf-8", "replace")),
        )

    def is_repository(self) -> bool:
        result = self.run(("rev-parse", "--is-inside-work-tree"))
        return result.returncode == 0 and result.stdout.strip() == "true"

    def status_records(self) -> tuple[CommandResult, list[str]]:
        result = self.run(("status", "--porcelain=v2", "-z", "--untracked-files=all"))
        records = [record for record in result.stdout.split("\0") if record]
        return result, records

    @staticmethod
    def has_staged_changes(records: Iterable[str]) -> bool:
        for record in records:
            if record.startswith(("1 ", "2 ")):
                fields = record.split(" ", 2)
                if len(fields) > 1 and fields[1][0] != ".":
                    return True
            elif record.startswith("u "):
                return True
        return False

    @staticmethod
    def has_any_changes(records: Iterable[str]) -> bool:
        return any(True for _ in records)

    def fetch(self) -> CommandResult:
        destination = self.validate_destination()
        if destination is None:
            return CommandResult(125, "", "destino remoto não validado")
        return self.run(
            (
                "fetch", "--no-tags", destination.fetch_url,
                f"+refs/heads/{destination.branch}:refs/remotes/origin/{destination.branch}",
            ),
            timeout=max(self.timeout, 30),
        )

    def ref(self, name: str) -> str | None:
        result = self.run(("rev-parse", "--verify", name))
        return result.stdout.strip() if result.returncode == 0 else None

    def is_ancestor(self, older: str, newer: str) -> bool:
        return self.run(("merge-base", "--is-ancestor", older, newer)).returncode == 0

    def fast_forward(self) -> CommandResult:
        status, records = self.status_records()
        if status.returncode != 0 or self.has_any_changes(records):
            return CommandResult(3, "", "alterações concorrentes preservadas")
        if self.has_lock_contention():
            return CommandResult(4, "", "repositório ocupado por outra operação Git")
        local = self.ref("HEAD")
        remote = self.ref("origin/main")
        if not local or not remote or not self.is_ancestor(local, remote):
            return CommandResult(2, "", "fast-forward recusado")
        advanced = self.run(
            ("-c", "merge.autoStash=false", "merge", "--ff-only", "--no-edit", "origin/main")
        )
        final_status, final_records = self.status_records()
        if advanced.returncode == 0 and (
            final_status.returncode != 0 or self.has_any_changes(final_records)
        ):
            return CommandResult(3, "", "alterações concorrentes preservadas")
        return advanced

    def has_lock_contention(self) -> bool:
        for lock_name in ("index.lock", "refs/heads/main.lock", "packed-refs.lock"):
            result = self.run(("rev-parse", "--git-path", lock_name))
            if result.returncode != 0:
                return True
            lock_path = Path(result.stdout.strip())
            if not lock_path.is_absolute():
                lock_path = self.repo / lock_path
            if lock_path.exists():
                return True
        return False

    def push(self) -> CommandResult:
        destination = self.validate_destination()
        if destination is None:
            return CommandResult(125, "", "destino remoto não validado")
        return self.run(
            ("push", destination.push_url, f"HEAD:refs/heads/{destination.branch}"),
            timeout=max(self.timeout, 30),
        )

    def validate_destination(self) -> ValidatedDestination | None:
        if self._destination_validator is not None:
            if not self._destination_validator():
                return None
            return self._capture_destination(require_official=False, verify_metadata=False)
        return self._capture_destination(require_official=True, verify_metadata=True)

    def _capture_destination(
        self,
        *,
        require_official: bool,
        verify_metadata: bool,
    ) -> ValidatedDestination | None:
        branch = self.run(("symbolic-ref", "--quiet", "--short", "HEAD"))
        fetch_origin = self.run(("remote", "get-url", "origin"))
        push_origins = self.run(("remote", "get-url", "--push", "--all", "origin"))
        if (
            branch.returncode != 0
            or branch.stdout.strip() != "main"
            or fetch_origin.returncode != 0
            or push_origins.returncode != 0
        ):
            self._validated_destination = None
            return None
        fetch_url = fetch_origin.stdout.strip()
        push_urls = [url.strip() for url in push_origins.stdout.splitlines() if url.strip()]
        if (
            len(push_urls) != 1
            or (require_official and fetch_url not in OFFICIAL_ORIGINS)
            or (require_official and push_urls[0] not in OFFICIAL_ORIGINS)
        ):
            self._validated_destination = None
            return None
        destination = ValidatedDestination(fetch_url=fetch_url, push_url=push_urls[0])
        if self._validated_destination == destination:
            return destination
        if verify_metadata and not validate_github_repository_metadata():
            self._validated_destination = None
            return None
        self._validated_destination = destination
        return destination

    def add_exact(self, relative_paths: Iterable[str]) -> CommandResult:
        paths = tuple(dict.fromkeys(relative_paths))
        if not paths:
            return CommandResult(0, "", "")
        for relative in paths:
            candidate = (self.repo / relative).resolve(strict=False)
            try:
                candidate.relative_to(self.repo)
            except ValueError:
                return CommandResult(2, "", "caminho fora do repositório")
        effective = tuple(
            relative
            for relative in paths
            if (self.repo / relative).exists()
            or self.run(("ls-files", "--error-unmatch", "--", relative)).returncode == 0
        )
        if not effective:
            return CommandResult(0, "", "")
        return self.run(("add", "-A", "--", *effective))

    def staged_paths(self) -> tuple[CommandResult, set[str]]:
        result = self.run(("diff", "--cached", "--name-only", "-z"))
        return result, {path for path in result.stdout.split("\0") if path}

    def commit(self, message: str) -> CommandResult:
        return self.run(("commit", "--no-verify", "-m", message))

    def unstage_exact(self, relative_paths: Iterable[str]) -> CommandResult:
        paths = tuple(dict.fromkeys(relative_paths))
        if not paths:
            return CommandResult(0, "", "")
        return self.run(("restore", "--staged", "--", *paths))


def classify_failure(result: CommandResult) -> str:
    message = f"{result.stdout}\n{result.stderr}".casefold()
    if result.returncode == 124 or any(
        marker in message
        for marker in (
            "could not resolve host", "failed to connect", "network is unreachable",
            "connection timed out", "connection refused", "no route to host",
        )
    ):
        return STATE_OFFLINE
    if any(
        marker in message
        for marker in (
            "authentication failed", "could not read username", "permission denied",
            "repository not found", "terminal prompts disabled", "http 401", "http 403",
        )
    ):
        return STATE_LOGIN
    if any(marker in message for marker in ("non-fast-forward", "[rejected]", "fetch first", "destino remoto não validado", "alterações concorrentes preservadas", "repositório ocupado")):
        return STATE_CONFLICT
    return STATE_ERROR


def detect_github_cli(timeout: float = 5.0) -> tuple[bool, str | None]:
    gh = shutil.which("gh")
    if gh is None:
        return False, None
    environment = os.environ.copy()
    environment.update({"GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "LC_ALL": "C"})
    try:
        result = subprocess.run(
            [gh, "auth", "status", "--hostname", "github.com"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return False, None
    output = sanitize_output(
        result.stdout.decode("utf-8", "replace") + "\n" + result.stderr.decode("utf-8", "replace")
    )
    if result.returncode != 0:
        return False, None
    match = re.search(r"Logged in to github\.com account ([^\s(]+)", output, re.IGNORECASE)
    username = match.group(1) if match else None
    if username and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", username):
        username = None
    return True, username


def validate_github_repository_metadata(timeout: float = 8.0) -> bool:
    gh = shutil.which("gh")
    if gh is None:
        return False
    environment = os.environ.copy()
    environment.update({"GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "LC_ALL": "C"})
    try:
        result = subprocess.run(
            [gh, "repo", "view", EXPECTED_REPOSITORY, "--json", "nameWithOwner,visibility"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    sanitized = sanitize_output(result.stdout.decode("utf-8", "replace"))
    try:
        metadata = json.loads(sanitized)
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        metadata.get("nameWithOwner") == EXPECTED_REPOSITORY
        and metadata.get("visibility") == "PRIVATE"
    )


class SyncCoordinator(QObject):
    stateChanged = Signal(str)
    authChanged = Signal(bool, str)
    repositoryUpdated = Signal()

    def __init__(self, repo: Path, auto_sync: bool = True, service: GitService | None = None):
        super().__init__()
        self.service = service or GitService(repo)
        self._queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._fetch_lock = threading.Lock()
        self._fetch_pending = False
        self._closed = False
        self._state = STATE_LOCAL
        self._worker = threading.Thread(target=self._run, name="biblioteca-git", daemon=True)
        self._worker.start()
        self._queue.put(("auth", None))
        if auto_sync:
            self.request_sync()

    @property
    def state(self) -> str:
        return self._state

    def _set_state(self, value: str) -> None:
        self._state = value
        self.stateChanged.emit(value)

    def request_sync(self) -> None:
        with self._fetch_lock:
            if self._fetch_pending:
                return
            self._fetch_pending = True
        self._queue.put(("fetch", None))

    def request_save(self, relative_paths: Iterable[str], message: str) -> None:
        self._set_state(STATE_LOCAL)
        self._queue.put(("save", (tuple(relative_paths), message)))

    def wait_for_idle(self, timeout: float = 10.0) -> bool:
        event = threading.Event()
        self._queue.put(("barrier", event))
        return event.wait(timeout)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(("stop", None))
        self._worker.join(timeout=2)

    def _run(self) -> None:
        while True:
            action, payload = self._queue.get()
            try:
                if action == "stop":
                    return
                if action == "auth":
                    authenticated, username = detect_github_cli()
                    self.authChanged.emit(authenticated, username or "")
                elif action == "fetch":
                    self._sync()
                elif action == "save":
                    paths, message = payload  # type: ignore[misc]
                    self._save(paths, message)
            except Exception:
                self._set_state(STATE_ERROR)
            finally:
                if action == "fetch":
                    with self._fetch_lock:
                        self._fetch_pending = False
                if action == "barrier" and isinstance(payload, threading.Event):
                    payload.set()
                self._queue.task_done()

    def _preflight(self) -> tuple[list[str] | None, str | None]:
        if not self.service.is_repository():
            return None, STATE_ERROR
        status, records = self.service.status_records()
        if status.returncode != 0:
            return None, classify_failure(status)
        if self.service.has_staged_changes(records):
            return None, STATE_CONFLICT
        return records, None

    def _sync(self) -> None:
        self._set_state(STATE_SYNCING)
        records, error = self._preflight()
        if error:
            self._set_state(error)
            return
        fetched = self.service.fetch()
        if fetched.returncode != 0:
            self._set_state(classify_failure(fetched))
            return
        if records and self.service.has_any_changes(records):
            self._set_state(STATE_LOCAL)
            return
        local = self.service.ref("HEAD")
        remote = self.service.ref("origin/main")
        if not local or not remote:
            self._set_state(STATE_ERROR)
        elif local == remote:
            self._set_state(STATE_SYNCED)
        elif self.service.is_ancestor(local, remote):
            advanced = self.service.fast_forward()
            if advanced.returncode == 0:
                self._set_state(STATE_SYNCED)
                self.repositoryUpdated.emit()
            else:
                self._set_state(STATE_CONFLICT)
        elif self.service.is_ancestor(remote, local):
            pushed = self.service.push()
            self._set_state(STATE_SYNCED if pushed.returncode == 0 else classify_failure(pushed))
        else:
            self._set_state(STATE_CONFLICT)

    def _save(self, paths: tuple[str, ...], message: str) -> None:
        self._set_state(STATE_SYNCING)
        _, error = self._preflight()
        if error:
            self._set_state(error)
            return
        added = self.service.add_exact(paths)
        if added.returncode != 0:
            self._set_state(STATE_ERROR)
            return
        staged_result, staged = self.service.staged_paths()
        allowed = set(paths)
        if staged_result.returncode != 0 or not staged.issubset(allowed):
            self.service.unstage_exact(staged & allowed)
            self._set_state(STATE_CONFLICT)
            return
        if staged:
            committed = self.service.commit(message)
            if committed.returncode != 0:
                self.service.unstage_exact(staged & allowed)
                self._set_state(STATE_ERROR)
                return
        fetched = self.service.fetch()
        if fetched.returncode != 0:
            self._set_state(STATE_PENDING if classify_failure(fetched) == STATE_OFFLINE else classify_failure(fetched))
            return
        local = self.service.ref("HEAD")
        remote = self.service.ref("origin/main")
        if not local or not remote:
            self._set_state(STATE_PENDING)
        elif self.service.is_ancestor(remote, local):
            pushed = self.service.push()
            failure = classify_failure(pushed)
            self._set_state(STATE_SYNCED if pushed.returncode == 0 else (STATE_PENDING if failure == STATE_OFFLINE else failure))
        else:
            self._set_state(STATE_CONFLICT)
