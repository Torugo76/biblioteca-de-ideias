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
    clone_existing_vault,
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
