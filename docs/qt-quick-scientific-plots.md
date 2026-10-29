# Native Quick scientific plots

`ui.plot_factory` selects `QuickScientificPlot` / `QuickImageView` when the
application property `spectralQuick` is true. Existing pages retain their
acquisition, processing, saved-data and metadata paths. The spectrum adapter
inherits the existing broken-band, reference and session-range policy.

The view is a `QQuickPaintedItem` hosted in Qt Quick. It draws axes, numerical
curve geometry, image pixels and sweep annotations directly with Qt painting;
it does not photograph a legacy plot or render it in a browser. The item is a
raster-backed Quick scene item, not a GPU curve implementation. NumPy arrays
stay in Python and are never serialized into QML lists. Source/engine loading
is deferred until the view is shown or explicitly accessed for rendering.

## Coordinates and data ownership

- Curves retain immutable copies of the original x/y samples. NaN/Inf and
  explicit edge masks split paths. Broken spectrum axes use separate real
  wavelength viewports, proportional data widths and shared numerical Y limits.
- Dense monotone curves use a per-pixel min/max envelope, retaining endpoints
  and both extrema in traversal order. Full-resolution samples are returned by
  `export_arrays`; the view reduction never changes scientific exports.
- Log axes transform display coordinates, exclude nonpositive samples and keep
  untransformed original arrays. Hover coordinates report physical values.
- New image code uses `set_image(rows_by_columns, rect=QRectF(...))`. The
  `setImage(columns_by_rows)` adapter explicitly transposes existing callers.
  ImageView's inverted Y convention puts row zero at the top. Signed rectangles
  preserve descending calibration. Missing pixels are transparent.
- Image level and colormap adjustments modify the rendered raster only. Native
  min/max fields and Auto expose intensity-level adjustment. Existing page PNG
  and CSV export functions keep their own numerical export semantics;
  `export_png` is explicitly a view framebuffer export.

## Interaction and compatibility

Wheel zooms about the pointer; drag pans. Shift wheel changes X only, Control
wheel changes Y only. Keyboard arrows pan, plus/minus zoom, F/Home fits. Manual
spectrum ranges pause following; Fit routes through the spectrum policy across
all segments. Reference colors stay unchanged in stored pens; their displayed
lightness is lifted on dark surfaces. Native Qt hover tooltips expose curve
descriptions. Plot controls and labels use the shared QML theme.

Sweep preview compatibility includes numerical scatter/line layers, rectangles,
polygons, fixed-screen-size arrows, changing text/positions, pen patterns and
progress updates. Unsupported annotation classes raise immediately.

## Explicit limits

- Rectangular image calibration preserves existing page behavior; it does not
  interpolate arbitrary nonuniform per-pixel axis coordinates.
- Independent X/Y axes are supported. Requesting aspect locking raises instead
  of silently claiming equal-axis scaling.
- Nonmonotone routes and individual scatter markers remain exact and are not
  decimated, so their painting work is proportional to their sample count.
- Editable intensity limits replace the old ImageView histogram interaction.
  The hidden legacy ROI/menu controls do not become visible Quick features.

## Reproducible verification

Run the project interpreter on `tools/run_quick_plot_regressions.py`. It creates
temporary config/history/QSettings before any UI imports, enables the actual
Quick factory branch and exercises the existing spectrum, reference, broken
axis, panel/reload, BFP, Dual Gate, MegaSweep and Motion regressions alongside
numeric/interaction tests. Batch tests use offscreen/software rendering.

`tools/verify_quick_scientific.py` separately renders synthetic spectrum bands,
calibrated image pixels and sweep geometry with the native Windows Qt platform,
at 100%, 150% and 200% in light/dark. Screenshots and numerical reports go into
ignored `tmp/quick-scientific-verification`. It imports no device controllers
and does not load or write user configuration.
