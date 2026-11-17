import os
import tempfile
import unicodedata
import unittest
import uuid
from pathlib import Path

from biblioteca_ideias.storage import (
    COLUMNS,
    MAX_FILE_SIZE,
    MAX_FILENAME_BYTES,
    CollisionError,
    ExternalChangeError,
    MarkdownStore,
    StorageError,
    title_filename,
)


class MarkdownStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "cofre"
        self.state = self.root / "estado"
        self.store = MarkdownStore(self.vault, self.state)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_filename_preserves_spaces_accents_unicode_and_nfc(self) -> None:
        decomposed = "Cafe\u0301 com ideias 🚀"
        self.assertEqual(title_filename(decomposed), "Café com ideias 🚀.md")
        self.assertTrue(unicodedata.is_normalized("NFC", title_filename(decomposed)))

    def test_filename_replaces_windows_characters_and_trailing_marks(self) -> None:
        self.assertEqual(title_filename('A<B>C:D"E/F\\G|H?I* . '), "A-B-C-D-E-F-G-H-I-.md")

    def test_filename_rejects_empty_dots_and_reserved_windows_names(self) -> None:
        for title in ("", " ", ".", "..", "CON", "con.txt", "Lpt9"):
            with self.subTest(title=title), self.assertRaises(StorageError):
                title_filename(title)

    def test_filename_limit_counts_utf8_bytes_and_extension(self) -> None:
        valid = "🚀" * ((MAX_FILENAME_BYTES - len(".md")) // 4)
        self.assertLessEqual(len(title_filename(valid).encode("utf-8")), MAX_FILENAME_BYTES)
        with self.assertRaises(StorageError):
            title_filename(valid + "🚀")

    def test_create_uses_title_filename_and_global_casefold_collision(self) -> None:
        first = self.store.create("Café Único", "## Resumo\n\nPrimeira\n")
        self.assertEqual(first.path.name, "Café Único.md")
        with self.assertRaises(CollisionError):
            self.store.create("CAFÉ ÚNICO", "Outra")

    def test_global_nfc_nfd_filename_collision_is_detected(self) -> None:
        first = self.store.create("Café", "Primeira")
        second = self.store.create("Outra", "Segunda")
        nfd_name = unicodedata.normalize("NFD", first.path.name)
        os.replace(second.path, self.vault / "avaliando" / nfd_name)
        with self.assertRaises(CollisionError):
            self.store.list_ideas()

    def test_duplicate_uuid_is_detected(self) -> None:
        idea = self.store.create("Original", "Resumo")
        duplicate = self.vault / "avaliando" / "Cópia.md"
        duplicate.write_bytes(idea.path.read_bytes())
        with self.assertRaises(CollisionError):
            self.store.list_ideas()

    def test_save_renames_moves_and_updates_in_one_transaction(self) -> None:
        created = self.store.create("Título inicial", "## Resumo\n\nUm\n")
        saved = self.store.save_idea(
            created.idea_id,
            "Novo título 🚀",
            "## Resumo\n\nDois\n\n## Extra\n\nPreservar\n",
            "avaliando",
            created.content_hash,
        )
        self.assertEqual(saved.idea_id, created.idea_id)
        self.assertEqual(saved.status, "avaliando")
        self.assertEqual(saved.path.name, "Novo título 🚀.md")
        self.assertFalse(created.path.exists())
        self.assertIn("## Extra\n\nPreservar", saved.body)

    def test_external_change_is_not_overwritten_or_moved(self) -> None:
        idea = self.store.create("Original", "Resumo")
        idea.path.write_text(idea.path.read_text(encoding="utf-8") + "\nexterna\n", encoding="utf-8")
        with self.assertRaises(ExternalChangeError):
            self.store.save_idea(
                idea.idea_id, "Renomeada", "Novo", "avaliando", idea.content_hash
            )
        self.assertTrue(idea.path.exists())
        self.assertFalse((self.vault / "avaliando" / "Renomeada.md").exists())

    def test_crash_journal_recovers_original_on_next_open(self) -> None:
        idea = self.store.create("Original", "Antes")
        original = idea.path.read_bytes()

        def crash(stage: str) -> None:
            if stage == "target-written":
                raise SystemExit("queda simulada")

        self.store._failure_hook = crash
        with self.assertRaises(SystemExit):
            self.store.save_idea(
                idea.idea_id, "Renomeada", "Depois", "avaliando", idea.content_hash
            )
        recovered = MarkdownStore(self.vault, self.state)
        recovered_idea = recovered.list_ideas()[0]
        self.assertEqual(recovered_idea.path, idea.path)
        self.assertEqual(recovered_idea.path.read_bytes(), original)
        self.assertFalse((self.vault / "avaliando" / "Renomeada.md").exists())
        self.assertFalse(recovered.journal_path.exists())

    def test_journal_permissions_are_private(self) -> None:
        idea = self.store.create("Original", "Antes")

        def crash(stage: str) -> None:
            if stage == "target-written":
                raise SystemExit

        self.store._failure_hook = crash
        with self.assertRaises(SystemExit):
            self.store.save_idea(idea.idea_id, "Nova", "Depois", "entrada", idea.content_hash)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.journal_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.backup_path.stat().st_mode & 0o777, 0o600)

    def test_migration_from_uuid_filename_preserves_bytes_id_and_timestamp(self) -> None:
        idea_id = str(uuid.uuid4())
        now = self.store._now()
        source = self.vault / "entrada" / f"{idea_id}.md"
        original = self.store._serialize(idea_id, "Ideia legada", "## Resumo\n\nIntacta\n", now, now)
        source.write_bytes(original)
        wanted_mtime = 1_700_000_000_123_456_789
        os.utime(source, ns=(wanted_mtime, wanted_mtime))

        plan = self.store.plan_title_migration()
        self.assertEqual(len(plan), 1)
        self.store.apply_title_migration(plan)
        migrated = self.store.list_ideas()[0]
        self.assertEqual(migrated.idea_id, idea_id)
        self.assertEqual(migrated.path.name, "Ideia legada.md")
        self.assertEqual(migrated.path.read_bytes(), original)
        self.assertEqual(migrated.path.stat().st_mtime_ns, wanted_mtime)

    def test_oversized_create_and_save_leave_original_state(self) -> None:
        with self.assertRaises(StorageError):
            self.store.create("Grande", "x" * MAX_FILE_SIZE)
        self.assertEqual(list(self.vault.glob("*/*.md")), [])
        idea = self.store.create("Original", "Preservado")
        before = idea.path.read_bytes()
        with self.assertRaises(StorageError):
            self.store.save_idea(
                idea.idea_id, "Renomeada", "x" * MAX_FILE_SIZE, "avaliando", idea.content_hash
            )
        self.assertEqual(idea.path.read_bytes(), before)
        self.assertFalse((self.vault / "avaliando" / "Renomeada.md").exists())

    def test_columns_and_keep_files_are_created(self) -> None:
        for key, _ in COLUMNS:
            self.assertTrue((self.vault / key / ".gitkeep").exists())

    def test_rejects_symlinked_idea_and_vault_root(self) -> None:
        idea = self.store.create("Segura", "Resumo")
        outside = self.root / "outside.md"
        outside.write_bytes(idea.path.read_bytes())
        idea.path.unlink()
        idea.path.symlink_to(outside)
        with self.assertRaises(StorageError):
            self.store.list_ideas()

        other_root = self.root / "real-vault"
        other_root.mkdir()
        linked = self.root / "linked-vault"
        linked.symlink_to(other_root, target_is_directory=True)
        with self.assertRaises(StorageError):
            MarkdownStore(linked, self.root / "other-state")


if __name__ == "__main__":
    unittest.main()
