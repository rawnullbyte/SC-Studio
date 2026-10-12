"""Small reusable widgets and table helpers for the editor.

These are pieces the editor builds out of rather than operates: a colour swatch,
the packed-alignment control, the table factory and its filler, the background
task runner. They depend on nothing in the editor, which is what makes them safe
to keep here rather than inside the panels that happen to use them.
"""

import json
from PyQt6.QtCore import Qt, QObject, QRunnable, QSignalBlocker, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QComboBox,
    QColorDialog,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTreeWidgetItem,
    QWidget,
)

from .document import FrameSequence, FrameElements
from .workspace import native_picker

# Textfield fields that hold a packed 0xAARRGGBB colour and are therefore edited
# with a colour picker rather than as the integer they are stored as.
COLOR_KEYS = ("font_color", "outline_color")

# The two packed alignment fields: horizontal in the low two bits, vertical in
# the next two, with the high nibble reserved (styles/unknown).
ALIGN_HORIZONTAL = ("Left", "Center", "Right", "Justify")
ALIGN_VERTICAL = ("Top", "Center", "Bottom", "Bottom (alternate)")


def _uv(value, size):
    """A pixel coordinate as the 0..65535 texture coordinate the format stores."""
    if size <= 0:
        return 0
    return round(max(0, min(size, value)) / size * 65535)


def save_picker(parent, title, initial="", filters=""):
    return native_picker(parent, title, initial, filters, save=True)


class ResourceItem(QTreeWidgetItem):
    def __lt__(self, other):
        column = self.treeWidget().sortColumn() if self.treeWidget() else 0
        if column == 1:
            try:
                return int(self.text(column)) < int(other.text(column))
            except ValueError:
                pass
        return self.text(column).casefold() < other.text(column).casefold()


class Signals(QObject):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)


class Task(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = Signals()

    def run(self):
        try:
            self.signals.done.emit(self.fn())
        except Exception as e:
            self.signals.failed.emit(str(e))


def jsonable(obj):
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, FrameSequence, FrameElements)):
        return [jsonable(v) for v in obj]
    return obj


def fill_table(table, rows):
    table.blockSignals(True)
    # Reset the selection through the selection model before resizing. The
    # rows are rebuilt with signals blocked and setRowCount drops the cells, but
    # a marked range from the previous layout can survive both that and
    # clearSelection, leaving a later single-row action to act on a stale row.
    if table.selectionModel() is not None:
        table.selectionModel().clear()
    table.setCurrentCell(-1, -1)
    table.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            table.setItem(i, j, QTableWidgetItem(str(value)))
    if (
        table.horizontalHeaderItem(0)
        and table.horizontalHeaderItem(0).text() == "Frame"
    ):
        for i in range(table.rowCount()):
            for j in (0, 2):
                table.item(i, j).setFlags(
                    table.item(i, j).flags() & ~Qt.ItemFlag.ItemIsEditable
                )
    if (
        table.horizontalHeaderItem(0)
        and table.horizontalHeaderItem(0).text() == "Texture atlas"
    ):
        for i in range(table.rowCount()):
            table.item(i, 2).setFlags(
                table.item(i, 2).flags() & ~Qt.ItemFlag.ItemIsEditable
            )
    table.blockSignals(False)
    table.resizeColumnsToContents()


def table(headers, name=""):
    widget = QTableWidget(0, len(headers))
    widget.setObjectName(name or headers[0].split()[0].lower())
    widget.setHorizontalHeaderLabels(headers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    return widget


def typing_into(widget):
    """True when an unmodified keystroke should go to the focused widget.

    A one-letter shortcut installed on a QAction is consumed even while the user
    is typing, so plain keys are routed by hand and these are the widgets that
    keep them: text edits, spin boxes, combo boxes and in-place table editors.
    A platform focus policy cannot stand in here -- PyQt6 reports StrongFocus
    for text fields and trees alike, and has no TextEditFocus flag.
    """
    while widget is not None:
        if isinstance(
            widget,
            (QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox, QComboBox),
        ):
            return True
        widget = widget.parentWidget()
    return False


class ColorSwatch(QPushButton):
    """A packed ARGB colour shown as a swatch that opens the colour picker.

    The value is the same integer the file stores; only the presentation is a
    colour. ``toggled`` is emitted when the value changes, named to match what
    the property form already listens for so it needs no special casing.
    """

    toggled = pyqtSignal(bool)

    def __init__(self, value, parent=None):
        super().__init__(parent)
        self._value = int(value)
        self.setMinimumWidth(96)
        self.clicked.connect(self.pick)
        self._refresh()

    def value(self):
        return self._value

    def setValue(self, value):
        self._value = int(value)
        self._refresh()

    def _refresh(self):
        colour = QColor.fromRgba(self._value)
        # The swatch carries its own colour so it reads without applying the
        # theme; the label keeps the alpha visible, which a brush cannot show.
        self.setStyleSheet(
            f"QPushButton {{ background-color: {colour.name()}; "
            f"color: {'#000' if colour.lightness() > 128 else '#fff'}; }}"
        )
        self.setText(f"{self._value:08X}")
        self.setToolTip(f"argb {self._value}")

    def pick(self):
        colour = QColorDialog.getColor(
            QColor.fromRgba(self._value), self, "Choose colour",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if colour.isValid() and colour.rgba() != self._value:
            self._value = colour.rgba()
            self._refresh()
            self.toggled.emit(True)


class AlignControl(QWidget):
    """The packed textfield alignment bitfield as two named dropdowns.

    Horizontal is the low two bits and vertical the next two; the high nibble is
    left untouched, so a file that carries other flags in there round-trips.
    """

    currentIndexChanged = pyqtSignal(int)

    def __init__(self, value, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.horizontal = QComboBox()
        self.horizontal.addItems(ALIGN_HORIZONTAL)
        self.vertical = QComboBox()
        self.vertical.addItems(ALIGN_VERTICAL)
        layout.addWidget(self.horizontal)
        layout.addWidget(self.vertical)
        self.setValue(value)
        self.horizontal.currentIndexChanged.connect(self._changed)
        self.vertical.currentIndexChanged.connect(self._changed)

    def _changed(self, *_):
        self.currentIndexChanged.emit(self.horizontal.currentIndex())

    def value(self):
        # Preserve the high nibble the two named fields do not cover.
        return (self._high & 0xF0) | (self.horizontal.currentIndex() & 3) | (
            (self.vertical.currentIndex() & 3) << 2
        )

    def setValue(self, value):
        self._high = int(value)
        with QSignalBlocker(self.horizontal), QSignalBlocker(self.vertical):
            self.horizontal.setCurrentIndex(value & 3)
            self.vertical.setCurrentIndex((value >> 2) & 3)