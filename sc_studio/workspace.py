"""Desktop integration, themes and dependency-complete clipboard transport."""

import io
import tempfile
from pathlib import Path
from zipfile import ZipFile, ZIP_STORED
from PyQt6.QtCore import QMimeData, QByteArray, Qt
from PyQt6.QtGui import QPalette, QColor
from PyQt6.QtWidgets import QApplication, QFileDialog
from . import themes
from .document import Document

CLIP_MIME = "application/x-sc-studio-resource-graph"


def apply_style(app, name):
    """Switch the application style only when it is actually different.

    QApplication.setStyle() re-polishes every live widget, and doing that
    repeatedly while widgets exist is not something Qt supports -- it has been
    observed to segfault. Re-applying the style already in use is pure work in
    any case, so skip it.
    """
    if app.style().objectName().casefold() != name.casefold():
        app.setStyle(name)


def configure_theme(app, mode, system_palette, scheme=None):
    if not hasattr(app, "_sc_system_style"):
        app._sc_system_style = app.style().objectName()
    app.setStyleSheet("")
    if mode == "system":
        apply_style(app, app._sc_system_style)
        scheme = scheme if scheme is not None else app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Unknown:
            app.setPalette(system_palette)
            return
        mode = "dark" if scheme == Qt.ColorScheme.Dark else "light"
    apply_style(app, "Fusion")
    p = app.style().standardPalette()
    scheme = themes.SCHEMES.get(mode)
    if scheme is not None:
        colors = themes.role_colors(mode)
        dark = themes.is_dark(mode)
    else:
        dark = mode == "dark"
        colors = _builtin_colors(dark)
    disabled = colors.pop("disabled_text", None) if scheme is not None else None
    for name, value in colors.items():
        for group in (
            QPalette.ColorGroup.Active,
            QPalette.ColorGroup.Inactive,
            QPalette.ColorGroup.Disabled,
        ):
            p.setColor(group, getattr(QPalette.ColorRole, name), QColor(value))
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.ButtonText,
    ):
        p.setColor(
            QPalette.ColorGroup.Disabled,
            role,
            QColor(disabled or ("#737e90" if dark else "#939aa6")),
        )
    app.setPalette(p)
    app.setStyleSheet(stylesheet())


def _builtin_colors(dark):
    """The default Light and Dark palettes, used when no scheme is chosen."""
    return {
        "Window": "#252931" if dark else "#f2f3f5",
        "WindowText": "#e8eaf0" if dark else "#20242b",
        "Base": "#1d2128" if dark else "#ffffff",
        "AlternateBase": "#282e38" if dark else "#f5f6f8",
        "Text": "#e8eaf0" if dark else "#20242b",
        "Button": "#303640" if dark else "#e9ecf0",
        "ButtonText": "#e8eaf0" if dark else "#20242b",
        "Highlight": "#356ca7",
        "HighlightedText": "#ffffff",
        "ToolTipBase": "#343b47" if dark else "#fffbe6",
        "ToolTipText": "#e8eaf0" if dark else "#20242b",
        "Link": "#81b7f2" if dark else "#235a93",
        "LinkVisited": "#c3a4ef" if dark else "#6d388e",
        "PlaceholderText": "#a5afc0" if dark else "#697586",
        "BrightText": "#ffffff",
        "Light": "#56606e" if dark else "#ffffff",
        "Midlight": "#404854" if dark else "#e4e8ed",
        "Mid": "#4a5361" if dark else "#bac1cb",
        "Dark": "#15181d" if dark else "#89929f",
        "Shadow": "#0d1015" if dark else "#636b77",
    }


def stylesheet():
    """The small stylesheet that fills gaps the palette alone cannot express."""
    return (
        "QLabel#sectionTitle { font-size: 10px; font-weight: 700; padding: 5px 3px; color: palette(placeholder-text); } "
        "QLabel#previewHeading { font-weight: 600; padding: 6px 4px; } "
        "QLabel#previewNotice, QLabel#browserCount { color: palette(placeholder-text); padding: 4px; font-size: 11px; } "
        "QSplitter::handle { background: palette(window); width: 6px; } "
        "QTabBar::tab:selected { background: palette(base); border-bottom: 2px solid palette(highlight); } "
        "QWidget { color: palette(window-text); } "
        "QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox { color: palette(text); background: palette(base); } "
        "QAbstractItemView { color: palette(text); background: palette(base); selection-color: palette(highlighted-text); selection-background-color: palette(highlight); } "
        "QHeaderView::section, QTabBar::tab, QPushButton, QToolButton { color: palette(button-text); background: palette(button); } "
        "QMenu, QMenuBar { color: palette(window-text); background: palette(window); } "
        "QMenu::item:selected, QMenuBar::item:selected { color: palette(highlighted-text); background: palette(highlight); } "
        "QToolTip { color: palette(tool-tip-text); background: palette(tool-tip-base); } "
        "QWidget:disabled { color: palette(mid); } "
        "QTreeView, QTableView { border: 1px solid palette(mid); } QHeaderView::section { padding: 3px 5px; } QPushButton { padding: 3px 7px; } QTabBar::tab { padding: 5px 8px; } QLineEdit { padding: 2px; }"
    )


def native_picker(parent, title, initial, filters, save=False):
    # Leave DontUseNativeDialog unset; Qt selects the desktop/portal dialog when available.
    dialog = QFileDialog(parent, title, str(initial), filters)
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, False)
    dialog.setAcceptMode(
        QFileDialog.AcceptMode.AcceptSave if save else QFileDialog.AcceptMode.AcceptOpen
    )
    dialog.setFileMode(
        QFileDialog.FileMode.AnyFile if save else QFileDialog.FileMode.ExistingFile
    )
    if save:
        dialog.setDefaultSuffix("sc" if "*.sc" in filters else "png")
    if dialog.exec() != QFileDialog.DialogCode.Accepted:
        return "", ""
    return dialog.selectedFiles()[0], dialog.selectedNameFilter()


def clipboard_pack(source, ids):
    doc = Document()
    mapping = doc.import_resources(source, ids)
    roots = [mapping[id] for id in ids]
    with tempfile.TemporaryDirectory(prefix="scstudio-copy-") as folder:
        path = Path(folder) / "clipboard.sc"
        doc.save(path)
        stream = io.BytesIO()
        with ZipFile(stream, "w", ZIP_STORED) as archive:
            archive.writestr("clipboard.sc", path.read_bytes())
            for name, data in doc.sidecars.items():
                archive.writestr("sidecars/" + name, data)
            archive.writestr("roots.txt", ",".join(map(str, roots)))
        payload = stream.getvalue()
    doc.path = None
    return doc, roots, payload


def clipboard_unpack(payload):
    if len(payload) > 512 * 1024 * 1024:
        raise ValueError("Clipboard graph exceeds 512 MiB")
    with ZipFile(io.BytesIO(payload)) as archive:
        if sum(item.file_size for item in archive.infolist()) > 512 * 1024 * 1024:
            raise ValueError("Clipboard graph exceeds 512 MiB")
        roots = [int(value) for value in archive.read("roots.txt").decode().split(",")]
        with tempfile.TemporaryDirectory(prefix="scstudio-paste-") as folder:
            path = Path(folder) / "clipboard.sc"
            path.write_bytes(archive.read("clipboard.sc"))
            doc = Document.load(path)
        doc.path = None
        for item in archive.infolist():
            if item.filename.startswith("sidecars/"):
                name = item.filename[len("sidecars/") :]
                if Path(name).is_absolute() or ".." in Path(name).parts:
                    raise ValueError("Invalid clipboard sidecar path")
                doc.sidecars[name] = archive.read(item)
    if not roots or any(id not in doc.index for id in roots):
        raise ValueError("Clipboard root resource missing")
    return doc, roots


def set_graph_clipboard(doc, roots, payload):
    mime = QMimeData()
    mime.setData(CLIP_MIME, QByteArray(payload))
    mime.setText("SC resources: " + ", ".join(str(id) for id in roots))
    QApplication.clipboard().setMimeData(mime)
