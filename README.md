# SC Studio

[![CI](https://github.com/rawnullbyte/SC-Studio/actions/workflows/ci.yml/badge.svg)](https://github.com/rawnullbyte/SC-Studio/actions/workflows/ci.yml)

A Python/PyQt6 desktop editor for Supercell **SC2 versions 5 and 6**. It opens and creates SC files, edits resources, and copies movieclips between opened files together with their dependency graphs.

## Screenshots

| Light | Dark |
| :---: | :---: |
| ![Editor in the light theme](examples/preview-light.png) | ![Editor in the dark theme](examples/preview-dark.png) |

| Reference tree | Scaling-grid popup |
| :---: | :---: |
| ![Recursive reference tree](examples/preview-reference-tree.png) | ![Scaling-grid popup with resize handles](examples/preview-strip-popup.png) |

| Atlas region outlines | Atlas picker |
| :---: | :---: |
| ![Atlas region outlines drawn over the preview](examples/preview-atlas-outlines.png) | ![Shape picker over an atlas](examples/preview-atlas-picker.png) |

| MCP menu | Create menu |
| :---: | :---: |
| ![MCP menu with agent access controls](examples/preview-mcp-menu.png) | ![Create menu](examples/preview-create-menu.png) |

## Install and run

Python 3.10 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install .
sc-studio
```

Open a file directly with `sc-studio /path/to/ui.sc`. The install provides the `sc-studio` and `sc-studio-mcp` commands; `sc-studio --help` lists startup options.

For development without installing the package:

```bash
python -m pip install -r requirements.txt
python run.py
```

Qt needs a working graphical desktop — this is a desktop program, not a web UI. A platform-plugin error on Linux usually means Qt's XCB/Wayland runtime dependencies are missing. On Arch Linux, the system packages `python-pyqt6`, `python-pillow`, `python-flatbuffers` and `python-zstandard` can be used, with `texture2ddecoder` in a virtual environment.

## MCP agent access

1. Start the editor and open your files.
2. Choose **MCP → Copy client configuration**. This starts local agent access and copies a ready-to-paste MCP client configuration. **Start agent access**, **Stop agent access** and **Connection status** are in the same menu.
3. Add the copied configuration to your agent's MCP settings and connect it. Keep the editor open. Your agent's environment needs `sc-studio-mcp` on its `PATH`, or replace `command` with its absolute installed path.

Access is off by default, binds only to loopback, and uses a private per-session token. The descriptor defaults to `~/.sc-studio/mcp-connection.json`; set `SC_STUDIO_MCP_CONNECTION=/absolute/path/connection.json` when launching the GUI to give another editor instance its own file. The copied configuration picks that file up automatically. Stop access from the menu or close the editor to disconnect clients. Start with access already on using `sc-studio --mcp /path/to/ui.sc`.

The stdio server uses the [official Python MCP SDK](https://py.sdk.modelcontextprotocol.io/v1/) and talks to the running GUI. It can list, open and create documents, search resources, inspect animation frames and placements, page through atlas regions, walk the reference graph, select resources, and return preview PNGs as MCP image content. Editing tools create, update and delete resources, rename exports and edit text through `patch_resource`, add children, move/resize/rotate placements with affine matrices, reorder layers, import images, copy dependency graphs between tabs, undo/redo, validate and save. Document IDs stay stable when tabs are reordered. Edits go through the same undo history and validation as manual editing, and malformed edits roll back. Agents get an error while the editor is busy, a modal dialog is open, or unapplied inspector drafts need attention.

Tools use zero-based frame and placement indices. A `get_resource` movieclip result contains a frame page, not a complete replacement — use `patch_resource` for individual properties; passing `frames` explicitly replaces the whole timeline. Files are written only when `save_document` is requested.

Example: call `documents`, then `list_resources` with `kind="movieclips"`, inspect `get_resource`, and call `view_preview`. To change a clip's speed, use `patch_resource` with `changes={"framerate": 24}`. To resize a placement, inspect `get_placement` and pass its adjusted six-component `[a,b,c,d,tx,ty]` matrix to `transform_placement`.

## Working with files

**Copy a movieclip between files**

1. Open the source and destination files; each gets its own tab.
2. Select the source movieclip by export name or resource ID.
3. Focus the resource browser and press **Ctrl+C**, or use **Resource → Copy with dependencies**. Ctrl/Shift-click selects multiple resources.
4. Switch to the destination tab, focus its resource browser, and press **Ctrl+V**, or use **Resource → Paste resource graph**.
5. Save the destination with **Ctrl+S**.

**Resource → Import from opened file** provides a searchable source-file picker. **File → Export selected with dependencies** writes a standalone version 6 SC containing the selected resource graph. The dependency-complete graph goes to the system clipboard and stays available after the source tab closes, so another SC Studio window can paste it too. New and exported documents use SC6; an opened SC5 file keeps its format when saved.

Import follows nested movieclips, shapes, textfields, mask modifiers, all frames, child names and blend flags, texture atlases including highres/lowres variants, scaling grids, matrices, color transforms, frame labels, font references and exports. It handles recursive graphs and shared dependencies, remaps object and texture IDs, compacts each clip's transform bank, renames conflicting exports with `_copy2`, `_copy3` and so on, and reuses identical atlases. External KTX/ZKTX textures are embedded; other external textures are copied under content-based filenames alongside the destination SC. Missing sidecars fail the import and roll back its document changes.

Each imported atlas is cropped to the region the imported shapes actually sample, and their UVs are rewritten to match. Cropping works on whole ASTC blocks, which store independently with no cross-block prediction, so the retained bytes are copied verbatim and the result decodes to exactly the same pixels as the source region — no re-encoding and no quality change. Raw-format textures crop the same way by rows. An atlas that cannot be cropped safely (an external sidecar, an unsupported encoding) is copied whole instead. On a large UI file this cut imported texture bytes by about 69% across 25 clips, and a clip drawing one small sprite from a 4096×4096 sheet imports a texture a few dozen pixels across.

**Import an image into a movieclip**

1. Select or create a movieclip.
2. Click **Import image…** below the preview (Ctrl+Alt+I).
3. Choose a PNG/JPEG/WebP/BMP, set width/height, position and rotation, then accept.
4. The editor creates the texture and shape, adds a child, and places it in the current frame at the original resolution.
5. Drag the image to move it, or use **Size / position…** to resize the placement. Save with Ctrl+S; undo reverses either operation.

**Reference tree**

Select a resource and use **Resource → Reference tree** (**Ctrl+Shift+R**), or the **Reference tree** tab in **Find usages**. Expand each parent to follow parents of parents up to a root with no referencing parents. Shared branches stay independently expandable; cyclic branches show **Cycle** and stop. Nodes load on expansion so large shared graphs do not build every possible path at once. Double-click a node to open it. The Usages tab filters direct/indirect instance references. The tree is a reverse-reference tree (what uses this resource), based on all child definitions including those not placed in the current frame.

**Texture atlases**

Open an atlas from the texture list. Every draw command that samples it is outlined, so the whole sheet is browsable: double-click any outline to open that shape, or right-click it to open the shape, run **Find usages**, or open a **Reference tree**. **Browse atlas shapes…** lists them with their command, reference and UV-rectangle columns. **Show referenced asset outlines** toggles the overlay.

Most commands in a real UI atlas are *zero-area* — all four corners sample a single texel, so there is no rectangle to outline. Those are drawn as small diamond markers over the texel they sample and are pickable and openable like any other region, which keeps shapes that would otherwise be invisible in the atlas reachable. Several shapes commonly share one texel, so a click can land on more than one; the innermost match opens and the rest stay available in the right-click menu. Atlas commands are also dense — a single sheet can hold tens of thousands — so the marker overlay is what makes atlas-wide browsing practical.
**Keyframe animation**

A keyframe strip sits under the preview, so a timeline is visible and editable while you work rather than living in the Frames tab. It shows every frame of the open movieclip with the keyframes for the selected placement marked as diamonds, and the current frame highlighted.

- **Left click** a frame to go there; clicking a keyframe also arms a drag, so dragging it moves the key to another frame.
- **Right click** a frame to add a keyframe on it, or remove the one already there.
- Picking a different placement in the Elements list switches the strip to that placement's keys.

A keyframe is the placement's transform on that frame. Adding one copies the transform the placement currently renders with, so creating a key never moves the object on screen. Keyframes are created in place — there is no need to duplicate frames by hand first.

**Between keyframes** chooses the curve used when filling the frames between two keys: Linear, Ease in, Ease out, Ease in-out, Smooth, Snap in, Snap out, Overshoot and Anticipate, drawn as a preview beside the list. **Bake tween** writes those in-between transforms as real frames; **Revert to keys** removes exactly the frames the last bake wrote, so keyframes you set yourself are never touched. The SC2 format stores one discrete transform per frame and has nowhere to record a curve, so a curve is an authoring choice that is baked into frames — the game reads ordinary frames.

Rotation interpolates by the shorter way round, and scale is kept positive through a turn, which a direct blend of the matrix components would not do.

## Interface

The compact menu bar has **File, Edit, View, Resource, Create, MCP, Help**. The right sidebar holds one panel at a time, chosen from the **INSPECTOR** dropdown: **Properties**, **Timeline**, **JSON**, **Textures**, **Geometry** and **Children**. Picking an entry swaps the panel below, and the dropdown stays available on every panel — including Children, which holds the child list and its editing actions (**Ctrl+1** through **Ctrl+6**). Drag splitters to resize panes, hide browser/inspector from View, or choose Reset layout. File paths and resource IDs/names appear in tooltips; clicking the ID header sorts numerically. The category dropdown lists only Movieclips, Shapes, Textfields, Modifiers, Texture atlases or Exports. Search applies to the chosen category and is debounced for large files; Enter selects the first match, Escape clears search and returns focus to the browser.

Press **G** anywhere outside a text field to open **Go to ID** and jump straight to a resource ID; **Ctrl+G** does the same and **F1** lists every shortcut. The single letter is ignored while you are typing in a search box, property field, spin box or the JSON editor, where the key belongs to the text instead.

Right-click a placed element in the preview to rename/open its child, edit size or transform/color, reset its transform, duplicate/hide the child, or delete a placement/child. **Bring to front**, **Move forward**, **Move backward**, **Send to back** and **Layer position…** modify placement order in the current frame. Layer 0 is drawn at the back; SC uses this ordered frame array rather than a separate stored z-index field. These edits support undo/redo.

A resize drag changes only the live preview until release, which creates one undo entry. It preserves the asset geometry, other frames, other placements sharing the transform, and scaling-grid metadata. Initial preview zoom and pan are retained while resizing.

**View → Theme** offers System, Light and Dark, plus sixteen named schemes grouped as **Dark schemes** (Nord, four Catppuccin variants, Dracula, Monokai, One Dark, GitHub Dark, Solarized Dark, Gruvbox Dark, Tokyo Night) and **Light schemes** (Catppuccin Latte, GitHub Light, Solarized Light, Gruvbox Light, One Light). Each scheme's colors come from its own project and are mapped onto Qt palette roles here, so no third-party code or data is bundled and there is no extra dependency. System follows live operating-system color-scheme changes; UI text, menus, headers, popups, disabled controls and dialogs have explicit palette colors. Game text uses its stored color. Theme, window geometry, splitter sizes, last open directory and ten recent files persist in the normal platform settings store. Native file pickers are requested for all file and image dialogs, with a Qt fallback when the desktop/portal has no native implementation.

### Flags

Textfield style bits: `1` device font, `2` outline, `4` bold, `8` italic, `16` multiline, `32` unknown, `64` auto kern. Alignment bits and blend/modifier values are retained as their underlying format values. Unrecognized enum values are retained, not normalized away.

## Support boundaries

| Area | Status |
| --- | --- |
| SC2 version 5 | Create, read, edit, write |
| SC2 version 6 | Default for new/exported files; create/read/edit/write; external transform banks preserved or extended |
| Movieclips, shapes, textfields, exports, modifiers | All fields in the bundled schemas retained and exposed through properties/JSON or geometry/timeline editors |
| Float/half matrices and color transforms | Read/preserve; placement edits and imported banks use float matrices |
| SC6 external packed matrices and compressed frame data | Decoder implemented; original external data preserved |
| Embedded and external texture bytes | Preserved; imported external sidecars copied |
| Raw RGBA/RGBA4444/RGB5A1/RGB565/LA/L | Preview and PNG export |
| KTX1 ASTC/ETC/BC1/BC3/PVRTC | Preview decoder; encoded bytes preserved on save |
| SCTX and unsupported KTX encodings | Preserved/copied; preview unavailable |
| Legacy SC1/tag-based files | **Not supported**; clear error on opening |
| Newer unknown FlatBuffers table fields | Opening rejected to prevent silent field loss |
| Game engine rendering | **Approximate preview**, not Titan Engine |

**This is not universal support for every SC variant.** The source repositories expose a reverse-engineered format. Unknown or opaque DataStorage byte fields are retained when editing existing files, but their meaning is not inferred. A format revision that adds fields to fixed-size structs requires updated schemas.

The preview renders triangle-strip shapes, nested timeline transforms, text and alpha. It does not simulate RGB multiply/add effects, stencil masks, blend modes, engine text outline/typography, or game font substitution. Those stored properties remain editable and survive saving and import; the game must supply its actual fonts and typography resources, as this editor does not bundle them. Scaling grids are supported for identity shape children; grids with transformed children fall back to affine rendering, and arbitrary cross-grid triangles are not subdivided. The preview caps rendering at 3,000 active objects, retains at most roughly 6,000 scene instances, and omits cycles. Shape previews rasterize to a maximum dimension of 2,048 pixels, which does not change saved geometry or images. Edited files have not been verified inside the game.

Preview performance: the renderer keeps scene instances and shared texture data between frames, caches rasterized meshes (128 MiB) and decoded atlases (640 MiB), and lazily decodes only the required frame placements. Resource JSON is generated when its panel is opened. Playback advances one frame per tick at the clip's framerate, so a heavy frame slows the animation down rather than skipping ahead. The scrollable area is fixed for the whole playthrough, so the view does not drift as objects move. It refreshes placement-table cells when paused rather than every tick. Initial texture/bank decoding can still cause a one-time pause.

Editing large files: an edit snapshots the document for undo, validates what it touched, and commits. Snapshots are pickled rather than deep-copied, structural checks are scoped to the edited resources (with a full sweep on save, on deletes, and periodically), and a browser relabel replaces a full rebuild for edits that cannot change the listing. **Help → Performance** reports the rolling cost of snapshots, validation, commits and frame rendering, which is the quickest way to see what a slow file is spending time on.

## Tests

```bash
python -m pip install pytest
python -m pytest tests -q
```

Most tests run offscreen. Qt tests should be run with `QT_QPA_PLATFORM=offscreen` on a headless machine. Tests that need a real SC file are skipped unless one is supplied through `SC_SAMPLE`:

```bash
SC_SAMPLE=/path/to/ui.sc QT_QPA_PLATFORM=offscreen python -m pytest tests -q
```

## Format sources and licenses

The implementation uses an independent schema-driven Python FlatBuffers codec. The bundled SC2 schemas and packed decoding logic are based on [SupercellFlash](https://github.com/sc-workshop/SupercellFlash), the backend referenced by [lilmuff2/X-coder](https://github.com/lilmuff2/X-coder). The newer [xcoder-tool/XCoder](https://github.com/xcoder-tool/XCoder) and its `sc-py` / `sc-compression` dependencies were inspected for format and container handling; their CLI was not wrapped as a substitute for editing.

Inspected revisions:

- SupercellFlash: `41e894d5a20cc17e47fe32db3106c4c1bec60a3e`
- xcoder-tool/XCoder: `c14a7949c198c362a93d931edf1ae96bfa797b2d`
- lilmuff2/X-coder: `285c36067251cb3b118f006329bc94785d09f183`

SC Studio source is GPL-3.0-or-later (`LICENSE`). SupercellFlash's MIT notice is retained in `THIRD_PARTY_LICENSE.txt`. PyQt6 is separately available under GPL/commercial terms; all dependencies retain their respective licenses. Extracted game assets retain their original ownership.