"""The textures panel's Editor methods, moved verbatim."""

from pathlib import Path
from PyQt6.QtCore import (
    QSignalBlocker,
    QTimer,
    Qt,
)
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)
from .workspace import native_picker
from .widgets import save_picker
from . import textures


# Late-bound so tests and callers can patch one place.
from . import app

class TexturesPanel:

    def view_shape_atlas(self):
        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "shapes"
            or not self.resolve_drafts()
        ):
            return
        obj = self.doc.resource(self.selected)
        if not obj["commands"]:
            return
        self.view_selection_atlas()

    def view_selection_atlas(self):
        """Open the first reachable shape's atlas, falling back to atlas zero."""
        if not self.doc.tables["Textures"]["textures"]:
            return
        pending, seen, atlas = [self.selected], set(), 0
        while pending:
            id = pending.pop()
            if id in seen or id not in self.doc.index:
                continue
            seen.add(id)
            kind, raw = self.doc.index[id]
            if kind == "shapes" and raw["commands"]:
                row = max(0, self.commands.currentRow()) if id == self.selected else 0
                atlas = raw["commands"][min(row, len(raw["commands"]) - 1)]["texture_index"]
                break
            if kind == "movieclips":
                pending.extend(reversed(raw["children_ids"]))
        self.timer.stop()
        self.play.setText("▶ Play")
        self.texture_index = atlas
        self.selected = None
        with QSignalBlocker(self.category):
            self.category.setCurrentIndex(self.resource_kinds.index("textures"))
        self.populate()
        with QSignalBlocker(self.tree):
            for i in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(i)
                if item.data(0, Qt.ItemDataRole.UserRole) == ("texture", atlas):
                    self.tree.setCurrentItem(item)
                    break
        self.show_texture()

    def toggle_atlas_outlines(self, visible):
        self.preview.show_atlas_outlines = visible
        self.preview.viewport().update()

    def open_atlas_shape(self, id):
        if self.go_to(id):
            self.set_panel(5)

    def atlas_context_menu(self, regions, position):
        menu = QMenu(self)
        unique = list(dict.fromkeys(region["id"] for region in regions))
        candidates = menu.addMenu(f"Shapes at cursor ({len(unique)})")
        candidates.setEnabled(bool(unique))
        for id in unique:
            label = " / ".join(self.doc.names.get(id, [])) or f"Shape #{id}"
            target = candidates.addMenu(f"{label} · #{id}" if self.doc.names.get(id) else label)
            target.addAction(f"Open shape #{id}").triggered.connect(
                lambda checked=False, value=id: self.open_atlas_shape(value)
            )
            target.addAction("Find usages…").triggered.connect(
                lambda checked=False, value=id: self.window().references(value)
            )
            target.addAction("Reference tree…").triggered.connect(
                lambda checked=False, value=id: self.window().references(value, tree_mode=True)
            )
        if unique:
            menu.addAction(f"Choose shape… ({len(unique)} matches)").triggered.connect(
                lambda: self.browse_atlas_shapes(regions)
            )
        if not unique:
            menu.addAction("No referenced shape at this point").setEnabled(False)
        menu.addAction("Browse all atlas shapes…").triggered.connect(self.browse_atlas_shapes)
        menu.addSeparator()
        outlines = menu.addAction("Show asset outlines")
        outlines.setCheckable(True)
        outlines.setChecked(self.atlas_outlines.isChecked())
        outlines.triggered.connect(self.atlas_outlines.setChecked)
        menu.addAction("Fit atlas").triggered.connect(self.preview.fit_content)
        menu.exec(position)

    def browse_atlas_shapes(self, regions=None):
        # QAction/QPushButton triggered signals pass a bool, not a region list.
        if regions is None or isinstance(regions, bool):
            regions = self.preview.atlas_regions
        if regions is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Atlas #{self.texture_index} — Shapes")
        dialog.resize(780, 500)
        layout = QVBoxLayout(dialog)
        search = QLineEdit()
        search.setPlaceholderText("Filter shape ID, export name or referencing movieclip…")
        layout.addWidget(search)
        tree = QTreeWidget()
        tree.setObjectName("atlas_shapes_picker")
        tree.setHeaderLabels(
            ["Shape", "ID", "Commands", "Refs", "UV rectangles (x, y, w, h)"]
        )
        tree.setColumnWidth(0, 220)
        layout.addWidget(tree)
        grouped = {}
        for region in regions:
            grouped.setdefault(region["id"], []).append(region)
        # Reverse lookup once: large atlases must not rescan all clips for each row.
        parents = {}
        for parent, (kind, raw) in self.doc.index.items():
            if kind == "movieclips":
                for child in set(raw["children_ids"]):
                    if child in grouped:
                        parents.setdefault(child, []).append(parent)
        rows = []
        for id, entries in grouped.items():
            label = " / ".join(self.doc.names.get(id, [])) or f"Shape #{id}"
            rects = "; ".join(", ".join(f"{v:.1f}" for v in entry["rect"]) for entry in entries)
            usages = " ".join(str(parent) + " " + " ".join(self.doc.names.get(parent, []))
                              for parent in parents.get(id, []))
            rows.append(
                (
                    id,
                    [label, str(id), str(len(entries)), str(len(parents.get(id, []))), rects],
                    usages,
                )
            )
        count = QLabel()
        layout.addWidget(count)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)

        def populate():
            tree.clear()
            query = search.text().casefold()
            for id, values, usages in rows:
                if query and query not in (" ".join(values) + " " + usages).casefold():
                    continue
                item = QTreeWidgetItem(tree, values)
                item.setData(0, Qt.ItemDataRole.UserRole, id)
                item.setToolTip(0, "Referenced by: " + (usages or "no movieclip"))
            count.setText(f"{tree.topLevelItemCount()} of {len(grouped)} shapes · {len(regions)} referenced regions")
            if tree.topLevelItemCount():
                tree.setCurrentItem(tree.topLevelItem(0))
            buttons.button(QDialogButtonBox.StandardButton.Open).setEnabled(bool(tree.topLevelItemCount()))

        search.textChanged.connect(populate)
        tree.itemDoubleClicked.connect(lambda *_: dialog.accept())
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        populate()
        search.setFocus()
        if dialog.exec() == QDialog.DialogCode.Accepted and tree.currentItem():
            self.open_atlas_shape(tree.currentItem().data(0, Qt.ItemDataRole.UserRole))

    def show_texture(self):
        self.play.setEnabled(False)
        self.frame.setEnabled(False)
        self.preview_resource = None
        t = self.doc.tables["Textures"]["textures"][self.texture_index]
        d = t["highres"]
        # Textures is panel 4 in the current order; this was left at the old
        # index and opened the JSON panel instead when an atlas was picked.
        self.tabs.setCurrentIndex(4)
        self.active_panel = 4
        self.preview.scene().clear()
        self.breadcrumb.setText(f"Texture atlas #{self.texture_index}")
        self.texture_info.setText(
            f'Atlas {self.texture_index}\n{d["width"]} × {d["height"]}\nPixel type {d["pixel_type"]} · Format flags {d["texture_format"]}\nExternal: {d.get("external_texture") or "embedded"}\nLowres: {"yes" if t.get("lowres") else "no"}'
        )
        try:
            self.preview.show_atlas(self.doc, self.texture_index)
            marks = len(self.preview.atlas_marks)
            self.notice.setText(
                f"{len(self.preview.atlas_regions):,} referenced regions"
                + (f" · {marks:,} zero-area markers (diamonds)" if marks else "")
                + " · Double-click to open a shape, right-click for usages"
            )
            index = self.texture_index
            QTimer.singleShot(
                0,
                lambda: (
                    self.preview.fit_content()
                    if self.texture_index == index and self.selected is None
                    else None
                ),
            )
        except Exception as e:
            self.error(e)

    def export_texture(self):
        if self.texture_index is None:
            return
        path, _ = app.save_picker(self, "Export atlas", "", "PNG (*.png)")
        if path:
            try:
                textures.image(
                    self.doc.tables["Textures"]["textures"][self.texture_index][
                        "highres"
                    ],
                    self.doc.path.parent if self.doc.path else ".",
                ).save(path)
            except Exception as e:
                self.error(e)

    def replace_texture(self):
        if self.texture_index is None:
            return
        path, _ = app.native_picker(
            self, "Replace highres atlas", "", "Images (*.png *.jpg *.webp *.bmp)"
        )
        if not path:
            return
        try:
            data = textures.replace_from_png({}, path)

            def change():
                self.doc.tables["Textures"]["textures"][self.texture_index] = {
                    "highres": data,
                    "lowres": None,
                }
                self.doc.dirty.add("Textures")

            if self.mutate(change):
                self.show_texture()
        except Exception as e:
            self.error(e)

    def add_texture(self):
        path, _ = app.native_picker(
            self, "Add atlas", "", "Images (*.png *.jpg *.webp *.bmp)"
        )
        if not path:
            return
        try:
            data = textures.replace_from_png({}, path)

            def change():
                self.doc.tables["Textures"]["textures"].append(
                    {"highres": data, "lowres": None}
                )
                self.doc.dirty.add("Textures")

            self.mutate(change)
        except Exception as e:
            self.error(e)

    def import_ktx(self):
        path, _ = app.native_picker(self, "Import encoded atlas", "", "KTX (*.ktx *.zktx)")
        if not path:
            return
        try:
            import struct, zstandard

            raw = Path(path).read_bytes()
            if raw.startswith(b"\x28\xb5\x2f\xfd"):
                raw = zstandard.ZstdDecompressor().decompress(raw)
            if raw[:12] != b"\xabKTX 11\xbb\r\n\x1a\n":
                raise ValueError("Expected KTX1")
            w, h = struct.unpack_from("<II", raw, 36)
            data = {
                "texture_format": 8,
                "pixel_type": 0,
                "width": w,
                "height": h,
                "data": raw,
                "external_texture": None,
            }

            def change():
                self.doc.tables["Textures"]["textures"].append(
                    {"highres": data, "lowres": None}
                )
                self.doc.dirty.add("Textures")

            self.mutate(change)
        except Exception as e:
            self.error(e)
