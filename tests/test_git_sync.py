import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from biblioteca_ideias.git_sync import (
    CommandResult,
    GitService,
    STATE_CONFLICT,
    STATE_ERROR,
    STATE_PENDING,
    STATE_SYNCED,
    SyncCoordinator,
    classify_failure,
    detect_github_cli,
    sanitize_output,
)


def git(cwd: Path, *args: str) -> str:
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, shell=False,
    )
    return result.stdout.decode().strip()


class GitIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        seed = self.root / "seed"
        git(self.root, "clone", str(self.remote), str(seed))
        self.configure(seed)
        (seed / "ideia.md").write_text("inicial\n", encoding="utf-8")
        git(seed, "add", "--", "ideia.md")
        git(seed, "commit", "-m", "inicial")
        git(seed, "push", "origin", "main")
        self.a = self.root / "a"
        self.b = self.root / "b"
        git(self.root, "clone", "--branch", "main", str(self.remote), str(self.a))
        git(self.root, "clone", "--branch", "main", str(self.remote), str(self.b))
        self.configure(self.a)
        self.configure(self.b)
        self.auth_patch = patch("biblioteca_ideias.git_sync.detect_github_cli", return_value=(True, "teste"))
        self.auth_patch.start()

    def tearDown(self) -> None:
        self.auth_patch.stop()
        self.temporary.cleanup()

    @staticmethod
    def configure(repo: Path) -> None:
        git(repo, "config", "user.name", "Teste")
        git(repo, "config", "user.email", "teste@example.invalid")

    def coordinator(self, repo: Path, timeout: float = 5) -> SyncCoordinator:
        return SyncCoordinator(repo, auto_sync=False, service=GitService(repo, timeout=timeout))

    def test_manual_sync_fast_forwards_clean_clone(self) -> None:
        (self.b / "remota.md").write_text("remota\n", encoding="utf-8")
        git(self.b, "add", "--", "remota.md")
        git(self.b, "commit", "-m", "remota")
        git(self.b, "push", "origin", "main")
        coordinator = self.coordinator(self.a)
        try:
            coordinator.request_sync()
            self.assertTrue(coordinator.wait_for_idle())
            self.assertEqual(coordinator.state, STATE_SYNCED)
            self.assertTrue((self.a / "remota.md").exists())
            self.assertEqual(git(self.a, "rev-parse", "HEAD"), git(self.b, "rev-parse", "HEAD"))
        finally:
            coordinator.close()

    def test_offline_save_keeps_local_commit_pending(self) -> None:
        git(self.a, "remote", "set-url", "origin", "http://127.0.0.1:9/inexistente.git")
        (self.a / "ideia.md").write_text("salva localmente\n", encoding="utf-8")
        coordinator = self.coordinator(self.a, timeout=2)
        before = git(self.a, "rev-parse", "HEAD")
        try:
            coordinator.request_save(("ideia.md",), "ideia: salvar")
            self.assertTrue(coordinator.wait_for_idle())
            self.assertEqual(coordinator.state, STATE_PENDING)
            self.assertNotEqual(git(self.a, "rev-parse", "HEAD"), before)
            self.assertEqual((self.a / "ideia.md").read_text(encoding="utf-8"), "salva localmente\n")
        finally:
            coordinator.close()

    def test_divergence_is_preserved_without_merge_or_reset(self) -> None:
        (self.a / "local.md").write_text("local\n", encoding="utf-8")
        git(self.a, "add", "--", "local.md")
        git(self.a, "commit", "-m", "local")
        local_head = git(self.a, "rev-parse", "HEAD")
        (self.b / "remota.md").write_text("remota\n", encoding="utf-8")
        git(self.b, "add", "--", "remota.md")
        git(self.b, "commit", "-m", "remota")
        git(self.b, "push", "origin", "main")
        coordinator = self.coordinator(self.a)
        try:
            coordinator.request_sync()
            self.assertTrue(coordinator.wait_for_idle())
            self.assertEqual(coordinator.state, STATE_CONFLICT)
            self.assertEqual(git(self.a, "rev-parse", "HEAD"), local_head)
            self.assertTrue((self.a / "local.md").exists())
        finally:
            coordinator.close()

    def test_save_detects_remote_divergence_and_preserves_both_histories(self) -> None:
        (self.a / "ideia.md").write_text("local\n", encoding="utf-8")
        (self.b / "remota.md").write_text("remota\n", encoding="utf-8")
        git(self.b, "add", "--", "remota.md")
        git(self.b, "commit", "-m", "remota")
        git(self.b, "push", "origin", "main")
        remote_head = git(self.b, "rev-parse", "HEAD")
        coordinator = self.coordinator(self.a)
        try:
            coordinator.request_save(("ideia.md",), "ideia: local")
            self.assertTrue(coordinator.wait_for_idle())
            self.assertEqual(coordinator.state, STATE_CONFLICT)
            self.assertNotEqual(git(self.a, "rev-parse", "HEAD"), remote_head)
            self.assertEqual(git(self.a, "rev-parse", "origin/main"), remote_head)
            self.assertEqual((self.a / "ideia.md").read_text(encoding="utf-8"), "local\n")
        finally:
            coordinator.close()

    def test_external_staged_change_blocks_app_commit(self) -> None:
        (self.a / "externo.txt").write_text("externo\n", encoding="utf-8")
        git(self.a, "add", "--", "externo.txt")
        (self.a / "ideia.md").write_text("nova\n", encoding="utf-8")
        coordinator = self.coordinator(self.a)
        try:
            coordinator.request_save(("ideia.md",), "ideia: salvar")
            self.assertTrue(coordinator.wait_for_idle())
            self.assertEqual(coordinator.state, STATE_CONFLICT)
            self.assertEqual(set(git(self.a, "diff", "--cached", "--name-only").splitlines()), {"externo.txt"})
        finally:
            coordinator.close()

    def test_rapid_rename_saves_remain_fifo_and_reach_remote(self) -> None:
        os.replace(self.a / "ideia.md", self.a / "Renomeada.md")
        coordinator = self.coordinator(self.a)
        try:
            coordinator.request_save(("ideia.md", "Renomeada.md"), "ideia: renomear")
            os.replace(self.a / "Renomeada.md", self.a / "Final.md")
            coordinator.request_save(("Renomeada.md", "Final.md"), "ideia: renomear novamente")
            self.assertTrue(coordinator.wait_for_idle())
            self.assertEqual(coordinator.state, STATE_SYNCED)
            verification = self.root / "verification"
            git(self.root, "clone", "--branch", "main", str(self.remote), str(verification))
            self.assertTrue((verification / "Final.md").exists())
            self.assertFalse((verification / "ideia.md").exists())
        finally:
            coordinator.close()

    def test_hooks_are_disabled_for_app_commit(self) -> None:
        hooks = self.a / ".git" / "hooks"
        marker = self.root / "hook-ran"
        hook = hooks / "pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        (self.a / "ideia.md").write_text("sem hook\n", encoding="utf-8")
        coordinator = self.coordinator(self.a)
        try:
            coordinator.request_save(("ideia.md",), "ideia: sem hook")
            self.assertTrue(coordinator.wait_for_idle())
            self.assertEqual(coordinator.state, STATE_SYNCED)
            self.assertFalse(marker.exists())
        finally:
            coordinator.close()

    def test_prompt_is_disabled_and_output_is_sanitized(self) -> None:
        service = GitService(self.a)
        self.assertEqual(service._environment()["GIT_TERMINAL_PROMPT"], "0")
        fake_credential = "ghp_" + "abcdefghijklmnopqrstuvwxyz"
        sanitized = sanitize_output(f"https://usuario:segredo@example.invalid {fake_credential}")
        self.assertNotIn("segredo", sanitized)
        self.assertNotIn("ghp_", sanitized)
        self.assertEqual(sanitize_output("primeiro\0segundo"), "primeiro\0segundo")

    def test_all_github_token_prefixes_and_encoded_underscore_are_masked(self) -> None:
        credentials = [
            "gh" + prefix + "_abcdefghijklmnopqrstuvwxyz"
            for prefix in ("p", "o", "u", "s", "r")
        ]
        credentials.extend(
            (
                "GHO" + "_ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                "github" + "_pat_abcdefghijklmnopqrstuvwxyz",
                "ghp" + "%5Fabcdefghijklmnopqrstuvwxyz",
                "%67%68%70%5F" + "abcdefghijklmnopqrstuvwxyz",
            )
        )
        sanitized = sanitize_output(" ".join(credentials))
        for credential in credentials:
            self.assertNotIn(credential, sanitized)
        self.assertEqual(sanitized.count("[credencial removida]"), len(credentials))

    def test_sensitive_query_keys_are_decoded_and_normal_parameters_survive(self) -> None:
        sensitive = (
            "https://example.invalid/path?access" + "%5Fto" + "ken=segredo1"
            "&pa%73sword=segredo2&AUTH=segredo3#client-secret=segredo4"
        )
        normal = "&version=1&tokenizer=palavra&author=alguem"
        sanitized = sanitize_output(sensitive + normal)
        for secret in ("segredo1", "segredo2", "segredo3", "segredo4"):
            self.assertNotIn(secret, sanitized)
        self.assertIn(normal, sanitized)

    @patch("biblioteca_ideias.git_sync.subprocess.run")
    def test_git_service_never_returns_credentials_from_diagnostics(self, run) -> None:
        credential = "gh" + "r_abcdefghijklmnopqrstuvwxyz"
        query = "?access" + "_to" + "ken=segredo-query"
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=credential.encode(),
            stderr=("falha https://usuario:senha@example.invalid/" + query).encode(),
        )
        service = GitService(self.a)
        service.git = "/usr/bin/git"
        result = service.run(("fetch", "--no-tags", "origin", "main"))
        combined = result.stdout + result.stderr
        self.assertNotIn(credential, combined)
        self.assertNotIn("senha", combined)
        self.assertNotIn("segredo-query", combined)
        self.assertIn("falha", combined)

    @patch("biblioteca_ideias.git_sync.shutil.which", return_value="/usr/bin/gh")
    @patch("biblioteca_ideias.git_sync.subprocess.run")
    def test_github_cli_detection_uses_only_auth_status(self, run, _which) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b"Logged in to github.com account usuario-teste\n"
        )
        authenticated, username = detect_github_cli()
        self.assertTrue(authenticated)
        self.assertEqual(username, "usuario-teste")
        command = run.call_args.args[0]
        self.assertEqual(command, ["/usr/bin/gh", "auth", "status", "--hostname", "github.com"])
        self.assertNotIn("token", command)


class SlowService:
    def __init__(self) -> None:
        self.fetch_count = 0
        self.release = threading.Event()

    def is_repository(self) -> bool:
        return True

    def status_records(self):
        return CommandResult(0, "", ""), []

    def has_staged_changes(self, records):
        return False

    def has_any_changes(self, records):
        return False

    def fetch(self):
        self.fetch_count += 1
        self.release.wait(2)
        return CommandResult(0, "", "")

    def ref(self, name):
        return "abc"


class FlakyService(SlowService):
    def fetch(self):
        self.fetch_count += 1
        if self.fetch_count == 1:
            raise RuntimeError("gh" + "p_credencial-nao-deve-vazar")
        return CommandResult(0, "", "")


class CoordinatorResponsivenessTest(unittest.TestCase):
    @patch("biblioteca_ideias.git_sync.detect_github_cli", return_value=(False, None))
    def test_fetch_requests_coalesce_and_caller_never_blocks(self, _auth) -> None:
        service = SlowService()
        coordinator = SyncCoordinator(Path("/tmp"), auto_sync=False, service=service)
        try:
            started = time.monotonic()
            for _ in range(20):
                coordinator.request_sync()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.1)
            service.release.set()
            self.assertTrue(coordinator.wait_for_idle())
            self.assertEqual(service.fetch_count, 1)
            self.assertEqual(coordinator.state, STATE_SYNCED)
        finally:
            coordinator.close()

    @patch("biblioteca_ideias.git_sync.detect_github_cli", return_value=(False, None))
    def test_worker_survives_exception_releases_fetch_and_processes_next_task(self, _auth) -> None:
        service = FlakyService()
        coordinator = SyncCoordinator(Path("/tmp"), auto_sync=False, service=service)
        try:
            coordinator.request_sync()
            self.assertTrue(coordinator.wait_for_idle())
            self.assertEqual(coordinator.state, STATE_ERROR)
            self.assertTrue(coordinator._worker.is_alive())

            coordinator.request_sync()
            self.assertTrue(coordinator.wait_for_idle())
            self.assertEqual(service.fetch_count, 2)
            self.assertEqual(coordinator.state, STATE_SYNCED)
            self.assertTrue(coordinator._worker.is_alive())
        finally:
            coordinator.close()


class GitServiceSafetyTest(unittest.TestCase):
    @patch("biblioteca_ideias.git_sync.subprocess.run")
    def test_commands_disable_hooks_shell_and_prompts(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
        service = GitService(Path("/tmp"))
        service.git = "/usr/bin/git"
        service.run(("status", "--porcelain=v2", "-z"))
        command = run.call_args.args[0]
        options = run.call_args.kwargs
        self.assertIn("core.hooksPath=/dev/null", command)
        self.assertFalse(options["shell"])
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["env"]["GIT_TERMINAL_PROMPT"], "0")

    @patch("biblioteca_ideias.git_sync.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 1))
    def test_timeout_is_bounded_and_sanitized(self, _run) -> None:
        service = GitService(Path("/tmp"), timeout=1)
        service.git = "/usr/bin/git"
        result = service.run(("fetch", "--no-tags", "origin", "main"))
        self.assertEqual(result.returncode, 124)
        self.assertNotEqual(classify_failure(result), STATE_ERROR)

    def test_rejected_push_is_a_conflict(self) -> None:
        result = CommandResult(1, "", "! [rejected] main -> main (non-fast-forward)")
        self.assertEqual(classify_failure(result), STATE_CONFLICT)

    @patch("biblioteca_ideias.git_sync.subprocess.run")
    def test_spawn_exception_is_generic_and_never_leaks_credentials(self, run) -> None:
        credential = "gh" + "s_credencial-nao-deve-vazar"
        run.side_effect = OSError(f"falha contendo {credential}")
        service = GitService(Path("/tmp"))
        service.git = "/usr/bin/git"
        result = service.run(("status", "--porcelain=v2", "-z"))
        self.assertEqual(result.returncode, 126)
        self.assertNotIn(credential, result.stderr)
        self.assertEqual(result.stderr, "falha ao iniciar operação Git")


if __name__ == "__main__":
    unittest.main()
