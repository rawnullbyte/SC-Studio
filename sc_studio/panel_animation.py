"""The animation panel's Editor methods, moved verbatim."""

import math
from pathlib import Path
from PyQt6.QtCore import (
    QSignalBlocker,
)
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMenu,
)
from PyQt6.QtGui import (
    QColor,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from .workspace import native_picker
from .widgets import _uv
from . import easing, textures


# Late-bound so tests and callers can patch one place.
from . import app

class AnimationPanel:

    # --- keypoint animation -------------------------------------------------
    #
    # The format stores a discrete transform per frame with nowhere to record a
    # curve, so a keypoint is simply "this frame holds a transform for this
    # placement". Baking fills the frames between keys from a chosen curve; the
    # game then reads ordinary frames. Nothing here is stored as a curve.

    def placement_keys(self, clip):
        """Frame indices where the placement's transform differs from identity.

        Identity means the placement inherits the child's own transform, which
        is the neutral state and not something a key needs to assert.
        """
        row = self.placement_row()
        if row < 0:
            return []
        keys = []
        for index, frame in enumerate(clip["frames"]):
            # A placement can be absent from some frames; skip those rather than
            # abandoning the scan, so the rest of the timeline is still reported.
            if row >= len(frame["elements"]):
                continue
            element = frame["elements"][row]
            if element[1] != 65535 or element[2] != 65535:
                keys.append(index)
        return keys

    # --- keyframe strip -----------------------------------------------------

    def refresh_keyframe_strip(self):
        """Push the timeline and every child's keys to the animator."""
        if not (
            self.selected in self.doc.index
            and self.doc.index[self.selected][0] == "movieclips"
        ):
            self.keyframes.set_state(0, 0, [], -1)
            return
        clip = self.obj
        tracks = []
        for row, child in enumerate(clip.get("children") or []):
            name = child.get("name") or ""
            if not name:
                kind = self.doc.index.get(child["id"], ("resource", None))[0]
                name = f"{kind} {child['id']}" if kind != "resource" else str(child["id"])
            tracks.append(
                {"row": row, "label": name, "keys": self.keys_on_row(clip, row)}
            )
        self.keyframes.set_state(
            len(clip["frames"]), self.cursor.value(), tracks, self.placement_row()
        )

    def keys_on_row(self, clip, row):
        """Frames where the given child row's transform actually changes.

        A frame holding no transform of its own inherits the last one set, so it
        is never a key. Only a frame that carries a transform different from the
        one already in force starts a new key, which is why a still hold shows a
        single key instead of one per frame.
        """
        bank = self.clip_bank(clip)
        matrices = bank.get("matrices") or []
        colours = bank.get("colors") or []
        keys = []
        current = None
        for index, frame in enumerate(clip["frames"]):
            if row >= len(frame["elements"]):
                continue
            element = frame["elements"][row]
            if element[1] == 65535 and element[2] == 65535:
                # Nothing set here, so this frame inherits and changes nothing.
                continue
            matrix = (
                list(matrices[element[1]])
                if element[1] != 65535 and element[1] < len(matrices)
                else None
            )
            colour = (
                list(colours[element[2]])
                if element[2] != 65535 and element[2] < len(colours)
                else None
            )
            state = (matrix, colour)
            if state != current:
                keys.append(index)
                current = state
        if not keys and clip["frames"]:
            # The first frame is the state everything else inherits from, so it
            # is shown as the base key even when nothing assigns one.
            keys.append(0)
        return keys

    def move_keypoint_on_track(self, source, target, row):
        """Move a key on a specific track, not only the selected one."""
        if self.elements.currentRow() != row:
            self.select_track(row)
        self.move_keypoint(source, target, row)

    def keyframe_selection_changed(self):
        """Keep the tween button in step with the selected keys."""
        keys = self.selected_key_frames()
        if keys:
            self.bake_button.setText(
                f"Curve between {len(keys)} key{'s' if len(keys) != 1 else ''}"
            )
        else:
            self.bake_button.setText("Curve between all keys")
        self.update_curve_info()

    def selected_key_frames(self):
        """Frames of the keypoints selected on the current track."""
        row = self.placement_row()
        return sorted(
            frame
            for frame, chosen_row in self.keyframes.selected_keys()
            if chosen_row == row
        )

    def keyed_matrix_at(self, clip, frame, row):
        """The placement's keyed transform, or None if it has no key there."""
        if not 0 <= frame < len(clip["frames"]):
            return None
        elements = clip["frames"][frame]["elements"]
        if row >= len(elements) or elements[row][1] == 65535:
            return None
        matrices = self.clip_bank(clip).get("matrices") or []
        index = elements[row][1]
        return list(matrices[index]) if index < len(matrices) else None

    def key_frames(self, clip, row, frames=None):
        """Split requested frames into the ones to write and the ones that are keys.

        A frame already carrying a key is left exactly as it is: writing the
        incoming transform over it would replace that pose, and where the new
        value equals the old the frame would stop reading as a key at all.
        """
        wanted = self.clip_frames(clip) if frames is None else sorted(set(frames))
        already = set(self.keyed_frames(clip, row))
        return (
            [f for f in wanted if 0 <= f < len(clip["frames"]) and f not in already],
            [f for f in wanted if 0 <= f < len(clip["frames"]) and f in already],
        )

    def keyed_frames(self, clip, row):
        """Frames where the placement carries a transform of its own."""
        keys = []
        for frame in self.clip_frames(clip):
            if self.keyed_matrix_at(clip, frame, row) is not None:
                keys.append(frame)
        return keys

    def curve_names(self):
        """Every easing curve an agent may ask for."""
        return list(easing.PRESETS)

    def update_curve_info(self):
        """Describe what applying the curve would affect, next to the button."""
        if not self.animatable():
            self.curve_info.setText("Select a keypoint or a placement to tween.")
            return
        keys = self.placement_keys(self.obj)
        if len(keys) < 2:
            self.curve_info.setText(
                "Set a keyframe on another frame to tween between them."
            )
            return
        chosen = self.selected_key_frames()
        span_keys = chosen if len(chosen) >= 2 else sorted(keys)
        dense = self.key_density.value()
        low, high = span_keys[0], span_keys[-1]
        # Counted the same way the bake writes, gap by gap, so the number here
        # is the number of frames that will actually be filled rather than an
        # approximation that drifts from it.
        # A frame that is already a key is one the curve will pass through but
        # not write, so it is not counted; the description then says how many
        # frames are left to fill, which is what the button reports afterwards
        # rather than the size of the gap.
        existing = set(keys)
        estimate = 0
        gaps = 0
        for start, end in zip(span_keys, span_keys[1:]):
            span = end - start
            if span < 2:
                continue
            gaps += 1
            step = max(1, min(span - 1, round(span / dense) if dense else 1))
            estimate += sum(
                1
                for index in range(1, span, step)
                if start + index not in existing
            )
        scope = (
            f"the {len(chosen)} keys you picked"
            if len(chosen) >= 2
            else f"all {len(keys)} keys"
        )
        self.curve_info.setText(
            f"Curve '{self.curve.currentText()}' between {scope} writes "
            f"{estimate} frame{'' if estimate == 1 else 's'} across "
            f"{low + 1}-{high + 1}{'' if gaps == 1 else f' in {gaps} gaps'}."
        )

    def selected_key_range(self):
        """The keyed frames inside the strip's selection, in order."""
        if self.selected not in self.doc.index:
            return []
        keys = self.placement_keys(self.obj)
        frames = self.keyframes.selected_frames()
        if frames:
            keys = [k for k in keys if k in set(frames)]
        return sorted(keys)

    def apply_curve_to_range(self):
        """Fill the gaps between the selected keys with the chosen curve.

        The selection is the keys themselves, not a range of frames: pick two
        keys and everything between them is tweened, pick several and each gap
        between them is filled. The keys are kept exactly and only the frames
        between them are written, at the interval "Keys to create" asks for.
        """
        if not self.animatable() or not self.resolve_drafts():
            return
        clip = self.doc.resource(self.selected)
        row = self.placement_row()
        found = self.key_transforms(clip)
        if len(found) < 2:
            self.window().statusBar().showMessage(
                "Set at least two keyframes to tween between", 5000
            )
            return
        chosen = sorted(
            key for key in self.keyframes.selected_keys() if key[0] in found
        )
        # Keys selected on this track; fall back to the whole key set when
        # nothing has been picked, which is the usual "tween it all" case.
        frames = sorted({frame for frame, chosen_row in chosen if chosen_row == row})
        if len(frames) < 2:
            frames = sorted(found)
        if len(frames) < 2:
            self.window().statusBar().showMessage(
                "Select at least two keys to tween between", 5000
            )
            return

        points = easing.curve_for(self.curve.currentText())
        wanted = self.key_density.value()
        written = set()
        # Frames carrying the author's own key. An intermediate one is still
        # given the curve's value, but it is never recorded as written, so
        # undoing the curve leaves it in place rather than deleting it.
        keys = set(found)
        gaps = 0
        pending = []
        for start_frame, end_frame in zip(frames, frames[1:]):
            span = end_frame - start_frame
            if span < 2:
                # Two keys side by side have no room for a tween between them,
                # which is not the same as the curve having nothing to do.
                continue
            gaps += 1
            # A step longer than the gap would write nothing at all, leaving
            # the frames between the keys holding the start pose; the gap is
            # filled instead, at one step per frame.
            step = max(1, min(span - 1, round(span / wanted) if wanted else 1))
            offset = 1
            while offset < span:
                index = start_frame + offset
                if index < end_frame and index < len(clip["frames"]):
                    # A placement can be missing from the frames in between even
                    # though it is keyed on both sides: the frames between two
                    # keys hold whatever the frame before them held, and that
                    # may be nothing at all. The frame is given the placement
                    # here so the tween has something to draw, which is the only
                    # way a curve can be authored across such a gap.
                    if not self.ensure_placement(clip, index, row):
                        self.window().statusBar().showMessage(
                            f"Frame {index + 1} has no child to place, so the "
                            "tween stops there",
                            5000,
                        )
                        break
                    if index in keys:
                        # A key the author set between the two ends is left
                        # exactly as it is. Writing the curve's value over it
                        # would replace that pose, and where the curve has
                        # flattened the frame would stop reading as a key at
                        # all, which looked like the curve deleting keys.
                        offset += step
                        continue
                    progress = offset / span
                    eased = easing.bezier(progress, *points)
                    # Collected first and interned in one batch below: appending
                    # one matrix per frame rewrites the whole bank each time,
                    # which is quadratic in the length of the tween.
                    pending.append((
                        index,
                        easing.interpolate_matrix(
                            found[start_frame][0], found[end_frame][0], eased
                        ),
                    ))
                    written.add(index)
                offset += step
        if pending:
            indices, rows = self.intern_matrices(clip, [m for _, m in pending])
            for (index, _), position in zip(pending, indices):
                clip["frames"][index]["elements"][row][1] = position
        if not gaps:
            self.window().statusBar().showMessage(
                f"{len(frames)} keys selected, with no frames to tween between",
                6000,
            )
            return
        if not written:
            # The gaps were already filled at this density, so nothing is
            # written and a previous bake is left as it was; re-running at a
            # different density still fills the frames the curve skipped.
            self.window().statusBar().showMessage(
                f"The gaps between {len(frames)} keys are already filled; "
                "raise Keys to add more frames",
                6000,
            )
            return
        if self.mutate(lambda: self.bake_and_compact(clip, row)):
            self.baked = (self.selected, row, written)
            self.refresh()
            self.elements.selectRow(row)
            self.window().statusBar().showMessage(
                f"Applied '{self.curve.currentText()}' between {len(frames)} keys, "
                f"writing {len(written)} frame{'s' if len(written) != 1 else ''} "
                f"({frames[0] + 1}-{frames[-1] + 1})",
                6000,
            )
    def keypoint_context_menu(self, index, row, position):
        """Actions for one keyframe, on the track it was clicked on."""
        if self.elements.currentRow() != row:
            self.select_track(row)
        menu = QMenu(self)
        menu.addAction("Remove this keypoint").triggered.connect(
            lambda: self.toggle_keypoint_at(index, row)
        )
        menu.addAction("Go to this frame").triggered.connect(
            lambda: self.goto_frame(index)
        )
        menu.addAction("Select up to this key").triggered.connect(
            lambda: self.keyframes.set_band(
                min(self.placement_keys(self.obj) or [index]), index
            )
        )
        menu.addAction("Delete this frame").triggered.connect(
            lambda: self.remove_frame_at(index)
        )
        menu.exec(position)

    def keyframe_context_menu(self, frames, position):
        """Frame and tween operations for the selected range of frames."""
        if self.selected not in self.doc.index:
            return
        frames = list(frames) or [self.cursor.value()]
        menu = QMenu(self)
        keyed = [f for f in frames if f in self.placement_keys(self.doc.resource(self.selected))]
        if keyed:
            menu.addAction(f"Remove {len(keyed)} keypoint(s)").triggered.connect(
                lambda: self.remove_keypoints(keyed)
            )
        else:
            menu.addAction("Add keypoint here").triggered.connect(
                lambda: self.toggle_keypoint_at(frames[0], self.placement_row())
            )
        if len(frames) > 1:
            menu.addAction("Select these frames").triggered.connect(
                lambda: self.keyframes.set_band(frames[0], frames[-1])
            )
        menu.addSeparator()
        tween = menu.addMenu("Apply curve to selection")
        for name in easing.PRESETS:
            tween.addAction(name).triggered.connect(
                lambda checked=False, value=name: self.apply_curve_named(value, frames)
            )
        menu.addSeparator()
        menu.addAction("Go to first frame").triggered.connect(
            lambda: self.goto_frame(frames[0])
        )
        menu.addAction("Add frame after").triggered.connect(
            lambda: self.add_frame_at(frames[0], False)
        )
        menu.addAction("Duplicate this frame").triggered.connect(
            lambda: self.add_frame_at(frames[0], True)
        )
        count = len(self.obj.get("frames") or [])
        remove = menu.addAction("Delete this frame")
        remove.setEnabled(count > 1)
        remove.triggered.connect(lambda: self.remove_frame_at(frames[0]))
        menu.exec(position)

    def apply_curve_named(self, name, frames):
        """Apply a named curve to a range, from the strip's context menu."""
        self.curve.setCurrentText(name)
        self.keyframes.set_band(frames[0], frames[-1])
        self.apply_curve_to_range()

    def remove_keypoints(self, frames):
        """Drop keys on the given frames for the selected placement."""
        if not self.resolve_drafts() or self.selected not in self.doc.index:
            return
        clip = self.doc.resource(self.selected)
        row = self.placement_row()
        removed = 0
        for index in frames:
            if not 0 <= index < len(clip["frames"]):
                continue
            if row >= len(clip["frames"][index]["elements"]):
                continue
            element = clip["frames"][index]["elements"][row]
            if element[1] == 65535 and element[2] == 65535:
                continue
            element[1] = 65535
            element[2] = 65535
            removed += 1
        if not removed:
            return
        if self.mutate(lambda: self.doc.update("movieclips", clip)):
            self.refresh()
            self.elements.selectRow(row)
            self.window().statusBar().showMessage(f"Removed {removed} keys", 4000)

    def select_all_keys(self):
        """Select from the first to the last key, so a whole tween is one gesture."""
        if self.selected in self.doc.index:
            self.keyframes.select_keys()

    def delete_selected_keys(self):
        """Delete every key the timeline has selected; True if any were.

        The selection can be keys picked one at a time or a range dragged out
        along a track, so this reads the selection rather than the cursor: one
        press removes the lot, on every track that was touched.
        """
        if not (
            self.selected in self.doc.index
            and self.doc.index[self.selected][0] == "movieclips"
        ):
            return False
        picked = self.keyframes.selected_keys()
        if not picked:
            # Nothing keyed is picked, so the frames themselves are what the
            # selection means: a range marked out on the ruler deletes those
            # frames, which is how a stretch of the timeline is taken out.
            frames = self.keyframes.selected_frames()
            if frames:
                return self.remove_frames(frames)
            return False
        clip = self.doc.resource(self.selected)
        removed = 0
        for index, row in sorted(picked):
            if not 0 <= index < len(clip["frames"]):
                continue
            if row >= len(clip["frames"][index]["elements"]):
                continue
            element = clip["frames"][index]["elements"][row]
            if element[1] == 65535 and element[2] == 65535:
                continue
            element[1] = 65535
            element[2] = 65535
            removed += 1
        if not removed:
            return False
        if self.mutate(lambda: self.doc.update("movieclips", clip)):
            self.keyframes.picked_keys = set()
            self.refresh()
            self.window().statusBar().showMessage(
                f"Removed {removed} key{'s' if removed != 1 else ''}", 4000
            )
        return True

    def toggle_keypoint_at(self, index, row):
        """Add a keypoint on a frame, or remove the one already there."""
        if not (
            self.selected in self.doc.index
            and self.doc.index[self.selected][0] == "movieclips"
            and 0 <= row < self.elements.rowCount()
        ) or not self.resolve_drafts():
            return
        clip = self.doc.resource(self.selected)
        if not 0 <= index < len(clip["frames"]):
            return
        if row >= len(clip["frames"][index]["elements"]):
            self.window().statusBar().showMessage(
                f"This placement is not present in frame {index + 1}", 4000
            )
            return
        element = clip["frames"][index]["elements"][row]

        def change():
            if element[1] != 65535 or element[2] != 65535:
                element[1] = 65535
                element[2] = 65535
                return False
            # A new key starts from the transform the placement already renders
            # with, so adding one never moves the object on screen.
            reference = self.matrix_at(clip, index, row)
            element[1] = self.intern_matrix(clip, reference)
            return True

        if self.mutate(lambda: (change(), self.doc.update("movieclips", clip))[1]):
            self.refresh()
            self.elements.selectRow(row)
            self.preview.selected_placement = row

    def move_keypoint(self, source, target, row=None, delta=None):
        """Drag a keypoint to another frame, carrying its transform with it.

        Dragging a key past the end extends the clip first, so a key can be
        placed on a frame that does not exist yet by moving it there. Several
        keys can be dragged as one selection, in which case they all move by the
        same offset and keep their spacing.
        """
        if not self.animatable() or not self.resolve_drafts():
            return
        if target >= len(self.doc.resource(self.selected)["frames"]):
            self.extend_to_frame(target)
            if not self.resolve_drafts():
                return
        clip = self.doc.resource(self.selected)
        row = self.placement_row() if row is None else row
        delta = target - source if delta is None else delta
        if not delta:
            return

        # Every key being dragged, on every track: either the whole selection,
        # or just the one that was grabbed.
        picking = self.keyframes.selected_keys()
        moving = (
            sorted(picking)
            if picking and (source, row) in picking
            else [(source, row)]
        )
        landed = {(frame + delta, kind) for frame, kind in moving}
        if min(frame for frame, _ in landed) < 0:
            return
        # A key dropped on top of one that is staying put would replace that
        # key's pose if written straight over it. For a single key that is the
        # gesture of putting two keys in each other's place, so the two poses
        # are exchanged and both survive. A block of keys has no such reading -
        # a rigid shift landing on other keys is genuinely ambiguous - so a
        # block that collides keeps the refusal, as does anything that is not a
        # clean one-for-one swap.
        staying = {
            (frame, kind)
            for _, kind in moving
            for frame in self.keys_on_row(clip, kind)
            if (frame, kind) not in set(moving)
        }
        blocked = landed & staying
        swap = None
        if blocked:
            if len(moving) == 1 and len(blocked) == 1:
                swap = next(iter(blocked))
            else:
                self.window().statusBar().showMessage(
                    f"Frame {min(frame for frame, _ in blocked) + 1} already has a "
                    "key; the move was left where it was",
                    4000,
                )
                return

        bank = self.clip_bank(clip)
        colours = bank.get("colors") or []
        # Every pose that moves, as (source frame, destination frame, track). A
        # swap adds the key that was landed on, travelling the other way to the
        # frame the dragged key came from.
        transfers = [(frame, frame + delta, kind) for frame, kind in moving]
        if swap is not None:
            transfers.append((swap[0], moving[0][0], swap[1]))
        # Read every pose before anything is written, since the source frames
        # are cleared as part of the same change.
        poses = {}
        for source_frame, _, kind in transfers:
            if not 0 <= source_frame < len(clip["frames"]):
                return
            if kind >= len(clip["frames"][source_frame]["elements"]):
                return
            origin = clip["frames"][source_frame]["elements"][kind]
            if origin[1] == 65535 and origin[2] == 65535:
                continue
            poses[(source_frame, kind)] = (
                self.matrix_at(clip, source_frame, kind),
                list(colours[origin[2]])
                if origin[2] != 65535 and origin[2] < len(colours)
                else None,
            )

        def change():
            for source_frame, _, kind in transfers:
                clip["frames"][source_frame]["elements"][kind][1] = 65535
                clip["frames"][source_frame]["elements"][kind][2] = 65535
            for source_frame, after, kind in transfers:
                pose = poses.get((source_frame, kind))
                if pose is None:
                    continue
                matrix, colour = pose
                destination = clip["frames"][after]["elements"][kind]
                destination[1] = self.intern_matrix(clip, matrix)
                if colour is not None:
                    destination[2] = self.intern_color(clip, colour)

        # The selection follows the dragged key to wherever it ended up: the
        # frame it was dropped on, not the frame it started from.
        settled = {(swap[0], swap[1])} if swap is not None else landed
        if self.mutate(lambda: (change(), self.doc.update("movieclips", clip))[1]):
            self.keyframes.picked_keys = settled
            self.refresh()
            self.elements.selectRow(row)
            self.preview.selected_placement = row
            if swap is not None:
                self.window().statusBar().showMessage(
                    f"Swapped keys at frames {source + 1} and {swap[0] + 1}",
                    4000,
                )
            else:
                moved = len(poses)
                self.window().statusBar().showMessage(
                    f"Moved {moved} key{'s' if moved != 1 else ''} to start at "
                    f"frame {min(f for f, _ in landed) + 1}",
                    4000,
                )

    def key_transforms(self, clip):
        """Matrix and colour per keyed frame, for the selected placement."""
        bank = self.clip_bank(clip)
        colours = bank.get("colors") or []
        matrices = bank.get("matrices") or []
        found = {}
        for index in self.placement_keys(clip):
            element = clip["frames"][index]["elements"][self.placement_row()]
            matrix = (
                list(matrices[element[1]])
                if element[1] != 65535 and element[1] < len(matrices)
                else list(easing.IDENTITY)
            )
            colour = (
                list(colours[element[2]])
                if element[2] != 65535 and element[2] < len(colours)
                else None
            )
            found[index] = (matrix, colour)
        return found

    def load_animation_fields(self):
        """Show the placement's transform on this frame in the editor fields."""
        enabled = self.animatable()
        for box in self.key_fields.values():
            box.setEnabled(enabled)
        if not enabled:
            self.animation_hint.setText(
                "Select a movieclip placement to animate it."
            )
            return
        clip = self.obj
        # Past the end the frame does not exist yet, so the last one is shown:
        # the frames a clip is about to gain are copies of it anyway, which is
        # the transform a change made out here starts from.
        frames = clip["frames"]
        index = min(self.cursor.value(), len(frames) - 1)
        beyond = self.cursor.value() >= len(frames)
        element = frames[index]["elements"][self.placement_row()]
        matrix = self.effective_matrix(clip, element)
        parts = easing.decompose(matrix)
        values = {
            "X": parts["tx"],
            "Y": parts["ty"],
            "Rotation": math.degrees(parts["rotation"]),
            "Scale X": parts["scale_x"],
            "Scale Y": parts["scale_y"],
        }
        for name, value in values.items():
            with QSignalBlocker(self.key_fields[name]):
                self.key_fields[name].setValue(value)
        keyed = element[1] != 65535
        if beyond:
            # The cursor is out in the space past the clip: a change here is
            # what adds the frames, so the hint says so rather than reporting on
            # the key of a frame that does not exist.
            self.animation_hint.setText(
                f"Frame {self.cursor.value() + 1}: past the end — a change here "
                f"adds {self.cursor.value() + 1 - len(frames)} frames."
            )
            return
        self.animation_hint.setText(
            f"Frame {self.cursor.value() + 1}: "
            + ("already has a key." if keyed else "no key yet; applying creates one.")
        )

    def apply_animation_fields(self):
        """Key the edited transform on the current frame.

        Editing the transform of a placement that shares a matrix with other
        frames would change those frames too, so this always gives the frame its
        own key rather than reusing the shared one.
        """
        if not self.animatable() or not self.resolve_drafts():
            return
        clip = self.doc.resource(self.selected)
        row = self.placement_row()
        index = self.cursor.value()
        # The clip is grown to hold the cursor before the key is written, so
        # keying past the end extends the animation rather than doing nothing.
        self.grow_clip(clip, index)
        if row >= len(clip["frames"][index]["elements"]):
            self.error(ValueError("that placement is not in this frame"))
            return
        element = clip["frames"][index]["elements"][row]
        matrix = easing.compose(
            {
                "tx": self.key_fields["X"].value(),
                "ty": self.key_fields["Y"].value(),
                "rotation": math.radians(self.key_fields["Rotation"].value()),
                "scale_x": self.key_fields["Scale X"].value(),
                "scale_y": self.key_fields["Scale Y"].value(),
            }
        )

        def change():
            element[1] = self.intern_matrix(clip, matrix)

        if self.mutate(lambda: (change(), self.doc.update("movieclips", clip))[1]):
            self.refresh()
            self.elements.selectRow(row)
            self.preview.selected_placement = row
            self.window().statusBar().showMessage(
                f"Key set on frame {index + 1}", 4000
            )

    def refresh_keypoints(self):
        """Redraw the curve preview; the timeline itself shows the keys."""
        self.draw_curve(self.curve.currentText())
        self.update_curve_info()

    def animatable(self):
        """Whether the current frame holds a placement the strip can key."""
        if self.selected not in self.doc.index:
            return False
        if self.doc.index[self.selected][0] != "movieclips":
            return False
        frames = self.obj.get("frames") or []
        if not frames:
            return False
        # Scrubbing past the last frame is allowed, and a change made there is
        # what extends the clip, so the cursor being beyond the end is not a
        # reason to refuse: the clip is grown to meet it. Only the cursor going
        # backwards past the first frame is out of range.
        index = min(self.cursor.value(), len(frames) - 1)
        if index < 0:
            return False
        reference = (
            frames[self.cursor.value()]
            if self.cursor.value() < len(frames)
            else frames[-1]
        )
        return 0 <= self.placement_row() < len(reference["elements"])

    def draw_curve(self, name):
        """Render the selected curve so its shape is visible before baking."""
        width, height = 110, 34
        image = QImage(width, height, QImage.Format.Format_ARGB32)
        image.fill(QColor("transparent"))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor("#6b7688"), 1))
        painter.drawRect(0, 0, width - 1, height - 1)
        points = easing.sample(name, width)
        # Overshoot and anticipate leave 0..1, so the plot is padded to keep the
        # whole curve on screen.
        low = min([0.0] + points)
        high = max([1.0] + points)
        span = max(high - low, 1e-6)
        path = QPainterPath()
        for x, value in enumerate(points):
            y = height - 2 - (value - low) / span * (height - 4)
            if x == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        painter.setPen(QPen(QColor("#63b5ff"), 2))
        painter.drawPath(path)
        painter.end()
        self.curve_preview.setPixmap(QPixmap.fromImage(image))

    def set_keypoint(self):
        """Give the current frame its own transform, creating a keypoint."""
        if not self.animatable() or not self.resolve_drafts():
            return
        clip = self.doc.resource(self.selected)
        row = self.placement_row()
        # Keying past the end is one of the ways a clip grows, so the frames are
        # added first rather than the key being dropped for being out of range.
        self.grow_clip(clip, self.cursor.value())
        if row >= len(clip["frames"][self.cursor.value()]["elements"]):
            self.error(ValueError("that placement is not in this frame"))
            return
        element = clip["frames"][self.cursor.value()]["elements"][row]
        matrix = self.effective_matrix(clip, element)
        if self.mutate(lambda: self.assign_matrix(clip, element, matrix)):
            self.refresh()
            self.elements.selectRow(row)
            self.preview.selected_placement = row
            self.window().statusBar().showMessage(
                f"Key set on frame {self.cursor.value() + 1}", 4000
            )

    def delete_keypoint(self):
        """Drop the current frame's key, so the frame inherits again."""
        if not self.animatable() or not self.resolve_drafts():
            return
        clip = self.doc.resource(self.selected)
        row = self.placement_row()
        element = clip["frames"][self.cursor.value()]["elements"][row]
        if element[1] == 65535 and element[2] == 65535:
            self.window().statusBar().showMessage(
                f"Frame {self.cursor.value() + 1} has no key", 4000
            )
            return
        element[1] = 65535
        element[2] = 65535
        if self.mutate(lambda: self.doc.update("movieclips", clip)):
            self.refresh()
            self.elements.selectRow(row)

    def clear_keypoints(self):
        if not self.animatable() or not self.resolve_drafts():
            return
        clip = self.doc.resource(self.selected)
        row = self.placement_row()
        keys = self.placement_keys(clip)
        if not keys:
            return
        for index in keys:
            element = clip["frames"][index]["elements"][row]
            element[1] = 65535
            element[2] = 65535
        if self.mutate(lambda: self.doc.update("movieclips", clip)):
            self.refresh()
            self.elements.selectRow(row)
            self.window().statusBar().showMessage(f"Cleared {len(keys)} keys", 4000)

    def bake_tween(self):
        """Curve the whole key range, ignoring the strip's current selection."""
        self.keyframes.clear_band()
        self.apply_curve_to_range()

    def bake_and_compact(self, clip, row):
        """Compact the baked bank once, then store the clip.

        The last real frame is the one compacted: the cursor may be sitting out
        in the space past the end, where there is no frame to refresh.
        """
        index = min(self.cursor.value(), len(clip["frames"]) - 1)
        if index < 0 or row >= len(clip["frames"][index]["elements"]):
            self.doc.update("movieclips", clip)
            return
        element = clip["frames"][index]["elements"][row]
        self.assign_matrix(clip, element, self.effective_matrix(clip, element))

    def revert_tween(self):
        """Drop exactly the frames the last bake wrote for this placement.

        Only frames recorded by bake_tween are touched. Inferring them from the
        key layout instead would be wrong: a clip keyed on every frame looks
        identical to a baked one, and guessing would delete the author's own
        keyframes.
        """
        if not self.resolve_drafts():
            return
        if self.selected not in self.doc.index:
            return
        if self.baked is None or self.baked[0] != self.selected:
            self.window().statusBar().showMessage(
                "No baked tween recorded for this clip; bake one first", 5000
            )
            return
        _, row, frames = self.baked
        clip = self.doc.resource(self.selected)
        removed = 0
        for index in sorted(frames):
            if not 0 <= index < len(clip["frames"]):
                continue
            if row >= len(clip["frames"][index]["elements"]):
                continue
            element = clip["frames"][index]["elements"][row]
            if element[1] == 65535 and element[2] == 65535:
                continue
            element[1] = 65535
            element[2] = 65535
            removed += 1
        if not removed:
            self.window().statusBar().showMessage("Baked frames are already clear", 4000)
            return
        if self.mutate(lambda: self.doc.update("movieclips", clip)):
            self.baked = None
            self.refresh()
            self.elements.selectRow(row)
            self.window().statusBar().showMessage(
                f"Reverted {removed} baked frames", 4000
            )

    def animation_dialog(self, frames, fps):
        """Ask how to name and time an imported animation.

        The frame names are built as prefix + number + suffix so a run of
        imported frames can be told apart in the child list and the browser
        without renaming them one at a time afterwards.
        """
        dialog = QDialog(self)
        dialog.setWindowTitle("Import animation")
        form = QFormLayout(dialog)
        prefix = QLineEdit("frame")
        suffix = QLineEdit("")
        rate = QDoubleSpinBox()
        rate.setRange(1.0, 120.0)
        rate.setDecimals(2)
        rate.setValue(fps)
        form.addRow("Name prefix", prefix)
        form.addRow("Name suffix", suffix)
        form.addRow("Framerate", rate)
        form.addRow(
            QLabel(
                f"{len(frames)} frames will each become a movieclip frame, named\n"
                "prefix + frame number + suffix, packed into one texture atlas."
            )
        )
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return {
            "prefix": prefix.text(),
            "suffix": suffix.text(),
            "fps": rate.value(),
        }

    def import_animation(self):
        """Import a GIF or animated WebP as a movieclip animation.

        Every source frame becomes a movieclip frame showing that frame's
        image, packed into a single atlas with one shape per frame. The frames
        are named from the dialog so a run of them stays legible, and any
        placements already in the clip are kept alongside the new images.
        """
        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "movieclips"
        ):
            self.error("Select or create a movieclip first, then import an animation.")
            return
        if not self.resolve_drafts():
            return
        path, _ = app.native_picker(
            self,
            "Import animation into movieclip",
            "",
            "Animations (*.gif *.webp *.apng *.png)",
        )
        if not path:
            return
        try:
            frames, duration = textures.decode_animation(path)
            if len(frames) < 2:
                self.error(
                    ValueError(
                        "that file holds a single frame; use Import image instead"
                    )
                )
                return
            default_fps = 1000.0 / (duration / len(frames)) if duration else 30.0
            options = self.animation_dialog(frames, min(120.0, max(1.0, default_fps)))
            if options is None:
                return
            self.insert_animation(frames, options, Path(path).stem)
        except Exception as ex:
            self.error(ex)

    def insert_animation(self, frames, options, stem="animation", raise_errors=False):
        """Place decoded frames into the open movieclip as a keyed animation.

        Every frame becomes a movieclip frame showing that frame's image, packed
        into a single atlas with one shape per frame. Any placements a frame
        already had are kept, so importing into a clip that is already rigged
        adds the animation rather than replacing it.
        """
        if (
            self.selected not in self.doc.index
            or self.doc.index[self.selected][0] != "movieclips"
        ):
            raise ValueError("Select a movieclip to import an animation into")
        placements, sheet, sheet_w, sheet_h = textures.pack_frames(frames)
        if sheet is None:
            return
        data = textures.frame_atlas_bytes(sheet)
        clip = self.doc.resource(self.selected)
        base = len(clip["children"])

        def change():
            atlas = len(self.doc.tables["Textures"]["textures"])
            self.doc.tables["Textures"]["textures"].append(
                {"highres": data, "lowres": None}
            )
            self.doc.dirty.add("Textures")
            for index, frame in enumerate(frames):
                px, py = placements[index]
                name = (
                    f"{options['prefix']}{index}{options['suffix']}"
                    or f"{stem}{index}"
                )
                shape_id = self.doc.new_resource("shapes")
                w, h = frame.width, frame.height
                self.doc.update(
                    "shapes",
                    {
                        "id": shape_id,
                        "exports": [],
                        "commands": [
                            {
                                "unk1": 0,
                                "texture_index": atlas,
                                # Quad at the origin; only the UVs point into the
                                # frame's slice of the atlas, so the placement
                                # needs no offset to show it.
                                "vertices": [
                                    [0, 0, _uv(px, sheet_w), _uv(py, sheet_h)],
                                    [w, 0, _uv(px + w, sheet_w), _uv(py, sheet_h)],
                                    [0, h, _uv(px, sheet_w), _uv(py + h, sheet_h)],
                                    [
                                        w,
                                        h,
                                        _uv(px + w, sheet_w),
                                        _uv(py + h, sheet_h),
                                    ],
                                ],
                            }
                        ],
                    },
                )
                clip["children"].append({"id": shape_id, "name": name, "blend": 0})
                while len(clip["frames"]) <= index:
                    clip["frames"].append({"label": "", "elements": []})
                frame_slot = clip["frames"][index]
                # Only this frame's image is added here, so a frame shows
                # exactly one picture while anything already placed stays.
                frame_slot["elements"] = list(frame_slot["elements"]) + [
                    [base + index, 65535, 65535]
                ]
                frame_slot["label"] = name
            clip["framerate"] = int(round(options["fps"]))
            self.doc.update("movieclips", clip)

        if self.mutate(change, raise_errors=raise_errors):
            self.refresh()
            self.set_panel(0)
            self.frames.selectRow(0)
            self.render_frame(fit=True)
            self.window().statusBar().showMessage(
                f"Imported {len(frames)} frames at {int(round(options['fps']))} fps",
                6000,
            )
