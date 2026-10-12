"""The geometry panel's Editor methods, moved verbatim."""

import math
from PyQt6.QtCore import (
    QSignalBlocker,
)
from PyQt6.QtWidgets import (
    QApplication,
    QInputDialog,
    QTableWidgetItem,
)
from .widgets import fill_table


class GeometryPanel:

    def show_vertices(self):
        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "shapes"
        ):
            return
        row = self.commands.currentRow()
        if not self.loading and "geometry" in self.drafts and row != self.geometry_row:
            target = row
            with QSignalBlocker(self.commands):
                self.commands.selectRow(self.geometry_row)
            if not self.resolve_drafts():
                return
            with QSignalBlocker(self.commands):
                self.commands.selectRow(target)
            row = target
        self.geometry_row = row
        obj = self.doc.resource(self.selected)
        fill_table(
            self.vertices,
            obj["commands"][row]["vertices"] if 0 <= row < len(obj["commands"]) else [],
        )

    def geometry_edit(self, fn):
        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "shapes"
        ):
            return
        obj = self.doc.resource(self.selected)
        try:
            fn(obj)
        except Exception as e:
            self.error(e)
            return
        if self.mutate(lambda: self.doc.update("shapes", obj)):
            self.refresh()

    def add_command(self):
        if not self.resolve_drafts():
            return
        if not self.doc.tables["Textures"]["textures"]:
            self.error("Add a texture atlas first.")
            return
        index, ok = QInputDialog.getInt(
            self,
            "Texture atlas",
            "Atlas index:",
            0,
            0,
            len(self.doc.tables["Textures"]["textures"]) - 1,
        )
        if ok:
            data = self.doc.tables["Textures"]["textures"][index]["highres"]
            w, h = data["width"], data["height"]
            self.geometry_edit(
                lambda o: o["commands"].append(
                    {
                        "unk1": 0,
                        "texture_index": index,
                        "vertices": [
                            [0, 0, 0, 0],
                            [w, 0, 65535, 0],
                            [0, h, 0, 65535],
                            [w, h, 65535, 65535],
                        ],
                    }
                )
            )

    def remove_command(self):
        if not self.resolve_drafts():
            return
        row = self.commands.currentRow()
        if row >= 0:
            self.geometry_edit(lambda o: o["commands"].pop(row))

    def add_vertex(self):
        row = self.vertices.rowCount()
        self.vertices.blockSignals(True)
        self.vertices.setRowCount(row + 1)
        for j in range(4):
            self.vertices.setItem(row, j, QTableWidgetItem("0"))
        self.vertices.blockSignals(False)
        self.mark_draft("geometry")

    def remove_vertex(self):
        row = self.vertices.currentRow()
        if row >= 0:
            self.vertices.removeRow(row)
            self.mark_draft("geometry")

    def apply_geometry(self):
        import math

        row = self.commands.currentRow()
        if row < 0:
            return

        def change(obj):
            for i, c in enumerate(obj["commands"]):
                c["texture_index"] = int(self.commands.item(i, 0).text())
                c["unk1"] = int(self.commands.item(i, 1).text())
            values = [
                [
                    (
                        float(self.vertices.item(i, j).text())
                        if j < 2
                        else int(self.vertices.item(i, j).text())
                    )
                    for j in range(4)
                ]
                for i in range(self.vertices.rowCount())
            ]
            if any(not math.isfinite(v) for row in values for v in row):
                raise ValueError("Coordinates must be finite")
            obj["commands"][row]["vertices"] = values

        self.geometry_edit(change)
        if row < self.commands.rowCount():
            self.commands.selectRow(row)

    def copy_vertices(self):
        """Copy the focused command's whole vertex list, ready to paste elsewhere.

        A Copy over the vertex table takes the whole list, whether or not a cell
        is selected: the vertex list is a portable unit, so it can be pasted
        onto another shape's command and the geometry is transplanted.
        """
        if self.selected not in self.doc.index:
            return
        rows = [
            "\t".join(
                self.vertices.item(r, c).text() if self.vertices.item(r, c) else ""
                for c in range(self.vertices.columnCount())
            )
            for r in range(self.vertices.rowCount())
        ]
        if rows:
            QApplication.clipboard().setText("\n".join(rows))
            self.window().statusBar().showMessage(f"Copied {len(rows)} vertices", 4000)

    def paste_vertices(self, grid):
        """Replace the current command's vertices with the pasted list.

        The pasted list becomes the command wholesale, so the row count may grow
        or shrink: pasting a whole shape's vertices onto another shape is the
        point, and refusing because the target happened to have fewer rows made
        that impossible.
        """
        if self.selected not in self.doc.index:
            return
        text = QApplication.clipboard().text().strip()
        rows = [line.split("\t") for line in text.splitlines() if line.strip()]
        if not rows:
            return
        width = grid.columnCount()
        # Rebuilding the rows emits the table's own signals, which resolve
        # geometry drafts and can rebuild the panel underneath this call. The
        # loading flag is the editor's existing "a rebuild is in progress"
        # latch, so it is held across the rebuild and released after.
        self.loading = True
        try:
            with QSignalBlocker(grid):
                grid.setRowCount(len(rows))
                for y, values in enumerate(rows):
                    for c in range(width):
                        value = values[c] if c < len(values) else "0"
                        grid.setItem(y, c, QTableWidgetItem(value))
            grid.selectRow(0)
        finally:
            self.loading = False
        self.mark_draft("geometry")
        self.window().statusBar().showMessage(
            f"Pasted {len(rows)} vertices; Apply geometry writes them", 5000
        )
