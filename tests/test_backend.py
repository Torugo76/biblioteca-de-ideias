import tempfile
import unittest
from pathlib import Path

from biblioteca_ideias.backend import Backend


class BackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.backend = Backend(self.root / "cofre")

    def tearDown(self) -> None:
        self.backend._lock.unlock()
        self.temporary.cleanup()

    def test_update_returns_false_and_preserves_status_on_failure(self) -> None:
        idea = self.backend._store.create("Original", "Resumo")
        self.backend.reload()

        saved = self.backend.updateIdea(
            idea.idea_id, "entrada", "Alterada", "Outro", "hash-incorreto"
        )

        self.assertFalse(saved)
        persisted = self.backend._store.list_ideas()[0]
        self.assertEqual(persisted.title, "Original")
        self.assertEqual(persisted.status, "entrada")
        self.assertIn("alterada por outro programa", self.backend.notice)

    def test_update_returns_true_on_success(self) -> None:
        idea = self.backend._store.create("Original", "Resumo")

        saved = self.backend.updateIdea(
            idea.idea_id, "entrada", "Alterada", "Outro", idea.content_hash
        )

        self.assertTrue(saved)
        self.assertEqual(self.backend._store.list_ideas()[0].title, "Alterada")

    def test_preview_escapes_html_and_removes_remote_images(self) -> None:
        preview = self.backend.safePreviewMarkdown(
            "<script>alert(1)</script>\n![externa](  https://example.invalid/a.png  )\n[link](https://example.invalid)"
        )

        self.assertIn("&lt;script&gt;", preview)
        self.assertNotIn("![", preview)
        self.assertIn("[imagem: externa]", preview)
        self.assertIn("[link](https://example.invalid)", preview)

    def test_preview_removes_reference_images_and_their_definitions(self) -> None:
        preview = self.backend.safePreviewMarkdown(
            "![Capa][  Imagem   Principal ]\n"
            "![Miniatura][]\n"
            "![Atalho]\n"
            "[imagem principal]: https://cdn.example.invalid/capa.png\n"
            "[MINIATURA]: <https://cdn.example.invalid/miniatura.png>\n"
            "[atalho]: https://cdn.example.invalid/atalho.png\n"
            "[documentação](https://example.invalid/docs)\n"
        )

        self.assertNotIn("![", preview)
        self.assertNotIn("cdn.example.invalid", preview)
        self.assertIn("[imagem: Capa]", preview)
        self.assertIn("[imagem: Miniatura]", preview)
        self.assertIn("[imagem: Atalho]", preview)
        self.assertIn("[documentação](https://example.invalid/docs)", preview)


if __name__ == "__main__":
    unittest.main()
