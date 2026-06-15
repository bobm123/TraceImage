"""Canvas: a QGraphicsView/QGraphicsScene showing the photo with zoom/pan,
two-point calibration, and foreground/background seed painting.

The photo is a background pixmap item; calibration markers, seed-stroke
overlays, and editable contour items are drawn on top. Modes:
  * pan        - drag to pan (Phase 0)
  * calibrate  - click two points (Phase 1)
  * seed_fg / seed_bg - paint inside/outside seeds (Phase 2)
  * edit       - interact with contour vertex handles (Phase 2)

Seed strokes support undo/redo: each painted stroke is one step.
"""

from PySide6.QtCore import Qt, QPoint, QPointF, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView

# Interaction modes.
MODE_PAN = "pan"
MODE_CALIBRATE = "calibrate"
MODE_SEED_FG = "seed_fg"
MODE_SEED_BG = "seed_bg"
MODE_EDIT = "edit"

_ZOOM_STEP = 1.25
_MIN_SCALE = 0.02
_MAX_SCALE = 50.0

_FG_COLOR = QColor(0, 200, 0, 100)
_BG_COLOR = QColor(220, 40, 40, 100)


class Canvas(QGraphicsView):
    """Photo viewport with zoom/pan, calibration and seed painting."""

    # Emitted with the two picked scene points once both are placed.
    calibrationPicked = Signal(QPointF, QPointF)
    # Emitted (live) as the cursor moves over the scene, in pixel coords.
    cursorMoved = Signal(QPointF)
    # Emitted on right-click (outside edit mode) with the global position, so
    # the main window can pop up a quick-action menu.
    contextMenuRequested = Signal(QPoint)
    # Emitted whenever the seed-stroke set changes, so undo/redo buttons can
    # refresh their enabled state.
    seedsChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        self.setRenderHints(
            QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setMouseTracking(True)
        self.setBackgroundBrush(QBrush(QColor(40, 40, 40)))

        self._photo_item = None
        self._scale = 1.0
        self._mode = MODE_PAN

        # Calibration overlay.
        self._calib_points = []
        self._calib_markers = []
        self._calib_line = None

        # Seed painting.
        self._brush_radius = 40.0
        self._seed_strokes = []      # list of dict(label, points, path, item)
        self._redo_strokes = []      # strokes removed by undo, for redo
        self._active_stroke = None

        # Bounding-box preview.
        self._bbox_item = None

        # Brush footprint ring (shown in seed modes).
        self._brush_cursor = None

    # ----- photo -----------------------------------------------------------

    def set_photo(self, pixmap):
        """Replace the displayed photo and reset all overlays and the view."""
        self._scene.clear()
        self._calib_points = []
        self._calib_markers = []
        self._calib_line = None
        self._seed_strokes = []
        self._redo_strokes = []
        self._active_stroke = None
        self._bbox_item = None
        self._brush_cursor = None
        self._photo_item = self._scene.addPixmap(pixmap)
        self._photo_item.setZValue(0)
        self._scene.setSceneRect(self._photo_item.boundingRect())
        self.fit_to_view()
        self.seedsChanged.emit()

    def has_photo(self):
        return self._photo_item is not None

    def scene_obj(self):
        """The QGraphicsScene, for hosting editable contour items."""
        return self._scene

    # ----- zoom / pan ------------------------------------------------------

    def fit_to_view(self):
        if self._photo_item is None:
            return
        self.fitInView(self._photo_item, Qt.KeepAspectRatio)
        self._scale = self.transform().m11()

    def zoom_in(self):
        self._apply_zoom(_ZOOM_STEP)

    def zoom_out(self):
        self._apply_zoom(1.0 / _ZOOM_STEP)

    def _apply_zoom(self, factor):
        new_scale = self._scale * factor
        if new_scale < _MIN_SCALE or new_scale > _MAX_SCALE:
            return
        self._scale = new_scale
        self.scale(factor, factor)

    def wheelEvent(self, event):
        if self._photo_item is None:
            return
        if event.angleDelta().y() > 0:
            self._apply_zoom(_ZOOM_STEP)
        else:
            self._apply_zoom(1.0 / _ZOOM_STEP)

    # ----- mode switching --------------------------------------------------

    def enter_pan_mode(self):
        self._mode = MODE_PAN
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.unsetCursor()
        self._hide_brush_cursor()

    def enter_edit_mode(self):
        # NoDrag so clicks reach the vertex handles instead of panning.
        self._mode = MODE_EDIT
        self.setDragMode(QGraphicsView.NoDrag)
        self.unsetCursor()
        self._hide_brush_cursor()

    # ----- calibration mode ------------------------------------------------

    def start_calibration(self):
        self._clear_calibration_overlay()
        self._calib_points = []
        self._mode = MODE_CALIBRATE
        self.setDragMode(QGraphicsView.NoDrag)
        self.setCursor(Qt.CrossCursor)
        self._hide_brush_cursor()

    def cancel_calibration(self):
        self.enter_pan_mode()

    def _clear_calibration_overlay(self):
        for item in self._calib_markers:
            self._scene.removeItem(item)
        self._calib_markers = []
        if self._calib_line is not None:
            self._scene.removeItem(self._calib_line)
            self._calib_line = None

    def _add_marker(self, pt):
        r = 4.0
        pen = QPen(QColor(255, 80, 80))
        pen.setCosmetic(True)
        pen.setWidth(2)
        item = self._scene.addEllipse(pt.x() - r, pt.y() - r, 2 * r, 2 * r, pen)
        item.setZValue(15)
        self._calib_markers.append(item)

    def _add_line(self, p0, p1):
        pen = QPen(QColor(255, 80, 80))
        pen.setCosmetic(True)
        pen.setWidth(2)
        self._calib_line = self._scene.addLine(
            p0.x(), p0.y(), p1.x(), p1.y(), pen)
        self._calib_line.setZValue(14)

    # ----- seed painting ---------------------------------------------------

    def set_brush_radius(self, radius_px):
        self._brush_radius = float(radius_px)

    def brush_radius(self):
        return self._brush_radius

    def start_seed_mode(self, label):
        """label: 'fg' (inside) or 'bg' (outside)."""
        self._mode = MODE_SEED_FG if label == "fg" else MODE_SEED_BG
        self.setDragMode(QGraphicsView.NoDrag)
        self.setCursor(Qt.CrossCursor)

    def clear_seeds(self):
        for s in self._seed_strokes:
            self._scene.removeItem(s["item"])
        self._seed_strokes = []
        self._redo_strokes = []
        self._active_stroke = None
        self.seedsChanged.emit()

    def has_seeds(self):
        return len(self._seed_strokes) > 0

    def seed_strokes(self):
        """Return seeds as a list of (label, [(x, y), ...], radius_px)."""
        return [(s["label"], list(s["points"]), self._brush_radius)
                for s in self._seed_strokes]

    def _begin_stroke(self, label, pt):
        color = _FG_COLOR if label == "fg" else _BG_COLOR
        pen = QPen(color)
        pen.setWidthF(2.0 * self._brush_radius)  # scene units, matches seed
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        path = QPainterPath()
        path.moveTo(pt)
        item = self._scene.addPath(path, pen)
        item.setZValue(5)
        # A fresh stroke invalidates the redo history.
        self._redo_strokes = []
        self._active_stroke = {
            "label": label, "points": [(pt.x(), pt.y())],
            "path": path, "item": item}
        self._seed_strokes.append(self._active_stroke)

    def _extend_stroke(self, pt):
        s = self._active_stroke
        s["points"].append((pt.x(), pt.y()))
        s["path"].lineTo(pt)
        s["item"].setPath(s["path"])

    # ----- seed undo / redo ------------------------------------------------

    def can_undo_seed(self):
        return len(self._seed_strokes) > 0

    def can_redo_seed(self):
        return len(self._redo_strokes) > 0

    def undo_seed(self):
        """Remove the most recent seed stroke (undo). Returns True if it did."""
        if not self._seed_strokes:
            return False
        s = self._seed_strokes.pop()
        self._scene.removeItem(s["item"])
        self._redo_strokes.append(s)
        self._active_stroke = None
        self.seedsChanged.emit()
        return True

    def redo_seed(self):
        """Re-add the last undone seed stroke (redo). Returns True if it did."""
        if not self._redo_strokes:
            return False
        s = self._redo_strokes.pop()
        self._scene.addItem(s["item"])   # removeItem kept the item alive
        self._seed_strokes.append(s)
        self.seedsChanged.emit()
        return True

    def _update_brush_cursor(self, scene_pt):
        """Show/move the brush-footprint ring at the cursor (seed modes)."""
        label_fg = self._mode == MODE_SEED_FG
        color = QColor(0, 200, 0) if label_fg else QColor(220, 40, 40)
        r = self._brush_radius
        if self._brush_cursor is None:
            pen = QPen(color)
            pen.setCosmetic(True)
            pen.setWidth(1)
            self._brush_cursor = self._scene.addEllipse(-r, -r, 2 * r, 2 * r,
                                                        pen)
            self._brush_cursor.setZValue(30)
            # Purely a visual guide -- never intercept painting clicks.
            self._brush_cursor.setAcceptedMouseButtons(Qt.NoButton)
        else:
            pen = QPen(color)
            pen.setCosmetic(True)
            pen.setWidth(1)
            self._brush_cursor.setPen(pen)
            self._brush_cursor.setRect(-r, -r, 2 * r, 2 * r)
        self._brush_cursor.setPos(scene_pt)
        self._brush_cursor.setVisible(True)

    def _hide_brush_cursor(self):
        if self._brush_cursor is not None:
            self._brush_cursor.setVisible(False)

    # ----- bounding-box preview --------------------------------------------

    def set_bbox(self, x, y, w, h):
        """Draw (or move) the dashed bounding-box rectangle, in pixel coords."""
        self.clear_bbox()
        pen = QPen(QColor(255, 230, 0))
        pen.setCosmetic(True)
        pen.setStyle(Qt.DashLine)
        pen.setWidth(1)
        self._bbox_item = self._scene.addRect(x, y, w, h, pen)
        self._bbox_item.setZValue(8)

    def clear_bbox(self):
        if self._bbox_item is not None:
            self._scene.removeItem(self._bbox_item)
            self._bbox_item = None

    # ----- mouse events ----------------------------------------------------

    def mouseMoveEvent(self, event):
        if self._photo_item is not None:
            scene_pt = self.mapToScene(event.position().toPoint())
            self.cursorMoved.emit(scene_pt)
            if self._mode in (MODE_SEED_FG, MODE_SEED_BG):
                self._update_brush_cursor(scene_pt)
                if (self._active_stroke is not None
                        and (event.buttons() & Qt.LeftButton)):
                    self._extend_stroke(scene_pt)
                    return
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if self._photo_item is None:
            super().mousePressEvent(event)
            return

        if self._mode == MODE_CALIBRATE and event.button() == Qt.LeftButton:
            pt = self.mapToScene(event.position().toPoint())
            self._calib_points.append(pt)
            self._add_marker(pt)
            if len(self._calib_points) == 2:
                p0, p1 = self._calib_points
                self._add_line(p0, p1)
                self.cancel_calibration()
                self.calibrationPicked.emit(p0, p1)
            return

        if (self._mode in (MODE_SEED_FG, MODE_SEED_BG)
                and event.button() == Qt.LeftButton):
            label = "fg" if self._mode == MODE_SEED_FG else "bg"
            self._begin_stroke(label, self.mapToScene(
                event.position().toPoint()))
            return

        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if (self._mode in (MODE_SEED_FG, MODE_SEED_BG)
                and self._active_stroke is not None):
            self._active_stroke = None
            self.seedsChanged.emit()   # a stroke was completed
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        # In edit mode the right button deletes vertices, so leave it alone
        # there. Otherwise ask the main window to show a quick-action menu.
        if self._photo_item is None or self._mode == MODE_EDIT:
            return
        self.contextMenuRequested.emit(event.globalPos())
        event.accept()
