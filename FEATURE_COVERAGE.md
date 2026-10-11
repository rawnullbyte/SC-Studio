# Supercell Animate comparison — SC Studio 0.6.0

This release implements a substantial set of portable equivalents. **It is not full Adobe Animate/plugin parity.** The supplied plugin delegates authoring, DOM evaluation, some rasterization and animation to Animate and native libraries. Its Adobe/third-party submodules are absent from the supplied ZIP. SC Studio remains a standalone Python editor for SC2 v5/v6, rather than an Adobe host or a FLA compiler.

The audit used the supplied plugin README, `features.json`, `PublisherSettings.ts`, `TextFieldWriter.cpp`, `MovieclipWriter.cpp`, and `Writer.cpp`. Where `features.json` says a host feature is enabled but the README says it is not exported, the comparison does not count the host declaration as working SC output.

| Plugin capability | SC Studio implementation | Scope / remaining difference |
| --- | --- | --- |
| Linkage/export-name selection | Publish linked symbols or selected dependency graphs | Exports in the existing SC document are the linkage names |
| Publish settings and output | File → Publish; JSON presets; persisted defaults; CLI; MCP | Publishes a separate SC output and preserves open sources |
| Multiple documents | Select several open tabs in Publish; multiple CLI sources | Inputs are SC documents |
| Export into another file | Merge destination SC; replace matching exports and referring instances | Replacement leaves old unlinked resources rather than deleting potentially useful assets |
| Atlas repacking | Deduplicated region packing, several pages, configurable padding with extrusion | Repacking decodes compressed textures to the chosen output encoding; use ordinary Save for untouched byte preservation |
| Maximum atlas dimensions | Configurable 16–8192 dimensions | Packing fails clearly if a single sampled region cannot fit |
| Texture reduction | Continuous scale from 0.01 to 1 | Shape geometry keeps its original size |
| Low-resolution variants | Generate half-size lowres images | Stored in SC texture variant fields |
| Raw texture quality | RGBA8, RGBA4, RGB565 | RGB565 rejects transparent art; RGBA4 reduces precision |
| KTX | RGBA8 KTX1 writer; embedded, external or Zstandard-compressed external | **No new ASTC/ETC/PVRTC encoding**; existing KTX decode/preserve support remains |
| SCTX / streaming textures | Not implemented | Requires another codec/container implementation |
| SC2 publishing | Versions 5 and 6 | Other SC2 revisions are not supported |
| SC1 / legacy compatibility | Not implemented | Legacy tag writer/reader and LZMA/LZHAM compatibility remain absent |
| Low-precision matrices | Quantized half matrices with explicit scale/translation precision | Out-of-range banks retain float matrices rather than clipping values |
| Short frames | Optional compact descriptors for unlabeled SC5 clips | SC6's supplied schema has no short-frame field; labeled frames stay full |
| Compression | Configurable Zstandard level 1–22 | SC1 LZMA/LZHAM modes remain absent |
| Stroke / fill / vector graphics | SVG import and rasterization; rectangle, rounded rectangle, ellipse and editable SVG-path artwork | Qt SVG supports common paths, strokes, solid/linear/radial and image fills. Animate-specific brushes, meshes and stroke styles are not imported from FLA |
| Bitmap sprites / graphics | Existing image import, texture/shape creation, dependency import | SVG/vector artwork becomes SC bitmap geometry |
| Movieclips / graphics | Existing resource creation and animator; new animated track grouping | No separate Adobe Button symbol type |
| Transform and color effects | Existing matrix/color editing; new color tweens and effects compositor | Positive byte color transforms; negative effects cannot be stored in SC |
| Blending | All 14 conventional SC/Flash mode values in the rendered-effects viewer/export | Interactive editable scene still uses its normal blend preview. Subtract is an approximation; game rendering may differ |
| Text | Dedicated text-style dialog; device/bold/italic/multiline/kerning flags; outline; horizontal/vertical alignment preview | Font substitution depends on installed fonts. Typography/bending, rich paragraph styles and exact game metrics remain unsupported; vertical alignment uses the conventional packed field interpretation |
| Write field contents | Publish option to retain or strip text strings | Text remains editable in the source |
| Normal layers | Existing per-frame draw order and child tracks | SC draw order is placement order |
| Masks | Create mask ranges using modifier tags 38/39/40; nested stencil-range compositor | Full effects preview is in the separate rendered viewer; unusual game stencil behavior may differ |
| Folders / groups | Group tracks into a nested animated symbol | Requires a contiguous placement range in each frame; preserves frames and local poses |
| Guides | Bake motion along XY guide points, optional orientation and easing | Polyline guides; not Adobe classic-motion-guide DOM evaluation |
| Parenting / rigging | Bake a child-index parent graph to world matrices | Bake is undoable; SC stores resulting frames, not editable rig metadata |
| Classic / motion animation | Existing keyed frame editing and easing; custom cubic curves; motion-guide and color baking | Does not evaluate every Adobe tween type/property or FLA motion object |
| Shape animation | Bake matching command/vertex topology and UVs to a clip | Endpoint shapes must share textures/topology; not arbitrary Adobe shape-hint morphing |
| Nine-slice | Cut a bitmap quad into nine commands and create a scaling-grid clip | Axis-aligned bitmap quads; existing grid editing remains available |
| Shape merging / aggressive rasterization | Atlas tile deduplication; SVG rasterization; explicit grouping | Native plugin's shape-merge optimizer and aggressive DOM rasterizer are not ported |
| Custom symbol properties | Existing SC fields are retained during dependency conversion | No Adobe custom-property DOM/import pipeline |
| Camera / 3D | Not implemented | Plugin documentation also limits/does not support these as general SC features |
| Scripts / sound / filters | Not exported as executable content | Plugin README excludes scripts/sound/general filters; SC text outline is supported separately |
| FLA/XFL documents | Not implemented | Use Animate/plugin to export SC2, then open the resulting SC in Studio |

## Quality of life and code changes

- Ctrl+Shift+P opens a searchable command palette.
- Ctrl+Alt+P opens Publish; Ctrl+Alt+R opens rendered effects/onion skin/export.
- Rendered viewer has playback, a stable animation canvas, scrubbing, neighboring-frame onion skin, current PNG export, and GIF/lossless WebP animation export.
- Batch export-name replacement validates collisions before committing.
- Custom cubic easing curves persist in application settings.
- Tween decomposition now retains shear/skew instead of silently discarding it.
- Portable authoring, publishing, dialogs and raster rendering live in separate modules rather than expanding the editor monolith.
- GUI and MCP use the same authoring functions and mutation/validation/undo history.
- Raster export has explicit size, recursion, object and layer-memory limits, with actionable errors.
- `authoring.transaction(doc)` provides rollback for standalone Python compound edits. The GUI/MCP supply their existing mutation transaction automatically.

## Validation

Tests cover existing editing/import behavior and new publishing round trips, encoding pixels, external sidecars, alias replacement, atlas deduplication/multiple pages, matrix quantization, short frames, parent composition/cycles, independent color/matrix holds, morphs, nine-slice geometry, grouped tracks, motion guides, SVG gradients, RGB effects, blending, stencil clipping, animation export, GUI actions/dialog settings, and MCP undo/rollback/publish/render.

Some existing integration tests require real game-file fixtures that were not included in these two uploads. Their skipped status is retained and reported; generated-fixture checks are not claimed as validation against every game asset.
