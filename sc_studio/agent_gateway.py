"""Authenticated loopback gateway, with all widget access on the Qt thread."""
import base64
import hmac
import json
import os
import secrets
import threading
import uuid
from concurrent.futures import Future, TimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal, QBuffer, QIODevice
from .document import Document, KINDS


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
        from .app import jsonable
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
                ed.frame.setValue(frame)
            if operation == "select_resource":
                return self.describe(ed)
            ed.render_frame(fit=True)
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            ed.preview.viewport().grab().save(buffer, "PNG")
            return {"png": base64.b64encode(bytes(buffer.data())).decode(),
                    "resource_id": ed.selected, "frame": ed.frame.value()}
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
            import math
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
            ed.frame.setValue(frame)
            ed.insert_image(data, args.get("name") or Path(args["path"]).stem, values, raise_errors=True)
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
                        import math
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
