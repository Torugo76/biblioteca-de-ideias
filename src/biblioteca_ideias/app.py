from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QStandardPaths, QUrl
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine

from .backend import APP_ID, Backend
from .storage import StorageError


def main() -> int:
    QCoreApplication.setOrganizationName("Torugo")
    QCoreApplication.setApplicationName("Biblioteca de Ideias")
    QCoreApplication.setApplicationVersion("0.4.0")
    QGuiApplication.setDesktopFileName(APP_ID)

    app = QGuiApplication(sys.argv)
    icon_search_paths = QIcon.themeSearchPaths()
    for data_path in QStandardPaths.standardLocations(QStandardPaths.GenericDataLocation):
        candidate = os.fspath(Path(data_path) / "icons")
        if candidate not in icon_search_paths:
            icon_search_paths.append(candidate)
    QIcon.setThemeSearchPaths(icon_search_paths)
    QIcon.setFallbackThemeName("hicolor")
    icon = Path(__file__).resolve().parents[2] / "assets" / f"{APP_ID}.svg"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    else:
        app.setWindowIcon(QIcon.fromTheme(APP_ID))

    try:
        backend = Backend()
    except StorageError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2
    app.aboutToQuit.connect(backend.close)

    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    qml = Path(__file__).with_name("qml") / "Main.qml"
    engine.load(QUrl.fromLocalFile(os.fspath(qml)))
    if not engine.rootObjects():
        return 1
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

