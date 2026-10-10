import asyncio
import base64
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PyQt6.QtCore import Qt
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_gui import window, application, wait_until
from test_document import populated
from sc_studio.document import Document
from sc_studio.references import ReferenceTree
from sc_studio.agent_gateway import AgentGateway


def referenced_document(cycle=False):
    doc = populated()
    outer = doc.resource(7)
    outer.update(id=8, exports=["outer"], children=[{"id": 7, "name": "nested", "blend": 0}],
                 frames=[{"label": "", "elements": [[0, 65535, 65535]]}])
    doc.update("movieclips", outer)
    if cycle:
        clip = doc.resource(7)
        clip["children"].append({"id": 8, "name": "cycle", "blend": 0})
        doc.update("movieclips", clip)
    return doc


def test_reference_tree_roots_and_cycles():
    application()
    doc = referenced_document()
    graph = doc.reference_graph(3)
    assert {(edge["from"], edge["to"]) for edge in graph["edges"]} == {(3, 7), (7, 8)}
    assert next(node for node in graph["nodes"] if node["id"] == 8)["terminal"]
    tree = ReferenceTree(doc, 3)
    parent = tree.topLevelItem(0).child(0)
    parent.setExpanded(True)
    assert "Root" in parent.child(0).text(0)
    selected = []
    tree.open_resource.connect(selected.append)
    tree.itemDoubleClicked.emit(parent.child(0), 0)
    assert selected == [8]
    tree.deleteLater()
    shared = doc.resource(8)
    shared.update(id=9, exports=["other_root"])
    doc.update("movieclips", shared)
    tree = ReferenceTree(doc, 3)
    parent = tree.topLevelItem(0).child(0)
    parent.setExpanded(True)
    assert {parent.child(i).text(1) for i in range(parent.childCount())} == {"8", "9"}
    assert all("Root" in parent.child(i).text(0) for i in range(parent.childCount()))
    tree.deleteLater()
    tree = ReferenceTree(referenced_document(cycle=True), 3)
    clip = tree.topLevelItem(0).child(0)
    clip.setExpanded(True)
    outer = clip.child(0)
    outer.setExpanded(True)
    assert "Cycle" in outer.child(0).text(0)
    assert outer.child(0).childCount() == 0
    tree.deleteLater()


def test_atlas_regions_tool_pages_large_atlases(window, tmp_path):
    doc = populated()
    data = doc.tables["Textures"]["textures"][0]["highres"]
    data.update(width=64, height=64, data=bytes([10, 20, 30, 255]) * 64 * 64)
    # One real sprite plus many zero-area commands, as a full UI atlas has.
    # 3 already exists as a shape; 4 is a textfield in the fixture, and 7 is a
    # movieclip, so the new shapes start above both.
    ids = [3] + list(range(20, 29))
    for id in ids:
        obj = doc.resource(3)
        obj["id"] = id
        obj["commands"][0]["vertices"] = (
            [[0, 0, 512, 512], [20, 0, 512, 512], [0, 20, 512, 512], [20, 20, 512, 512]]
            if id == 3
            else [[0, 0, 2048, 2048], [20, 0, 2048, 2048],
                  [0, 20, 2048, 2048], [20, 20, 2048, 2048]]
        )
        doc.update("shapes", obj)
    window.add_doc(doc)
    gateway = AgentGateway(window, tmp_path / "connection.json")
    gateway.start()
    try:
        page = gateway.execute("atlas_regions", {"atlas_index": 0, "limit": 4})
        assert page["total"] == len(ids) and page["offset"] == 0
        assert len(page["regions"]) == 4
        rest = gateway.execute("atlas_regions", {"atlas_index": 0, "offset": 4, "limit": 100})
        assert len(rest["regions"]) == len(ids) - 4
        assert {r["id"] for r in page["regions"] + rest["regions"]} == set(ids)
        # A zero-area command is reported as a single-texel rect, not dropped.
        zero = next(r for r in page["regions"] + rest["regions"] if r["id"] != 3)
        assert zero["rect"][2] == 0 and zero["rect"][3] == 0
    finally:
        gateway.stop()


def test_agent_rollback_drafts_and_connection_cleanup(window, tmp_path):
    window.add_doc(populated())
    ed = window.current()
    path = tmp_path / "connection.json"
    gateway = AgentGateway(window, path)
    gateway.start()
    try:
        original = ed.doc.resource(7)
        with pytest.raises(ValueError):
            gateway.execute("patch_resource", {"resource_id": 7, "changes": {"children": [{"id": 999, "name": "bad", "blend": 0}]}})
        assert ed.doc.resource(7) == original
        assert not ed.history
        ed.drafts.add("properties")
        with pytest.raises(ValueError, match="unapplied"):
            gateway.execute("patch_resource", {"resource_id": 7, "changes": {"framerate": 20}})
        ed.drafts.clear()
        gateway.execute("patch_resource", {"resource_id": 7, "changes": {"framerate": 20}})
        assert ed.doc.resource(7)["framerate"] == 20
        gateway.execute("undo", {})
        assert ed.doc.resource(7)["framerate"] == original["framerate"]
        assert json.loads(path.read_text())["url"].startswith("http://127.0.0.1:")
        if os.name == "posix":
            # Windows has no POSIX permission bits for chmod to set.
            assert path.stat().st_mode & 0o077 == 0
    finally:
        gateway.stop()
    assert not path.exists()


def test_mcp_menu_config_auth_and_stable_tab_ids(window, tmp_path, monkeypatch):
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    path = tmp_path / "connection.json"
    monkeypatch.setenv("SC_STUDIO_MCP_CONNECTION", str(path))
    window.new()
    window.actions["mcp_config"].trigger()
    gateway = window.agent_gateway
    assert gateway.running
    config = json.loads(application().clipboard().text())
    assert config["mcpServers"]["sc-studio"]["args"] == ["--connection", str(path)]
    first = gateway.execute("documents", {})[0]["document_id"]
    window.new()
    ids = {row["document_id"] for row in gateway.execute("documents", {})}
    window.editors.tabBar().moveTab(0, 1)
    assert {row["document_id"] for row in gateway.execute("documents", {})} == ids
    assert gateway.editor(first).doc.version == 6
    with pytest.raises(HTTPError) as error:
        urlopen(Request(gateway.url, data=b'{}'), timeout=2)
    assert error.value.code == 401
    window.actions["mcp_stop"].trigger()
    assert window.agent_gateway is None and not path.exists()


def test_real_mcp_stdio_client_edit_view_import_and_save(window, tmp_path):
    window.add_doc(referenced_document())
    connection = tmp_path / "connection.json"
    gateway = AgentGateway(window, connection)
    gateway.start()
    target = tmp_path / "agent-edited.sc"

    async def session_test():
        parameters = StdioServerParameters(command=sys.executable,
            args=["-m", "sc_studio.mcp_server", "--connection", str(connection)],
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert {"view_preview", "patch_resource", "references", "save_document"} <= {tool.name for tool in listed.tools}

                async def call(name, args=None):
                    result = await session.call_tool(name, args or {})
                    assert not result.isError, result.content
                    if result.structuredContent:
                        return result.structuredContent.get("result", result.structuredContent)
                    return json.loads(result.content[0].text)

                docs = await call("documents")
                source = docs[0]["document_id"]
                listing = await call("list_resources", {"kind": "movieclips", "limit": 1})
                assert listing["total"] == 2 and len(listing["resources"]) == 1
                graph = await call("references", {"resource_id": 3})
                assert len(graph["edges"]) == 2
                original = await call("get_resource", {"resource_id": 8})
                copy = await call("duplicate_resources", {"resource_ids": [8]})
                copied_id = copy["mapping"]["8"]
                assert (await call("get_resource", {"resource_id": copied_id}))["children"] == original["children"]
                await call("delete_resources", {"resource_ids": [copied_id]})
                await call("patch_resource", {"resource_id": 7, "changes": {"framerate": 17}})
                assert (await call("get_resource", {"resource_id": 7}))["framerate"] == 17
                await call("undo")
                assert (await call("get_resource", {"resource_id": 7}))["framerate"] == 30
                await call("redo")
                old_placement = await call("get_placement", {"movieclip_id": 7, "placement_index": 0})
                assert old_placement["matrix"][4:] == [40, 20]
                await call("transform_placement", {"movieclip_id": 7, "placement_index": 0, "matrix": [2, 0, 0, 2, 9, 12]})
                assert (await call("get_placement", {"movieclip_id": 7, "placement_index": 0}))["matrix"] == [2, 0, 0, 2, 9, 12]
                # The original bank's shared placement in the next frame remains intact.
                assert (await call("get_placement", {"movieclip_id": 7, "placement_index": 0, "frame": 1}))["matrix"][4:] == [40, 20]
                await call("undo")
                await call("move_placement", {"movieclip_id": 7, "placement_index": 0, "new_index": 1})
                assert (await call("get_placement", {"movieclip_id": 7, "placement_index": 1}))["child"]["id"] == 3
                await call("undo")
                image = await session.call_tool("view_preview", {"resource_id": 7, "frame": 0})
                assert not image.isError
                assert image.content[0].type == "image"
                assert base64.b64decode(image.content[0].data).startswith(b"\x89PNG\r\n\x1a\n")
                created = await call("new_document")
                destination = created["document_id"]
                assert destination != source and created["version"] == 6
                imported = await call("import_resources", {"source_document_id": source, "resource_ids": [8]})
                root = imported["mapping"]["8"]
                assert (await call("get_resource", {"resource_id": root}))["exports"] == ["outer"]
                shape = await call("new_resource", {"kind": "shapes"})
                await call("add_child", {"movieclip_id": root, "child_id": shape["id"], "name": "extra"})
                assert (await call("get_resource", {"resource_id": root}))["children"][-1]["name"] == "extra"
                await call("undo")
                await call("delete_resources", {"resource_ids": [shape["id"]]})
                from PIL import Image
                image_path = tmp_path / "input.png"
                Image.new("RGBA", (12, 8), "red").save(image_path)
                await call("import_image", {"path": str(image_path), "movieclip_id": root,
                                             "width": 60, "height": 40, "x": 5})
                imported_placement = await call("get_placement", {"movieclip_id": root, "placement_index": 1})
                assert imported_placement["matrix"] == [5, 0, 0, 5, 5, 0]
                assert (await call("validate"))["errors"] == []
                await call("save_document", {"path": str(target)})
                opened = await call("open_document", {"path": str(target)})
                assert opened["version"] == 6 and not opened["dirty"]
                return source

    try:
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(lambda: asyncio.run(session_test()))
            wait_until(future.done, timeout=40)
            source = future.result()
        assert gateway.editor(source).doc.resource(7)["framerate"] == 17
        assert Document.load(target).validate() == []
    finally:
        gateway.stop()
