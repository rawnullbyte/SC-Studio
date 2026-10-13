"""The children panel's Editor methods, moved verbatim."""

import copy
import math
from PyQt6.QtCore import (
    QSignalBlocker,
    Qt,
)
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QInputDialog,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)
from PyQt6.QtGui import (
    QTransform,
)
from .widgets import fill_table


class ChildrenPanel:

    def focused_child_index(self):
        """The child-list index the keyboard is on, or -1.

        The Children list row is that index directly. The Placements table
        addresses the current frame's placements, whose first column is the
        child index, so a focused placement resolves to the same child. A
        placement picked in the preview is the same answer, by the same route,
        because clicking an object there is how most of these edits start.
        Copy, Duplicate and Delete take a focused child to mean that child
        rather than the whole clip - the resource you are looking at, not the
        one it lives in.
        """
        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "movieclips"
        ):
            return -1
        clip = self.obj
        children = clip.get("children") or []
        focus = QApplication.focusWidget()
        if focus is self.children:
            row = self.children.currentRow()
            return row if 0 <= row < len(children) else -1
        if focus is self.elements or self.preview.hasFocus():
            frame = self.cursor_frame(clip)
            elements = frame["elements"] if frame is not None else []
            row = (
                self.elements.currentRow()
                if focus is self.elements
                else self.preview.selected_placement
            )
            if row is None or not 0 <= row < len(elements):
                return -1
            index = elements[row][0]
            return index if 0 <= index < len(children) else -1
        return -1

    def focused_child_id(self):
        """The resource id of the focused child instance, or None."""
        index = self.focused_child_index()
        if index < 0:
            return None
        return self.obj["children"][index]["id"]

    def selected_child_rows(self):
        """Rows marked in the Children list, or the current row when none is.

        The marked ranges win when there are any; otherwise this is the current
        row, which is what a plain click sets.
        """
        rows = self.highlighted_rows(self.children)
        if rows:
            return rows
        row = self.children.currentRow()
        return [row] if row >= 0 else []

    def selected_placement_rows(self):
        """Rows picked in the Placements list, or the focused one."""
        rows = self.highlighted_rows(self.elements)
        if rows:
            return rows
        row = self.elements.currentRow()
        return [row] if row >= 0 else []

    def picked_child_ids(self):
        """The resource id of every picked child instance, in list order.

        Reads the marked rows, so Ctrl- and Shift-click over several children
        copy exactly those, and the result is deduplicated: two instances of one
        shape are one resource to copy, and clipboard_pack would otherwise emit
        the same id twice.

        Deliberately not ``focused_child_id``: that answers from the focus
        widget, so a marked range whose table does not currently hold focus came
        back empty and Copy fell through to the browser selection - a whole
        movieclip copied when a few children were meant.
        """
        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "movieclips"
        ):
            return []
        children = self.obj.get("children") or []
        ids = []
        for row in self.selected_child_rows():
            if 0 <= row < len(children):
                id = children[row]["id"]
                if id not in ids:
                    ids.append(id)
        return ids

    def children_context_menu(self, position):
        self.list_context_menu(
            self.children,
            position,
            [
                ("Add child…", self.add_child),
                ("Delete", self.remove_child),
                ("Duplicate", self.duplicate_child),
                ("Rename…", self.rename_child),
                ("Open child", self.open_child),
                ("Send back", lambda: self.reorder_child(False)),
                ("Bring front", lambda: self.reorder_child(True)),
                ("Show all", self.show_all_children),
            ],
        )

    def placements_context_menu(self, position):
        self.list_context_menu(
            self.elements,
            position,
            [
                ("Add placement", self.add_element),
                ("Remove", self.remove_element),
                ("Transform / color…", self.edit_transform),
            ],
        )

    def placement_row(self):
        """The placement row the timeline is acting on.

        currentRow() is -1 whenever the table has no current cell, which happens
        when it was rebuilt rather than clicked, so fall back to the first row.
        """
        row = self.elements.currentRow()
        if row < 0 and self.elements.rowCount():
            return 0
        return row

    def placement_selection_changed(self):
        """A different placement is picked: follow it with strip and fields.

        Selecting another row changes which keys the strip shows and whose
        transform the animator's X/Y/Rotation/Scale fields describe, so both are
        reloaded. It does not move the selection in the preview: choosing in the
        table is a different gesture from clicking the canvas, and stealing the
        preview highlight would make the two disagree.
        """
        if self.loading:
            return
        self.refresh_keyframe_strip()
        self.load_animation_fields()

    def ensure_placement(self, clip, index, row):
        """Give a frame a placement for ``row``, inserting it if it is missing.

        The frames of a movieclip each list the placements they draw, and a
        frame may list fewer than its neighbours. Inserting the child at the
        same row keeps the row numbering every other frame uses, so the frames
        gain the placement rather than the whole clip being renumbered.
        """
        if not 0 <= index < len(clip["frames"]):
            return False
        elements = clip["frames"][index]["elements"]
        if row < len(elements):
            return True
        # Which child the row slot belongs to is taken from the nearest earlier
        # frame that has it, so the frame is given the placement it is missing
        # rather than a different one. Failing that, the child with that index
        # is used, and a clip with no such child cannot be filled at all.
        child = None
        for earlier in range(min(index, len(clip["frames"]) - 1), -1, -1):
            occupied = clip["frames"][earlier]["elements"]
            if row < len(occupied):
                child = occupied[row][0]
                break
        if child is None:
            children = clip.get("children") or []
            if row >= len(children):
                return False
            child = row
        while len(elements) <= row:
            elements.append([child, 65535, 65535])
        return True

    def show_elements(self):
        if (
            self.selected in self.doc.index
            and self.doc.index[self.selected][0] == "movieclips"
        ):
            obj = self.obj
            i = self.frame_index()
            fill_table(
                self.elements,
                obj["frames"][i]["elements"] if i < len(obj["frames"]) else [],
            )
            self.refresh_keypoints()

    def child_changed(self, row, column):
        if column != 3:
            self.timeline_changed()
            return
        if self.loading or self.selected not in self.doc.index:
            return
        hidden = self.child_visibility.setdefault(self.selected, set())
        if self.children.item(row, 3).checkState() == Qt.CheckState.Checked:
            hidden.discard(row)
        else:
            hidden.add(row)
        self.render_frame(value=self.cursor.value(), fit=False)

    def show_all_children(self):
        self.child_visibility.pop(self.selected, None)
        self.fill_visibility()
        self.render_frame(value=self.cursor.value(), fit=False)

    def add_child(self):
        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "movieclips"
        ):
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Add child resource")
        dialog.resize(480, 400)
        layout = QVBoxLayout(dialog)
        search = QLineEdit()
        search.setPlaceholderText("Filter by name or ID…")
        layout.addWidget(search)
        tree = QTreeWidget()
        tree.setHeaderLabels(["Resource", "ID"])
        layout.addWidget(tree)

        def populate():
            tree.clear()
            query = search.text().casefold()
            for id, (kind, raw) in self.doc.index.items():
                label = " / ".join(self.doc.names.get(id, [])) or f"{kind[:-1]} {id}"
                if query and query not in label.casefold() and query not in str(id):
                    continue
                item = QTreeWidgetItem(tree, [label, str(id)])
                item.setData(0, Qt.ItemDataRole.UserRole, id)

        search.textChanged.connect(populate)
        populate()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        new_clip = QPushButton("Create new movieclip…")
        layout.addWidget(new_clip)

        def create_child():
            name, ok = QInputDialog.getText(
                dialog, "New child movieclip", "Instance name:"
            )
            if not ok:
                return
            created = []

            def change():
                child_id = self.doc.new_resource("movieclips")
                created.append(child_id)
                self.place_child(child_id, name or "movieclip")

            if self.mutate(change):
                dialog.reject()
                self.refresh()
                self.set_panel(0)
                self.elements.selectRow(self.elements.rowCount() - 1)

        new_clip.clicked.connect(create_child)
        layout.addWidget(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        tree.itemDoubleClicked.connect(lambda *_: dialog.accept())
        if dialog.exec() == QDialog.DialogCode.Accepted and tree.currentItem():
            id = tree.currentItem().data(0, Qt.ItemDataRole.UserRole)
            if self.mutate(
                lambda: self.place_child(id, " / ".join(self.doc.names.get(id, [])))
            ):
                self.refresh()
                self.set_panel(0)
                self.elements.selectRow(self.elements.rowCount() - 1)

    def place_child(self, id, name):
        obj = self.doc.resource(self.selected)
        if not obj["frames"]:
            obj["frames"].append({"label": "", "elements": []})
        # A new child is placed on the frame the cursor is on, which may be one
        # the clip does not have yet; it is added so the child lands where it
        # was asked for rather than on the last frame.
        self.grow_clip(obj, self.cursor.value(), update=False)
        index = len(obj["children"])
        obj["children"].append({"id": id, "name": name, "blend": 0})
        obj["frames"][self.cursor.value()]["elements"].append([index, 65535, 65535])
        self.doc.update("movieclips", obj)

    def reorder_placement(self, row, target):
        if not self.resolve_drafts():
            return
        obj = self.doc.resource(self.selected)
        frame = self.cursor_frame(obj)
        if frame is None:
            # Nothing is drawn on this frame yet, so there is no order to
            # rearrange; moving something here is what creates it.
            return
        elements = frame["elements"]
        target = max(0, min(target, len(elements) - 1))
        if row == target:
            return
        element = elements.pop(row)
        elements.insert(target, element)
        if self.mutate(lambda: self.doc.update("movieclips", obj)):
            self.refresh()
            self.select_placement(target)
            self.preview.selected_placement = target

    def set_layer(self, row):
        frame = self.cursor_frame(self.obj)
        if frame is None:
            return
        count = len(frame["elements"])
        target, ok = QInputDialog.getInt(
            self, "Layer position", "Draw order (0 = back):", row, 0, count - 1
        )
        if ok:
            self.reorder_placement(row, target)

    def filter_children(self):
        needle = self.children_search.text().strip().casefold()
        for row in range(self.children.rowCount()):
            values = [self.children.item(row, col).text() for col in (0, 1)
                      if self.children.item(row, col)]
            item = self.children.item(row, 0)
            if item:
                try:
                    values.extend(self.doc.names.get(int(item.text()), []))
                except ValueError:
                    pass
            self.children.setRowHidden(row, bool(needle and needle not in " ".join(values).casefold()))

    def first_child_match(self):
        for row in range(self.children.rowCount()):
            if not self.children.isRowHidden(row):
                self.children.selectRow(row)
                self.children.scrollToItem(self.children.item(row, 0))
                self.children.setFocus()
                break

    def children_selection_changed(self):
        if (
            self.loading
            or self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "movieclips"
        ):
            return
        picked = set(self.selected_child_rows())
        primary = self.children.currentRow()
        frame = self.cursor_frame(self.obj)
        elements = frame["elements"] if frame is not None else []
        # Every placement in this frame whose child is picked, so choosing
        # several children outlines all of them in the canvas rather than only
        # the last one clicked.
        rows = [i for i, e in enumerate(elements) if e[0] in picked]
        with QSignalBlocker(self.elements):
            if rows:
                self.elements.clearSelection()
                for r in rows:
                    self.elements.selectRow(r)
                self.elements.setCurrentCell(primary_row := rows[-1], 0)
            else:
                self.elements.clearSelection()
                self.elements.setCurrentCell(-1, -1)
        self.preview.selected_rows = set(rows)
        self.preview.selected_placement = rows[-1] if rows else None
        self.preview.viewport().update()
        # The transform fields describe the placement the panels are acting on,
        # so picking a child in the list moves them here too rather than leaving
        # the previous child's numbers on screen.
        self.load_animation_fields()

    def rename_child(self):
        row = self.children.currentRow()
        if row < 0 or not self.resolve_drafts():
            return
        obj = self.doc.resource(self.selected)
        name, ok = QInputDialog.getText(
            self, "Rename child", "Instance name:", text=obj["children"][row]["name"]
        )
        if ok:
            obj["children"][row]["name"] = name
            if self.mutate(lambda: self.doc.update("movieclips", obj)):
                self.refresh()
                self.children.selectRow(row)

    def open_child(self):
        row = self.children.currentRow()
        if row >= 0 and self.selected in self.doc.index:
            self.go_to(self.doc.resource(self.selected)["children"][row]["id"])

    def duplicate_child(self, child_index=None):
        ci = self.children.currentRow() if child_index is None else child_index
        if ci < 0 or not self.resolve_drafts():
            return
        obj = self.doc.resource(self.selected)
        # Every picked child gets a copy, so a set of instances can be
        # duplicated in one step rather than one at a time.
        rows = (
            self.selected_child_rows() if child_index is None else [ci]
        )
        rows = sorted({c for c in rows if 0 <= c < len(obj["children"])})
        if not rows:
            return
        # The copies are appended in the order the rows appear, so the last one
        # lands highest and selects predictably.
        for source in rows:
            child = copy.deepcopy(obj["children"][source])
            child["name"] = (child["name"] or "child") + "_copy"
            index = len(obj["children"])
            obj["children"].append(child)
            for frame in obj["frames"]:
                frame["elements"].extend(
                    [[index, e[1], e[2]] for e in list(frame["elements"]) if e[0] == source]
                )
        index = len(obj["children"]) - 1
        if self.mutate(lambda: self.doc.update("movieclips", obj)):
            self.refresh()
            # Cleared first: after the rows were rebuilt a previous range can
            # still be marked, and selecting the copy without clearing left the
            # old row picked alongside it - so the next single-row action, a
            # click then Delete, took both.
            self.children.clearSelection()
            self.children.selectRow(index)

    def reorder_child(self, front):
        ci = self.children.currentRow()
        if ci < 0 or not self.resolve_drafts():
            return
        obj = self.doc.resource(self.selected)
        frame = self.cursor_frame(obj)
        if frame is None:
            return
        elements = frame["elements"]
        selected = [e for e in elements if e[0] == ci]
        others = [e for e in elements if e[0] != ci]
        frame["elements"] = others + selected if front else selected + others
        if self.mutate(lambda: self.doc.update("movieclips", obj)):
            self.refresh()
            self.children.selectRow(ci)

    def commit_placement_transform(self, row, matrix):
        """Set one placement's transform outright.

        The single-item resize already knows the final pose, so this is an
        absolute set; a group transform is relative to each placement because
        they all share one box, which is what ``transform_placements`` does.
        """
        if not self.resolve_drafts():
            self.render_frame(value=self.cursor.value(), fit=False)
            return
        clip = self.doc.resource(self.selected)
        fi = self.cursor.value()
        # A resize made with the cursor past the end grows the clip the same
        # way a move does, so the change is not dropped.
        self.grow_clip(clip, fi)
        if row >= len(clip["frames"][fi]["elements"]):
            self.error(
                ValueError(f"placement {row} is not in frame {fi + 1}")
            )
            return
        element = clip["frames"][fi]["elements"][row]
        if self.mutate(lambda: self.assign_matrix(clip, element, matrix)):
            self.refresh()
            self.select_placement(row)
            self.preview.selected_placement = row
            self.preview.viewport().update()

    def transform_placements(self, rows, group):
        """Apply a scene-space transform to several placements at once.

        ``group`` is a QTransform in the clip's own space - the group box's
        scale about its anchor - so each placement's new transform is its own
        current one composed with the group. One mutation, so one undo step.
        """
        rows = [r for r in rows if r is not None]
        if not rows:
            return
        if not self.resolve_drafts():
            self.render_frame(value=self.cursor.value(), fit=False)
            return
        clip = self.doc.resource(self.selected)
        fi = self.cursor.value()
        # A resize made with the cursor past the end grows the clip the same
        # way a move does, so the change is not dropped.
        self.grow_clip(clip, fi)
        elements = clip["frames"][fi]["elements"]
        group_matrix = [
            group.m11(),
            group.m12(),
            group.m21(),
            group.m22(),
            group.dx(),
            group.dy(),
        ]
        changed = []
        for row in rows:
            if row >= len(elements):
                continue
            element = elements[row]
            current = QTransform(
                *(
                    list(self.doc.bank(clip["matrix_bank_index"])["matrices"][element[1]])
                    if element[1] != 65535
                    else [1, 0, 0, 1, 0, 0]
                )
            )
            # The group applies after the placement's own transform: a
            # placement is already in clip space, so its new pose is its own
            # matrix followed by the group's. Qt multiplies row-vectors, so that
            # order is ``current * group``, not the other way round.
            composed = current * group
            changed.append(
                (
                    element,
                    [
                        composed.m11(),
                        composed.m12(),
                        composed.m21(),
                        composed.m22(),
                        composed.dx(),
                        composed.dy(),
                    ],
                )
            )
        if not changed:
            self.error(ValueError(f"placement {rows[0]} is not in frame {fi + 1}"))
            return
        if self.mutate(lambda: self.assign_matrices(clip, changed)):
            self.refresh()
            self.select_placement(rows[-1])
            self.preview.selected_placement = rows[-1]
            self.preview.viewport().update()

    def remove_child(self, child_index=None):
        # QPushButton.clicked and QAction.triggered both carry a checked bool,
        # which Qt hands to a slot that takes an argument. Without this the
        # button deleted child 0 whatever was picked, because False reads as 0.
        if child_index is None or isinstance(child_index, bool):
            rows = self.selected_child_rows()
        else:
            rows = [child_index]
        self.remove_child_rows(rows)

    def remove_child_rows(self, rows):
        """Remove several children, and their placements, in one edit."""
        doomed = sorted({c for c in rows if c is not None and c >= 0})
        if not doomed:
            return
        i = doomed[0]
        if not self.resolve_drafts():
            return
        obj = self.doc.resource(self.selected)
        if not 0 <= i < len(obj["children"]):
            return
        # Everything picked in the list goes, not just the current row. Done
        # downwards so the earlier indices still mean the same child as they are
        # removed, and with one renormalisation pass at the end.
        doomed = [c for c in doomed if c < len(obj["children"])]
        if not doomed:
            return
        names = [
            obj["children"][c]["name"] or f"child {c}" for c in doomed
        ]
        for c in reversed(doomed):
            obj["children"].pop(c)
        drop = set(doomed)
        for frame in obj["frames"]:
            kept = [e for e in frame["elements"] if e[0] not in drop]
            for e in kept:
                # Every removed child below this one shifts it down.
                e[0] -= sum(1 for c in doomed if c < e[0])
            frame["elements"] = kept
        if self.mutate(lambda: self.doc.update("movieclips", obj)):
            hidden = self.child_visibility.get(self.selected, set())
            self.child_visibility[self.selected] = {
                c - sum(1 for d in doomed if d < c) for c in hidden if c not in drop
            }
            self.refresh()
            self.children.selectRow(
                min(min(doomed), max(0, self.children.rowCount() - 1))
            )
            # No confirmation dialog: the change is one undo step, so Ctrl+Z
            # takes it straight back. Saying what went keeps it discoverable.
            label = names[0] if len(names) == 1 else f"{len(names)} children"
            self.window().statusBar().showMessage(
                f"Removed {label} from every frame · Ctrl+Z to undo", 4000
            )

    def add_element(self):
        i = self.frame_index()

        def change(o):
            if not o["children"] or not o["frames"]:
                raise ValueError("Add a child and a frame first")
            o["frames"][i]["elements"].append([0, 65535, 65535])

        self.clip_edit(change)

    def remove_element(self):
        self.remove_element_rows(self.selected_placement_rows())

    def remove_element_rows(self, rows):
        i = self.frame_index()
        rows = [r for r in rows if r is not None]
        if not rows:
            return
        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "movieclips"
        ):
            return
        obj = self.doc.resource(self.selected)
        frames = obj["frames"]
        if not 0 <= i < len(frames):
            return
        elements = frames[i]["elements"]
        doomed = sorted({r for r in rows if 0 <= r < len(elements)})
        if not doomed:
            return
        drop = set(doomed)
        children = {elements[r][0] for r in doomed}
        kept = [e for r, e in enumerate(elements) if r not in drop]
        # A child nothing places any more has no route back through the preview
        # or the frames, so it only lingers in the Children list. That is the
        # entry the request was to be rid of; drop it in the same undo step.
        gone = []
        for child in sorted(children, reverse=True):
            if child >= len(obj["children"]):
                continue
            still_placed = any(e[0] == child for e in kept) or any(
                e[0] == child
                for n, frame in enumerate(frames)
                if n != i
                for e in frame["elements"]
            )
            if not still_placed:
                gone.append(obj["children"][child]["name"] or f"child {child}")
                obj["children"].pop(child)
                for frame in frames:
                    for element in frame["elements"]:
                        if element[0] > child:
                            element[0] -= 1
        frames[i]["elements"] = kept
        if self.mutate(lambda: self.doc.update("movieclips", obj)):
            self.refresh()
            if gone:
                label = gone[0] if len(gone) == 1 else f"{len(gone)} children"
                self.window().statusBar().showMessage(
                    f"Removed the placement and its unused child {label}"
                    " · Ctrl+Z to undo",
                    4000,
                )

    def canvas_placement_selected(self, row):
        """The canvas picked a placement: mirror it into the lists.

        This must not rebuild the canvas selection. The preview has already set
        ``selected_rows`` for the whole pick - including a Ctrl-click that kept
        the earlier rows - so narrowing it to the one row here would undo the
        Ctrl-click that produced this signal.
        """
        self.sync_lists_to_selection()

    def select_placement(self, row):
        """Pick a single placement from the app side, canvas and lists together."""
        self.preview.selected_placement = row
        self.preview.selected_rows = {row} if row is not None else set()
        self.preview.viewport().update()
        if self.timer.isActive():
            self.toggle_play()
        self.set_panel(0)
        self.elements.selectRow(row)
        self.sync_lists_to_selection()

    def move_placement(self, row, dx, dy):
        self.move_placements([row], dx, dy)

    def move_placements(self, rows, dx, dy):
        """Offset several placements by the same amount, as one edit.

        A group drag moves every picked placement; doing it as one mutation
        keeps it one undo step instead of one per row.
        """
        rows = [r for r in rows if r is not None]
        if not rows:
            return
        if not self.resolve_drafts():
            self.render_frame(fit=False)
            return
        fi = self.cursor.value()
        obj = self.doc.resource(self.selected)
        # The cursor may sit past the last frame while the clip is still short;
        # changing something there is exactly how the clip gets its new frames.
        self.grow_clip(obj, fi)
        elements = obj["frames"][fi]["elements"]
        moving = []
        for row in rows:
            if row >= len(elements):
                continue
            element = elements[row]
            matrix = (
                list(self.doc.bank(obj["matrix_bank_index"])["matrices"][element[1]])
                if element[1] != 65535
                else [1, 0, 0, 1, 0, 0]
            )
            matrix[4] += dx
            matrix[5] += dy
            moving.append((element, matrix))
        if not moving:
            self.error(
                ValueError(f"placement {rows[0]} is not in frame {fi + 1}")
            )
            return
        # Moving a placement cannot rename anything, so the browser list is
        # left alone; rebuilding it was the bulk of the lag after a drag.
        if self.mutate(
            lambda: self.assign_matrices(obj, moving), rebuild_browser=False
        ):
            self.refresh()
            self.elements.selectRow(min(rows))
            self.preview.selected_placement = rows[-1]

    def resize_placement(self):
        import math

        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "movieclips"
        ):
            return
        if not self.resolve_drafts():
            return
        clip = self.doc.resource(self.selected)
        fi = self.cursor.value()
        frames = clip["frames"]
        # The cursor is allowed past the last frame - that is where an edit
        # extends the clip - so indexing it directly raised IndexError as soon
        # as the playhead sat in that headroom.
        if not frames:
            self.error("Import an image or add a placement first.")
            return
        fi = min(fi, len(frames) - 1)
        elements = frames[fi]["elements"]
        if not elements:
            self.error("Import an image or add a placement first.")
            return
        row = self.elements.currentRow()
        if not 0 <= row < len(elements):
            row = len(elements) - 1
        element = elements[row]
        children = clip["children"]
        # A placement stores a child index, and that index can point past the
        # end after children were removed; reading the child by it raised
        # IndexError rather than saying what was wrong.
        if not 0 <= element[0] < len(children):
            self.error(
                "This placement refers to a child that is no longer in this movieclip."
            )
            return
        child_id = children[element[0]]["id"]
        item = next(
            (
                item
                for item, _ in self.preview.instances.values()
                if item.data(0) == row and item.isVisible()
            ),
            None,
        )
        if item is None:
            self.error("Show the child in the preview before resizing it.")
            return
        bounds = item.boundingRect()
        bw, bh = bounds.width(), bounds.height()
        if bw <= 0 or bh <= 0:
            self.error("This shape has zero width or height.")
            return
        matrix = (
            self.doc.bank(clip["matrix_bank_index"])["matrices"][element[1]]
            if element[1] != 65535
            else [1, 0, 0, 1, 0, 0]
        )
        a, b, c, d, x, y = matrix
        values = self.size_dialog(
            "Placement — size and position",
            bw * math.hypot(a, b),
            bh * math.hypot(c, d),
            x,
            y,
            math.degrees(math.atan2(b, a)),
        )
        if values is None:
            return
        w, h, x, y, angle = values
        # Preserve skew and reflection while scaling each basis vector; apply rotation delta.
        delta = math.radians(angle) - math.atan2(b, a)
        cs, sn = math.cos(delta), math.sin(delta)
        sx, sy = w / (bw * max(math.hypot(a, b), 1e-9)), h / (
            bh * max(math.hypot(c, d), 1e-9)
        )
        matrix = [
            (a * cs - b * sn) * sx,
            (a * sn + b * cs) * sx,
            (c * cs - d * sn) * sy,
            (c * sn + d * cs) * sy,
            x,
            y,
        ]
        if self.mutate(lambda: self.assign_matrix(clip, element, matrix)):
            self.refresh()
            self.frames.selectRow(fi)
            self.elements.selectRow(row)
