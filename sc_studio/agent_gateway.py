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
            return doc.atlas_regions(args["atlas_index"])
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
