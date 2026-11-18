import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication

from biblioteca_ideias.bootstrap import (
    BootstrapCoordinator,
    BootstrapError,
    _renameat2_noreplace,
    clone_existing_vault,
    harden_vault_tree,
    validate_existing_vault,
)
from biblioteca_ideias.storage import MarkdownStore


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", *args], cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, shell=False,
    )
    return result.stdout.decode().strip()


class BootstrapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.seed.mkdir()
        store = MarkdownStore(self.seed, state_dir=self.root / "seed-state")
        store.create("Ideia de teste", "## Resumo\n\nConteúdo")
        git(self.seed, "init", "--initial-branch=main")
        git(self.seed, "config", "user.name", "Teste")
        git(self.seed, "config", "user.email", "teste@example.invalid")
        git(self.seed, "add", "--all")
        git(self.seed, "commit", "-m", "cofre")
        git(self.seed, "remote", "add", "origin", self.remote.as_uri())
        git(self.seed, "push", "origin", "main")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def clone_patches(self):
        return (
            patch("biblioteca_ideias.bootstrap.OFFICIAL_CLONE_URL", self.remote.as_uri()),
            patch("biblioteca_ideias.git_sync.OFFICIAL_ORIGINS", {self.remote.as_uri()}),
            patch("biblioteca_ideias.bootstrap.detect_github_cli", return_value=(True, "Torugo76")),
            patch("biblioteca_ideias.bootstrap.validate_github_repository_metadata", return_value=True),
            patch("biblioteca_ideias.git_sync.validate_github_repository_metadata", return_value=True),
        )

    def test_second_computer_clone_is_validated_and_moved_atomically(self) -> None:
        destination = self.root / "novo" / "cofre"
        patches = self.clone_patches()
        for item in patches:
            item.start()
        try:
            installed = clone_existing_vault(destination)
        finally:
            for item in reversed(patches):
                item.stop()
        self.assertEqual(installed, destination)
        self.assertTrue((destination / ".git").is_dir())
        self.assertEqual(len(list(destination.rglob("*.md"))), 1)
        for path in (destination, *destination.rglob("*")):
            if path.is_dir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o700, path)
            elif path.is_file():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600, path)
        self.assertEqual(git(destination, "status", "--porcelain"), "")
        self.assertFalse(list(destination.parent.glob(".cofre-bootstrap-*")))

    def test_existing_nonempty_destination_is_preserved(self) -> None:
        destination = self.root / "destino"
        destination.mkdir()
        marker = destination / "preservar.txt"
        marker.write_text("não apagar", encoding="utf-8")
        with self.assertRaises(BootstrapError):
            clone_existing_vault(destination)
        self.assertEqual(marker.read_text(encoding="utf-8"), "não apagar")

    def test_all_existing_destination_types_are_preserved(self) -> None:
        variants = ("empty-dir", "file", "symlink", "dangling-symlink")
        for variant in variants:
            with self.subTest(variant=variant):
                destination = self.root / f"destino-{variant}"
                if variant == "empty-dir":
                    destination.mkdir()
                elif variant == "file":
                    destination.write_text("preservar", encoding="utf-8")
                elif variant == "symlink":
                    target = self.root / f"alvo-{variant}"
                    target.mkdir()
                    destination.symlink_to(target, target_is_directory=True)
                else:
                    destination.symlink_to(self.root / "alvo-ausente", target_is_directory=True)
                before_link = os.readlink(destination) if destination.is_symlink() else None
                with self.assertRaises(BootstrapError):
                    clone_existing_vault(destination)
                self.assertTrue(os.path.lexists(destination))
                if before_link is not None:
                    self.assertEqual(os.readlink(destination), before_link)

    def test_atomic_publish_refuses_destination_created_at_syscall(self) -> None:
        destination = self.root / "corrida" / "cofre"
        original_rename = _renameat2_noreplace

        def create_destination_then_publish(source: Path, target: Path) -> None:
            target.mkdir()
            (target / "concorrente.txt").write_text("preservar", encoding="utf-8")
            original_rename(source, target)

        patches = self.clone_patches()
        for item in patches:
            item.start()
        try:
            with patch(
                "biblioteca_ideias.bootstrap._renameat2_noreplace",
                side_effect=create_destination_then_publish,
            ):
                with self.assertRaises(BootstrapError):
                    clone_existing_vault(destination)
        finally:
            for item in reversed(patches):
                item.stop()
        self.assertEqual((destination / "concorrente.txt").read_text(encoding="utf-8"), "preservar")
        self.assertFalse((destination / ".git").exists())
        self.assertFalse(list(destination.parent.glob(".cofre-bootstrap-*")))

    def test_publish_fails_closed_without_renameat2(self) -> None:
        source = self.root / "origem"
        destination = self.root / "destino-sem-syscall"
        source.mkdir()
        with patch("biblioteca_ideias.bootstrap.ctypes.CDLL", side_effect=AttributeError):
            with self.assertRaisesRegex(BootstrapError, "instalação atômica"):
                _renameat2_noreplace(source, destination)
        self.assertTrue(source.exists())
        self.assertFalse(destination.exists())

    def test_hardening_rejects_symlinks_and_special_files(self) -> None:
        symlink_root = self.root / "com-link"
        symlink_root.mkdir()
        (symlink_root / "link").symlink_to(self.root / "fora")
        with self.assertRaisesRegex(BootstrapError, "link simbólico"):
            harden_vault_tree(symlink_root)

        special_root = self.root / "especial"
        special_root.mkdir()
        os.mkfifo(special_root / "fifo")
        with self.assertRaisesRegex(BootstrapError, "arquivo especial"):
            harden_vault_tree(special_root)

    def test_invalid_clone_is_rejected_before_atomic_move(self) -> None:
        bad_remote = self.root / "bad.git"
        bad_seed = self.root / "bad-seed"
        git(self.root, "init", "--bare", "--initial-branch=main", str(bad_remote))
        git(self.root, "init", "--initial-branch=main", str(bad_seed))
        git(bad_seed, "config", "user.name", "Teste")
        git(bad_seed, "config", "user.email", "teste@example.invalid")
        (bad_seed / "inesperado.txt").write_text("inválido", encoding="utf-8")
        git(bad_seed, "add", "--all")
        git(bad_seed, "commit", "-m", "inválido")
        git(bad_seed, "remote", "add", "origin", bad_remote.as_uri())
        git(bad_seed, "push", "origin", "main")
        destination = self.root / "novo-invalido" / "cofre"
        with (
            patch("biblioteca_ideias.bootstrap.OFFICIAL_CLONE_URL", bad_remote.as_uri()),
            patch("biblioteca_ideias.git_sync.OFFICIAL_ORIGINS", {bad_remote.as_uri()}),
            patch("biblioteca_ideias.bootstrap.detect_github_cli", return_value=(True, "Torugo76")),
            patch("biblioteca_ideias.bootstrap.validate_github_repository_metadata", return_value=True),
            patch("biblioteca_ideias.git_sync.validate_github_repository_metadata", return_value=True),
        ):
            with self.assertRaises(BootstrapError):
                clone_existing_vault(destination)
        self.assertFalse(destination.exists())
        self.assertFalse(list(destination.parent.glob(".cofre-bootstrap-*")))

    def test_wrong_account_auth_missing_and_private_repo_inaccessible(self) -> None:
        destination = self.root / "novo" / "cofre"
        with patch("biblioteca_ideias.bootstrap.detect_github_cli", return_value=(False, None)):
            with self.assertRaisesRegex(BootstrapError, "Conecte"):
                clone_existing_vault(destination)
        with patch("biblioteca_ideias.bootstrap.detect_github_cli", return_value=(True, "outra-conta")):
            with self.assertRaisesRegex(BootstrapError, "conta conectada"):
                clone_existing_vault(destination)
        with (
            patch("biblioteca_ideias.bootstrap.detect_github_cli", return_value=(True, "Torugo76")),
            patch("biblioteca_ideias.bootstrap.validate_github_repository_metadata", return_value=False),
        ):
            with self.assertRaisesRegex(BootstrapError, "não está acessível"):
                clone_existing_vault(destination)
        self.assertFalse(destination.exists())

    def test_existing_invalid_vault_is_not_modified(self) -> None:
        destination = self.root / "inesperado"
        destination.mkdir()
        marker = destination / "arquivo.txt"
        marker.write_text("preservado", encoding="utf-8")
        before = marker.read_bytes()
        with self.assertRaises(BootstrapError):
            validate_existing_vault(destination)
        self.assertEqual(marker.read_bytes(), before)

    def test_login_output_is_sanitized_and_browser_failure_is_clear(self) -> None:
        vault = self.root / "existente"
        vault.mkdir()
        fake_gh = self.root / "gh-falso"
        fake_gh.write_text(
            "#!/bin/sh\nprintf 'Use o código ABCD-1234 ghp_'\nsleep 0.1\n"
            "printf 'segredonaoexibir\\n'\nexit 1\n",
            encoding="utf-8",
        )
        fake_gh.chmod(0o755)
        coordinator = BootstrapCoordinator(vault)
        progress: list[str] = []
        coordinator.progressChanged.connect(progress.append)
        try:
            with (
                patch("biblioteca_ideias.bootstrap.detect_github_cli", return_value=(False, None)),
                patch("biblioteca_ideias.bootstrap.resolve_system_executable", return_value=str(fake_gh)),
            ):
                with self.assertRaises(BootstrapError):
                    coordinator._login()
            combined = " ".join(progress)
            self.assertIn("ABCD-1234", combined)
            self.assertNotIn("ghp_", combined)

            with (
                patch("biblioteca_ideias.bootstrap.detect_github_cli", return_value=(False, None)),
                patch("biblioteca_ideias.bootstrap.resolve_system_executable", return_value=str(self.root / "ausente")),
            ):
                with self.assertRaisesRegex(BootstrapError, "iniciar o login"):
                    coordinator._login()
        finally:
            coordinator.close()

    def test_login_reports_missing_github_cli(self) -> None:
        vault = self.root / "existente-sem-gh"
        vault.mkdir()
        coordinator = BootstrapCoordinator(vault)
        try:
            with (
                patch("biblioteca_ideias.bootstrap.detect_github_cli", return_value=(False, None)),
                patch("biblioteca_ideias.bootstrap.resolve_system_executable", return_value=None),
            ):
                with self.assertRaisesRegex(BootstrapError, "não está instalado"):
                    coordinator._login()
        finally:
            coordinator.close()

    def test_login_can_be_cancelled_without_touching_vault(self) -> None:
        vault = self.root / "existente"
        vault.mkdir()
        marker = vault / "preservar.txt"
        marker.write_text("preservado", encoding="utf-8")
        fake_gh = self.root / "gh-lento"
        fake_gh.write_text("#!/bin/sh\nprintf 'Código WXYZ-9876\\n'\nsleep 10\n", encoding="utf-8")
        fake_gh.chmod(0o755)
        coordinator = BootstrapCoordinator(vault)
        states: list[str] = []
        coordinator.stateChanged.connect(states.append)
        try:
            with (
                patch("biblioteca_ideias.bootstrap.detect_github_cli", return_value=(False, None)),
                patch("biblioteca_ideias.bootstrap.resolve_system_executable", return_value=str(fake_gh)),
            ):
                worker = threading.Thread(target=coordinator._login)
                worker.start()
                deadline = time.monotonic() + 3
                while coordinator._login_process is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                coordinator.cancel_login()
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.application.processEvents()
            self.assertTrue(any("cancelado" in state.casefold() for state in states))
            self.assertEqual(marker.read_text(encoding="utf-8"), "preservado")
        finally:
            coordinator.close()

    def test_already_authenticated_session_does_not_relogin(self) -> None:
        vault = self.root / "existente"
        vault.mkdir()
        coordinator = BootstrapCoordinator(vault)
        states: list[str] = []
        auth: list[tuple[bool, str]] = []
        coordinator.stateChanged.connect(states.append)
        coordinator.authChanged.connect(lambda ok, user: auth.append((ok, user)))
        try:
            with (
                patch("biblioteca_ideias.bootstrap.detect_github_cli", return_value=(True, "Torugo76")),
                patch("biblioteca_ideias.bootstrap.subprocess.Popen") as popen,
            ):
                coordinator._login()
            popen.assert_not_called()
            self.assertEqual(auth, [(True, "Torugo76")])
            self.assertTrue(any("já está conectada" in state for state in states))
        finally:
            coordinator.close()


if __name__ == "__main__":
    unittest.main()
