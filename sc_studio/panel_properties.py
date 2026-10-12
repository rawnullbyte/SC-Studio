"""The properties panel's Editor methods, moved verbatim."""

import json
from PyQt6.QtWidgets import (
    QDoubleSpinBox,
    QMessageBox,
    QSpinBox,
)
from .widgets import AlignControl, ColorSwatch


class PropertiesPanel:

    def mark_draft(self, area):
        if not self.loading and self.selected in self.doc.index:
            self.drafts.add(area)
            self.changed.emit()

    def resolve_drafts(self):
        if not self.drafts:
            return True
        answer = QMessageBox.question(
            self,
            "Unapplied edits",
            "Apply changes before continuing?",
            QMessageBox.StandardButton.Apply
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Apply,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Discard:
            self.drafts.clear()
            self.refresh()
            self.changed.emit()
            return True
        area = next(iter(self.drafts))
        if area == "properties":
            self.apply_properties()
        elif area == "json":
            self.apply_json()
        elif area == "geometry":
            self.apply_geometry()
        return not self.drafts

    def align_control(self, value):
        """The two alignment dropdowns, wired to draft tracking."""
        widget = AlignControl(value)
        widget.currentIndexChanged.connect(lambda *_: self.mark_draft("properties"))
        return widget

    def apply_properties(self):
        if self.selected is None:
            return
        try:
            obj = self.doc.resource(self.selected)
            for key, (w, t) in self.property_widgets.items():
                if isinstance(w, (ColorSwatch, AlignControl)):
                    # Both already hand back the integer the file stores.
                    obj[key] = w.value()
                    continue
                v = w.value() if isinstance(w, (QSpinBox, QDoubleSpinBox)) else w.text()
                obj[key] = (
                    [n.strip() for n in v.split(",") if n.strip()]
                    if t == list
                    else int(v, 0) if t is int and isinstance(v, str) else t(v)
                )
            kind = self.doc.index[self.selected][0]
            if self.mutate(lambda: self.doc.update(kind, obj)):
                self.refresh()
        except Exception as e:
            self.error(e)

    def apply_json(self):
        if self.selected is None:
            return
        try:
            obj = json.loads(self.code.toPlainText())
            if obj["id"] != self.selected:
                raise ValueError(
                    "IDs cannot be changed directly; duplicate/import to assign a new ID"
                )
            kind = self.doc.index[self.selected][0]
            if self.mutate(lambda: self.doc.update(kind, obj)):
                self.refresh()
        except Exception as e:
            self.error(e)
