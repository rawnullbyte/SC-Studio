"""Authenticated loopback gateway, with all widget access on the Qt thread."""
import base64
import hmac
import json
import math
import os
import secrets
import threading
import uuid
from concurrent.futures import Future, TimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal, QBuffer, QIODevice
from .document import Document, KINDS
from . import easing


def connection_path():
    return Path(os.environ.get("SC_STUDIO_MCP_CONNECTION",
                               str(Path.home() / ".sc-studio" / "mcp-connection.json"))).expanduser().absolute()


class AgentGateway(QObject):
    requested = pyqtSignal(object)

    def __init__(self, window, path=None):
        super().__init__(window)
        self.window = window
        self.connection_path = Path(path) if path else connection_path()
        self.token = secrets.token_urlsafe(32)
        self.server = None
        self.running = False
        self.requested.connect(self.dispatch)

    def start(self):
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                if self.path != "/rpc":
                    self.send_error(404)
                    return
                if not hmac.compare_digest(self.headers.get("Authorization", ""),
                                           "Bearer " + gateway.token):
                    self.send_error(401)
                    return
                future = Future()
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 16 * 1024 * 1024:
                        raise ValueError("Request size must be between 1 byte and 16 MiB")
                    payload = json.loads(self.rfile.read(size))
                    gateway.requested.emit((payload, future))
                    result = {"result": future.result(timeout=90)}
                except TimeoutError:
                    future.cancel()
                    result = {"error": "Editor request timed out; check whether the app is busy"}
                except Exception as error:
                    result = {"error": str(error)}
                data = json.dumps(result).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}/rpc"
        self.connection_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.connection_path.with_name(self.connection_path.name + "." + self.token[:8])
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump({"url": self.url, "token": self.token}, stream)
            temporary.replace(self.connection_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            self.server.server_close()
            self.server = None
            raise
        self.running = True
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.1), daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        try:
            if json.loads(self.connection_path.read_text())["token"] == self.token:
                self.connection_path.unlink()
        except (OSError, ValueError, KeyError):
            pass

    def client_config(self):
        return {"mcpServers": {"sc-studio": {"command": "sc-studio-mcp",
                "args": ["--connection", str(self.connection_path)]}}}

    def editors(self):
        for i in range(self.window.editors.count()):
            ed = self.window.editors.widget(i)
            if not hasattr(ed, "agent_id"):
                ed.agent_id = uuid.uuid4().hex
            yield ed

    def editor(self, id=None):
        editors = list(self.editors())
        ed = self.window.current() if id is None else next((e for e in editors if e.agent_id == id), None)
        if ed is None:
            raise ValueError("Document is not open; call documents to obtain current document IDs")
        return ed

    def describe(self, ed):
        return {"document_id": ed.agent_id, "path": str(ed.doc.path) if ed.doc.path else None,
                "version": ed.doc.version, "resources": len(ed.doc.index),
                "selected": ed.selected, "dirty": ed.is_dirty(),
                "active": ed is self.window.current()}

    def document_summary(self, doc):
        """A whole-document overview: what exists, what is reusable, how to size.

        Built so one call replaces the reconnaissance an agent otherwise does by
        hand: a per-kind count, the exported names that are the entry points, the
        atlases with their pixel sizes, and the conventions for nine-slice art
        and text, which were previously only discoverable by reading each shape
        and each textfield in turn.
        """
        counts = {kind: 0 for kind in KINDS}
        for kind, _ in doc.index.values():
            counts[kind] = counts.get(kind, 0) + 1
        textures = doc.tables["Textures"]["textures"]
        atlases = []
        for index, texture in enumerate(textures):
            data = texture.get("highres")
            atlases.append({
                "atlas_index": index,
                "width": data.get("width") if isinstance(data, dict) else None,
                "height": data.get("height") if isinstance(data, dict) else None,
            })
        # Exports are the entry points: a shape behind one is a finished piece
        # of art, which is what is worth reusing rather than rebuilding.
        exports = sorted(
            ({"id": id, "exports": names, "kind": doc.index[id][0]}
             for id, names in doc.names.items() if id in doc.index),
            key=lambda row: row["id"],
        )
        grids = [id for id, (kind, raw) in doc.index.items()
                 if kind == "movieclips" and raw.get("scaling_grid")]
        return {
            **self.describe(self.editor()),
            "counts": counts,
            "atlases": atlases,
            "exports": exports,
            "scaling_grid_clips": grids,
            "conventions": {
                "nine_slice": (
                    "There is no nine-slice flag. A nine-slice asset is a "
                    "movieclip whose child shape is already cut into a 3x3 grid "
                    "of commands, placed with an identity matrix; the renderer "
                    "stretches the middle ones. Sizing it means scaling the "
                    "placement, and screen span is shape bounds times that scale."
                ),
                "sizing": (
                    "A shape's bounds come from its commands' vertices, which "
                    "are already in pixels. A sprite drawn from an atlas can be "
                    "sized from its UV rectangle instead, which get_texture and "
                    "atlas_regions report in atlas pixels."
                ),
                "text": (
                    "A textfield stores left/top/right/bottom, font_size, "
                    "font_name, text, align and styles. styles is a bitfield: "
                    "1 device font, 2 outline, 4 bold, 8 italic, 16 multiline, "
                    "64 auto kern. align: 0 left, 1 center, 2 right."
                ),
                "children": (
                    "A movieclip's children are named instances; the name is the "
                    "slot the game's own code looks up, so an existing component "
                    "should be reused by importing it rather than rebuilt."
                ),
                "units": (
                    "Frames and placement indices are zero-based. Matrices are "
                    "[a,b,c,d,tx,ty]. A placement that inherits has no matrix of "
                    "its own and is reported as identity."
                ),
            },
        }

    def atlas_index(self, doc, document_id=None):
        """Every atlas with its size and the distinct shapes drawn from it.

        An atlas holds one region per command, and a full UI sheet can hold tens
        of thousands, so this collapses them per shape: an agent wants to know
        which shape to place, not every rectangle behind it.
        """
        textures = doc.tables["Textures"]["textures"]
        by_atlas = {}
        for id, (kind, raw) in doc.index.items():
            if kind != "shapes":
                continue
            for ci, command in enumerate(raw["commands"]):
                by_atlas.setdefault(command["texture_index"], []).append((id, ci))
        result = []
        for index, texture in enumerate(textures):
            data = texture.get("highres")
            shapes = sorted({id for id, _ in by_atlas.get(index, [])})
            result.append({
                "atlas_index": index,
                "width": data.get("width") if isinstance(data, dict) else None,
                "height": data.get("height") if isinstance(data, dict) else None,
                "shapes": shapes,
                "shape_count": len(shapes),
                "command_count": len(by_atlas.get(index, [])),
            })
        return {"atlases": result}

    def find_components(self, doc, query, with_size, limit):
        """Drawable resources with their size, exported or used as a child.

        In a real UI file the exported names are usually only the screens; the
        panels, buttons and tabs behind them are plain clips named in some other
        clip's children. Both are listed, so a piece that exists can be found
        and reused instead of authored again. Sizes mean a screen can be laid
        out without fetching each candidate in turn.
        """
        needle = query.casefold()
        limit = max(1, min(500, limit))
        # How many clips name each resource as a child, so a component that is
        # actually used somewhere is distinguishable from a leftover.
        used_by = {}
        for id, (kind, raw) in doc.index.items():
            if kind != "movieclips":
                continue
            for child in raw.get("children_ids", []):
                used_by[child] = used_by.get(child, 0) + 1
        rows = []
        for id, (kind, _) in sorted(doc.index.items()):
            if kind not in ("movieclips", "shapes", "textfields"):
                continue
            names = doc.names.get(id, [])
            haystack = " ".join(names).casefold() + " " + str(id)
            if needle and needle not in haystack:
                continue
            row = {
                "id": id,
                "kind": kind,
                "exports": names,
                "used_as_child": used_by.get(id, 0),
            }
            if with_size:
                row["size"] = self.resource_size(doc, id)
            rows.append(row)
            if len(rows) >= limit:
                break
        return {"total": len(rows), "components": rows}

    def resource_size(self, doc, id, seen=None):
        """Rendered size of a resource in pixels, or None when it has none.

        A clip's size is the union of what its first frame draws, a shape's is
        its vertex bounds, a textfield's is its box. This is the one number an
        agent needs to place something without measuring the art itself.
        """
        if id not in doc.index:
            return None
        seen = set() if seen is None else seen
        if id in seen:
            # A clip that draws itself, directly or through another, has no
            # finite size; stopping keeps the walk finite.
            return None
        seen = seen | {id}
        kind, _ = doc.index[id]
        if kind == "shapes":
            # The index holds the file's packed form, so the normalized
            # commands are read to get vertices in pixels.
            return self.vertex_bounds(doc.resource(id, lazy_frames=True))
        if kind == "textfields":
            obj = doc.resource(id, lazy_frames=True)
            left, top = float(obj["left"]), float(obj["top"])
            return [round(float(obj["right"]) - left, 3),
                    round(float(obj["bottom"]) - top, 3)]
        if kind != "movieclips":
            return None
        clip = doc.resource(id, lazy_frames=True)
        if not clip["frames"]:
            return None
        bounds = None
        for child_index, matrix_index, _ in clip["frames"][0]["elements"]:
            if child_index >= len(clip["children"]):
                continue
            child = clip["children"][child_index]
            inner = self.resource_size(doc, child["id"], seen)
            if inner is None:
                continue
            matrix = [1, 0, 0, 1, 0, 0]
            if matrix_index != 65535:
                matrices = doc.bank(clip["matrix_bank_index"])["matrices"]
                if matrix_index < len(matrices):
                    matrix = matrices[matrix_index]
            corners = [
                (x, y)
                for x in (0.0, inner[0])
                for y in (0.0, inner[1])
            ]
            points = [
                (matrix[0] * x + matrix[2] * y + matrix[4],
                 matrix[1] * x + matrix[3] * y + matrix[5])
                for x, y in corners
            ]
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            box = [min(xs), min(ys), max(xs), max(ys)]
            bounds = box if bounds is None else [
                min(bounds[0], box[0]), min(bounds[1], box[1]),
                max(bounds[2], box[2]), max(bounds[3], box[3]),
            ]
        if bounds is None:
            return None
        return [round(bounds[2] - bounds[0], 3), round(bounds[3] - bounds[1], 3)]

    @staticmethod
    def vertex_bounds(raw):
        vertices = [v for command in raw["commands"] for v in command["vertices"]]
        if not vertices:
            return None
        xs = [v[0] for v in vertices]
        ys = [v[1] for v in vertices]
        return [round(max(xs) - min(xs), 3), round(max(ys) - min(ys), 3)]

    def assign_many(self, ed, clip, row, poses):
        """Key many frames in a single edit.

        The per-frame UI path — pick the frame, toggle a key — snapshots the
        document, validates and redraws the preview once per frame, and appends
        to the transform bank by copying the whole bank each time, so keying a
        long animation costs several times the animation itself. Here the
        matrices are appended once, the frames are annotated in one pass, and
        the whole thing is one snapshot, one validation, one render and one undo
        step.
        """
        matrices = list(ed.clip_bank(clip).get("matrices") or [])
        first = len(matrices)
        fresh = []
        for _, matrix in poses:
            matrices.append([float(v) for v in matrix])
            fresh.append(first + len(fresh))
        bank = ed.clip_bank(clip)
        clip["matrix_bank_index"] = ed.doc.add_bank(
            matrices, list(bank.get("colors") or [])
        )
        slot = {tuple(m): i for i, m in enumerate(matrices)}
        for (frame, matrix), fallback in zip(poses, fresh):
            element = clip["frames"][frame]["elements"][row]
            element[1] = slot.get(tuple(float(v) for v in matrix), fallback)

        def change():
            # The bank is rewritten through the document's allocator rather
            # than assigned, so identical banks still collapse and the clip's
            # index stays valid.
            ed.doc.add_bank(
                ed.doc.bank(clip["matrix_bank_index"])["matrices"],
                ed.doc.bank(clip["matrix_bank_index"])["colors"],
            )
            ed.doc.update("movieclips", clip)

        if not ed.mutate(change):
            return False
        ed.cursor.setValue(poses[0][0])
        return True

    @staticmethod
    def coerce_vertex(vertex):
        """One vertex as the file stores it: [x, y, u, v].

        Position is a float in pixels; the texture coordinate is a 16-bit
        unsigned fraction of the atlas, so it is stored as an integer. Callers
        work in fractions and would otherwise be caught out by the storage type.
        """
        x, y, u, v = vertex
        return [float(x), float(y), int(u), int(v)]

    def requested_matrix(self, ed, clip, row, args, base_frame=None, transform=None,
                         scale=None):
        """The transform a call asked for, or None when it only named frames.

        A partial transform is applied on top of what the row already renders on
        the frame it is being computed for: naming only `x` should move the
        placement, not drop its rotation and scale. `scale` is read as a resize
        about the placement's own origin, so the object grows where it stands
        instead of sliding away from the world origin.
        """
        if transform is None:
            transform = args.get("transform")
        if scale is None:
            scale = args.get("scale")
        if transform is None and scale is None:
            return None
        if base_frame is None:
            frames = args.get("frames") or [0]
            base_frame = min(frames) if frames else 0
        if not 0 <= base_frame < len(clip["frames"]):
            raise ValueError("Frame is out of range")
        base = list(ed.matrix_at(clip, base_frame, row))
        if transform is not None:
            if "matrix" in transform:
                matrix = [float(v) for v in transform["matrix"]]
                if len(matrix) != 6:
                    raise ValueError("matrix must be [a,b,c,d,tx,ty]")
                base = matrix
            else:
                parts = easing.decompose(base)
                for name, key in (("x", "tx"), ("y", "ty")):
                    if name in transform:
                        parts[key] = float(transform[name])
                if "rotation" in transform:
                    parts["rotation"] = math.radians(float(transform["rotation"]))
                for name in ("scale_x", "scale_y"):
                    if name in transform:
                        parts[name] = float(transform[name])
                base = easing.compose(parts)
        if scale is not None:
            factor = float(scale) if not isinstance(scale, (list, tuple)) else 1.0
            if not isinstance(scale, (list, tuple)):
                # Scale about the placement's own origin: t' = t + (s-1) * t.
                base = [
                    base[0] * factor, base[1] * factor,
                    base[2] * factor, base[3] * factor,
                    base[4] + (factor - 1.0) * base[4],
                    base[5] + (factor - 1.0) * base[5],
                ]
        if any(not math.isfinite(v) for v in base):
            raise ValueError("Transform values must be finite")
        return base

    def requested_frames_and_matrices(self, ed, clip, row, args):
        """The (frame, matrix) pairs a call asked for, or None for plain keying.

        `transform` may be one mapping applied to every named frame, or
        `transforms` a list parallel to `frames` giving each frame its own. The
        list form is what makes a computed animation one call: a spiral where
        every frame has its own angle and radius is expressible directly,
        instead of one round trip per frame.
        """
        transforms = args.get("transforms")
        single = args.get("transform")
        scale = args.get("scale")
        if transforms is None and single is None and scale is None:
            return None
        frames = args.get("frames")
        if transforms is not None:
            if frames is None:
                raise ValueError("transforms needs frames, one entry per frame")
            if len(transforms) != len(frames):
                raise ValueError(
                    f"transforms has {len(transforms)} entries but frames has "
                    f"{len(frames)}; they must be parallel"
                )
            return [
                (frame, self.requested_matrix(ed, clip, row, args,
                                              base_frame=frame, transform=entry,
                                              scale=None))
                for frame, entry in zip(frames, transforms)
            ]
        if isinstance(scale, (list, tuple)):
            if frames is None or len(scale) != len(frames):
                raise ValueError(
                    "scale as a list needs frames, one factor per frame"
                )
            return [
                (frame, self.scaled_matrix(ed.matrix_at(clip, frame, row), factor))
                for frame, factor in zip(frames, scale)
            ]
        matrix = self.requested_matrix(ed, clip, row, args)
        wanted = frames if frames is not None else ed.clip_frames(clip)
        return [(frame, matrix) for frame in wanted]

    @staticmethod
    def scaled_matrix(matrix, factor):
        """A matrix scaled about the placement's own origin, t' = t + (s-1)*t."""
        factor = float(factor)
        return [
            matrix[0] * factor, matrix[1] * factor,
            matrix[2] * factor, matrix[3] * factor,
            matrix[4] + (factor - 1.0) * matrix[4],
            matrix[5] + (factor - 1.0) * matrix[5],
        ]

    def keyframe_edit(self, ed, operation, args):
        """Key, unkey and tween, through the animator's own operations.

        These run the same methods the timeline's menus and buttons call, so an
        agent's edit and a person's edit are the same edit: the same rules about
        which frames are keys, the same refusal to overwrite a key, the same
        single undo step. The selection the operations act on is the timeline's,
        which is set here from what the call asked for.
        """
        if not ed.animatable():
            raise ValueError(
                "That movieclip has no placement on this row and frame; add a "
                "child with add_child first"
            )
        clip = ed.doc.resource(ed.selected)
        row = args.get("row")
        if row is None:
            row = ed.placement_row()
        if not 0 <= row < len(clip["children"]):
            raise ValueError("Placement row is out of range")
        if row != ed.elements.currentRow():
            ed.select_track(row)
        frames = args.get("frames")

        if operation == "set_keys":
            writes, keys = ed.key_frames(clip, row, frames)
            if args.get("curve") is not None:
                ed.curve.setCurrentText(args["curve"])
                if ed.curve.currentText() != args["curve"]:
                    raise ValueError(f"Unknown curve; choose from {ed.curve_names()}")
            if args.get("key_density") is not None:
                ed.key_density.setValue(int(args["key_density"]))
            # Nothing named means the whole clip, and a tween is the reason to
            # do that, so the frames are limited to where keys already are.
            if frames is None and args.get("curve") is not None:
                writes = [f for f in writes if f in set(ed.keyed_frames(clip, row))]
            poses = self.requested_frames_and_matrices(ed, clip, row, args)
            if poses is None:
                # No transform given: the named frames are keyed at the
                # transform they already render with, so keying never moves
                # anything on screen.
                poses = [(frame, ed.matrix_at(clip, frame, row)) for frame in writes]
            giving_transform = (
                args.get("transform") is not None
                or args.get("transforms") is not None
                or args.get("scale") is not None
            )
            if not args.get("overwrite"):
                # A frame that already carries a key is left exactly as it is,
                # unless the caller asked to replace it.
                poses = [(frame, matrix) for frame, matrix in poses
                         if frame not in set(keys)]
            if giving_transform and not poses and keys and not args.get("overwrite"):
                # Nothing would be written and nothing else was asked for, so
                # say why rather than reporting a silent success.
                raise ValueError(
                    "Every named frame already carries a key; pass "
                    "overwrite=true to give them these transforms instead, or "
                    "clear_keys first"
                )
            if poses:
                if args.get("transform") is not None or args.get("transforms") is not None \
                        or args.get("scale") is not None:
                    # Written in one edit: a snapshot, a validation and a
                    # preview render per frame is what made keying a long
                    # animation cost several times the animation itself.
                    if not self.assign_many(ed, clip, row, poses):
                        return {}
                else:
                    for frame, _ in poses:
                        ed.cursor.setValue(frame)
                        ed.toggle_keypoint_at(frame, row)
                clip = ed.doc.resource(ed.selected)
                if args.get("curve") is not None and len(poses) >= 2:
                    # The named frames are the keys; the curve fills between
                    # them, keeping each one exact.
                    ed.keyframes.pick_keys([(frame, row) for frame, _ in poses])
                    ed.apply_curve_to_range()
            elif args.get("curve") is not None and len(writes) >= 2:
                # The named frames become the keys; the curve fills between them.
                for frame in writes:
                    ed.cursor.setValue(frame)
                    ed.toggle_keypoint_at(frame, row)
                clip = ed.doc.resource(ed.selected)
                ed.keyframes.pick_keys([(frame, row) for frame in writes])
                ed.apply_curve_to_range()
            else:
                for frame in writes:
                    ed.cursor.setValue(frame)
                    ed.toggle_keypoint_at(frame, row)
            clip = ed.doc.resource(ed.selected)
            result_keys = ed.keyed_frames(clip, row)
            return {
                "movieclip_id": ed.selected, "row": row,
                "keys": result_keys, "key_count": len(result_keys),
                "keyed": sorted(set(result_keys) - set(keys)),
                "skipped_existing_keys": sorted(keys),
                "curve": ed.curve.currentText() if args.get("curve") else None,
            }

        if operation == "clear_keys":
            targets = frames if frames is not None else ed.keyed_frames(clip, row)
            ed.remove_keypoints(sorted(targets))
            clip = ed.doc.resource(ed.selected)
            return {
                "movieclip_id": ed.selected, "row": row,
                "keys": ed.keyed_frames(clip, row),
            }

        if operation == "tween":
            picks = args.get("keys")
            if picks is None:
                picks = [(frame, row) for frame in ed.keyed_frames(clip, row)]
            else:
                picks = [tuple(pair) for pair in picks]
            if len(picks) < 2:
                raise ValueError("Select at least two keys to tween between")
            if args.get("curve") is not None:
                ed.curve.setCurrentText(args["curve"])
                if ed.curve.currentText() != args["curve"]:
                    raise ValueError(f"Unknown curve; choose from {ed.curve_names()}")
            if args.get("key_density") is not None:
                ed.key_density.setValue(int(args["key_density"]))
            before = set(ed.keyed_frames(clip, row))
            ed.keyframes.pick_keys(picks)
            ed.apply_curve_to_range()
            clip = ed.doc.resource(ed.selected)
            after = ed.keyed_frames(clip, row)
            return {
                "movieclip_id": ed.selected, "row": row,
                "keys": after, "key_count": len(after),
                "written": sorted(set(after) - before),
                "curve": ed.curve.currentText(),
                "key_density": ed.key_density.value(),
            }

        if operation == "move_keys":
            delta = int(args["delta"])
            picks = args.get("keys")
            if picks is None:
                picks = [(frame, row) for frame in ed.keyed_frames(clip, row)]
            picks = sorted(tuple(pair) for pair in picks)
            if not picks:
                raise ValueError("No keys to move")
            if min(frame for frame, _ in picks) + delta < 0:
                raise ValueError("That would move a key before the first frame")
            ed.keyframes.pick_keys(picks)
            source = picks[0][0]
            # Every pick moves by the same offset, so the selection keeps its
            # spacing; the editor refuses a landing that would clobber a key.
            ed.move_keypoint(source, source + delta, picks[0][1], delta=delta)
            clip = ed.doc.resource(ed.selected)
            return {
                "movieclip_id": ed.selected, "row": picks[0][1],
                "keys": ed.keyed_frames(clip, picks[0][1]),
                "moved": sorted([frame + delta for frame, _ in picks]),
                "delta": delta,
            }

        raise ValueError(f"Unknown keyframe operation: {operation}")

    def batch(self, args):
        """Run several operations in one round trip.

        Without this, a session that keys forty placements or builds a screen
        from a dozen pieces pays a request per step, and the cost of the session
        becomes its round trips rather than its work. Operations run in the
        order given, all inside one undo step when they edit, so a batch is one
        thing to accept or roll back.

        A step names an operation and its arguments, and may set ``label`` so a
        failed step can be identified. Stops at the first failure by default and
        reports which step it was and what went wrong; ``continue_on_error``
        records the failure and carries on. ``undo_on_error`` rolls the whole
        batch back, which contradicts undoable=false.
        """
        steps = args.get("operations")
        if not isinstance(steps, list) or not steps:
            raise ValueError("operations must be a non-empty list of "
                             "{operation, arguments} entries")
        if len(steps) > 2000:
            raise ValueError("A batch is limited to 2000 operations")
        document_id = args.get("document_id")
        stop = not args.get("continue_on_error")
        rollback = bool(args.get("undo_on_error"))
        results, failures = [], []
        for position, step in enumerate(steps):
            if not isinstance(step, dict) or "operation" not in step:
                raise ValueError(f"Step {position} needs an operation")
            name = step["operation"]
            if name == "batch":
                raise ValueError("A batch cannot contain a batch")
            label = step.get("label")
            try:
                # A step may name its own document, so one batch can span tabs.
                inner = dict(step.get("arguments") or {})
                inner.setdefault("document_id", document_id)
                value = self.execute(name, inner)
                results.append({"index": position, "operation": name,
                                "label": label, "result": value})
            except Exception as error:
                failures.append({"index": position, "operation": name,
                                 "label": label, "error": str(error)})
                if rollback:
                    # Roll back every edit the batch has made so far. Each edit
                    # is one undo entry, including a batched key write.
                    while self.undo_edits(document_id, len(results)):
                        results.pop()
                    self.undo_edits(document_id, len(failures) - 1)
                    results = []
                    failures = failures[-1:]
                if stop:
                    return {"completed": len(results), "results": results,
                            "failed": failures,
                            "stopped_at": position, "rolled_back": rollback}
        return {"completed": len(results), "results": results,
                "failed": failures, "rolled_back": rollback}

    def undo_edits(self, document_id, count):
        """Undo up to ``count`` edits of the active document; True if any were."""
        if count <= 0:
            return False
        try:
            ed = self.editor(document_id)
        except ValueError:
            return False
        if not ed.history:
            return False
        ed.undo()
        return True

    @staticmethod
    def guide():
        """The data model and its traps, so an agent need not read the source.

        Every point here is something that costs an agent a round trip, a wrong
        edit, or an excursion into the code to learn. They are stated once, in
        the words an agent needs, rather than left to be inferred from field
        names.
        """
        return {
            "model": {
                "document": (
                    "A document holds four kinds of resource — movieclips, "
                    "shapes, textfields, modifiers — plus texture atlases and "
                    "export names. Resources are addressed by integer ID, which "
                    "is stable for the life of the document."
                ),
                "movieclip": (
                    "A clip has children (named instances of other resources), "
                    "frames, and a framerate. A frame is a list of placements, "
                    "one per child drawn on that frame."
                ),
                "placement": (
                    "A placement is [child_index, matrix_index, color_index]. "
                    "child_index is into the clip's children list, NOT the "
                    "resource ID. 65535 in matrix_index means the placement has "
                    "no transform of its own and inherits; 65535 in color_index "
                    "means no colour transform."
                ),
                "matrix_bank": (
                    "Transforms are not stored per placement. Each clip points "
                    "at a shared bank of matrices, and placements index into "
                    "it, so many placements can share one matrix. Writing a "
                    "matrix for one placement allocates a bank entry for it; "
                    "editing a shared entry would move every placement using it."
                ),
                "shape": (
                    "A shape is a list of triangular-strip commands, each "
                    "sampling one atlas. A vertex is [x, y, u, v]: x and y are "
                    "pixels, u and v are texel coordinates scaled to 0..65535 "
                    "against the atlas, not pixels."
                ),
                "textfield": (
                    "A textfield stores its box as left/top/right/bottom, plus "
                    "font_size, font_name, text, align and styles. styles is a "
                    "bitfield: 1 device font, 2 outline, 4 bold, 8 italic, 16 "
                    "multiline, 64 auto kern."
                ),
            },
            "traps": [
                "child_index is an index into the clip's children list, not a "
                "resource ID. Removing a child renumbers every later one, so "
                "placements recorded against the old numbering shift.",
                "A placement that inherits (matrix_index 65535) draws with the "
                "nearest earlier key's transform, not with no transform. Use "
                "get_timeline's poses, which resolve this, rather than reading "
                "frame elements directly.",
                "A frame's element list can be shorter than its neighbours: a "
                "child may be placed on some frames and not others. A frame "
                "that omits a row still draws the child through inheritance "
                "from the frame before it, if that frame placed it.",
                "Draw order is the frame's element order, back to front. "
                "There is no separate z-index field.",
                "transform_placement writes a transform; it does not key a "
                "frame. To animate, use set_keys. Frames must exist before "
                "they can be keyed — use edit_frames to add them.",
                "There is no nine-slice flag. A nine-slice asset is a clip "
                "whose child shape is already cut into a 3x3 grid of commands "
                "placed with an identity matrix, optionally with a scaling_grid "
                "on the clip. Sizing it means scaling the placement: on-screen "
                "span is the shape's pixel bounds times the placement scale.",
                "A shape's on-screen size is not its vertex bounds alone when "
                "it is drawn through a clip: multiply by the placement's scale "
                "and add its translation. find_components reports the size a "
                "clip renders at at scale 1.",
                "Scaling an atlas texture replaces the art at every UV a shape "
                "samples; UVs are fractions of the sheet, so art shifts if the "
                "new sheet has a different layout. Replace a sheet only with "
                "one of the same layout.",
                "Export names are the slots the game looks up. Reusing one "
                "means taking over that slot; a clash is refused rather than "
                "silently reassigned.",
                "Every edit is undoable and validated, so a malformed change "
                "rolls back and reports why instead of corrupting the file. "
                "Files are written only by save_document or export_graph.",
            ],
            "workflow": (
                "documents → document_summary (what exists, what is reusable, "
                "the conventions) → find_components or search to pick pieces → "
                "get_resource / get_shape / get_timeline to inspect them → "
                "edit → view_preview → validate → save_document. Nothing is "
                "written to disk until save_document."
            ),
        }

    def search(self, doc, args):
        """Search the whole document, not just ID and export names.

        list_resources matches an ID or an export name, which is not enough to
        find things by what they say or how they are wired: a button labelled
        "Join", a child instance named "close_button", a clip that places a
        given asset. Each match reports where it matched, so a hit can be judged
        without fetching the resource.
        """
        needle = (args.get("query") or "").casefold()
        if not needle:
            raise ValueError("Give something to search for")
        kinds = args.get("kinds")
        if kinds is not None:
            unknown = [k for k in kinds if k not in KINDS]
            if unknown:
                raise ValueError(f"Unknown kinds: {unknown}; choose from {list(KINDS)}")
        limit = max(1, min(500, int(args.get("limit", 100))))
        matches = []
        for id, (kind, _) in sorted(doc.index.items()):
            if kinds is not None and kind not in kinds:
                continue
            fields = []
            names = doc.names.get(id, [])
            if any(needle in name.casefold() for name in names):
                fields.append("export")
            if needle == str(id):
                fields.append("id")
            if fields:
                matches.append({"id": id, "kind": kind, "matched": fields,
                                "exports": names, "where": "resource"})
                continue
            # Textfields carry the words on screen, which nothing else indexes.
            if kind == "textfields":
                obj = doc.resource(id, lazy_frames=True)
                for field in ("text", "font_name", "typography"):
                    if needle in str(obj.get(field, "")).casefold():
                        fields.append(field)
                if fields:
                    matches.append({"id": id, "kind": kind, "matched": fields,
                                    "exports": names, "where": "textfield"})
                    continue
            if kind == "movieclips":
                obj = doc.resource(id, lazy_frames=True)
                for index, child in enumerate(obj.get("children", [])):
                    if needle in (child.get("name") or "").casefold():
                        matches.append({
                            "id": id, "kind": kind, "matched": ["child_name"],
                            "exports": names, "where": "child",
                            "child_index": index, "child_name": child["name"],
                            "child_id": child["id"],
                        })
                        break
                else:
                    for index, child in enumerate(obj.get("children", [])):
                        if needle in " ".join(
                            doc.names.get(child["id"], [])
                        ).casefold():
                            matches.append({
                                "id": id, "kind": kind,
                                "matched": ["child_export"], "exports": names,
                                "where": "child", "child_index": index,
                                "child_name": child.get("name") or "",
                                "child_id": child["id"],
                            })
                            break
            if len(matches) >= limit:
                break
        return {"query": args["query"], "count": len(matches), "matches": matches}

    def get_shape(self, doc, args):
        """A shape's draw commands with their vertices in full.

        get_resource already carries this, but the vertex arrays are the part an
        agent edits, so they are reported with each command's index, atlas and
        UV rectangle resolved alongside, which is what sizing and re-texturing
        decisions are made from.
        """
        id = args["resource_id"]
        if doc.index[id][0] != "shapes":
            raise ValueError("Resource must be a shape")
        shape = doc.resource(id, lazy_frames=True)
        atlases = doc.tables["Textures"]["textures"]
        regions = {}
        for region in doc.atlas_regions(shape["commands"][0]["texture_index"]) if shape["commands"] else []:
            regions.setdefault(region["id"], []).append(region)
        commands = []
        for index, command in enumerate(shape["commands"]):
            texture = atlases[command["texture_index"]] if command["texture_index"] < len(atlases) else None
            highres = texture.get("highres") if texture else None
            commands.append({
                "index": index,
                "texture_index": command["texture_index"],
                "unk1": command.get("unk1", 0),
                "vertices": [list(v) for v in command["vertices"]],
                "atlas": {
                    "width": highres.get("width") if highres else None,
                    "height": highres.get("height") if highres else None,
                },
            })
        return {
            "id": id,
            "exports": doc.names.get(id, []),
            "command_count": len(commands),
            "commands": commands,
            "size": self.vertex_bounds(shape),
            "conventions": {
                "vertices": (
                    "Each vertex is [x, y, u, v]. x and y are pixels and are "
                    "what sizing uses. u and v are texel coordinates scaled to "
                    "0..65535 against the atlas, not pixels."
                ),
                "commands": (
                    "A command is a triangle strip: its vertices are drawn in "
                    "order, so a four-vertex sprite is two triangles. A shape's "
                    "commands are drawn in order."
                ),
            },
        }

    def export_graph(self, doc, args):
        """Write a self-contained SC file holding resources and their graph.

        Importing into a fresh document is what makes the result standalone: it
        brings the whole dependency graph, crops the atlases to what those
        resources sample and rewrites the UVs to match, so the file does not
        depend on the document it came from.
        """
        from .document import Document as FreshDocument
        ids = args.get("resource_ids")
        if ids is None:
            ids = sorted(doc.index)
        missing = [id for id in ids if id not in doc.index]
        if missing:
            raise ValueError(f"Resources do not exist: {missing}")
        destination = Path(args["path"]).expanduser()
        built = FreshDocument()
        built.import_resources(doc, list(ids))
        built.save(destination)
        return {"path": str(destination), "resource_count": len(ids),
                "file_size": destination.stat().st_size}

    def export_texture(self, doc, args):
        """Write one atlas out as a PNG."""
        from . import textures as texture_module
        index = args["atlas_index"]
        atlases = doc.tables["Textures"]["textures"]
        if not 0 <= index < len(atlases):
            raise ValueError("Atlas index is out of range")
        destination = Path(args["path"]).expanduser()
        image = texture_module.image(
            atlases[index]["highres"],
            doc.path.parent if doc.path else Path("."),
        )
        image.save(str(destination))
        return {"path": str(destination), "width": image.width, "height": image.height}

    def get_timeline(self, doc, ed, args):
        """A movieclip's whole animation: every track, its keys, and what is picked.

        The animator's operations act on a track row and a selection of keys, so
        an agent needs both before it changes anything. Poses are resolved
        through inheritance, which is how the engine reads a frame that sets
        nothing: reporting the raw references would leave an agent unable to
        tell a held pose from a reset to no transform.
        """
        id = args["movieclip_id"]
        if doc.index[id][0] != "movieclips":
            raise ValueError("Resource must be a movieclip")
        clip = doc.resource(id, lazy_frames=True)
        row = args.get("row")
        tracks = []
        for index, child in enumerate(clip["children"]):
            if row is not None and index != row:
                continue
            names = doc.names.get(child["id"], [])
            tracks.append({
                "row": index,
                "name": child["name"],
                "child_id": child["id"],
                "child_exports": names,
                "keys": ed.keyed_frames(clip, index),
                "key_count": len(ed.keyed_frames(clip, index)),
            })
        result = {
            "movieclip_id": id,
            "frame_count": len(clip["frames"]),
            "framerate": clip.get("framerate"),
            "tracks": tracks,
            "picked_keys": sorted(
                [list(key) for key in ed.keyframes.selected_keys()]
            ),
            "picked_frames": ed.keyframes.selected_frames(),
            "curve": ed.curve.currentText(),
            "curve_names": ed.curve_names(),
            "key_density": ed.key_density.value(),
        }
        if args.get("frames") is not None or args.get("row") is not None:
            target = row if row is not None else 0
            if not 0 <= target < len(clip["children"]):
                raise ValueError("Placement row is out of range")
            frames = args.get("frames") or ed.clip_frames(clip)
            result["poses"] = [
                {
                    "frame": frame,
                    "matrix": ed.matrix_at(clip, frame, target),
                    "keyed": ed.keyed_matrix_at(clip, frame, target) is not None,
                }
                for frame in frames
                if 0 <= frame < len(clip["frames"])
            ]
            result["row"] = target
        return result

    def dispatch(self, request):
        payload, future = request
        if not future.set_running_or_notify_cancel():
            return
        try:
            if not self.running:
                raise ValueError("Agent access has stopped")
            if self.window.jobs:
                raise ValueError("Editor is busy; retry after the current operation completes")
            # Modal Qt loops can otherwise permit an agent to edit behind a dialog.
            from PyQt6.QtWidgets import QApplication
            if QApplication.activeModalWidget():
                raise ValueError("Close the editor's modal dialog before using agent tools")
            future.set_result(self.execute(payload["operation"], payload.get("arguments", {})))
        except Exception as error:
            future.set_exception(error)

    def execute(self, operation, args):
        from .widgets import jsonable
        # Tools that group a family of edits behind one name with an action, so
        # an agent does not have to find five near-identical tools.
        actions = {
            "edit_commands": {"add": "add_command", "remove": "remove_command"},
            "edit_children": {"rename": "set_child_name",
                              "duplicate": "duplicate_child",
                              "reorder": "reorder_child"},
            "edit_textures": {"add": "add_texture", "replace": "replace_texture"},
        }.get(operation)
        if actions is not None:
            chosen = actions.get((args.get("action") or "").strip())
            if chosen is None:
                raise ValueError(
                    f"action must be one of {', '.join(sorted(actions))}"
                )
            operation = chosen
        if operation == "edit_frames":
            # One tool with an action, so an agent new to the format does not
            # have to find five near-identical frame tools.
            operation = {
                "insert": "insert_frames",
                "duplicate": "duplicate_frames",
                "delete": "delete_frames",
                "set_label": "set_frame_label",
                "set_framerate": "set_framerate",
            }.get((args.get("action") or "").strip())
            if operation is None:
                raise ValueError(
                    "action must be one of insert, duplicate, delete, "
                    "set_label, set_framerate"
                )
        if operation == "batch":
            return self.batch(args)
        if operation == "documents":
            return [self.describe(ed) for ed in self.editors()]
        if operation in ("open_document", "new_document"):
            if operation == "open_document":
                doc = Document.load(Path(args["path"]).expanduser())
            else:
                version = args.get("version", 6)
                if version not in (5, 6):
                    raise ValueError("Only SC2 versions 5 and 6 are supported")
                doc = Document(version=version)
            self.window.add_doc(doc)
            list(self.editors())
            return self.describe(self.window.current())
        ed = self.editor(args.get("document_id"))
        doc = ed.doc
        if operation == "list_resources":
            kind, query = args.get("kind"), args.get("query", "").casefold()
            if kind is not None and kind not in KINDS:
                raise ValueError(f"Kind must be one of {list(KINDS)}")
            rows = [{"id": id, "kind": entry[0], "exports": doc.names.get(id, [])}
                    for id, entry in sorted(doc.index.items())
                    if (kind is None or entry[0] == kind)
                    and (not query or query in (str(id) + " " + " ".join(doc.names.get(id, []))).casefold())]
            offset, limit = max(0, args.get("offset", 0)), max(1, min(500, args.get("limit", 100)))
            return {"total": len(rows), "offset": offset, "resources": rows[offset:offset + limit]}
        if operation == "guide":
            return self.guide()
        if operation == "document_summary":
            # One call that answers the questions every session opens with, so
            # an agent does not have to poll list_resources per kind, fetch each
            # resource and page every atlas to work out what a document holds.
            return self.document_summary(doc)
        if operation == "atlas_index":
            return self.atlas_index(doc, args.get("document_id"))
        if operation == "find_components":
            return self.find_components(doc, args.get("query", ""),
                                        bool(args.get("with_size")),
                                        int(args.get("limit", 100)))
        if operation == "get_resources":
            ids = args["resource_ids"]
            offset = max(0, args.get("frame_offset", 0))
            limit = max(1, min(100, args.get("frame_limit", 1)))
            found, missing = {}, []
            for resource_id in ids:
                if resource_id not in doc.index:
                    missing.append(resource_id)
                    continue
                obj = doc.resource(resource_id, lazy_frames=True)
                if doc.index[resource_id][0] == "movieclips":
                    frames = obj["frames"]
                    obj["frame_count"] = len(frames)
                    obj["frames"] = frames[offset : offset + limit]
                    obj["frame_start"] = offset
                found[str(resource_id)] = jsonable(obj)
            return {"resources": found, "missing": missing}
        if operation == "get_texture":
            index = args["atlas_index"]
            textures = doc.tables["Textures"]["textures"]
            if not 0 <= index < len(textures):
                raise ValueError("Atlas index is out of range")
            texture = textures[index]
            result = {"atlas_index": index}
            for name, key in (("highres", "highres"), ("lowres", "lowres")):
                data = texture.get(key)
                if isinstance(data, dict):
                    result[name] = {
                        "width": data.get("width"),
                        "height": data.get("height"),
                        "format": data.get("format"),
                        "external": data.get("external"),
                    }
            result["regions"] = doc.atlas_regions(index)
            return result
        if operation == "get_resource":
            obj = doc.resource(args["resource_id"], lazy_frames=True)
            if doc.index[args["resource_id"]][0] == "movieclips":
                frames = obj["frames"]
                obj["frame_count"] = len(frames)
                start = max(0, args.get("frame_start", 0))
                stop = start + max(1, min(100, args.get("frame_limit", 1)))
                obj["frames"] = [frames[i] for i in range(start, min(stop, len(frames)))]
                obj["frame_start"] = start
            return jsonable(obj)
        if operation == "references":
            return doc.reference_graph(args["resource_id"])
        if operation == "find_usages":
            # The direct question "who places this, and what calls it by name":
            # references answers the transitive graph, which is a different and
            # much larger answer for a shared asset.
            indirect = bool(args.get("indirect"))
            rows = doc.usages(args["resource_id"], indirect=indirect)
            usages = []
            for row in rows:
                parent = row.get("id")
                usages.append({
                    **row,
                    "parent_exports": doc.names.get(parent, []),
                    "parent_kind": doc.index[parent][0] if parent in doc.index else None,
                })
            return {"resource_id": args["resource_id"], "indirect": indirect,
                    "count": len(usages), "usages": usages}
        if operation == "search":
            return self.search(doc, args)
        if operation == "atlas_regions":
            # A full UI atlas holds tens of thousands of commands, most of them
            # zero-area, so this is paged instead of returning everything.
            regions = doc.atlas_regions(args["atlas_index"])
            offset = max(0, args.get("offset", 0))
            limit = max(1, min(5000, args.get("limit", 1000)))
            return {
                "total": len(regions),
                "offset": offset,
                "regions": regions[offset : offset + limit],
            }
        if operation == "get_timeline":
            return self.get_timeline(doc, ed, args)
        if operation == "get_shape":
            return self.get_shape(doc, args)
        if operation == "export_texture":
            return self.export_texture(doc, args)
        if operation == "export_graph":
            return self.export_graph(doc, args)
        if operation == "get_placement":
            id = args["movieclip_id"]
            if doc.index[id][0] != "movieclips":
                raise ValueError("Resource must be a movieclip")
            clip = doc.resource(id, lazy_frames=True)
            frame, index = args.get("frame", 0), args["placement_index"]
            if not 0 <= frame < len(clip["frames"]):
                raise ValueError("Frame is out of range")
            elements = clip["frames"][frame]["elements"]
            if not 0 <= index < len(elements):
                raise ValueError("Placement is out of range")
            child, matrix, color = elements[index]
            bank = doc.bank(clip["matrix_bank_index"]) if matrix != 65535 or color != 65535 else None
            return {"child_index": child, "child": clip["children"][child],
                    "matrix": list(bank["matrices"][matrix]) if matrix != 65535 else [1, 0, 0, 1, 0, 0],
                    "color": list(bank["colors"][color]) if color != 65535 else [255, 255, 255, 255, 0, 0, 0]}
        if operation == "validate":
            return {"errors": doc.validate()}
        # Navigation also resolves draft widgets in the normal GUI; do not prompt agents.
        if ed.drafts:
            raise ValueError("Apply or discard unapplied inspector edits in the app before agent changes")
        if operation == "publish":
            from .publishing import publish, PublishSettings
            path = Path(args["path"]).expanduser().resolve()
            if any(e.doc.path and path == e.doc.path.resolve() for e in self.editors()):
                raise ValueError("Publish to a separate output path, not an open source document")
            if path.exists() and not args.get("overwrite", False):
                raise ValueError("Output exists; pass overwrite=true to replace it")
            settings = PublishSettings(**args.get("settings", {}))
            sources = [self.editor(i).doc for i in args.get("document_ids", [])] or [doc]
            if any(self.editor(i).drafts for i in args.get("document_ids", [])):
                raise ValueError("Apply source inspector drafts before publishing")
            destination = Document.load(args["merge_path"]) if args.get("merge_path") else None
            return publish(sources, path, settings, args.get("resource_ids"), destination)
        if operation == "render_resource":
            from .rendering import Renderer
            renderer = Renderer(doc)
            image, bounds = renderer.render(args["resource_id"], args.get("frame", 0), scale=args.get("scale", 1))
            buffer = QBuffer(); buffer.open(QIODevice.OpenModeFlag.WriteOnly); image.save(buffer, "PNG")
            return {"png": base64.b64encode(bytes(buffer.data())).decode(),
                    "width": image.width(), "height": image.height(),
                    "origin": [bounds.left(), bounds.top()]}
        if operation == "author":
            from . import authoring
            action, params = args["action"], args.get("parameters", {})
            actions = {
                "parenting": authoring.bake_parenting,
                "color_tween": authoring.tween_colors,
                "morph_shapes": authoring.morph_shapes,
                "mask_range": authoring.mask_range,
                "nine_slice": authoring.nine_slice,
                "group_tracks": authoring.group_tracks,
                "motion_path": authoring.motion_path,
            }
            result = {}
            def apply_authoring():
                if action == "import_svg":
                    value = authoring.svg_shape(doc, Path(params["path"]).expanduser().read_bytes(),
                                                params.get("width"), params.get("height"))
                elif action == "repack_atlases":
                    from .publishing import repack_atlases, PublishSettings
                    settings = PublishSettings(**params); settings.validate()
                    value = repack_atlases(doc, settings)
                elif action in actions:
                    value = actions[action](doc, **params)
                else:
                    raise ValueError("Unknown authoring action")
                result["result"] = value
            ed.mutate(apply_authoring, raise_errors=True, full_validate=True)
            return result
        if operation in ("set_keys", "clear_keys", "tween", "move_keys"):
            # These drive the animator's own widgets, so the document has to be
            # the one on screen and no inspector draft may be pending.
            self.window.editors.setCurrentWidget(ed)
            if args.get("movieclip_id") is not None:
                id = args["movieclip_id"]
                if id not in doc.index:
                    raise ValueError("Resource does not exist")
                ed.go_to(id)
            if doc.index[ed.selected][0] != "movieclips":
                raise ValueError("Resource must be a movieclip")
            return self.keyframe_edit(ed, operation, args)
        if operation in ("select_resource", "view_preview"):
            self.window.editors.setCurrentWidget(ed)
            if args.get("resource_id") is not None:
                if args["resource_id"] not in doc.index:
                    raise ValueError("Resource does not exist")
                ed.go_to(args["resource_id"])
            if args.get("frame") is not None:
                frame = args["frame"]
                if not ed.frame.minimum() <= frame <= ed.frame.maximum():
                    raise ValueError("Frame is out of range")
                ed.cursor.setValue(frame)
            if operation == "select_resource":
                return self.describe(ed)
            ed.render_frame(fit=True)
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            ed.preview.viewport().grab().save(buffer, "PNG")
            return {"png": base64.b64encode(bytes(buffer.data())).decode(),
                    "resource_id": ed.selected, "frame": ed.cursor.value()}
        if operation == "save_document":
            path = args.get("path") or doc.path
            if not path:
                raise ValueError("Specify a path for a new document")
            doc.save(Path(path).expanduser())
            ed.mark_saved()
            self.window.title(ed)
            return self.describe(ed)
        if operation in ("undo", "redo"):
            available = bool(ed.history if operation == "undo" else ed.future)
            getattr(ed, operation)()
            return {"changed": available, **self.describe(ed)}
        if operation == "import_image":
            from . import textures
            id, frame = args["movieclip_id"], args.get("frame", 0)
            if doc.index[id][0] != "movieclips":
                raise ValueError("Select a movieclip for image import")
            data = textures.replace_from_png({}, Path(args["path"]).expanduser())
            values = [data["width"] if args.get("width") is None else args["width"],
                      data["height"] if args.get("height") is None else args["height"],
                      args.get("x", 0), args.get("y", 0), args.get("angle", 0)]
            if any(not math.isfinite(v) for v in values) or min(values[:2]) <= 0:
                raise ValueError("Image size must be positive and transform values finite")
            frames = doc.resource(id, lazy_frames=True)["frames"]
            if not 0 <= frame < max(1, len(frames)):
                raise ValueError("Frame is out of range")
            self.window.editors.setCurrentWidget(ed)
            ed.go_to(id)
            ed.cursor.setValue(frame)
            ed.insert_image(data, args.get("name") or Path(args["path"]).stem, values, raise_errors=True)
            return self.describe(ed)
        if operation == "import_animation":
            from . import textures
            id = args["movieclip_id"]
            if doc.index[id][0] != "movieclips":
                raise ValueError("Select a movieclip for animation import")
            frames, duration = textures.decode_animation(
                Path(args["path"]).expanduser()
            )
            if len(frames) < 2:
                raise ValueError("That file holds a single frame; use import_image")
            if len(frames) > 512:
                raise ValueError("Animation exceeds the 512-frame import limit")
            default_fps = 1000.0 / (duration / len(frames)) if duration else 30.0
            options = {
                "prefix": args.get("prefix", "frame"),
                "suffix": args.get("suffix", ""),
                "fps": float(args.get("fps") or min(120.0, max(1.0, default_fps))),
            }
            self.window.editors.setCurrentWidget(ed)
            ed.go_to(id)
            ed.cursor.setValue(0)
            ed.insert_animation(
                frames,
                options,
                Path(args["path"]).stem,
                raise_errors=True,
            )
            return self.describe(ed)
        result = {}

        def change():
            if operation == "update_resource":
                obj = args["resource"]
                id = obj["id"]
                if "frame_count" in obj or "frame_start" in obj:
                    raise ValueError("get_resource returns a frame page; use patch_resource or supply a complete resource")
                doc.update(doc.index[id][0], obj)
                result["id"] = id
            elif operation == "patch_resource":
                id = args["resource_id"]
                obj = doc.resource(id)
                changes = args["changes"]
                if any(key not in obj or key == "id" for key in changes):
                    raise ValueError("Patch keys must be existing editable resource fields (excluding id)")
                obj.update(changes)
                doc.update(doc.index[id][0], obj)
                result["id"] = id
            elif operation == "new_resource":
                if args["kind"] not in KINDS:
                    raise ValueError("Unknown resource kind")
                result["id"] = doc.new_resource(args["kind"])
            elif operation == "delete_resources":
                doc.delete_many(args["resource_ids"])
            elif operation == "duplicate_resources":
                result["mapping"] = doc.duplicate_resources(args["resource_ids"])
            elif operation == "import_resources":
                source = self.editor(args["source_document_id"])
                if source is ed:
                    raise ValueError("Choose a different source document")
                result["mapping"] = doc.import_resources(source.doc, args["resource_ids"])
            elif operation in ("set_vertices", "add_command", "remove_command",
                                "set_child_name", "duplicate_child", "reorder_child",
                                "replace_texture", "add_texture", "set_exports"):
                # The grouped tools take whichever ID name reads best for what
                # they act on; they all mean "the resource to edit". The texture
                # operations are the exception: those belong to the document.
                if operation in ("replace_texture", "add_texture"):
                    id = None
                else:
                    id = args.get("resource_id")
                    if id is None:
                        id = args.get("movieclip_id")
                    if id is None or id not in doc.index:
                        raise ValueError("Resource does not exist")
                if operation in ("replace_texture", "add_texture"):
                    # Textures belong to the document, not to one resource.
                    from . import textures as texture_module
                    data = texture_module.replace_from_png(
                        {}, Path(args["path"]).expanduser()
                    )
                    atlases = doc.tables["Textures"]["textures"]
                    if operation == "replace_texture":
                        index = args["atlas_index"]
                        if not 0 <= index < len(atlases):
                            raise ValueError("Atlas index is out of range")
                        atlases[index] = {"highres": data, "lowres": None}
                    else:
                        atlases.append({"highres": data, "lowres": None})
                        result["atlas_index"] = len(atlases) - 1
                    doc.dirty.add("Textures")
                    result["width"] = data["width"]
                    result["height"] = data["height"]
                elif operation == "set_vertices":
                    if doc.index[id][0] != "shapes":
                        raise ValueError("Resource must be a shape")
                    shape = doc.resource(id)
                    index = args["command_index"]
                    if not 0 <= index < len(shape["commands"]):
                        raise ValueError("Command index is out of range")
                    vertices = args["vertices"]
                    for vertex in vertices:
                        if len(vertex) != 4 or any(
                            not math.isfinite(float(v)) for v in vertex
                        ):
                            raise ValueError(
                                "Each vertex must be [x, y, u, v] with finite values"
                            )
                    if len(vertices) < 3:
                        raise ValueError("A command needs at least three vertices")
                    shape["commands"][index]["vertices"] = [
                        self.coerce_vertex(vertex) for vertex in vertices
                    ]
                    result["size"] = self.vertex_bounds(shape)
                    doc.update("shapes", shape)
                elif operation == "add_command":
                    if doc.index[id][0] != "shapes":
                        raise ValueError("Resource must be a shape")
                    atlases = doc.tables["Textures"]["textures"]
                    atlas = args["atlas_index"]
                    if not 0 <= atlas < len(atlases):
                        raise ValueError("Atlas index is out of range")
                    vertices = args.get("vertices")
                    if vertices is None:
                        highres = atlases[atlas]["highres"]
                        width, height = highres["width"], highres["height"]
                        # A new command is the whole atlas as a quad, so there is
                        # something visible to edit rather than a blank command.
                        vertices = [[0, 0, 0, 0], [width, 0, 65535, 0],
                                    [0, height, 0, 65535], [width, height, 65535, 65535]]
                    shape = doc.resource(id)
                    shape["commands"].append({
                        "unk1": int(args.get("unk1", 0)),
                        "texture_index": atlas,
                        "vertices": [self.coerce_vertex(v) for v in vertices],
                    })
                    result["command_index"] = len(shape["commands"]) - 1
                    doc.update("shapes", shape)
                elif operation == "remove_command":
                    if doc.index[id][0] != "shapes":
                        raise ValueError("Resource must be a shape")
                    shape = doc.resource(id)
                    index = args["command_index"]
                    if not 0 <= index < len(shape["commands"]):
                        raise ValueError("Command index is out of range")
                    if len(shape["commands"]) <= 1:
                        raise ValueError("A shape must keep at least one command")
                    shape["commands"].pop(index)
                    doc.update("shapes", shape)
                elif operation in ("set_child_name", "duplicate_child", "reorder_child"):
                    if doc.index[id][0] != "movieclips":
                        raise ValueError("Resource must be a movieclip")
                    clip = doc.resource(id)
                    index = args["child_index"]
                    if not 0 <= index < len(clip["children"]):
                        raise ValueError("Child index is out of range")
                    if operation == "set_child_name":
                        clip["children"][index]["name"] = args["name"]
                        result["name"] = args["name"]
                    elif operation == "duplicate_child":
                        child = dict(clip["children"][index])
                        child["name"] = (child["name"] or "child") + (
                            args.get("suffix") or "_copy"
                        )
                        # The duplicate shares the same definition and is only
                        # added to the child list; placing it is a separate
                        # step, so it cannot silently change what a frame draws.
                        clip["children"].append(child)
                        result["child_index"] = len(clip["children"]) - 1
                    else:
                        frame_index = args.get("frame", 0)
                        if not 0 <= frame_index < len(clip["frames"]):
                            raise ValueError("Frame is out of range")
                        elements = clip["frames"][frame_index]["elements"]
                        matches = [e for e in elements if e[0] == index]
                        rest = [e for e in elements if e[0] != index]
                        if not matches:
                            raise ValueError(
                                "That child is not placed in this frame"
                            )
                        to_front = bool(args.get("to_front"))
                        # Draw order is the frame's element order, back to
                        # front, so the child moves to one end of it.
                        clip["frames"][frame_index]["elements"] = (
                            rest + matches if to_front else matches + rest
                        )
                    doc.update("movieclips", clip)
                else:
                    # A resource's export names are how the game finds it, so
                    # this is the rename step rather than a display label.
                    names = [str(n) for n in args["exports"]]
                    if id not in doc.index:
                        raise ValueError("Resource does not exist")
                    taken = {
                        name: other
                        for other, values in doc.names.items()
                        if other != id
                        for name in values
                    }
                    clash = [n for n in names if n in taken]
                    if clash:
                        raise ValueError(
                            f"Export name already used by resource "
                            f"{taken[clash[0]]}: {clash[0]}"
                        )
                    doc.set_exports(id, names)
                    result["exports"] = names
            elif operation in ("insert_frames", "delete_frames", "duplicate_frames",
                                "set_frame_label", "set_framerate", "remove_child"):
                id = args["movieclip_id"]
                if doc.index[id][0] != "movieclips":
                    raise ValueError("Resource must be a movieclip")
                clip = doc.resource(id)
                count = len(clip["frames"])
                if operation == "remove_child":
                    index = args["child_index"]
                    if not 0 <= index < len(clip["children"]):
                        raise ValueError("Child index is out of range")
                    if any(any(e[0] == index for e in frame["elements"])
                           for frame in clip["frames"]):
                        raise ValueError(
                            "That child is placed in a frame; remove its "
                            "placements first"
                        )
                    removed = clip["children"].pop(index)
                    result["removed"] = removed
                    doc.update("movieclips", clip)
                elif operation == "set_framerate":
                    rate = args["framerate"]
                    if not 1 <= rate <= 120:
                        raise ValueError("framerate must be between 1 and 120")
                    clip["framerate"] = rate
                    doc.update("movieclips", clip)
                elif operation == "set_frame_label":
                    index = args["frame"]
                    if not 0 <= index < count:
                        raise ValueError("Frame is out of range")
                    clip["frames"][index]["label"] = args.get("label", "")
                    doc.update("movieclips", clip)
                else:
                    index = args.get("frame", 0)
                    amount = max(1, int(args.get("count", 1)))
                    if operation == "insert_frames":
                        if not 0 <= index <= count:
                            raise ValueError("Frame is out of range")
                        label = args.get("label", "")
                        label_every = bool(args.get("label_every_frame"))
                        # The inserted frames draw the children the frame before
                        # them draws, with no transform of their own, so the
                        # animation holds through the gap instead of the objects
                        # disappearing. A frame with no children would blank the
                        # stage for however many frames were added.
                        template = (
                            clip["frames"][index - 1]["elements"]
                            if index > 0
                            else (clip["frames"][0]["elements"] if clip["frames"] else [])
                        )
                        clip["frames"][index:index] = [
                            {
                                "label": label if (index == 0 or label_every) else "",
                                "elements": [[e[0], 65535, 65535] for e in template],
                            }
                            for _ in range(amount)
                        ]
                    elif operation == "delete_frames":
                        frames = args.get("frames")
                        if frames is None:
                            frames = list(range(index, index + amount))
                        if not frames:
                            raise ValueError("No frames to delete")
                        targets = sorted({f for f in frames
                                          if 0 <= f < count}, reverse=True)
                        while targets and len(targets) >= count:
                            # A movieclip always keeps one frame.
                            targets = targets[1:]
                        if not targets:
                            raise ValueError("A movieclip must keep at least one frame")
                        for f in targets:
                            clip["frames"].pop(f)
                    else:
                        # Duplicating copies the frame as it renders, so the new
                        # frame holds the pose that was on screen.
                        frames = args.get("frames") or [index]
                        frames = sorted({f for f in frames if 0 <= f < count})
                        if not frames:
                            raise ValueError("Frame is out of range")
                        built = []
                        for f in frames:
                            source = clip["frames"][f]
                            elements = []
                            for row_index, element in enumerate(source["elements"]):
                                # The pose the frame renders is written onto the
                                # duplicate as its own key. Leaving it to
                                # inherit would resolve against the end of the
                                # clip instead, which is not the pose that was
                                # on screen when it was copied.
                                elements.append([
                                    element[0],
                                    ed.intern_matrix(
                                        clip, ed.matrix_at(clip, f, row_index)
                                    ),
                                    element[2],
                                ])
                            for _ in range(amount):
                                built.append({"label": source["label"],
                                              "elements": [list(e) for e in elements]})
                        clip["frames"].extend(built)
                        result["added"] = len(built)
                    doc.update("movieclips", clip)
            elif operation in ("add_child", "transform_placement", "move_placement"):
                id = args["movieclip_id"]
                if doc.index[id][0] != "movieclips":
                    raise ValueError("Resource must be a movieclip")
                clip = doc.resource(id)
                frame = args.get("frame", 0)
                if not 0 <= frame < len(clip["frames"]):
                    raise ValueError("Frame is out of range")
                elements = clip["frames"][frame]["elements"]
                if operation == "add_child":
                    child = args["child_id"]
                    if child not in doc.index:
                        raise ValueError("Child resource is missing")
                    index = len(clip["children"])
                    clip["children"].append({"id": child, "name": args.get("name", ""), "blend": 0})
                    elements.append([index, 65535, 65535])
                    result["child_index"] = index
                    doc.update("movieclips", clip)
                else:
                    index = args["placement_index"]
                    if not 0 <= index < len(elements):
                        raise ValueError("Placement is out of range")
                    if operation == "transform_placement":
                        matrix = args["matrix"]
                        if len(matrix) != 6 or any(not math.isfinite(value) for value in matrix):
                            raise ValueError("Matrix must contain six finite values [a,b,c,d,tx,ty]")
                        ed.assign_matrix(clip, elements[index], matrix)
                    else:
                        new_index = args["new_index"]
                        if not 0 <= new_index < len(elements):
                            raise ValueError("New layer index is out of range")
                        elements.insert(new_index, elements.pop(index))
                        doc.update("movieclips", clip)
                result["id"] = id
            else:
                raise ValueError(f"Unknown operation: {operation}")

        ed.mutate(change, raise_errors=True)
        ed.refresh()
        return {**result, **self.describe(ed)}
