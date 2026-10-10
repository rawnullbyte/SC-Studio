"""Standard MCP stdio bridge to a running SC Studio GUI; stdout is protocol only."""
import argparse
import base64
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlparse
from mcp.server.fastmcp import FastMCP, Image


def create_server(connection):
    server = FastMCP("SC Studio")

    def call(operation, **arguments):
        try:
            config = json.loads(Path(connection).read_text())
        except (OSError, ValueError) as error:
            raise ValueError("Start MCP → Start agent access in SC Studio first") from error
        url = urlparse(config["url"])
        if url.scheme != "http" or url.hostname != "127.0.0.1" or url.path != "/rpc":
            raise ValueError("Invalid local SC Studio connection descriptor")
        request = Request(config["url"], data=json.dumps({"operation": operation, "arguments": arguments}).encode(),
                          headers={"Authorization": "Bearer " + config["token"], "Content-Type": "application/json"})
        with urlopen(request, timeout=95) as response:
            result = json.load(response)
        if "error" in result:
            raise ValueError(result["error"])
        return result["result"]

    @server.tool()
    def documents() -> list[dict]:
        """List open documents and stable IDs, active selection, version and unsaved state.

        Start here. Then call document_summary once for the document you are
        working in: it reports what the file holds, which pieces are reusable
        and the conventions for sizing and text, so the reconnaissance does not
        have to be done one resource at a time.
        """
        return call("documents")

    @server.tool()
    def document_summary(document_id: str | None = None) -> dict:
        """Everything about a document in one call: counts per resource kind, every atlas with its pixel size, the exported entry points, and the authoring conventions (nine-slice, sizing, text styles, naming, units).

        Prefer this over paging list_resources by kind and fetching resources to
        find out what a file contains. It is the fastest way to learn the model
        of an unfamiliar file before authoring anything in it.
        """
        return call("document_summary", document_id=document_id)

    @server.tool()
    def atlas_index(document_id: str | None = None) -> dict:
        """Every texture atlas with its size and the distinct shape IDs drawn from it, without paging through regions.

        Use this to find which atlas and shapes hold the art you need. Call
        get_texture for one atlas when you need the actual UV rectangles, which
        are in atlas pixels and can be used to size a sprite.
        """
        return call("atlas_index", document_id=document_id)

    @server.tool()
    def find_components(query: str = "", with_size: bool = False,
                        limit: int = 100, document_id: str | None = None) -> dict:
        """Search exported movieclips/shapes; with_size adds each one's rendered pixel size.

        In a real UI file the exported clips and shapes are the finished,
        reusable pieces: a panel, a button, a tab. Search here for something
        that already matches what you are building and reuse it, rather than
        authoring art from scratch. with_size lets you lay out a screen without
        fetching each candidate.
        """
        return call("find_components", document_id=document_id, query=query,
                    with_size=with_size, limit=limit)

    @server.tool()
    def open_document(path: str) -> dict:
        """Open an existing SC2 v5/v6 file in a new editor tab."""
        return call("open_document", path=path)

    @server.tool()
    def new_document(version: int = 6) -> dict:
        """Create an empty document. Version 6 is the default; 5 is also supported."""
        return call("new_document", version=version)

    @server.tool()
    def list_resources(document_id: str | None = None, kind: str | None = None,
                       query: str = "", offset: int = 0, limit: int = 100) -> dict:
        """Search/paginate resource IDs, kinds and export names. Kinds: movieclips, shapes, textfields, modifiers. Null document means active tab.

        For orientation prefer document_summary (counts, atlases, exports and
        conventions in one call) or find_components (drawable pieces with their
        sizes). Reach for this when you need a full ID listing or to match on an
        export name you already know.
        """
        return call("list_resources", document_id=document_id, kind=kind, query=query, offset=offset, limit=limit)

    @server.tool()
    def get_resource(resource_id: int, document_id: str | None = None,
                     frame_start: int = 0, frame_limit: int = 1) -> dict:
        """Inspect normalized properties, children and a frame page (max 100). Use patch_resource to preserve other frames."""
        return call("get_resource", document_id=document_id, resource_id=resource_id,
                    frame_start=frame_start, frame_limit=frame_limit)

    @server.tool()
    def get_resources(resource_ids: list[int], document_id: str | None = None,
                      frame_offset: int = 0, frame_limit: int = 1) -> dict:
        """Fetch several resources in one call, keyed by ID, with IDs that are missing listed separately.

        Use this when comparing or auditing resources: it is the batched form of
        get_resource, and asking for more than one at a time avoids a round trip
        per resource.
        """
        return call("get_resources", document_id=document_id,
                    resource_ids=resource_ids, frame_offset=frame_offset,
                    frame_limit=frame_limit)

    @server.tool()
    def get_texture(atlas_index: int, document_id: str | None = None) -> dict:
        """One atlas: its highres and lowres pixel sizes, and every referenced UV region in atlas pixels.

        The regions are what a shape samples. A sprite can be sized from its
        rectangle here without fetching the shape's geometry.
        """
        return call("get_texture", document_id=document_id, atlas_index=atlas_index)

    @server.tool()
    def references(resource_id: int, document_id: str | None = None) -> dict:
        """Complete reverse-reference graph to all roots; shared/cyclic edges are retained without recursion."""
        return call("references", document_id=document_id, resource_id=resource_id)

    @server.tool()
    def atlas_regions(atlas_index: int, offset: int = 0, limit: int = 1000,
                      document_id: str | None = None) -> dict:
        """Page through shapes and their UV rectangles in an atlas (max 5000 per page); overlapping regions stay separate, and commands with no area are reported as a single-texel rect.

This is the by-command view. To just find which atlas holds what, atlas_index
answers it without paging; get_texture returns one atlas's sizes and regions
together. Use this when you are walking an atlas's commands in bulk.
        """
        return call("atlas_regions", document_id=document_id, atlas_index=atlas_index,
                    offset=offset, limit=limit)

    @server.tool()
    def select_resource(resource_id: int, document_id: str | None = None, frame: int | None = None) -> dict:
        """Select a resource in the GUI, optionally displaying a zero-based animation frame."""
        return call("select_resource", document_id=document_id, resource_id=resource_id, frame=frame)

    @server.tool()
    def view_preview(document_id: str | None = None, resource_id: int | None = None,
                     frame: int | None = None) -> Image:
        """Return a PNG of the editor's rendered preview, optionally selecting a resource/frame. Uses the GUI renderer."""
        result = call("view_preview", document_id=document_id, resource_id=resource_id, frame=frame)
        return Image(data=base64.b64decode(result["png"]), format="png")

    @server.tool()
    def patch_resource(resource_id: int, changes: dict, document_id: str | None = None) -> dict:
        """Replace specified normalized fields, retaining all others. Supports children, frames, geometry, text, exports etc. Undoable, validated."""
        return call("patch_resource", document_id=document_id, resource_id=resource_id, changes=changes)

    @server.tool()
    def update_resource(resource: dict, document_id: str | None = None) -> dict:
        """Replace a complete normalized resource including its ID. Never submit a get_resource frame page. Undoable, validated."""
        return call("update_resource", document_id=document_id, resource=resource)

    @server.tool()
    def new_resource(kind: str, document_id: str | None = None) -> dict:
        """Create an editable movieclips/shapes/textfields/modifiers resource and return its ID. Undoable."""
        return call("new_resource", document_id=document_id, kind=kind)

    @server.tool()
    def delete_resources(resource_ids: list[int], document_id: str | None = None) -> dict:
        """Delete resources as one undo step. Fails if surviving resources still reference them."""
        return call("delete_resources", document_id=document_id, resource_ids=resource_ids)

    @server.tool()
    def duplicate_resources(resource_ids: list[int], document_id: str | None = None) -> dict:
        """Duplicate selected definitions only, sharing existing children, atlases and transform banks. Undoable."""
        return call("duplicate_resources", document_id=document_id, resource_ids=resource_ids)

    @server.tool()
    def import_resources(source_document_id: str, resource_ids: list[int], document_id: str | None = None) -> dict:
        """Copy resources from another open document, importing dependencies/atlases and remapping IDs. Undoable."""
        return call("import_resources", document_id=document_id, source_document_id=source_document_id, resource_ids=resource_ids)

    @server.tool()
    def get_placement(movieclip_id: int, placement_index: int, frame: int = 0,
                      document_id: str | None = None) -> dict:
        """Inspect a placement's child, resolved affine matrix and color transform."""
        return call("get_placement", document_id=document_id, movieclip_id=movieclip_id,
                    placement_index=placement_index, frame=frame)

    @server.tool()
    def add_child(movieclip_id: int, child_id: int, name: str = "", frame: int = 0,
                  document_id: str | None = None) -> dict:
        """Append a child instance and place it in one frame with identity transform. Undoable."""
        return call("add_child", document_id=document_id, movieclip_id=movieclip_id,
                    child_id=child_id, name=name, frame=frame)

    @server.tool()
    def transform_placement(movieclip_id: int, placement_index: int, matrix: list[float],
                            frame: int = 0, document_id: str | None = None) -> dict:
        """Set placement affine matrix [a,b,c,d,tx,ty]. Supports move, resize, rotate without changing shared transforms. Undoable."""
        return call("transform_placement", document_id=document_id, movieclip_id=movieclip_id,
                    placement_index=placement_index, matrix=matrix, frame=frame)

    @server.tool()
    def move_placement(movieclip_id: int, placement_index: int, new_index: int,
                       frame: int = 0, document_id: str | None = None) -> dict:
        """Reorder a placement's layer within a frame. Index 0 is back; the last is front. Undoable."""
        return call("move_placement", document_id=document_id, movieclip_id=movieclip_id,
                    placement_index=placement_index, new_index=new_index, frame=frame)

    @server.tool()
    def import_image(path: str, movieclip_id: int, frame: int = 0, name: str = "",
                     width: float | None = None, height: float | None = None,
                     x: float = 0, y: float = 0, angle: float = 0,
                     document_id: str | None = None) -> dict:
        """Import an image as an atlas/shape/child placement. Size defaults to image pixels; angle is degrees. Undoable."""
        return call("import_image", document_id=document_id, path=path, movieclip_id=movieclip_id,
                    frame=frame, name=name, width=width, height=height, x=x, y=y, angle=angle)

    @server.tool()
    def undo(document_id: str | None = None) -> dict:
        """Undo the last committed edit in a document."""
        return call("undo", document_id=document_id)

    @server.tool()
    def redo(document_id: str | None = None) -> dict:
        """Redo the last undone edit in a document."""
        return call("redo", document_id=document_id)

    @server.tool()
    def validate(document_id: str | None = None) -> dict:
        """Return document consistency errors without writing the file."""
        return call("validate", document_id=document_id)

    @server.tool()
    def save_document(path: str | None = None, document_id: str | None = None) -> dict:
        """Save to an explicit path or the existing document path. New documents require a path."""
        return call("save_document", document_id=document_id, path=path)

    return server


def main():
    parser = argparse.ArgumentParser(description="Connect an MCP stdio client to SC Studio")
    parser.add_argument("--connection", default=os.environ.get("SC_STUDIO_MCP_CONNECTION",
                        str(Path.home() / ".sc-studio" / "mcp-connection.json")),
                        help="Connection descriptor written by the GUI MCP menu")
    args = parser.parse_args()
    create_server(args.connection).run(transport="stdio")


if __name__ == "__main__":
    main()
