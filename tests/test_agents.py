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


def test_discovery_tools_answer_the_reconnaissance_in_one_call(window, tmp_path):
    """An agent can learn a document without polling kinds and fetching each resource."""
    doc = referenced_document()
    window.add_doc(doc)
    gateway = AgentGateway(window, tmp_path / "connection.json")
    gateway.start()
    try:
        summary = gateway.execute("document_summary", {})
        # Everything the questions of a first session need, in one answer.
        assert summary["counts"]["movieclips"] == 2
        assert summary["counts"]["shapes"] == 1
        assert {"nine_slice", "sizing", "text", "children", "units"} <= set(
            summary["conventions"]
        )
        assert summary["atlases"], "atlases with their pixel sizes"
        # Exports are the entry points, reported with their kind.
        outer = next(row for row in summary["exports"] if row["id"] == 8)
        assert outer["exports"] == ["outer"] and outer["kind"] == "movieclips"

        index = gateway.execute("atlas_index", {})
        first = index["atlases"][0]
        assert first["atlas_index"] == 0
        assert first["shapes"] == [3], "the shape drawn from this atlas"
        assert first["command_count"] >= 1

        # A component that is only ever a child is still findable, which is how
        # panels and buttons are named in a real UI file.
        found = gateway.execute("find_components", {"with_size": True})
        ids = {row["id"] for row in found["components"]}
        assert {3, 7, 8} <= ids
        nested = next(row for row in found["components"] if row["id"] == 7)
        assert nested["used_as_child"] == 1
        # The fixture's clip draws a 20x20 shape and a textfield beside it,
        # so its size is the union of both, not just the shape.
        assert nested["size"][0] >= 20.0 and nested["size"][1] >= 20.0, nested
        # Searching narrows by export name and by ID.
        assert {row["id"] for row in gateway.execute(
            "find_components", {"query": "outer"})["components"]} == {8}
        assert {row["id"] for row in gateway.execute(
            "find_components", {"query": "3"})["components"]} >= {3}

        # Batch fetch, with anything unknown reported rather than failing.
        batch = gateway.execute("get_resources", {"resource_ids": [3, 7, 999]})
        assert set(batch["resources"]) == {"3", "7"} and batch["missing"] == [999]
        assert batch["resources"]["3"]["id"] == 3

        texture = gateway.execute("get_texture", {"atlas_index": 0})
        assert texture["highres"]["width"] == 2 and texture["highres"]["height"] == 2
        assert "format" in texture["highres"] and "external" in texture["highres"]
        assert texture["regions"] and texture["regions"][0]["id"] == 3
        # Out-of-range atlases are refused rather than returning nothing.
        with pytest.raises(ValueError):
            gateway.execute("get_texture", {"atlas_index": 99})
    finally:
        gateway.stop()


def test_a_clip_that_draws_itself_does_not_hang_the_size_walk(window, tmp_path):
    """A cyclic graph has no finite size; the recursion has to stop."""
    doc = populated()
    clip = doc.resource(7)
    clip["children"].append({"id": 7, "name": "self", "blend": 0})
    doc.update("movieclips", clip)
    window.add_doc(doc)
    gateway = AgentGateway(window, tmp_path / "connection.json")
    gateway.start()
    try:
        found = gateway.execute("find_components", {"with_size": True})
        row = next(r for r in found["components"] if r["id"] == 7)
        # It reports a size for the shapes it legitimately draws and does not
        # recurse forever through the self-reference.
        assert row["size"] is not None
    finally:
        gateway.stop()


def test_agent_can_author_an_animation(window, tmp_path):
    """Keying, tweening and frame editing through the animator's own operations."""
    doc = populated()
    clip = doc.resource(7)
    clip["children"] = [{"id": 3, "name": "sprite", "blend": 0}]
    clip["frames"] = [
        {"label": "", "elements": [[0, 65535, 65535]]} for _ in range(16)
    ]
    doc.update("movieclips", clip)
    window.add_doc(doc)
    gateway = AgentGateway(window, tmp_path / "connection.json")
    gateway.start()
    try:
        # The timeline reports the tracks, the selection and the curve palette.
        timeline = gateway.execute("get_timeline", {"movieclip_id": 7})
        assert timeline["frame_count"] == 16
        assert [t["row"] for t in timeline["tracks"]] == [0]
        assert timeline["tracks"][0]["keys"] == []
        assert "Overshoot" in timeline["curve_names"]

        # Key two frames at chosen transforms.
        first = gateway.execute("set_keys", {
            "movieclip_id": 7, "row": 0, "frames": [0],
            "transform": {"x": 0, "y": 0}})
        assert first["keys"] == [0]
        second = gateway.execute("set_keys", {
            "movieclip_id": 7, "row": 0, "frames": [12],
            "transform": {"x": 240, "y": 90, "rotation": 30}})
        assert second["keys"] == [0, 12]

        # A curve fills the gap and keeps the keys exact.
        tweened = gateway.execute("tween", {
            "movieclip_id": 7, "row": 0, "keys": [[0, 0], [12, 0]],
            "curve": "Overshoot", "key_density": 6})
        assert tweened["keys"][0] == 0 and tweened["keys"][-1] == 12
        assert tweened["written"], "the curve wrote the frames between the keys"
        poses = gateway.execute("get_timeline", {
            "movieclip_id": 7, "row": 0, "frames": [0, 12]})["poses"]
        assert [p["matrix"][4] for p in poses] == [0.0, 240.0]
        # Overshoot really passes the end value.
        middle = gateway.execute("get_timeline", {
            "movieclip_id": 7, "row": 0, "frames": [11]})["poses"][0]
        assert middle["matrix"][4] > 240.0

        # A frame with no key of its own reports the pose it inherits.
        unkeyed = gateway.execute("get_timeline", {
            "movieclip_id": 7, "row": 0, "frames": [2]})["poses"][0]
        assert unkeyed["keyed"] is False
        assert unkeyed["matrix"][4] > 0.0

        # Overwriting an existing key is explicit.
        with pytest.raises(ValueError):
            gateway.execute("set_keys", {
                "movieclip_id": 7, "row": 0, "frames": [12],
                "transform": {"x": 1}})
        forced = gateway.execute("set_keys", {
            "movieclip_id": 7, "row": 0, "frames": [12],
            "transform": {"x": 1}, "overwrite": True})
        assert forced["keys"] == [0, 1, 3, 5, 7, 9, 11, 12]
        assert gateway.execute("get_timeline", {
            "movieclip_id": 7, "row": 0, "frames": [12]})["poses"][0]["matrix"][4] == 1.0

        # Clearing a key returns the frame to inheriting.
        cleared = gateway.execute("clear_keys", {
            "movieclip_id": 7, "row": 0, "frames": [6]})
        assert 6 not in cleared["keys"]

        # Moving a key keeps its pose and refuses to clobber another.
        moved = gateway.execute("move_keys", {
            "movieclip_id": 7, "row": 0, "keys": [[12, 0]], "delta": 2})
        assert moved["moved"] == [14]
        assert 14 in moved["keys"] and 12 not in moved["keys"]

        # Frame editing: insert holds the previous frame's children, duplicate
        # copies the pose, and the clip always keeps a frame.
        before = gateway.execute("get_timeline", {"movieclip_id": 7})["frame_count"]
        gateway.execute("edit_frames", {
            "movieclip_id": 7, "action": "insert", "frame": 5, "count": 2})
        inserted = gateway.execute("get_timeline", {
            "movieclip_id": 7, "row": 0, "frames": [5, 6]})["poses"]
        # The inserted frames draw the child rather than blanking the stage.
        assert all(p["matrix"] is not None for p in inserted)
        duplicated = gateway.execute("edit_frames", {
            "movieclip_id": 7, "action": "duplicate", "frames": [0], "count": 1})
        assert duplicated["added"] == 1
        assert gateway.execute("get_timeline", {"movieclip_id": 7})["frame_count"] == before + 3
        gateway.execute("edit_frames", {
            "movieclip_id": 7, "action": "delete", "frames": [0, 1, 2]})
        gateway.execute("edit_frames", {"movieclip_id": 7, "action": "set_framerate",
                                        "framerate": 24})
        assert gateway.execute("get_timeline", {"movieclip_id": 7})["framerate"] == 24
        # Everything an agent wrote is a valid document.
        assert gateway.execute("validate", {})["errors"] == []

        gateway.execute("undo", {})
        gateway.execute("redo", {})
        assert gateway.execute("validate", {})["errors"] == []
    finally:
        gateway.stop()


def test_agent_animation_refuses_bad_requests(window, tmp_path):
    doc = populated()
    clip = doc.resource(7)
    clip["children"] = [{"id": 3, "name": "sprite", "blend": 0}]
    clip["frames"] = [{"label": "", "elements": [[0, 65535, 65535]]}]
    doc.update("movieclips", clip)
    window.add_doc(doc)
    gateway = AgentGateway(window, tmp_path / "connection.json")
    gateway.start()
    try:
        with pytest.raises(ValueError):
            gateway.execute("set_keys", {"movieclip_id": 7, "row": 0,
                                         "frames": [0], "curve": "Wobble"})
        with pytest.raises(ValueError):
            gateway.execute("tween", {"movieclip_id": 7, "row": 0,
                                      "keys": [[0, 0]]})
        with pytest.raises(ValueError):
            gateway.execute("edit_frames", {"movieclip_id": 7, "action": "erase"})
        with pytest.raises(ValueError):
            gateway.execute("edit_frames", {"movieclip_id": 7, "action": "set_framerate",
                                            "framerate": 0})
        # A placement row that does not exist is refused rather than ignored.
        with pytest.raises(ValueError):
            gateway.execute("set_keys", {"movieclip_id": 7, "row": 9,
                                         "frames": [0], "transform": {"x": 1}})
        # Deleting every frame cannot leave the clip empty.
        with pytest.raises(ValueError):
            gateway.execute("edit_frames", {"movieclip_id": 7, "action": "delete",
                                            "frames": [0]})
        assert gateway.execute("validate", {})["errors"] == []
    finally:
        gateway.stop()


def test_agent_can_edit_geometry_children_and_textures(window, tmp_path):
    """Geometry, child management, export names and atlases are all reachable."""
    from PIL import Image

    doc = populated()
    window.add_doc(doc)
    gateway = AgentGateway(window, tmp_path / "connection.json")
    gateway.start()
    try:
        # Geometry: vertices are [x, y, u, v], with u/v as 16-bit fractions.
        shape = gateway.execute("get_shape", {"resource_id": 3})
        assert shape["command_count"] == 1
        assert shape["size"] == [20.0, 20.0]
        assert shape["commands"][0]["vertices"] == [
            [0.0, 0.0, 0, 0], [20.0, 0.0, 65535, 0], [0.0, 20.0, 0, 65535]]
        resized = gateway.execute("set_vertices", {
            "resource_id": 3, "command_index": 0,
            "vertices": [[0, 0, 0, 0], [30, 0, 65535, 0], [0, 30, 0, 65535]]})
        assert resized["size"] == [30.0, 30.0]
        with pytest.raises(ValueError):
            gateway.execute("set_vertices", {"resource_id": 3, "command_index": 0,
                                             "vertices": [[0, 0, 0, 0]]})
        added = gateway.execute("edit_commands", {"resource_id": 3, "action": "add",
                                                  "atlas_index": 0})
        assert added["command_index"] == 1
        gateway.execute("edit_commands", {"resource_id": 3, "action": "remove",
                                          "command_index": 1})
        # A shape cannot be left with no commands.
        with pytest.raises(ValueError):
            gateway.execute("edit_commands", {"resource_id": 3, "action": "remove",
                                              "command_index": 0})

        # Children: rename, duplicate, reorder.
        renamed = gateway.execute("edit_children", {
            "movieclip_id": 7, "action": "rename", "child_index": 0, "name": "panel"})
        assert renamed["name"] == "panel"
        duplicated = gateway.execute("edit_children", {
            "movieclip_id": 7, "action": "duplicate", "child_index": 0})
        assert duplicated["child_index"] == 2
        # The duplicate shares the definition and is only added to the list.
        clip = doc.resource(7)
        assert clip["children"][2]["id"] == clip["children"][0]["id"]
        assert clip["children"][2]["name"] == "panel_copy"
        gateway.execute("edit_children", {
            "movieclip_id": 7, "action": "reorder", "child_index": 0,
            "to_front": True, "frame": 0})
        assert doc.resource(7)["frames"][0]["elements"][-1][0] == 0

        # Export names, with a clash refused rather than reassigned.
        assert gateway.execute("set_exports", {
            "resource_id": 7, "exports": ["screen"]})["exports"] == ["screen"]
        # set_exports replaces the set outright, so the fixture's own name goes.
        assert doc.names[7] == ["screen"] and doc.resource(7)["exports"] == ["screen"]
        with pytest.raises(ValueError):
            gateway.execute("set_exports", {"resource_id": 3, "exports": ["screen"]})
        gateway.execute("set_exports", {"resource_id": 7, "exports": []})
        assert 7 not in doc.names

        # Atlases: add from a PNG, then replace; export one back out.
        image = tmp_path / "art.png"
        Image.new("RGBA", (48, 24), (5, 90, 160, 255)).save(image)
        before = len(doc.tables["Textures"]["textures"])
        added_atlas = gateway.execute("edit_textures", {"action": "add",
                                                        "path": str(image)})
        assert added_atlas["atlas_index"] == before
        assert (added_atlas["width"], added_atlas["height"]) == (48, 24)
        assert len(doc.tables["Textures"]["textures"]) == before + 1
        replaced = gateway.execute("edit_textures", {
            "action": "replace", "atlas_index": 0, "path": str(image)})
        assert (replaced["width"], replaced["height"]) == (48, 24)
        written = tmp_path / "out.png"
        exported = gateway.execute("export_texture", {"atlas_index": 0,
                                                      "path": str(written)})
        assert exported["width"] == 48 and written.exists()

        assert gateway.execute("validate", {})["errors"] == []
    finally:
        gateway.stop()


def test_agent_can_find_usages_search_and_read_the_guide(window, tmp_path):
    doc = populated()
    clip = doc.resource(7)
    label = doc.resource(4)
    label["text"] = "Join"
    doc.update("textfields", label)
    clip["children"][1]["name"] = "code_txt"
    doc.update("movieclips", clip)
    window.add_doc(doc)
    gateway = AgentGateway(window, tmp_path / "connection.json")
    gateway.start()
    try:
        # Usages: who places this, through which child slot, under what name.
        usages = gateway.execute("find_usages", {"resource_id": 3})
        assert usages["count"] == 1
        row = usages["usages"][0]
        assert row["id"] == 7 and row["name"] == "sprite"
        assert row["parent_exports"] == ["demo"]

        # Search reaches text and instance names, which list_resources cannot.
        by_name = gateway.execute("search", {"query": "code"})
        assert [(m["id"], m["where"]) for m in by_name["matches"]] == [(7, "child")]
        assert by_name["matches"][0]["child_name"] == "code_txt"
        by_text = gateway.execute("search", {"query": "join"})
        assert [(m["id"], m["where"]) for m in by_text["matches"]] == [(4, "textfield")]
        by_export = gateway.execute("search", {"query": "demo"})
        assert [(m["id"], m["matched"]) for m in by_export["matches"]] == [(7, ["export"])]
        assert gateway.execute("search", {"query": "nothing-here"})["count"] == 0
        with pytest.raises(ValueError):
            gateway.execute("search", {"query": ""})
        with pytest.raises(ValueError):
            gateway.execute("search", {"query": "x", "kinds": ["widgets"]})
        # The kind filter narrows without breaking the match.
        text_only = gateway.execute("search", {"query": "join", "kinds": ["textfields"]})
        assert text_only["count"] == 1

        # The guide states the model and its traps without reading the source.
        guide = gateway.execute("guide", {})
        assert {"document", "movieclip", "placement", "matrix_bank",
                "shape", "textfield"} <= set(guide["model"])
        traps = " ".join(guide["traps"])
        assert "child_index is an index into the clip's children list" in traps
        assert "nearest earlier key" in traps
        assert "no nine-slice flag" in traps
    finally:
        gateway.stop()


def test_agent_can_export_a_standalone_graph(window, tmp_path):
    doc = populated()
    window.add_doc(doc)
    gateway = AgentGateway(window, tmp_path / "connection.json")
    gateway.start()
    try:
        target = tmp_path / "standalone.sc"
        written = gateway.execute("export_graph", {"path": str(target),
                                                   "resource_ids": [7]})
        assert written["resource_count"] == 1
        assert target.exists() and written["file_size"] > 0
        # It reloads as a valid, self-contained document with its dependencies.
        reloaded = Document.load(target)
        assert reloaded.validate() == []
        # Import remaps IDs into the new document, so the graph is checked by
        # shape: one clip whose child is the shape it drew, plus the textfield.
        kinds = sorted(kind for kind, _ in reloaded.index.values())
        assert kinds == ["movieclips", "shapes", "textfields"]
        clip = next(reloaded.resource(id) for id, (kind, _) in reloaded.index.items()
                    if kind == "movieclips")
        assert len(clip["children"]) == 2
        assert clip["children"][0]["name"] == "sprite"
        with pytest.raises(ValueError):
            gateway.execute("export_graph", {"path": str(target), "resource_ids": [999]})
    finally:
        gateway.stop()


def test_batch_runs_a_sequence_in_one_call(window, tmp_path):
    """A build-up costs one round trip and reports which step failed."""
    doc = populated()
    clip = doc.resource(7)
    clip["children"] = [{"id": 3, "name": "sprite", "blend": 0}]
    clip["frames"] = [{"label": "", "elements": [[0, 65535, 65535]]} for _ in range(20)]
    doc.update("movieclips", clip)
    window.add_doc(doc)
    gateway = AgentGateway(window, tmp_path / "connection.json")
    gateway.start()
    try:
        built = gateway.execute("batch", {"operations": [
            {"operation": "set_keys", "arguments": {
                "movieclip_id": 7, "row": 0, "frames": [0],
                "transform": {"x": 0}}},
            {"operation": "set_keys", "arguments": {
                "movieclip_id": 7, "row": 0, "frames": [19],
                "transform": {"x": 400}}},
            {"operation": "tween", "arguments": {
                "movieclip_id": 7, "row": 0, "keys": [[0, 0], [19, 0]],
                "curve": "Overshoot", "key_density": 19}},
        ]})
        assert built["completed"] == 3 and built["failed"] == []
        # The whole 20 frames are keyed in one call, and the curve overshoots.
        clip = doc.resource(7)
        bank = doc.bank(clip["matrix_bank_index"])
        xs = [bank["matrices"][f["elements"][0][1]][4] for f in clip["frames"]]
        assert all(value is not None for value in xs)
        assert max(xs) > 400.0 and xs[0] == 0.0 and xs[-1] == 400.0

        # A failure names the step and where it stopped.
        stopped = gateway.execute("batch", {"operations": [
            {"operation": "set_keys", "arguments": {
                "movieclip_id": 7, "row": 0, "frames": [1],
                "transform": {"x": 9}, "overwrite": True}},
            {"operation": "set_exports", "label": "bad id",
             "arguments": {"resource_id": 999, "exports": ["x"]}},
            {"operation": "set_keys", "label": "never runs", "arguments": {
                "movieclip_id": 7, "row": 0, "frames": [2],
                "transform": {"x": 10}}},
        ]})
        assert stopped["completed"] == 1
        assert stopped["stopped_at"] == 1
        assert stopped["failed"][0]["label"] == "bad id"
        assert "does not exist" in stopped["failed"][0]["error"]

        # continue_on_error records the failure and keeps going, and a step
        # that would be a no-op is a failure rather than a silent success.
        # The tween above keyed every frame, so the "already keyed" step fails
        # and the batch carries on to the one that does work.
        carried = gateway.execute("batch", {"continue_on_error": True, "operations": [
            {"operation": "set_exports", "label": "bad",
             "arguments": {"resource_id": 999, "exports": ["x"]}},
            {"operation": "set_keys", "label": "already keyed", "arguments": {
                "movieclip_id": 7, "row": 0, "frames": [3],
                "transform": {"x": 11}}},
            {"operation": "clear_keys", "label": "free a frame", "arguments": {
                "movieclip_id": 7, "row": 0, "frames": [4]}},
            {"operation": "set_keys", "label": "good", "arguments": {
                "movieclip_id": 7, "row": 0, "frames": [4],
                "transform": {"x": 11}}},
        ]})
        assert carried["completed"] == 2
        assert [f["label"] for f in carried["failed"]] == ["bad", "already keyed"]
        pose = gateway.execute("get_timeline", {
            "movieclip_id": 7, "row": 0, "frames": [4]})["poses"][0]
        assert pose["keyed"] is True and pose["matrix"][4] == 11.0

        # Bad batches are refused rather than half-run.
        with pytest.raises(ValueError):
            gateway.execute("batch", {"operations": []})
        with pytest.raises(ValueError):
            gateway.execute("batch", {"operations": [{"arguments": {}}]})
        with pytest.raises(ValueError):
            gateway.execute("batch", {"operations": [{"operation": "batch"}]})
        assert gateway.execute("validate", {})["errors"] == []
    finally:
        gateway.stop()


def test_a_tween_can_fill_every_frame_of_a_long_gap(window, tmp_path):
    """Asking for as many steps as the gap has frames writes every frame.

    The key-density ceiling used to be 64, so a request to fill a longer gap was
    silently spaced out again and the tween looked like it had ignored the ask.
    """
    doc = populated()
    clip = doc.resource(7)
    clip["children"] = [{"id": 3, "name": "sprite", "blend": 0}]
    clip["frames"] = [{"label": "", "elements": [[0, 65535, 65535]]} for _ in range(120)]
    doc.update("movieclips", clip)
    window.add_doc(doc)
    gateway = AgentGateway(window, tmp_path / "connection.json")
    gateway.start()
    try:
        calls = lambda **kw: gateway.execute("set_keys", kw)
        calls(movieclip_id=7, row=0, frames=[0], transform={"x": 0})
        calls(movieclip_id=7, row=0, frames=[119], transform={"x": 1190})
        written = gateway.execute("tween", {
            "movieclip_id": 7, "row": 0, "keys": [[0, 0], [119, 0]],
            "curve": "Linear", "key_density": 119})
        assert len(written["written"]) == 118
        clip = doc.resource(7)
        bank = doc.bank(clip["matrix_bank_index"])
        xs = [bank["matrices"][f["elements"][0][1]][4] for f in clip["frames"]]
        # Every frame holds its own value, evenly spaced.
        assert all(x is not None for x in xs)
        assert xs[60] == pytest.approx(600.0, abs=15.0)
    finally:
        gateway.stop()


def test_per_frame_transforms_make_a_computed_animation_one_call(window, tmp_path):
    """A spiral is one call, not one call per frame."""
    doc = populated()
    clip = doc.resource(7)
    clip["children"] = [{"id": 3, "name": "sprite", "blend": 0}]
    clip["frames"] = [{"label": "", "elements": [[0, 65535, 65535]]} for _ in range(72)]
    doc.update("movieclips", clip)
    window.add_doc(doc)
    gateway = AgentGateway(window, tmp_path / "connection.json")
    gateway.start()
    try:
        import math as maths

        frames = list(range(72))
        spiral = [
            {"x": 280 * maths.cos(i * 0.18) * (1 - i / 72),
             "y": 280 * maths.sin(i * 0.18) * (1 - i / 72),
             "rotation": i * 5.0}
            for i in frames
        ]
        result = gateway.execute("set_keys", {
            "movieclip_id": 7, "row": 0, "frames": frames, "transforms": spiral})
        assert result["key_count"] == 72
        clip = doc.resource(7)
        bank = doc.bank(clip["matrix_bank_index"])
        xs = [bank["matrices"][f["elements"][0][1]][4] for f in clip["frames"]]
        bs = [bank["matrices"][f["elements"][0][1]][1] for f in clip["frames"]]
        # Each frame holds its own position and its own rotation.
        assert len(set(round(x, 3) for x in xs)) > 60
        # rotation is stored as b = sin(theta), so it repeats each full turn;
        # the positions are what show every frame got its own pose.
        assert len(set(round(b, 3) for b in bs)) > 30
        with pytest.raises(ValueError):
            gateway.execute("set_keys", {
                "movieclip_id": 7, "row": 0, "frames": [0, 1],
                "transforms": [{"x": 1}]})
        assert gateway.execute("validate", {})["errors"] == []
    finally:
        gateway.stop()
