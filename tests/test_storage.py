import tempfile
import unittest
from pathlib import Path

from biblioteca_ideias.storage import (
    COLUMNS,
    MAX_FILE_SIZE,
    ExternalChangeError,
    MarkdownStore,
    StorageError,
)


class MarkdownStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_create_list_update_and_move(self) -> None:
        store = MarkdownStore(self.root / "cofre")
        created = store.create("Minha ideia", "Um resumo")
        self.assertEqual(created.status, "entrada")
        self.assertEqual(created.title, "Minha ideia")
        self.assertTrue((self.root / "cofre" / "entrada" / f"{created.idea_id}.md").exists())

        updated = store.update(
            created.idea_id, "entrada", "Ideia melhor", "Outro resumo", created.content_hash
        )
        self.assertEqual(updated.title, "Ideia melhor")

        moved = store.move(updated.idea_id, "entrada", "avaliando", updated.content_hash)
        self.assertEqual(moved.status, "avaliando")
        self.assertFalse((self.root / "cofre" / "entrada" / f"{created.idea_id}.md").exists())
        self.assertEqual(len(store.list_ideas()), 1)

    def test_external_change_is_not_overwritten(self) -> None:
        store = MarkdownStore(self.root / "cofre")
        idea = store.create("Original", "Resumo")
        path = self.root / "cofre" / "entrada" / f"{idea.idea_id}.md"
        path.write_text(path.read_text(encoding="utf-8") + "\nMudança externa\n", encoding="utf-8")

        with self.assertRaises(ExternalChangeError):
            store.update(idea.idea_id, "entrada", "Sobrescrita", "Não", idea.content_hash)
        self.assertIn("Mudança externa", path.read_text(encoding="utf-8"))

    def test_columns_and_keep_files_are_created(self) -> None:
        MarkdownStore(self.root / "cofre")
        for key, _ in COLUMNS:
            self.assertTrue((self.root / "cofre" / key / ".gitkeep").exists())

    def test_rejects_symlinked_idea(self) -> None:
        store = MarkdownStore(self.root / "cofre")
        idea = store.create("Segura", "Resumo")
        path = self.root / "cofre" / "entrada" / f"{idea.idea_id}.md"
        outside = self.root / "outside.md"
        outside.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.unlink()
        path.symlink_to(outside)

        with self.assertRaises(StorageError):
            store.list_ideas()

    def test_rejects_symlinked_vault_root(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        linked_vault = self.root / "cofre"
        linked_vault.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(StorageError):
            MarkdownStore(linked_vault)

        for key, _ in COLUMNS:
            self.assertFalse((outside / key).exists())

    def test_oversized_create_leaves_no_markdown(self) -> None:
        store = MarkdownStore(self.root / "cofre")

        with self.assertRaises(StorageError):
            store.create("Grande", "x" * MAX_FILE_SIZE)

        self.assertEqual(list((self.root / "cofre").glob("*/*.md")), [])

    def test_oversized_update_preserves_previous_file(self) -> None:
        store = MarkdownStore(self.root / "cofre")
        idea = store.create("Original", "Resumo preservado")
        path = self.root / "cofre" / "entrada" / f"{idea.idea_id}.md"
        before = path.read_bytes()

        with self.assertRaises(StorageError):
            store.update(
                idea.idea_id,
                "entrada",
                "Não deve salvar",
                "x" * MAX_FILE_SIZE,
                idea.content_hash,
            )

        self.assertEqual(path.read_bytes(), before)

    def test_full_body_and_unknown_sections_are_preserved(self) -> None:
        store = MarkdownStore(self.root / "cofre")
        body = "## Resumo\n\nTrecho curto\n\n## Pesquisa própria\n\nNão perder isto.\n"
        idea = store.create("Com seção extra", body)

        self.assertEqual(idea.body, body)
        self.assertEqual(idea.summary, "Trecho curto")
        updated = store.update(
            idea.idea_id, "entrada", "Novo título", idea.body, idea.content_hash
        )
        self.assertIn("## Pesquisa própria\n\nNão perder isto.", updated.body)

    def test_empty_body_uses_initial_template(self) -> None:
        store = MarkdownStore(self.root / "cofre")
        idea = store.create("Modelo", "")

        self.assertIn("## Resumo", idea.body)
        self.assertIn("## Problema", idea.body)
        self.assertIn("## Próximos passos", idea.body)


if __name__ == "__main__":
    unittest.main()

