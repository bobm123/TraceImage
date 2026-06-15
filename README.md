# TraceImage

A desktop GUI tool for tracing the outlines of objects in photographs and exporting
**true-scale SVG** files, with ruler-based calibration and tiled large-format printing.

See [`PLAN.md`](PLAN.md) for the full design and rationale.

## Status

- **Phase 0 — Scaffold:** ✅ app skeleton that loads a photo with zoom/pan.
- **Phase 1 — Calibration:** ✅ two-point measurement, unit selection, live scale + real-size readout.
- **Phase 2 — Segmentation:** ✅ foreground/background seed painting, GrabCut, contour extraction with holes, editable polygons (move/add/delete vertices), re-seed/re-run refine loop.
- **Phase 3 — Object model:** ✅ multiple objects (each with outer + holes + disjoint loops) via the Objects panel (new/delete/rename/select), margin control, bounding-box preview, real-size readout.
- **Phase 4 — SVG export:** ✅ mm-scaled `<svg>` with matching viewBox, optional base64-embedded (downscalable) photo layer, one compound `<path>` per object with `fill-rule="evenodd"`, outline or filled. Plain (browser-friendly) or **Inkscape** flavor (named layers + mm document units) so nodes can be fine-tuned in Inkscape.
- **Phase 5 — Tiled printing:** ✅ split the drawing across Letter/A4/Legal/A3 pages (portrait/landscape) at true 1:1, with per-tile live-area rectangle, R#-C# label, and diamond registration marks that coincide across overlapping sheets; photo on/off.
- **Phase 6 — Polish (in progress):** ✅ save/load project as JSON (File → Save/Open Project; calibration, margin, units, traced polygons, source-image reference); ✅ undo/redo command stack for vertex edits (move/add/delete) and object delete; ✅ marquee (rubber-band) multi-vertex selection in Edit Vertices mode with **Delete** to group-delete (one undo step); if a delete would leave a contour with fewer than 3 vertices the whole contour is removed (and restored on undo), since a sub-3 contour is not a polygon. Ctrl+Z/Y dispatch by context — while seeding they undo/redo seed strokes, otherwise the edit command stack. Still to come: folding object-create/trace into the undo history. (Bézier smoothing deprioritized in favor of the Inkscape export flavor.)
- **Phase 7 — Port readiness:** not started.

Keyboard: **Ctrl+O** open project, **Ctrl+S** save project, **Ctrl+Shift+O** import photo, **Ctrl+E** export SVG, **Ctrl+Z/Ctrl+Y** undo/redo (seed strokes while seeding; vertex/object edits otherwise).

## Install

Python 3.10+ recommended.

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

Confirm the stack imports:

```bash
python -c "import cv2, numpy, PySide6; print('ok')"
```

## Run

From the project root:

```bash
python -m traceimage.main
```

Or install the package (editable) and use the console entry point:

```bash
pip install -e .
traceimage
```

(`src/` is also added to `sys.path` by the entry point, so the `python -m`
form works without installing.)

## Usage so far

1. **File → Import Photo…** (Ctrl+Shift+O) to load an image. Scroll wheel
   zooms; in Pan/Zoom mode drag to pan, and from *any* mode you can hold the
   middle mouse button (wheel) to pan. **Fit** rescales to the window.
2. **Tools → Calibrate Scale…** then click two points on a feature of known
   length (e.g. a ruler). Enter the real distance and unit in the dialog.
3. The status bar shows `mm/px` and the computed real-world size of the photo.
4. **Trace an object:** pick **Mark Foreground (inside)** and paint a few
   strokes inside the object, then **Mark Background (outside)** and paint
   outside it (the brush defaults to 40 px; change it in the toolbar or the
   right-click menu, and a ring previews its size). Click **Trace Poly** to run
   GrabCut and extract the outline (plus any interior holes). A right-click on
   the canvas gives quick access to the marking tools, Trace Poly, and zoom.
5. **Edit Vertices:** drag a handle to move a point, right-click a handle to
   delete it, double-click an edge to insert a point. Drag a box over empty
   canvas to marquee-select several vertices, then press **Delete** to remove
   them at once (dropping a contour entirely if it would fall below 3 points).
   To improve a poor result, add more seeds and Trace Poly again.
6. **More objects:** in the **Objects** panel click **New**, then seed and Run
   Segmentation again to trace another object. Set the **Margin** there; the
   dashed bounding box shows the exported extent and real size.
7. **File → Export SVG…** choose whether to embed the photo (and downscale it),
   outline vs filled, and plain vs **Inkscape** format. The SVG is sized in
   millimetres at true 1:1 scale. Plain SVG opens in any browser; Inkscape
   format adds named layers (photo locked) and mm document units so you can
   fine-tune control points there.
8. **File → Export Print Tiles…** pick page size, orientation, printer margin
   and overlap to split the drawing across pages at 1:1. Each tile carries a
   live-area rectangle, an R#-C# label, and diamond registration marks that
   line up when you overlap the printed sheets. Print with auto-scaling / "fit
   to page" OFF; if the diamonds don't line up at the stated overlap, scaling
   was applied somewhere.
9. **Projects:** **File → Save Project** (Ctrl+S) writes a `.tiproj.json` with
   the calibration, margin, units, traced polygons and a reference to the
   source image; **File → Open Project** (Ctrl+O) reloads it (prompting you to
   locate the image if it has moved).

Notes / current rough edges:

- In Edit Vertices mode, left-drag on empty canvas makes a selection box rather
  than panning; pan with the wheel/zoom or by holding the middle mouse button.
  Double-clicking an edge to insert a point is easiest when zoomed in.
- Seeds map 1:1 onto image pixels. If a photo carries EXIF rotation that Qt
  applies but OpenCV does not, pre-rotate the file before tracing (handled more
  robustly in a later phase).
- Deleting a contour's only contour leaves the object empty in the Objects
  panel; remove it there if you don't want it (export ignores empty objects).

## Tests

Pure-Python tests cover the GUI-free core: calibration, geometry, SVG export,
tiling, the undo stack, and project save/load. Segmentation/contour tests use
NumPy + OpenCV and are skipped if those aren't installed.

```bash
python -m pytest tests/
```

## Layout

```
src/traceimage/
  main.py          entry point
  model.py         Project / TracedObject / Contour
  core/            image_io, calibration, segmentation, contours,
                   geometry, svg_export, tiling, project_io, undo
  ui/              main_window, canvas, objects, editable, dialogs
tests/             unit tests for the GUI-free core
samples/           sample photos
output/            generated SVGs (git-ignored)
```
