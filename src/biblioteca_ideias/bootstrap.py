from __future__ import annotations

import os
import queue
import selectors
import stat
import shutil
import subprocess
import tempfile
import threading
import time
import ctypes
import errno
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .git_sync import (
    EXPECTED_REPOSITORY,
    OFFICIAL_ORIGINS,
    GitService,
    detect_github_cli,
    resolve_system_executable,
    safe_subprocess_environment,
    sanitize_output,
    validate_github_repository_metadata,
)
from .storage import COLUMNS, MarkdownStore, StorageError


OFFICIAL_CLONE_URL = "https://github.com/Torugo76/biblioteca-de-ideias-cofre.git"


class BootstrapError(RuntimeError):
    pass


AT_FDCWD = -100
RENAME_NOREPLACE = 1


def terminate_and_reap(process: subprocess.Popen[bytes], *, grace_seconds: float = 0.5) -> None:
    try:
        running = process.poll() is None
    except Exception:
        running = True
    if running:
        try:
            process.terminate()
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.kill()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.wait(timeout=grace_seconds)
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        pass


def _renameat2_noreplace(source: Path, destination: Path) -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (OSError, AttributeError) as exc:
        raise BootstrapError("Este sistema não oferece instalação atômica segura para o cofre.") from exc
    renameat2.argtypes = (
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(destination), RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in (errno.EEXIST, errno.ENOTEMPTY, errno.ENOTDIR, errno.EISDIR):
        raise BootstrapError("O destino do cofre já existe. Nada foi sobrescrito.")
    if error in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
        raise BootstrapError("Este sistema não oferece instalação atômica segura para o cofre.")
    raise BootstrapError("Não foi possível instalar o cofre com segurança. Nada foi sobrescrito.")


def harden_vault_tree(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise BootstrapError("A árvore do cofre contém um caminho inválido.")
    directories: list[Path] = [root]
    files: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise BootstrapError("Não foi possível validar a árvore privada do cofre.") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise BootstrapError("Não foi possível validar a árvore privada do cofre.") from exc
            mode = metadata.st_mode
            if stat.S_ISLNK(mode):
                raise BootstrapError("O cofre contém link simbólico e não pode ser instalado.")
            if stat.S_ISDIR(mode):
                directories.append(path)
                pending.append(path)
            elif stat.S_ISREG(mode):
                files.append(path)
            else:
                raise BootstrapError("O cofre contém arquivo especial e não pode ser instalado.")
    try:
        for directory in directories:
            directory.chmod(0o700)
        for file_path in files:
            file_path.chmod(0o600)
    except OSError as exc:
        raise BootstrapError("Não foi possível aplicar permissões privadas ao cofre.") from exc


def validate_existing_vault(vault: Path, *, require_remote_metadata: bool = False) -> None:
    if vault.is_symlink() or not vault.is_dir():
        raise BootstrapError("O caminho do cofre não é uma pasta válida.")
    if not (vault / ".git").is_dir():
        raise BootstrapError("A pasta existente não é o cofre Git esperado. Nada foi alterado.")
    for column, _title in COLUMNS:
        directory = vault / column
        if directory.is_symlink() or not directory.is_dir():
            raise BootstrapError(f"Estrutura inválida: falta a coluna {column}.")

    service = GitService(vault)
    destination = service._capture_destination(
        require_official=True,
        verify_metadata=require_remote_metadata,
    )
    if destination is None:
        raise BootstrapError("O remoto do cofre não corresponde ao repositório privado esperado.")
    status, records = service.status_records()
    if status.returncode != 0 or service.has_any_changes(records):
        raise BootstrapError("O cofre recebido contém alterações inesperadas e foi preservado para revisão.")

    validation_root = Path(tempfile.mkdtemp(prefix=".bootstrap-validation-", dir=vault.parent))
    validation_root.chmod(0o700)
    validation_state = validation_root / "state"
    try:
        store = MarkdownStore(vault, state_dir=validation_state)
        store.list_ideas()
    except StorageError as exc:
        raise BootstrapError(f"Os arquivos Markdown do cofre são inválidos: {exc}") from exc
    finally:
        if validation_root.exists():
            shutil.rmtree(validation_root)
    final_status, final_records = service.status_records()
    if final_status.returncode != 0 or service.has_any_changes(final_records):
        raise BootstrapError("A validação alteraria o cofre; a instalação foi interrompida.")


def clone_existing_vault(vault: Path) -> Path:
    if os.path.lexists(vault):
        raise BootstrapError("Já existe conteúdo no destino do cofre. Nada foi sobrescrito.")
    authenticated, username = detect_github_cli()
    if not authenticated:
        raise BootstrapError("Conecte sua conta do GitHub antes de baixar o cofre.")
    if not username or username.casefold() != "torugo76":
        raise BootstrapError("A conta conectada não é a conta autorizada para este cofre.")
    if not validate_github_repository_metadata():
        raise BootstrapError("O cofre privado existente não está acessível nesta conta.")

    git = resolve_system_executable("git")
    gh = resolve_system_executable("gh")
    if not git or not gh:
        raise BootstrapError("Git e GitHub CLI precisam estar instalados.")
    vault.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(tempfile.mkdtemp(prefix=".cofre-bootstrap-", dir=vault.parent))
    temporary_root.chmod(0o700)
    candidate = temporary_root / "cofre"
    environment = safe_subprocess_environment()
    command = [
        git,
        "-c", "core.hooksPath=/dev/null",
        "-c", "core.fsmonitor=false",
        "-c", "core.attributesFile=/dev/null",
        "-c", "credential.interactive=never",
        "-c", "credential.helper=",
        "-c", f"credential.helper=!{gh} auth git-credential",
        "-c", f"url.{OFFICIAL_CLONE_URL}.insteadOf={OFFICIAL_CLONE_URL}",
        "clone", "--branch", "main", "--single-branch", "--no-tags",
        "--", OFFICIAL_CLONE_URL, os.fspath(candidate),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=vault.parent,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=90,
            check=False,
            shell=False,
            umask=0o077,
        )
        if completed.returncode != 0:
            raise BootstrapError("Não foi possível baixar o cofre privado. Seus dados locais não foram alterados.")
        harden_vault_tree(candidate)
        validate_existing_vault(candidate, require_remote_metadata=True)
        harden_vault_tree(candidate)
        _renameat2_noreplace(candidate, vault)
        return vault
    except subprocess.TimeoutExpired as exc:
        raise BootstrapError("O download do cofre excedeu o tempo limite.") from exc
    finally:
        if temporary_root.exists():
            shutil.rmtree(temporary_root)


class BootstrapCoordinator(QObject):
    stateChanged = Signal(str)
    authChanged = Signal(bool, str)
    progressChanged = Signal(str)
    ready = Signal(str)

    def __init__(self, vault: Path, parent: QObject | None = None):
        super().__init__(parent)
        self.vault = vault
        self._queue: queue.Queue[str] = queue.Queue()
        self._cancel = threading.Event()
        self._login_process: subprocess.Popen[bytes] | None = None
        self._worker = threading.Thread(target=self._run, name="biblioteca-bootstrap", daemon=True)
        self._worker.start()

    def inspect_or_bootstrap(self) -> None:
        self._queue.put("bootstrap")

    def connect_account(self) -> None:
        self._cancel.clear()
        self._queue.put("login")

    def cancel_login(self) -> None:
        self._cancel.set()
        process = self._login_process
        if process is not None and process.poll() is None:
            process.terminate()

    def close(self) -> None:
        self.cancel_login()
        self._queue.put("stop")
        self._worker.join(timeout=2)

    def _run(self) -> None:
        while True:
            action = self._queue.get()
            try:
                if action == "stop":
                    return
                if action == "bootstrap":
                    self._bootstrap()
                elif action == "login":
                    self._login()
            except BootstrapError as exc:
                self.stateChanged.emit(str(exc))
            except Exception:
                self.stateChanged.emit("Não foi possível concluir a configuração. Seus dados foram preservados.")
            finally:
                self._queue.task_done()

    def _bootstrap(self) -> None:
        if self.vault.exists():
            validate_existing_vault(self.vault)
            self.stateChanged.emit("Cofre local validado.")
            self.ready.emit(os.fspath(self.vault))
            return
        self.stateChanged.emit("Verificando acesso ao cofre privado…")
        installed = clone_existing_vault(self.vault)
        self.stateChanged.emit("Cofre privado instalado com segurança.")
        self.ready.emit(os.fspath(installed))

    def _login(self) -> None:
        authenticated, username = detect_github_cli()
        if authenticated:
            self.authChanged.emit(True, username or "")
            self.stateChanged.emit("A conta do GitHub já está conectada.")
            if not self.vault.exists():
                self._bootstrap()
            return
        gh = resolve_system_executable("gh")
        if not gh:
            raise BootstrapError("GitHub CLI não está instalado.")
        self.stateChanged.emit("Abrindo o login oficial do GitHub no navegador…")
        environment = safe_subprocess_environment(for_gh=True)
        try:
            process = subprocess.Popen(
                [gh, "auth", "login", "--web", "--hostname", "github.com", "--git-protocol", "https"],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
                umask=0o077,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BootstrapError("Não foi possível iniciar o login oficial do GitHub.") from exc
        self._login_process = process
        captured = bytearray()
        pending = bytearray()
        deadline = time.monotonic() + 180
        selector = selectors.DefaultSelector()
        try:
            if process.stdout is not None:
                selector.register(process.stdout, selectors.EVENT_READ)
            while process.poll() is None:
                if self._cancel.is_set():
                    break
                if time.monotonic() >= deadline:
                    raise BootstrapError("O login do GitHub excedeu o tempo limite.")
                for key, _mask in selector.select(timeout=0.2):
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if not chunk:
                        continue
                    captured.extend(chunk)
                    pending.extend(chunk)
                    if len(captured) > 8192:
                        del captured[:-8192]
                    while b"\n" in pending:
                        line, _, remainder = pending.partition(b"\n")
                        pending = bytearray(remainder)
                        safe_progress = sanitize_output(line.decode("utf-8", "replace")).strip()
                        if safe_progress:
                            self.progressChanged.emit(safe_progress[-500:])
            if pending:
                safe_progress = sanitize_output(pending.decode("utf-8", "replace")).strip()
                if safe_progress:
                    self.progressChanged.emit(safe_progress[-500:])
        finally:
            terminate_and_reap(process)
            selector.close()
            if process.stdout is not None:
                process.stdout.close()
            self._login_process = None
        if self._cancel.is_set():
            self.stateChanged.emit("Login cancelado. O cofre local não foi alterado.")
            return
        safe_message = sanitize_output(captured.decode("utf-8", "replace")).strip()
        if process.returncode != 0:
            if safe_message:
                self.progressChanged.emit(safe_message[-500:])
            raise BootstrapError("Não foi possível concluir o login pelo navegador.")
        authenticated, username = detect_github_cli()
        if not authenticated:
            raise BootstrapError("O GitHub CLI não confirmou a nova sessão.")
        self.authChanged.emit(True, username or "")
        self.stateChanged.emit("Conta do GitHub conectada.")
        if not self.vault.exists():
            self._bootstrap()
