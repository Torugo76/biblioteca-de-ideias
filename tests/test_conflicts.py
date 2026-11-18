from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from biblioteca_ideias.conflicts import ConflictError, ConflictManager
from biblioteca_ideias.git_sync import CommandResult, GitService
from biblioteca_ideias.storage import MAX_FILE_SIZE, MarkdownStore


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", *args], cwd=repo, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return result.stdout.strip()


class LocalService(GitService):
    def __init__(self, repo: Path, remote: Path):
        super().__init__(repo, destination_validator=lambda: True)
        self.remote = remote

    def fetch(self) -> CommandResult:
        return self.run(("fetch", "--no-tags", str(self.remote),
                         "+refs/heads/main:refs/remotes/origin/main"))

    def push(self) -> CommandResult:
        return self.run(("push", str(self.remote), "HEAD:refs/heads/main"))


class ConflictResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.bare = root / "vault.git"
        self.seed = root / "seed"
        self.local = root / "local"
        self.other = root / "other"
        self.state = root / "state"
        git(root, "init", "--bare", str(self.bare))
        self.seed.mkdir()
        git(self.seed, "init", "-b", "main")
        git(self.seed, "config", "user.name", "Teste")
        git(self.seed, "config", "user.email", "teste@example.invalid")
        store = MarkdownStore(self.seed, state_dir=root / "seed-state")
        idea = store.create("Ideia principal", "base\n")
        self.idea_id = idea.idea_id
        git(self.seed, "add", ".")
        git(self.seed, "commit", "-m", "base")
        git(self.seed, "push", str(self.bare), "main")
        git(root, "clone", "-b", "main", str(self.bare), str(self.local))
        git(root, "clone", "-b", "main", str(self.bare), str(self.other))
        for repo in (self.local, self.other):
            git(repo, "config", "user.name", "Teste")
            git(repo, "config", "user.email", "teste@example.invalid")
        self.service = LocalService(self.local, self.bare)
        self.manager = ConflictManager(self.local, self.state, self.service)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def diverge(self, *, local_body: str = "local\n", remote_body: str = "remoto\n",
                local_title: str = "Ideia principal", remote_title: str = "Ideia principal",
                local_status: str = "entrada", remote_status: str = "entrada") -> tuple[str, str]:
        for repo, body, title, status in (
            (self.local, local_body, local_title, local_status),
            (self.other, remote_body, remote_title, remote_status),
        ):
            store = MarkdownStore(repo, state_dir=repo.parent / f"{repo.name}-state")
            idea = {entry.idea_id: entry for entry in store.list_ideas()}[self.idea_id]
            store.save_idea(self.idea_id, title, body, status, idea.content_hash)
            git(repo, "add", ".")
            git(repo, "commit", "-m", body.strip())
        git(self.other, "push", str(self.bare), "main")
        self.service.fetch()
        return git(self.local, "rev-parse", "HEAD"), git(self.local, "rev-parse", "origin/main")

    def test_prepare_uses_uuid_across_rename_move_and_is_private(self) -> None:
        local, remote = self.diverge(
            local_title="Renomeada", remote_title="Ideia principal",
            local_status="planejada", remote_status="em-andamento",
        )
        before = git(self.local, "status", "--porcelain")
        conflict_id = self.manager.prepare(local, remote)
        self.assertEqual(before, git(self.local, "status", "--porcelain"))
        pending = self.manager.list_pending()
        self.assertEqual([self.idea_id], [entry["id"] for entry in pending])
        self.assertEqual(conflict_id, self.manager.prepare(local, remote))
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        for path in (self.state / conflict_id).rglob("*"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        self.assertEqual(git(self.local, "rev-parse", f"refs/recovery/conflict-{conflict_id}-local"), local)

    def test_all_explicit_resolution_actions(self) -> None:
        for action in ("local", "remote", "both", "combined"):
            with self.subTest(action=action):
                if action != "local":
                    self.tearDown()
                    self.setUp()
                local, remote = self.diverge()
                conflict_id = self.manager.prepare(local, remote)
                kwargs = ({"title": "Combinada", "body": "texto combinado\n", "status": "avaliando"}
                          if action == "combined" else {})
                self.manager.choose(conflict_id, self.idea_id, action, **kwargs)
                self.assertEqual(git(self.local, "rev-parse", "HEAD"), git(self.bare, "rev-parse", "main"))
                ideas = MarkdownStore(self.local, state_dir=self.local.parent / "read-state").list_ideas()
                if action == "both":
                    self.assertEqual(len(ideas), 2)
                    self.assertEqual(len({idea.idea_id for idea in ideas}), 2)
                else:
                    self.assertEqual(len(ideas), 1)
                    expected = {"local": "local", "remote": "remoto", "combined": "texto combinado"}[action]
                    self.assertIn(expected, ideas[0].body)
                self.assertEqual(self.manager.list_pending(), [])

    def test_remote_change_during_resolution_preserves_local_checkout(self) -> None:
        local, remote = self.diverge()
        conflict_id = self.manager.prepare(local, remote)
        # Terceiro avanço remoto depois da análise.
        git(self.other, "pull", "--ff-only", str(self.bare), "main")
        store = MarkdownStore(self.other, state_dir=self.other.parent / "late-state")
        store.create("Outra ideia", "mudança tardia\n")
        git(self.other, "add", ".")
        git(self.other, "commit", "-m", "mudança tardia")
        git(self.other, "push", str(self.bare), "main")
        with self.assertRaises(ConflictError):
            self.manager.choose(conflict_id, self.idea_id, "local")
        self.assertEqual(git(self.local, "rev-parse", "HEAD"), local)
        self.assertTrue(self.manager.list_pending())

    def test_invalid_yaml_and_duplicate_uuid_are_integrity_errors(self) -> None:
        local, remote = self.diverge()
        path = next(self.other.glob("*/*.md"))
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("titulo:", "titulo: ["), encoding="utf-8")
        git(self.other, "add", ".")
        git(self.other, "commit", "-m", "yaml inválido")
        with self.assertRaises(ConflictError):
            self.manager._snapshot(git(self.other, "rev-parse", "HEAD"))
        git(self.other, "checkout", "HEAD^", "--", str(path.relative_to(self.other)))
        duplicate = self.other / "avaliando" / "duplicada.md"
        duplicate.parent.mkdir(exist_ok=True)
        duplicate.write_bytes(path.read_bytes())
        git(self.other, "add", ".")
        git(self.other, "commit", "-m", "uuid duplicado")
        with self.assertRaises(ConflictError):
            self.manager._snapshot(git(self.other, "rev-parse", "HEAD"))

    def test_combined_size_boundary_and_oversize_leave_no_result_artifacts(self) -> None:
        local, remote = self.diverge()
        conflict_id = self.manager.prepare(local, remote)
        version = self.manager.version(conflict_id, self.idea_id, "local")
        directory = self.state / conflict_id
        manifest_before = (directory / "manifest.json").read_bytes()
        result_ref = f"refs/recovery/conflict-{conflict_id}-result"
        head_before = git(self.local, "rev-parse", "HEAD")
        remote_before = git(self.bare, "rev-parse", "main")

        source = self.manager._load_snapshot_idea(
            directory, self.idea_id, "local", self.manager._manifest(conflict_id)[1]
        )
        assert source is not None
        now = source.updated_at
        empty = MarkdownStore._serialize(self.idea_id, "Limite", "", source.created_at, now)
        boundary_body = "x" * (MAX_FILE_SIZE - len(empty))
        boundary = MarkdownStore._serialize(
            self.idea_id, "Limite", boundary_body, source.created_at, now
        )
        self.assertEqual(len(boundary), MAX_FILE_SIZE)
        self.manager._validate_result_bytes(boundary, "entrada/Limite.md")

        with self.assertRaises(ConflictError):
            self.manager.choose(
                conflict_id, self.idea_id, "combined", title="Grande",
                body="x" * MAX_FILE_SIZE, status="entrada",
            )
        self.assertEqual((directory / "manifest.json").read_bytes(), manifest_before)
        self.assertEqual(git(self.local, "rev-parse", "HEAD"), head_before)
        self.assertEqual(git(self.bare, "rev-parse", "main"), remote_before)
        self.assertFalse((directory / "resolution-files").exists())
        missing = subprocess.run(
            ["/usr/bin/git", "rev-parse", "--verify", result_ref], cwd=self.local,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertTrue(version["titulo"])

    def test_each_pre_apply_change_aborts_without_applying_or_pushing(self) -> None:
        mutations = ("untracked", "staged", "edited", "head", "remote-config")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                if mutation != mutations[0]:
                    self.tearDown()
                    self.setUp()
                local, remote = self.diverge()
                conflict_id = self.manager.prepare(local, remote)
                remote_main = git(self.bare, "rev-parse", "main")

                def inject(stage: str) -> None:
                    if stage != "pre_apply":
                        return
                    if mutation == "untracked":
                        (self.local / "concorrente.txt").write_text("novo", encoding="utf-8")
                    elif mutation == "staged":
                        (self.local / "staged.txt").write_text("staged", encoding="utf-8")
                        git(self.local, "add", "staged.txt")
                    elif mutation == "edited":
                        path = next(self.local.glob("*/*.md"))
                        path.write_text(path.read_text(encoding="utf-8") + "editado", encoding="utf-8")
                    elif mutation == "head":
                        git(self.local, "update-ref", "refs/heads/main", remote)
                    else:
                        git(self.local, "remote", "set-url", "origin", str(self.local.parent / "falso.git"))

                self.manager._reconcile_hook = inject  # type: ignore[method-assign]
                with self.assertRaises(ConflictError):
                    self.manager.choose(conflict_id, self.idea_id, "local")
                expected_head = remote if mutation == "head" else local
                self.assertEqual(git(self.local, "rev-parse", "HEAD"), expected_head)
                self.assertEqual(git(self.bare, "rev-parse", "main"), remote_main)
                self.assertTrue(self.manager.list_pending())

    def test_markdown_outside_contract_is_never_ignored(self) -> None:
        invalid_paths = ("raiz.md", "entrada/nivel/ideia.md", "desconhecida/ideia.md")
        for invalid_path in invalid_paths:
            with self.subTest(path=invalid_path):
                if invalid_path != invalid_paths[0]:
                    self.tearDown()
                    self.setUp()
                local, remote = self.diverge()
                path = self.local / invalid_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("conteúdo que não pode sumir\n", encoding="utf-8")
                git(self.local, "add", invalid_path)
                git(self.local, "commit", "-m", "markdown fora do contrato")
                invalid_commit = git(self.local, "rev-parse", "HEAD")
                with self.assertRaises(ConflictError):
                    self.manager.prepare(invalid_commit, remote)
                self.assertEqual(git(self.local, "rev-parse", "HEAD"), invalid_commit)
                self.assertEqual(path.read_text(encoding="utf-8"), "conteúdo que não pode sumir\n")
                self.assertFalse(any("-result" in ref for ref in git(
                    self.local, "for-each-ref", "--format=%(refname)", "refs/recovery"
                ).splitlines()))

    def test_retry_reuses_exact_built_result_and_recognizes_completed_push(self) -> None:
        local, remote = self.diverge()
        conflict_id = self.manager.prepare(local, remote)
        stopped = {"done": False}

        def crash_after_build(stage: str) -> None:
            if stage == "pre_apply" and not stopped["done"]:
                stopped["done"] = True
                raise ConflictError("queda simulada")

        self.manager._reconcile_hook = crash_after_build  # type: ignore[method-assign]
        with self.assertRaises(ConflictError):
            self.manager.choose(conflict_id, self.idea_id, "both")
        directory, manifest = self.manager._manifest(conflict_id)
        first_result = str(manifest["result"])
        first_copy_id = manifest["choices"][self.idea_id]["copy_id"]
        self.manager._reconcile_hook = lambda _stage: None  # type: ignore[method-assign]
        self.manager.choose(conflict_id, self.idea_id, "both")
        _, completed = self.manager._manifest(conflict_id)
        self.assertEqual(completed["result"], first_result)
        self.assertEqual(completed["choices"][self.idea_id]["copy_id"], first_copy_id)

        # Simula queda após push, antes de persistir status resolved, e tracking já atualizado.
        completed["status"] = "pending"
        self.manager._write_manifest(directory, completed)
        git(self.local, "update-ref", "refs/remotes/origin/main", first_result)
        self.manager._push_locked = lambda _destination: self.fail("push duplicado")  # type: ignore[method-assign]
        self.manager._reconcile(directory, completed)
        self.assertEqual(self.manager._manifest(conflict_id)[1]["status"], "resolved")

        # Tracking antigo: o fetch reconhece que o remoto já contém o resultado.
        _, completed = self.manager._manifest(conflict_id)
        completed["status"] = "pending"
        self.manager._write_manifest(directory, completed)
        git(self.local, "update-ref", "refs/remotes/origin/main", remote)
        self.manager._reconcile(directory, completed)
        self.assertEqual(git(self.local, "rev-parse", "origin/main"), first_result)
        self.assertEqual(self.manager._manifest(conflict_id)[1]["status"], "resolved")

    def test_result_ref_failure_aborts_before_manifest_apply_or_push(self) -> None:
        local, remote = self.diverge()
        conflict_id = self.manager.prepare(local, remote)
        original_run = self.service.run

        def fail_result_ref(args, timeout=None):
            values = tuple(args)
            if values[:1] == ("update-ref",) and values[1].endswith("-result"):
                return CommandResult(1, "", "falha simulada")
            return original_run(values, timeout)

        self.service.run = fail_result_ref  # type: ignore[method-assign]
        with self.assertRaises(ConflictError):
            self.manager.choose(conflict_id, self.idea_id, "local")
        _, manifest = self.manager._manifest(conflict_id)
        self.assertNotIn("result", manifest)
        self.assertEqual(git(self.local, "rev-parse", "HEAD"), local)
        self.assertEqual(git(self.bare, "rev-parse", "main"), remote)


if __name__ == "__main__":
    unittest.main()
