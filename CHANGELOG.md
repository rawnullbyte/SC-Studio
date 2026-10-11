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
