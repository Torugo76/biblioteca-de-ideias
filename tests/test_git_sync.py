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
    validate_github_repository_metadata,
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
        return SyncCoordinator(
            repo,
            auto_sync=False,
            service=GitService(repo, timeout=timeout, destination_validator=lambda: True),
        )

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
            self.assertEqual(git(self.a, "status", "--porcelain"), "")
            self.assertEqual(git(self.a, "diff", "--cached", "--name-only"), "")
            self.assertEqual(len(git(self.a, "rev-list", "--parents", "-n", "1", "HEAD").split()), 2)
        finally:
            coordinator.close()

    def test_concurrent_edit_during_fetch_is_preserved_and_ff_is_refused(self) -> None:
        (self.b / "ideia.md").write_text("remota\n", encoding="utf-8")
        git(self.b, "add", "--", "ideia.md")
        git(self.b, "commit", "-m", "remota")
        git(self.b, "push", "origin", "main")
        original_head = git(self.a, "rev-parse", "HEAD")

        class EditingFetchService(GitService):
            def fetch(inner_self):
                result = super().fetch()
                (self.a / "ideia.md").write_bytes(b"edicao externa preservada\n")
                return result

        service = EditingFetchService(
            self.a, destination_validator=lambda: True
        )
        coordinator = SyncCoordinator(self.a, auto_sync=False, service=service)
        try:
            coordinator.request_sync()
            self.assertTrue(coordinator.wait_for_idle())
            self.assertEqual(coordinator.state, STATE_CONFLICT)
            self.assertEqual((self.a / "ideia.md").read_bytes(), b"edicao externa preservada\n")
            self.assertEqual(git(self.a, "rev-parse", "HEAD"), original_head)
            self.assertEqual(git(self.a, "diff", "--cached", "--name-only"), "")
            self.assertEqual(git(self.a, "stash", "list"), "")
        finally:
            coordinator.close()

    def test_edit_injected_after_revalidation_is_refused_without_autostash(self) -> None:
        (self.b / "ideia.md").write_text("remota\n", encoding="utf-8")
        git(self.b, "add", "--", "ideia.md")
        git(self.b, "commit", "-m", "remota")
        git(self.b, "push", "origin", "main")
        original_head = git(self.a, "rev-parse", "HEAD")

        class EditingDuringMergeService(GitService):
            injected = False

            def run(inner_self, args, timeout=None):
                if "merge" in args and not inner_self.injected:
                    inner_self.injected = True
                    (self.a / "ideia.md").write_bytes(b"edicao durante operacao\n")
                return super().run(args, timeout)

        service = EditingDuringMergeService(self.a, destination_validator=lambda: True)
        coordinator = SyncCoordinator(self.a, auto_sync=False, service=service)
        try:
            coordinator.request_sync()
            self.assertTrue(coordinator.wait_for_idle())
            self.assertEqual(coordinator.state, STATE_CONFLICT)
            self.assertEqual((self.a / "ideia.md").read_bytes(), b"edicao durante operacao\n")
            self.assertEqual(git(self.a, "rev-parse", "HEAD"), original_head)
            self.assertEqual(git(self.a, "diff", "--cached", "--name-only"), "")
            self.assertEqual(git(self.a, "stash", "list"), "")
        finally:
            coordinator.close()

    def test_concurrent_ref_movement_is_detected_with_coherent_tree(self) -> None:
        (self.b / "remota.md").write_text("remota\n", encoding="utf-8")
        git(self.b, "add", "--", "remota.md")
        git(self.b, "commit", "-m", "remota")
        git(self.b, "push", "origin", "main")

        class MovingRefService(GitService):
            def fetch(inner_self):
                result = super().fetch()
                old = git(self.a, "rev-parse", "HEAD")
                tree = git(self.a, "rev-parse", "HEAD^{tree}")
                concurrent = git(self.a, "commit-tree", tree, "-p", old, "-m", "concorrente")
                git(self.a, "update-ref", "refs/heads/main", concurrent, old)
                return result

        service = MovingRefService(self.a, destination_validator=lambda: True)
        coordinator = SyncCoordinator(self.a, auto_sync=False, service=service)
        try:
            coordinator.request_sync()
            self.assertTrue(coordinator.wait_for_idle())
            self.assertEqual(coordinator.state, STATE_CONFLICT)
            self.assertEqual(git(self.a, "status", "--porcelain"), "")
            self.assertEqual(git(self.a, "diff", "--cached", "--name-only"), "")
        finally:
            coordinator.close()

    def test_ref_lock_contention_refuses_ff_without_partial_changes(self) -> None:
        (self.b / "ideia.md").write_text("remota\n", encoding="utf-8")
        git(self.b, "add", "--", "ideia.md")
        git(self.b, "commit", "-m", "remota")
        git(self.b, "push", "origin", "main")
        original_head = git(self.a, "rev-parse", "HEAD")
        original_bytes = (self.a / "ideia.md").read_bytes()

        class LockedRefService(GitService):
            def fast_forward(inner_self):
                lock = self.a / ".git" / "refs" / "heads" / "main.lock"
                lock.write_text("ocupado", encoding="utf-8")
                try:
                    return super().fast_forward()
                finally:
                    lock.unlink(missing_ok=True)

        service = LockedRefService(self.a, destination_validator=lambda: True)
        coordinator = SyncCoordinator(self.a, auto_sync=False, service=service)
        try:
            coordinator.request_sync()
            self.assertTrue(coordinator.wait_for_idle())
            self.assertEqual(coordinator.state, STATE_CONFLICT)
            self.assertEqual(git(self.a, "rev-parse", "HEAD"), original_head)
            self.assertEqual((self.a / "ideia.md").read_bytes(), original_bytes)
            self.assertEqual(git(self.a, "status", "--porcelain"), "")
            self.assertEqual(git(self.a, "diff", "--cached", "--name-only"), "")
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

    def test_failed_commit_unstages_only_app_paths_and_preserves_external_staging(self) -> None:
        (self.a / "externo.txt").write_text("externo\n", encoding="utf-8")
        (self.a / "ideia.md").write_text("local preservado\n", encoding="utf-8")

        class FailingCommitService(GitService):
            def commit(inner_self, message):
                git(self.a, "add", "--", "externo.txt")
                return CommandResult(1, "", "falha simulada")

        service = FailingCommitService(self.a, destination_validator=lambda: True)
        coordinator = SyncCoordinator(self.a, auto_sync=False, service=service)
        try:
            coordinator.request_save(("ideia.md",), "ideia: falhar")
            self.assertTrue(coordinator.wait_for_idle())
            self.assertEqual(coordinator.state, STATE_ERROR)
            self.assertEqual(
                set(git(self.a, "diff", "--cached", "--name-only").splitlines()),
                {"externo.txt"},
            )
            self.assertEqual((self.a / "ideia.md").read_text(encoding="utf-8"), "local preservado\n")
            self.assertIn("ideia.md", git(self.a, "diff", "--name-only").splitlines())
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
        self.assertEqual(options["env"]["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(options["env"]["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(options["env"]["GIT_CONFIG_COUNT"], "0")

    @patch("biblioteca_ideias.git_sync.subprocess.run")
    def test_network_command_pins_credential_and_ssh_helpers(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
        service = GitService(Path("/tmp"))
        service.git = "/usr/bin/git"
        approved = "ssh://git@github.com/Torugo76/biblioteca-de-ideias-cofre.git"
        service._run_network(approved, ("ls-remote", approved), timeout=1)
        command = run.call_args.args[0]
        joined = "\0".join(command)
        self.assertIn("credential.helper=", command)
        self.assertIn("credential.helper=!/usr/bin/gh auth git-credential", command)
        self.assertIn("core.sshCommand=/usr/bin/ssh", command)
        self.assertIn(f"url.{approved}.insteadOf={approved}", joined)

    def test_automatic_commit_uses_fixed_noreply_identity_without_global_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            git(repo, "init", "--initial-branch=main")
            (repo / "ideia.md").write_text("ideia\n", encoding="utf-8")
            service = GitService(repo)
            self.assertEqual(service.add_exact(("ideia.md",)).returncode, 0)
            self.assertEqual(service.commit("ideia: salvar").returncode, 0)
            identity = git(repo, "show", "-s", "--format=%an%x00%ae", "HEAD").split("\0")
            self.assertEqual(identity, ["Biblioteca de Ideias", "Torugo76@users.noreply.github.com"])

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

    @patch("biblioteca_ideias.git_sync.validate_github_repository_metadata", return_value=True)
    def test_destination_validation_rejects_malicious_swap_after_valid_session(self, metadata) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            git(repo, "init", "--initial-branch=main")
            git(repo, "remote", "add", "origin", "https://github.com/Torugo76/biblioteca-de-ideias-cofre.git")
            service = GitService(repo)
            self.assertTrue(service.validate_destination())
            self.assertEqual(metadata.call_count, 1)
            git(repo, "remote", "set-url", "origin", "https://github.com/atacante/cofre.git")
            result = service.fetch()
            self.assertEqual(result.returncode, 125)
            self.assertEqual(metadata.call_count, 1)

    @patch("biblioteca_ideias.git_sync.validate_github_repository_metadata", return_value=True)
    def test_official_fetch_with_malicious_pushurl_is_rejected_without_push(self, _metadata) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            false_remote = root / "false.git"
            git(root, "init", "--initial-branch=main", str(repo))
            git(root, "init", "--bare", "--initial-branch=main", str(false_remote))
            git(repo, "remote", "add", "origin", "https://github.com/Torugo76/biblioteca-de-ideias-cofre.git")
            git(repo, "remote", "set-url", "--add", "--push", "origin", false_remote.as_uri())
            service = GitService(repo)
            self.assertFalse(service.validate_destination())
            self.assertEqual(service.push().returncode, 125)
            self.assertEqual(list((false_remote / "refs" / "heads").iterdir()), [])

    @patch("biblioteca_ideias.git_sync.validate_github_repository_metadata", return_value=True)
    def test_multiple_pushurls_are_rejected_even_with_one_official(self, _metadata) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            git(repo, "init", "--initial-branch=main")
            git(repo, "remote", "add", "origin", "https://github.com/Torugo76/biblioteca-de-ideias-cofre.git")
            git(repo, "remote", "set-url", "--add", "--push", "origin", "git@github.com:Torugo76/biblioteca-de-ideias-cofre.git")
            git(repo, "remote", "set-url", "--add", "--push", "origin", "https://github.com/atacante/cofre.git")
            self.assertFalse(GitService(repo).validate_destination())

    @patch("biblioteca_ideias.git_sync.validate_github_repository_metadata", return_value=True)
    def test_single_official_pushurl_accepts_https_or_ssh_and_fetch_fallback(self, _metadata) -> None:
        push_variants = (
            None,
            "https://github.com/Torugo76/biblioteca-de-ideias-cofre.git",
            "git@github.com:Torugo76/biblioteca-de-ideias-cofre.git",
        )
        for push_url in push_variants:
            with self.subTest(push_url=push_url), tempfile.TemporaryDirectory() as temporary:
                repo = Path(temporary)
                git(repo, "init", "--initial-branch=main")
                git(repo, "remote", "add", "origin", "https://github.com/Torugo76/biblioteca-de-ideias-cofre.git")
                if push_url is not None:
                    git(repo, "remote", "set-url", "--add", "--push", "origin", push_url)
                self.assertTrue(GitService(repo).validate_destination())

    @patch("biblioteca_ideias.git_sync.validate_github_repository_metadata", return_value=True)
    def test_pushurl_swap_after_validation_is_rechecked_before_push(self, metadata) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            git(repo, "init", "--initial-branch=main")
            git(repo, "remote", "add", "origin", "https://github.com/Torugo76/biblioteca-de-ideias-cofre.git")
            service = GitService(repo)
            self.assertTrue(service.validate_destination())
            git(repo, "remote", "set-url", "--add", "--push", "origin", "https://github.com/atacante/cofre.git")
            self.assertEqual(service.push().returncode, 125)
            self.assertEqual(metadata.call_count, 1)

    def test_push_uses_immutable_validated_url_if_origin_changes_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approved = root / "approved.git"
            false_remote = root / "false.git"
            seed = root / "seed"
            repo = root / "repo"
            git(root, "init", "--bare", "--initial-branch=main", str(approved))
            git(root, "init", "--bare", "--initial-branch=main", str(false_remote))
            git(root, "clone", str(approved), str(seed))
            git(seed, "config", "user.name", "Teste")
            git(seed, "config", "user.email", "teste@example.invalid")
            (seed / "ideia.md").write_text("segura\n", encoding="utf-8")
            git(seed, "add", "--", "ideia.md")
            git(seed, "commit", "-m", "segura")
            git(seed, "push", "origin", "main")
            git(root, "clone", "--branch", "main", str(approved), str(repo))
            git(repo, "config", "user.name", "Teste")
            git(repo, "config", "user.email", "teste@example.invalid")
            (repo / "ideia.md").write_text("nova\n", encoding="utf-8")
            git(repo, "add", "--", "ideia.md")
            git(repo, "commit", "-m", "segura")

            class SwappingPushService(GitService):
                swapped = False
                mutation_returncode = None

                def run(inner_self, args, timeout=None):
                    if "push" in args and not inner_self.swapped:
                        inner_self.swapped = True
                        mutation = subprocess.run(
                            ["git", "remote", "set-url", "--add", "--push", "origin", false_remote.as_uri()],
                            cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            check=False, shell=False,
                        )
                        inner_self.mutation_returncode = mutation.returncode
                    return super().run(args, timeout)

            service = SwappingPushService(repo, destination_validator=lambda: True)
            result = service.push()
            self.assertEqual(result.returncode, 0)
            self.assertTrue(service.swapped)
            self.assertNotEqual(service.mutation_returncode, 0)
            self.assertEqual(
                git(approved, "rev-parse", "refs/heads/main"),
                git(repo, "rev-parse", "HEAD"),
            )
            false_ref = subprocess.run(
                ["git", "rev-parse", "--verify", "refs/heads/main"],
                cwd=false_remote, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, shell=False,
            )
            self.assertNotEqual(false_ref.returncode, 0)

    def test_fetch_uses_immutable_validated_url_if_origin_changes_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approved = root / "approved.git"
            false_remote = root / "false.git"
            seed = root / "seed"
            repo = root / "repo"
            git(root, "init", "--bare", "--initial-branch=main", str(approved))
            git(root, "init", "--bare", "--initial-branch=main", str(false_remote))
            git(root, "clone", str(approved), str(seed))
            git(seed, "config", "user.name", "Teste")
            git(seed, "config", "user.email", "teste@example.invalid")
            (seed / "remota.md").write_text("oficial\n", encoding="utf-8")
            git(seed, "add", "--", "remota.md")
            git(seed, "commit", "-m", "oficial")
            git(seed, "push", "origin", "main")
            git(root, "clone", "--branch", "main", str(approved), str(repo))
            approved_head = git(approved, "rev-parse", "refs/heads/main")

            class SwappingFetchService(GitService):
                swapped = False
                mutation_returncode = None

                def run(inner_self, args, timeout=None):
                    if "fetch" in args and not inner_self.swapped:
                        inner_self.swapped = True
                        mutation = subprocess.run(
                            ["git", "remote", "set-url", "origin", false_remote.as_uri()],
                            cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            check=False, shell=False,
                        )
                        inner_self.mutation_returncode = mutation.returncode
                    return super().run(args, timeout)

            service = SwappingFetchService(repo, destination_validator=lambda: True)
            result = service.fetch()
            self.assertEqual(result.returncode, 0)
            self.assertTrue(service.swapped)
            self.assertNotEqual(service.mutation_returncode, 0)
            self.assertEqual(git(repo, "rev-parse", "origin/main"), approved_head)

    def test_local_url_rewrites_and_includes_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approved = root / "approved.git"
            false_remote = root / "false.git"
            repo = root / "repo"
            git(root, "init", "--bare", "--initial-branch=main", str(approved))
            git(root, "init", "--bare", "--initial-branch=main", str(false_remote))
            git(root, "clone", str(approved), str(repo))
            approved_url = approved.as_uri()
            git(repo, "config", f"url.{false_remote.as_uri()}.insteadOf", approved_url)
            service = GitService(repo, destination_validator=lambda: True)
            result = service.fetch()
            self.assertEqual(result.returncode, 125)
            self.assertIn("redirecionamento", result.stderr)

            git(repo, "config", "--unset-all", f"url.{false_remote.as_uri()}.insteadOf")
            include_file = root / "included.conf"
            include_file.write_text(
                f"[url \"{false_remote.as_uri()}\"]\n\tinsteadOf = {approved_url}\n",
                encoding="utf-8",
            )
            git(repo, "config", "include.path", str(include_file))
            included = service.fetch()
            self.assertEqual(included.returncode, 125)
            self.assertIn("redirecionamento", included.stderr)

    def test_global_and_system_url_rewrites_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approved = root / "approved.git"
            false_remote = root / "false.git"
            repo = root / "repo"
            seed = root / "seed"
            git(root, "init", "--bare", "--initial-branch=main", str(approved))
            git(root, "init", "--bare", "--initial-branch=main", str(false_remote))
            git(root, "clone", str(approved), str(seed))
            git(seed, "config", "user.name", "Teste")
            git(seed, "config", "user.email", "teste@example.invalid")
            (seed / "segura.md").write_text("segura\n", encoding="utf-8")
            git(seed, "add", "--", "segura.md")
            git(seed, "commit", "-m", "segura")
            git(seed, "push", "origin", "main")
            git(root, "clone", "--branch", "main", str(approved), str(repo))
            malicious_config = root / "malicious.conf"
            malicious_config.write_text(
                f"[url \"{false_remote.as_uri()}\"]\n"
                f"\tinsteadOf = {approved.as_uri()}\n"
                f"\tpushInsteadOf = {approved.as_uri()}\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"GIT_CONFIG_GLOBAL": str(malicious_config), "GIT_CONFIG_SYSTEM": str(malicious_config)},
            ):
                service = GitService(repo, destination_validator=lambda: True)
                self.assertEqual(service.fetch().returncode, 0)
                git(repo, "config", "user.name", "Teste")
                git(repo, "config", "user.email", "teste@example.invalid")
                (repo / "local.md").write_text("local\n", encoding="utf-8")
                git(repo, "add", "--", "local.md")
                git(repo, "commit", "-m", "local")
                self.assertEqual(service.push().returncode, 0)
            self.assertEqual(
                git(repo, "rev-parse", "origin/main"),
                git(approved, "rev-parse", "refs/heads/main"),
            )
            self.assertEqual(git(repo, "rev-parse", "HEAD"), git(approved, "rev-parse", "refs/heads/main"))
            false_ref = subprocess.run(
                ["git", "rev-parse", "--verify", "refs/heads/main"], cwd=false_remote,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, shell=False,
            )
            self.assertNotEqual(false_ref.returncode, 0)

    def test_environment_config_injection_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approved = root / "approved.git"
            false_remote = root / "false.git"
            seed = root / "seed"
            repo = root / "repo"
            git(root, "init", "--bare", "--initial-branch=main", str(approved))
            git(root, "init", "--bare", "--initial-branch=main", str(false_remote))
            git(root, "clone", str(approved), str(seed))
            git(seed, "config", "user.name", "Teste")
            git(seed, "config", "user.email", "teste@example.invalid")
            (seed / "ideia.md").write_text("segura\n", encoding="utf-8")
            git(seed, "add", "--", "ideia.md")
            git(seed, "commit", "-m", "segura")
            git(seed, "push", "origin", "main")
            git(root, "clone", "--branch", "main", str(approved), str(repo))
            injected = {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"url.{false_remote.as_uri()}.insteadOf",
                "GIT_CONFIG_VALUE_0": approved.as_uri(),
            }
            with patch.dict(os.environ, injected):
                service = GitService(repo, destination_validator=lambda: True)
                self.assertEqual(service.fetch().returncode, 0)
                effective = service._environment()
            self.assertEqual(effective["GIT_CONFIG_COUNT"], "0")
            self.assertNotIn("GIT_CONFIG_KEY_0", effective)
            self.assertNotIn("GIT_CONFIG_VALUE_0", effective)

    def test_dangerous_environment_is_not_inherited_or_executed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            marker = root / "executed"
            wrapper = root / "wrapper.sh"
            wrapper.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
            wrapper.chmod(0o755)
            git(root, "init", "--initial-branch=main", str(repo))
            (repo / "arquivo.txt").write_text("conteudo\n", encoding="utf-8")
            contaminated = {
                "GIT_SSH_COMMAND": str(wrapper),
                "GIT_SSH": str(wrapper),
                "GIT_ASKPASS": str(wrapper),
                "SSH_ASKPASS": str(wrapper),
                "GIT_PROXY_COMMAND": str(wrapper),
                "GIT_EXTERNAL_DIFF": str(wrapper),
                "GIT_EDITOR": str(wrapper),
                "GIT_SEQUENCE_EDITOR": str(wrapper),
                "EDITOR": str(wrapper),
                "VISUAL": str(wrapper),
                "LD_PRELOAD": str(wrapper),
                "LD_LIBRARY_PATH": str(root),
                "PYTHONPATH": str(root),
                "BASH_ENV": str(wrapper),
                "ENV": str(wrapper),
                "http_proxy": f"file://{wrapper}",
                "https_proxy": f"file://{wrapper}",
            }
            with patch.dict(os.environ, contaminated):
                service = GitService(repo)
                result = service.run(("diff", "--", "arquivo.txt"))
                environment = service._environment()
            self.assertEqual(result.returncode, 0)
            self.assertFalse(marker.exists())
            for key in contaminated:
                self.assertNotIn(key, environment)
            self.assertIn("HOME", environment)

    def test_local_executable_helpers_are_rejected_without_running(self) -> None:
        unsafe_entries = (
            ("core.sshCommand", "wrapper"),
            ("core.gitProxy", "wrapper"),
            ("credential.helper", "wrapper"),
            ("filter.perigoso.clean", "wrapper"),
            ("filter.perigoso.smudge", "wrapper"),
            ("filter.perigoso.process", "wrapper"),
            ("diff.external", "wrapper"),
            ("diff.perigoso.command", "wrapper"),
            ("http.https://github.com.proxy", "http://127.0.0.1:9"),
            ("remote.origin.proxy", "http://127.0.0.1:9"),
        )
        for key, value in unsafe_entries:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                approved = root / "approved.git"
                repo = root / "repo"
                marker = root / "executed"
                wrapper = root / "wrapper"
                wrapper.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
                wrapper.chmod(0o755)
                git(root, "init", "--bare", "--initial-branch=main", str(approved))
                git(root, "init", "--initial-branch=main", str(repo))
                git(repo, "remote", "add", "origin", approved.as_uri())
                git(repo, "config", key, str(wrapper) if value == "wrapper" else value)
                service = GitService(repo, destination_validator=lambda: True)
                result = service.fetch()
                self.assertEqual(result.returncode, 125)
                self.assertFalse(marker.exists())

    def test_concurrent_pushinstead_rewrite_is_blocked_by_config_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approved = root / "approved.git"
            false_remote = root / "false.git"
            repo = root / "repo"
            git(root, "init", "--bare", "--initial-branch=main", str(approved))
            git(root, "init", "--bare", "--initial-branch=main", str(false_remote))
            git(root, "clone", str(approved), str(repo))
            git(repo, "config", "user.name", "Teste")
            git(repo, "config", "user.email", "teste@example.invalid")
            (repo / "ideia.md").write_text("segura\n", encoding="utf-8")
            git(repo, "add", "--", "ideia.md")
            git(repo, "commit", "-m", "segura")

            class RacingRewriteService(GitService):
                attempted = False
                mutation_returncode = None

                def run(inner_self, args, timeout=None):
                    if "push" in args and not inner_self.attempted:
                        inner_self.attempted = True
                        mutation = subprocess.run(
                            [
                                "git", "config",
                                f"url.{false_remote.as_uri()}.pushInsteadOf",
                                approved.as_uri(),
                            ],
                            cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            check=False, shell=False,
                        )
                        inner_self.mutation_returncode = mutation.returncode
                    return super().run(args, timeout)

            service = RacingRewriteService(repo, destination_validator=lambda: True)
            self.assertEqual(service.push().returncode, 0)
            self.assertTrue(service.attempted)
            self.assertNotEqual(service.mutation_returncode, 0)
            self.assertEqual(git(repo, "rev-parse", "HEAD"), git(approved, "rev-parse", "refs/heads/main"))
            false_ref = subprocess.run(
                ["git", "rev-parse", "--verify", "refs/heads/main"], cwd=false_remote,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, shell=False,
            )
            self.assertNotEqual(false_ref.returncode, 0)

    @patch("biblioteca_ideias.git_sync.validate_github_repository_metadata", return_value=True)
    def test_validated_snapshot_contains_only_approved_urls(self, _metadata) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            git(repo, "init", "--initial-branch=main")
            official = "https://github.com/Torugo76/biblioteca-de-ideias-cofre.git"
            git(repo, "remote", "add", "origin", official)
            destination = GitService(repo).validate_destination()
            self.assertIsNotNone(destination)
            assert destination is not None
            self.assertEqual(destination.fetch_url, official)
            self.assertEqual(destination.push_url, official)
            self.assertNotIn("@github.com", destination.fetch_url)
            self.assertNotIn("@github.com", destination.push_url)

    @patch("biblioteca_ideias.git_sync.validate_github_repository_metadata", return_value=True)
    def test_destination_validation_rejects_userinfo_and_non_main_branch(self, _metadata) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            git(repo, "init", "--initial-branch=outra")
            git(repo, "remote", "add", "origin", "https://usuario@github.com/Torugo76/biblioteca-de-ideias-cofre.git")
            self.assertFalse(GitService(repo).validate_destination())

    @patch("biblioteca_ideias.git_sync.shutil.which", return_value="/usr/bin/gh")
    @patch("biblioteca_ideias.git_sync.subprocess.run")
    def test_public_or_wrong_github_repository_metadata_is_rejected(self, run, _which) -> None:
        for metadata in (
            b'{"nameWithOwner":"Torugo76/biblioteca-de-ideias-cofre","visibility":"PUBLIC"}',
            b'{"nameWithOwner":"outro/cofre","visibility":"PRIVATE"}',
        ):
            with self.subTest(metadata=metadata):
                run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=metadata, stderr=b""
                )
                self.assertFalse(validate_github_repository_metadata())


if __name__ == "__main__":
    unittest.main()
