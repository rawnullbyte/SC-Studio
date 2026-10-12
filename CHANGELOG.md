# 0.6.2

- Dragging a key onto an occupied frame now swaps the two poses instead of refusing; a multi-key selection still refuses a colliding drop, with the reason in the status bar.
- The animator's X/Y/Rotation/Scale fields follow the selected placement, so selecting another child or placement shows that object's transform instead of the last one's.
- Copy, Duplicate and Delete act on the focused child when the Children or Placements list has focus. Delete on the Frames list or the timeline removes the picked frame, never the open movieclip.
- Added a **Snap to pixels** toggle (on by default, remembered) so a dragged placement lands on whole asset pixels.
- Textfield font and outline colors are edited with a color swatch, and alignment with two named dropdowns, in the Properties panel and the Text style dialog; the preview now applies alignment, and a textfield is measured and resized by its authored box rather than its glyph height.
- Added **Import animation (GIF/WebP)…** (Ctrl+Alt+A): one movieclip frame per source frame, packed into one atlas, with configurable frame-name prefix/suffix and framerate. Video formats are not decoded.
- Added geometry **Copy vertices** / **Paste vertices** to transplant a vertex list between shapes, **Export PNG…** for a shape's rendered image, and a plain **X** shortcut to open Find usages on the focused child.
- Added this release's focused GUI coverage and a `tests/conftest.py` that forces the offscreen Qt platform so the suite never opens real windows.
- Fixed the Children list's **Delete** button deleting the first child whatever row was picked: `QPushButton.clicked` passes its checked state, which `remove_child` read as a child index (`False` is `0`). The same signal now reaches the Children, Placements and Frames lists, so **Del** removes the focused child.
- Copy, Duplicate, Delete and Find usages now act on a placement picked **in the preview**, not just on one picked in a list. The preview was neither a Delete shortcut target nor a focus the child index was read from, so **Del** on an object clicked in the viewer did nothing. Delete in the preview acts on the picked object and nothing else — never on the whole resource.
- Removed the confirmation dialog when deleting a child. The change is one undo step, so Ctrl+Z takes it straight back, and the status bar says what was removed instead.
- Removing the last placement of a child now removes the child too: SC has no per-frame child list, so an unplaced entry only lingered in the Children list with no way back from any frame.
- Fixed a placement being nudged by a plain click. With snapping on, a zero-movement press was rounded onto the pixel grid, which moved a placement already sitting between pixels; only movement off the press pixel is a drag now. A press on a resize handle does nothing too, rather than committing float noise as a resize.
- Fixed publishing external KTX sidecars failing on Windows: sidecar keys are stored POSIX-style but the save path re-derived them with `str(Path(...))`, which renders backslashes there and missed every key.

Validation: **216 passed, 5 skipped** in the full suite (`python -m pytest tests/`). The three fixture-dependent skips that need real SC files remain. `test_table_tsv_paste_and_preview_frame_sync` passes in a full run but not in isolation; that ordering sensitivity predates this release.

# 0.6.1

- Fixed playback progress range and frame-label synchronization; the transport slider now stops at the last real frame.
- Separated the authoring cursor from the transport so only explicit timeline authoring seeks can enter headroom.
- Added fit-all timeline layout, pointer-anchored zoom, zoom buttons, horizontal/vertical scrolling, dense key markers and playback-follow scrolling. Keys are never dropped to fit the view.
- Added an inline Children filter for instance names, IDs and exports, with Ctrl+F, Enter and Escape support; filtering preserves original row indices.
- Moved the timeline into a dedicated module and added interaction regression coverage.

Validation: **190 passed, 5 skipped** in the full suite, plus focused GUI/MCP checks after the module split. Existing fixture-dependent skips remain.

# 0.6.0

- Added non-destructive publishing, JSON presets, a CLI and MCP publishing.
- Added multi-document export, replacement of destination export linkages/references, deterministic atlas region deduplication/repacking, edge padding/extrusion, texture reduction, lowres generation, RGBA8/RGBA4/RGB565/KTX output, external KTX/ZKTX sidecars, optional text stripping, low-precision matrices, SC5 short frames and configurable Zstandard compression.
- Added SVG artwork import and solid/gradient/path primitive creation.
- Added grouping into animated symbols, nine-slice cutting, parenting, polyline motion guides, color tweens, matching-topology shape morphs, text styling, persistent custom cubic easing and collision-checked batch export renaming.
- Added a separate effects renderer/viewer with color transforms, conventional blend modes, stencil ranges, text styles, playback, stable-canvas onion skin and PNG/GIF/WebP exports.
- Added a searchable command palette and new MCP authoring/rendering tools.
- Preserved shear during matrix interpolation.
- Split portable authoring, publishing, rendering and their dialogs into dedicated modules.

Validation: **181 passed, 5 skipped** in the final full test run. The skipped existing tests need real SC fixtures absent from the uploaded projects. New feature modules pass focused static checks, all package modules compile, and the distributable wheel builds.

See FEATURE_COVERAGE.md for the full comparison. This release does not implement SC1, SCTX/streaming, new ASTC compression, direct FLA/XFL ingestion, or all of Adobe Animate's DOM/tween/brush behavior.
