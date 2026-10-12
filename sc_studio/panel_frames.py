"""The frames panel's Editor methods, moved verbatim."""

import copy
import time
from PyQt6.QtCore import (
    QSignalBlocker,
)


class FramesPanel:

    def frames_context_menu(self, position):
        self.list_context_menu(
            self.frames,
            position,
            [
                ("New frame", lambda: self.add_frame(False)),
                ("Duplicate frame", lambda: self.add_frame(True)),
                ("Delete frame", self.remove_frame),
            ],
        )

    def render_frame(self, value=None, fit=False):
        if self.texture_index is not None:
            if fit:
                self.preview.fit_content()
            return
        if self.loading or self.selected not in self.doc.index:
            return
        if (
            self.doc.index[self.selected][0] == "movieclips"
            and self.frames.currentRow() != self.cursor.value()
        ):
            with QSignalBlocker(self.frames):
                self.frames.selectRow(self.cursor.value())
            if not self.timer.isActive():
                self.show_elements()
        # The clip's own length, not the slider's: the slider reaches a little
        # past the end so the cursor can go out there and extend it, and
        # counting that headroom would claim frames the clip does not have.
        clip = self.obj.get("frames")
        count = len(clip) if clip else 1
        with QSignalBlocker(self.frame):
            self.frame.setRange(0, max(0, count - 1))
            self.frame.setValue(min(self.cursor.value(), count - 1))
        beyond = self.cursor.value() >= count
        self.frame_label.setText(
            f"Frame {self.cursor.value()+1} / {count}"
            + (" · past the end" if beyond else "")
        )
        self.preview.hidden_children = self.child_visibility.get(self.selected, set())
        started = time.perf_counter()
        message = self.preview.show_resource(
            self.doc, self.selected, self.cursor.value(), fit=fit or value is None
        )
        self.record_timing("render", time.perf_counter() - started)
        self.notice.setText(
            message
            or "Click / drag a placement to move · Drag empty space to pan · Wheel to zoom"
        )
        if self.timer.isActive():
            # Playback only moves the cursor: the tracks, their keys and the
            # widget layout cannot change on their own, so rebuilding them every
            # frame is work that is thrown away. On a clip with thousands of
            # keys it cost more than drawing the frame did, which is what made
            # playback crawl on a long animation.
            self.keyframes.set_cursor(self.cursor.value())
        else:
            self.refresh_keyframe_strip()
            # The transform fields belong to the current frame, so they follow
            # it. Nothing is editing them mid-playback, so they can wait too.
            self.load_animation_fields()

    def frame_index(self):
        return max(0, self.frames.currentRow())

    def cursor_frame(self, clip):
        """The frame the cursor is on, or ``None`` while it is past the end.

        The cursor is allowed past the last frame — that is where a change
        extends the clip — so code that reads a frame's placements has to cope
        with there being nothing there yet. Every such place goes through this
        rather than indexing the frame list directly, which is what used to
        raise IndexError and take the editor down.
        """
        frames = clip["frames"]
        index = self.cursor.value()
        return frames[index] if 0 <= index < len(frames) else None

    def select_frame(self):
        if self.loading:
            return
        self.show_elements()
        self.cursor.setValue(self.frame_index())

    def extend_to_frame(self, needed):
        """Add frames until the clip is long enough for the frame asked for.

        Scrubbing or dragging a key past the end grows the movieclip, so a
        timeline extends by working on it rather than by pre-adding frames.
        """
        if not self.animatable():
            return
        clip = self.doc.resource(self.selected)
        self.grow_clip(clip, needed)
        if self.mutate(lambda: self.doc.update("movieclips", clip)):
            self.refresh()
            self.cursor.setValue(min(needed, len(clip["frames"]) - 1))

    def goto_frame(self, index):
        """Jump to a frame from the strip, stopping playback first."""
        if self.timer.isActive():
            self.toggle_play()
        index = max(0, index)
        if index >= self.cursor.maximum():
            # Reaching the far end pushes it further out, so scrubbing towards
            # the end of the timeline is never stopped by a fixed limit.
            self.frame_span = max(
                self.frame_span, index + self.FRAME_HEADROOM
            )
            self.cursor.setRange(0, self.frame_span)
        self.cursor.setValue(min(index, self.cursor.maximum()))

    # --- keying helpers, shared with agent access -----------------------------
    #
    # Each of these is the body of the animator's own operation with the widget
    # handling arranged around it, so an agent driving the timeline keys frames
    # exactly the way the buttons do. They are guarded by ``animatable`` rather
    # than assuming a selection, so an out-of-context call is a no-op instead of
    # a crash on stale widget state.

    @staticmethod
    def clip_frames(clip):
        """Frame indices, for defaulting an operation to the whole clip."""
        return list(range(len(clip.get("frames") or [])))

    def remove_frames(self, indexes):
        """Delete a set of frames at once, keeping at least one.

        Every frame the clip must keep is kept, so selecting the whole timeline
        empties it down to a single frame rather than leaving a clip with none.
        """
        clip = self.doc.resource(self.selected)
        targets = sorted(
            {i for i in indexes if 0 <= i < len(clip["frames"])}, reverse=True
        )
        asked = len(targets)
        # The selection cannot take the clip below one frame; the lowest of the
        # overlapping frames are the ones given up.
        while targets and len(targets) >= len(clip["frames"]):
            targets = targets[1:]
        if not targets:
            self.window().statusBar().showMessage(
                "A movieclip must keep at least one frame"
                if asked
                else "No frames selected",
                4000,
            )
            return False

        def change():
            for index in targets:
                clip["frames"].pop(index)

        if self.mutate(lambda: (change(), self.doc.update("movieclips", clip))[1]):
            self.keyframes.clear_band()
            self.refresh()
            self.goto_frame(min(targets[0], len(clip["frames"]) - 1))
            self.window().statusBar().showMessage(
                f"Removed {len(targets)} frame{'s' if len(targets) != 1 else ''}",
                4000,
            )
            return True
        return False

    def add_frame_at(self, index, duplicate):
        """Insert a frame after ``index``, keeping the playhead on the original."""
        self.goto_frame(index)
        self.add_frame(duplicate)

    def remove_frame_at(self, index):
        self.goto_frame(index)
        self.remove_frame()

    def add_frame(self, duplicate):
        i = self.frame_index()
        self.clip_edit(
            lambda o: o["frames"].insert(
                i + 1,
                (
                    copy.deepcopy(o["frames"][i])
                    if duplicate and o["frames"]
                    else {"label": "", "elements": []}
                ),
            )
        )

    def remove_frame(self):
        """Delete the current frame, never the last one a clip must keep."""
        i = self.frame_index()
        if len(self.obj.get("frames") or []) <= 1:
            self.window().statusBar().showMessage(
                "A movieclip must keep at least one frame", 4000
            )
            return
        self.clip_edit(lambda o: o["frames"].pop(i))
