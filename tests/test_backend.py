import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, Signal

from biblioteca_ideias.backend import Backend
from biblioteca_ideias.git_sync import STATE_ERROR


class FakeCoordinator(QObject):
    stateChanged = Signal(str)
    authChanged = Signal(bool, str)
    repositoryUpdated = Signal()

    def __init__(self):
        super().__init__()
        self.save_requests = []
        self.sync_requests = 0

    def request_save(self, paths, message):
        self.save_requests.append((tuple(paths), message))

    def request_sync(self):
        self.sync_requests += 1

    def close(self):
        pass


class BackendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.backend = Backend(self.root / "cofre")

    def tearDown(self) -> None:
        self.backend.close()
        self.backend._lock.unlock()
        self.temporary.cleanup()

    def test_save_returns_false_and_preserves_status_on_failure(self) -> None:
        idea = self.backend._store.create("Original", "Resumo")
        self.backend.reload()

        saved = self.backend.saveIdea(
            idea.idea_id, "Alterada", "Outro", "avaliando", "hash-incorreto"
        )

        self.assertFalse(saved)
        persisted = self.backend._store.list_ideas()[0]
        self.assertEqual(persisted.title, "Original")
        self.assertEqual(persisted.status, "entrada")
        self.assertIn("alterada por outro programa", self.backend.notice)

    def test_save_returns_true_on_rename_and_move(self) -> None:
        idea = self.backend._store.create("Original", "Resumo")

        saved = self.backend.saveIdea(
            idea.idea_id, "Alterada", "Outro", "avaliando", idea.content_hash
        )

        self.assertTrue(saved)
        persisted = self.backend._store.list_ideas()[0]
        self.assertEqual(persisted.title, "Alterada")
        self.assertEqual(persisted.status, "avaliando")
        self.assertEqual(persisted.path.name, "Alterada.md")

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

    def test_repository_update_signal_reloads_ui_model(self) -> None:
        coordinator = FakeCoordinator()
        backend = Backend(self.root / "outro-cofre", sync_coordinator=coordinator)
        try:
            self.assertEqual(backend.ideaCount, 0)
            backend._store.create("Chegou do remoto", "Corpo")
            coordinator.repositoryUpdated.emit()
            self.application.processEvents()
            self.assertEqual(backend.ideaCount, 1)
            self.assertEqual(backend.ideas[0]["titulo"], "Chegou do remoto")
        finally:
            backend._lock.unlock()

    def test_repository_update_reload_failure_preserves_error_state(self) -> None:
        coordinator = FakeCoordinator()
        backend = Backend(self.root / "cofre-invalido", sync_coordinator=coordinator)
        try:
            idea = backend._store.create("Original", "Corpo")
            duplicate = backend._store.vault / "avaliando" / "Duplicada.md"
            duplicate.write_bytes(idea.path.read_bytes())
            coordinator.stateChanged.emit("Sincronizado")
            coordinator.repositoryUpdated.emit()
            self.application.processEvents()
            self.assertEqual(backend.syncStatus, STATE_ERROR)
            self.assertIn("UUID duplicado", backend.notice)
        finally:
            backend._lock.unlock()

    def test_disconnect_is_local_and_prevents_automatic_sync(self) -> None:
        coordinator = FakeCoordinator()
        backend = Backend(self.root / "cofre-desconectado", sync_coordinator=coordinator)
        try:
            backend.disconnectGithub()
            self.assertFalse(backend.githubEnabled)
            self.assertTrue(backend._disabled_marker.exists())
            self.assertEqual(backend._disabled_marker.stat().st_mode & 0o777, 0o600)
            self.assertIn("não foi alterado", backend.notice.casefold())
            backend.syncNow()
            self.assertEqual(coordinator.sync_requests, 0)
            self.assertIn("desativada", backend.notice.casefold())
        finally:
            backend.close()
            backend._lock.unlock()


if __name__ == "__main__":
    unittest.main()
