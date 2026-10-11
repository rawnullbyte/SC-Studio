"""Authoring position, independent of the bounded playback transport slider."""

from PyQt6.QtCore import QObject, pyqtSignal


class FrameCursor(QObject):
    valueChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 0
        self._maximum = 0

    def value(self):
        return self._value

    def maximum(self):
        return self._maximum

    def setRange(self, minimum, maximum):
        self._maximum = max(0, min(65534, int(maximum)))
        self.setValue(self._value)

    def setValue(self, value):
        value = max(0, min(self._maximum, int(value)))
        if value != self._value:
            self._value = value
            self.valueChanged.emit(value)
