"""Main window: photo loading (Phase 0), calibration (Phase 1), the seed ->
GrabCut -> editable-contour workflow (Phase 2), multi-object management with a
margin/bounding-box preview and per-object show/hide (Phase 3), true-scale SVG
export (Phase 4), tiled printing (Phase 5), project save/load and an undo/redo
command stack for vertex/object edits (Phase 6).
"""

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDockWidget, QDoubleSpinBox, QFileDialog,
    QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
    QWidget,
)

from ..core import calibration as calib
from ..core import contours as cont
from ..core import geometry as geo
from ..core import image_io
from ..core import project_io
from ..core import segmentation as seg
from ..core import svg_export
from ..core import tiling
from ..core import undo
from ..model import Project
from .canvas import Canvas
from .dialogs import CalibrationDialog, ExportSvgDialog, TilingDialog
from .editable import VertexHandle
from .objects import ObjectLayer

_IMAGE_FILTER = (
    "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp);;All files (*)")
_PROJECT_FILTER = "TraceImage project (*.tiproj.json *.json);;All files (*)"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TraceImage")
        self.resize(1200, 820)

        self.project = Project()
        self._loaded = None            # image_io.LoadedImage (BGR array source)
        self._objects = []             # list[ObjectLayer]
        self._active_index = -1
        self._polygon_counter = 0      # monotonic, for default polygon names

        # Undo stack for vertex/object edits (seed strokes use the canvas's own
        # stack; Ctrl+Z/Y dispatch between them by mode).
        self.undo_stack = undo.UndoStack()
        self.undo_stack.on_change(self._refresh_undo_actions)

        self.canvas = Canvas(self)
        self.setCentralWidget(self.canvas)
        self.canvas.calibrationPicked.connect(self._on_calibration_picked)
        self.canvas.cursorMoved.connect(self._on_cursor_moved)
        self.canvas.contextMenuRequested.connect(self._show_canvas_menu)
        self.canvas.seedsChanged.connect(self._refresh_undo_actions)
        self.canvas.deleteSelectionRequested.connect(
            self.delete_selected_vertices)

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_object_dock()
        self._build_statusbar()
        self._set_tools_enabled(False)
        self._refresh_undo_actions()
        self._refresh_scale_readout()

    # ----- construction ----------------------------------------------------

    def _build_actions(self):
        self.act_open_project = QAction("&Open Project…", self)
        self.act_open_project.setShortcut(QKeySequence.Open)        # Ctrl+O
        self.act_open_project.triggered.connect(self.open_project_file)

        self.act_save_project = QAction("&Save Project…", self)
        self.act_save_project.setShortcut(QKeySequence.Save)        # Ctrl+S
        self.act_save_project.triggered.connect(self.save_project_file)

        self.act_open = QAction("Import &Photo…", self)
        self.act_open.setShortcut("Ctrl+Shift+O")
        self.act_open.triggered.connect(self.open_photo)

        self.act_export = QAction("&Export SVG…", self)
        self.act_export.setShortcut("Ctrl+E")
        self.act_export.triggered.connect(self.export_svg)

        self.act_export_tiles = QAction("Export &Print Tiles…", self)
        self.act_export_tiles.triggered.connect(self.export_tiles)

        self.act_quit = QAction("&Quit", self)
        self.act_quit.setShortcut(QKeySequence.Quit)
        self.act_quit.triggered.connect(self.close)

        self.act_zoom_in = QAction("Zoom &In", self)
        self.act_zoom_in.setShortcut(QKeySequence.ZoomIn)
        self.act_zoom_in.triggered.connect(self.canvas.zoom_in)

        self.act_zoom_out = QAction("Zoom &Out", self)
        self.act_zoom_out.setShortcut(QKeySequence.ZoomOut)
        self.act_zoom_out.triggered.connect(self.canvas.zoom_out)

        self.act_fit = QAction("&Fit to Window", self)
        self.act_fit.triggered.connect(self.canvas.fit_to_view)

        self.act_show_bbox = QAction("Show &Bounding Box", self, checkable=True)
        self.act_show_bbox.setChecked(True)
        self.act_show_bbox.triggered.connect(self._update_bbox)

        self.act_calibrate = QAction("&Calibrate Scale…", self)
        self.act_calibrate.triggered.connect(self.start_calibration)

        # Mutually exclusive interaction modes.
        self.mode_group = QActionGroup(self)
        self.mode_group.setExclusive(True)
        self.act_mode_pan = QAction("&Pan / Zoom", self, checkable=True)
        self.act_mode_pan.setChecked(True)
        self.act_mode_pan.triggered.connect(self._mode_pan)
        self.act_mode_fg = QAction("Mark &Foreground (inside)", self,
                                   checkable=True)
        self.act_mode_fg.triggered.connect(self._mode_seed_fg)
        self.act_mode_bg = QAction("Mark &Background (outside)", self,
                                   checkable=True)
        self.act_mode_bg.triggered.connect(self._mode_seed_bg)
        self.act_mode_edit = QAction("&Edit Vertices", self, checkable=True)
        self.act_mode_edit.triggered.connect(self._mode_edit)
        for a in (self.act_mode_pan, self.act_mode_fg, self.act_mode_bg,
                  self.act_mode_edit):
            self.mode_group.addAction(a)

        self.act_run_seg = QAction("Trace &Poly", self)
        self.act_run_seg.setToolTip("Trace a polygon from the seeds (GrabCut)")
        self.act_run_seg.triggered.connect(self.run_segmentation)

        # Undo / redo: dispatch to seed strokes or the edit command stack.
        self.act_undo = QAction("&Undo", self)
        self.act_undo.setShortcut(QKeySequence.Undo)        # Ctrl+Z
        self.act_undo.triggered.connect(self._do_undo)
        self.act_redo = QAction("&Redo", self)
        self.act_redo.setShortcut("Ctrl+Y")
        self.act_redo.triggered.connect(self._do_redo)
        self.act_clear_seeds = QAction("Clear &Seeds", self)
        self.act_clear_seeds.triggered.connect(self.clear_seeds)

        self.act_units = {}
        for unit in ("mm", "cm", "in"):
            a = QAction(unit, self, checkable=True)
            a.setChecked(unit == self.project.calibration.display_unit)
            a.triggered.connect(lambda _=False, u=unit: self.set_unit(u))
            self.act_units[unit] = a

    def _build_menus(self):
        mb = self.menuBar()

        m_file = mb.addMenu("&File")
        m_file.addAction(self.act_open_project)
        m_file.addAction(self.act_save_project)
        m_file.addSeparator()
        m_file.addAction(self.act_open)
        m_file.addAction(self.act_export)
        m_file.addAction(self.act_export_tiles)
        m_file.addSeparator()
        m_file.addAction(self.act_quit)

        m_edit = mb.addMenu("&Edit")
        m_edit.addAction(self.act_undo)
        m_edit.addAction(self.act_redo)

        m_view = mb.addMenu("&View")
        m_view.addAction(self.act_zoom_in)
        m_view.addAction(self.act_zoom_out)
        m_view.addAction(self.act_fit)
        m_view.addSeparator()
        m_view.addAction(self.act_show_bbox)
        m_view.addSeparator()
        m_units = m_view.addMenu("Display &Units")
        for unit in ("mm", "cm", "in"):
            m_units.addAction(self.act_units[unit])

        m_tools = mb.addMenu("&Tools")
        m_tools.addAction(self.act_calibrate)
        m_tools.addSeparator()
        m_tools.addAction(self.act_mode_pan)
        m_tools.addAction(self.act_mode_fg)
        m_tools.addAction(self.act_mode_bg)
        m_tools.addAction(self.act_mode_edit)
        m_tools.addSeparator()
        m_tools.addAction(self.act_run_seg)
        m_tools.addAction(self.act_clear_seeds)

    def _build_toolbar(self):
        tb = self.addToolBar("Main")
        tb.setMovable(False)
        tb.addAction(self.act_open_project)
        tb.addAction(self.act_save_project)
        tb.addSeparator()
        tb.addAction(self.act_open)
        tb.addAction(self.act_export)
        tb.addAction(self.act_export_tiles)
        tb.addSeparator()
        tb.addAction(self.act_zoom_out)
        tb.addAction(self.act_fit)
        tb.addAction(self.act_zoom_in)
        tb.addSeparator()
        tb.addAction(self.act_calibrate)
        tb.addSeparator()
        tb.addAction(self.act_mode_pan)
        tb.addAction(self.act_mode_fg)
        tb.addAction(self.act_mode_bg)
        tb.addAction(self.act_mode_edit)
        tb.addSeparator()
        tb.addWidget(QLabel(" Brush: "))
        self._brush_spin = QSpinBox(self)
        self._brush_spin.setRange(1, 500)
        self._brush_spin.setValue(int(self.canvas.brush_radius()))
        self._brush_spin.setSuffix(" px")
        self._brush_spin.valueChanged.connect(self.canvas.set_brush_radius)
        tb.addWidget(self._brush_spin)
        tb.addSeparator()
        tb.addAction(self.act_run_seg)
        tb.addAction(self.act_clear_seeds)
        tb.addSeparator()
        tb.addAction(self.act_undo)
        tb.addAction(self.act_redo)

    def _build_object_dock(self):
        dock = QDockWidget("Objects", self)
        dock.setFeatures(QDockWidget.DockWidgetMovable
                         | QDockWidget.DockWidgetFloatable)
        panel = QWidget(dock)
        layout = QVBoxLayout(panel)

        self._obj_list = QListWidget(panel)
        self._obj_list.currentRowChanged.connect(self._on_object_row_changed)
        layout.addWidget(self._obj_list)

        row = QHBoxLayout()
        btn_new = QPushButton("New", panel)
        btn_new.clicked.connect(self.new_object)
        btn_del = QPushButton("Delete", panel)
        btn_del.clicked.connect(self.delete_active_object)
        btn_ren = QPushButton("Rename", panel)
        btn_ren.clicked.connect(self.rename_active_object)
        row.addWidget(btn_new)
        row.addWidget(btn_del)
        row.addWidget(btn_ren)
        layout.addLayout(row)

        margin_row = QHBoxLayout()
        margin_row.addWidget(QLabel("Margin:"))
        self._margin_spin = QDoubleSpinBox(panel)
        self._margin_spin.setRange(0.0, 1000.0)
        self._margin_spin.setDecimals(1)
        self._margin_spin.setSingleStep(1.0)
        self._margin_spin.setValue(self.project.margin_mm)
        self._margin_spin.setSuffix(" mm")
        self._margin_spin.valueChanged.connect(self._on_margin_changed)
        margin_row.addWidget(self._margin_spin)
        layout.addLayout(margin_row)

        self._size_label = QLabel("", panel)
        self._size_label.setWordWrap(True)
        layout.addWidget(self._size_label)

        panel.setLayout(layout)
        dock.setWidget(panel)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        self._obj_buttons = (btn_new, btn_del, btn_ren)

    def _build_statusbar(self):
        self._scale_label = QLabel("", self)
        self._cursor_label = QLabel("", self)
        self.statusBar().addWidget(self._scale_label, 1)
        self.statusBar().addPermanentWidget(self._cursor_label)

    def _set_tools_enabled(self, enabled):
        for a in (self.act_zoom_in, self.act_zoom_out, self.act_fit,
                  self.act_calibrate, self.act_mode_pan, self.act_mode_fg,
                  self.act_mode_bg, self.act_mode_edit, self.act_run_seg,
                  self.act_clear_seeds, self.act_export,
                  self.act_export_tiles, self.act_show_bbox,
                  self.act_save_project):
            a.setEnabled(enabled)

    # ----- photo loading ---------------------------------------------------

    def open_photo(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Photo", "", _IMAGE_FILTER)
        if not path:
            return
        try:
            loaded = image_io.load_image(path)
        except IOError as exc:
            QMessageBox.critical(self, "Import Photo", str(exc))
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            QMessageBox.critical(
                self, "Import Photo", "Qt could not display this image.")
            return

        self.project = Project()
        self.project.set_source_image(loaded)
        self._loaded = loaded
        self._objects = []             # scene.clear() in set_photo drops items
        self._active_index = -1
        self._polygon_counter = 0
        self.undo_stack.clear()

        self.canvas.set_photo(pixmap)
        self._margin_spin.setValue(self.project.margin_mm)
        self.act_mode_pan.setChecked(True)
        self._mode_pan()
        self._set_tools_enabled(True)
        self._refresh_object_list()
        self.set_unit(self.project.calibration.display_unit)
        self.setWindowTitle("TraceImage — %s" % os.path.basename(path))
        self._refresh_scale_readout()

    # ----- project save / load ---------------------------------------------

    def save_project_file(self):
        if self._loaded is None:
            return
        self._sync_model()
        base = os.path.splitext(
            os.path.basename(self._loaded.path or "project"))[0]
        default_path = os.path.join(os.getcwd(), base + ".tiproj.json")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Project", default_path, _PROJECT_FILTER)
        if not path:
            return
        try:
            project_io.save_project(self.project, path)
        except Exception as exc:
            QMessageBox.critical(self, "Save Project",
                                 "Could not save: %s" % (exc,))
            return
        self.statusBar().showMessage("Saved project %s" % path, 6000)

    def open_project_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Project", "", _PROJECT_FILTER)
        if not path:
            return
        try:
            project = project_io.load_project(path)
        except project_io.ProjectIOError as exc:
            QMessageBox.critical(self, "Open Project", str(exc))
            return

        img_path = project.source_image_path
        if not img_path or not os.path.exists(img_path):
            QMessageBox.warning(
                self, "Open Project",
                "Source image not found:\n%s\n\nPlease locate it."
                % (img_path,))
            img_path, _ = QFileDialog.getOpenFileName(
                self, "Locate Source Image", "", _IMAGE_FILTER)
            if not img_path:
                return
        try:
            loaded = image_io.load_image(img_path)
        except IOError as exc:
            QMessageBox.critical(self, "Open Project", str(exc))
            return
        pixmap = QPixmap(img_path)
        if pixmap.isNull():
            QMessageBox.critical(
                self, "Open Project", "Qt could not display the source image.")
            return

        saved_w, saved_h = project.pixel_width, project.pixel_height

        self.project = project
        self.project.set_source_image(loaded)   # match actual image dims/path
        self._loaded = loaded
        self.undo_stack.clear()

        self.canvas.set_photo(pixmap)
        self._load_layers_from_project()
        self._polygon_counter = self._max_polygon_number()

        self._set_tools_enabled(True)
        self.act_mode_pan.setChecked(True)
        self._mode_pan()
        self._margin_spin.setValue(self.project.margin_mm)
        self.set_unit(self.project.calibration.display_unit)
        self._refresh_object_list()
        self._refresh_scale_readout()
        self._update_bbox()
        self.setWindowTitle("TraceImage — %s" % os.path.basename(path))

        if saved_w and (saved_w != loaded.pixel_width
                        or saved_h != loaded.pixel_height):
            QMessageBox.warning(
                self, "Open Project",
                "The source image size differs from when the project was "
                "saved; traced points may not line up.")
        self.statusBar().showMessage("Opened project %s" % path, 6000)

    def _build_layer_from_model(self, model_obj):
        """Create an on-canvas ObjectLayer from a model.TracedObject."""
        layer = ObjectLayer(self.canvas.scene_obj(), model_obj.name,
                            edit_sink=self)
        layer.style = model_obj.style
        for c in model_obj.contours:
            layer.add_contour(c.points, role=c.role, closed=c.closed)
        return layer

    def _load_layers_from_project(self):
        """Rebuild on-canvas ObjectLayers from self.project.objects."""
        self._objects = [self._build_layer_from_model(o)
                         for o in self.project.objects]
        self._active_index = 0 if self._objects else -1

    def _max_polygon_number(self):
        """Largest N among existing 'Polygon N' names (0 if none)."""
        best = 0
        for layer in self._objects:
            name = layer.name
            if name.startswith("Polygon "):
                tail = name[len("Polygon "):].strip()
                if tail.isdigit():
                    best = max(best, int(tail))
        return best

    # ----- calibration -----------------------------------------------------

    def start_calibration(self):
        if not self.canvas.has_photo():
            return
        self.statusBar().showMessage(
            "Calibration: click two points on a feature of known length.", 0)
        self.canvas.start_calibration()

    def _on_calibration_picked(self, p0, p1):
        self.statusBar().clearMessage()
        self.act_mode_pan.setChecked(True)
        pixel_distance = calib.pixel_distance(
            (p0.x(), p0.y()), (p1.x(), p1.y()))
        dlg = CalibrationDialog(pixel_distance, self)
        if dlg.exec() != QDialog.Accepted:
            return
        value, unit = dlg.values()
        try:
            mpp = calib.mm_per_pixel(
                (p0.x(), p0.y()), (p1.x(), p1.y()), value, unit)
        except ValueError as exc:
            QMessageBox.warning(self, "Calibrate Scale", str(exc))
            return
        self.project.calibration.mm_per_pixel = mpp
        self.set_unit(unit)
        self._refresh_scale_readout()
        self._update_bbox()

    # ----- modes -----------------------------------------------------------

    def _mode_pan(self):
        self.canvas.enter_pan_mode()
        self._refresh_editability()
        self._update_bbox()
        self._refresh_undo_actions()

    def _mode_seed_fg(self):
        self.canvas.start_seed_mode("fg")
        self._refresh_editability()
        self._refresh_undo_actions()
        self.statusBar().showMessage(
            "Paint inside the object (foreground seeds).", 4000)

    def _mode_seed_bg(self):
        self.canvas.start_seed_mode("bg")
        self._refresh_editability()
        self._refresh_undo_actions()
        self.statusBar().showMessage(
            "Paint outside the object (background seeds).", 4000)

    def _mode_edit(self):
        self.canvas.enter_edit_mode()
        self._refresh_editability()
        self._refresh_undo_actions()
        self.statusBar().showMessage(
            "Edit: drag handles to move, right-click to delete, "
            "double-click an edge to add a vertex, drag a box to "
            "select then Delete to remove several.", 6000)

    def _is_edit_mode(self):
        return self.act_mode_edit.isChecked()

    def _show_canvas_menu(self, global_pos):
        """Quick-action context menu on right-click (pan/seed modes)."""
        menu = QMenu(self)
        menu.addAction(self.act_mode_fg)
        menu.addAction(self.act_mode_bg)

        brush_menu = menu.addMenu("Brush Size")
        cur = int(round(self.canvas.brush_radius()))
        for sz in (10, 20, 40, 80, 120):
            a = brush_menu.addAction("%d px" % sz)
            a.setCheckable(True)
            a.setChecked(sz == cur)
            a.triggered.connect(
                lambda _=False, s=sz: self._brush_spin.setValue(s))

        menu.addAction(self.act_run_seg)       # "Trace Poly"
        menu.addSeparator()
        menu.addAction(self.act_undo)
        menu.addAction(self.act_redo)
        menu.addSeparator()
        menu.addAction(self.act_zoom_in)
        menu.addAction(self.act_zoom_out)
        menu.addAction(self.act_fit)
        menu.exec(global_pos)

    # ----- undo / redo (context dispatch) ----------------------------------

    def _in_seed_mode(self):
        return self.act_mode_fg.isChecked() or self.act_mode_bg.isChecked()

    def _do_undo(self):
        if self._in_seed_mode():
            self.canvas.undo_seed()
        else:
            self.undo_stack.undo()
            self._update_bbox()
        self._refresh_undo_actions()

    def _do_redo(self):
        if self._in_seed_mode():
            self.canvas.redo_seed()
        else:
            self.undo_stack.redo()
            self._update_bbox()
        self._refresh_undo_actions()

    def _refresh_undo_actions(self):
        if self._in_seed_mode():
            cu = self.canvas.has_photo() and self.canvas.can_undo_seed()
            cr = self.canvas.has_photo() and self.canvas.can_redo_seed()
            ulabel = rlabel = "seed stroke"
        else:
            cu = self.undo_stack.can_undo()
            cr = self.undo_stack.can_redo()
            ulabel = self.undo_stack.undo_label()
            rlabel = self.undo_stack.redo_label()
        self.act_undo.setEnabled(cu)
        self.act_redo.setEnabled(cr)
        self.act_undo.setToolTip(("Undo " + ulabel).strip() if cu else "Undo")
        self.act_redo.setToolTip(("Redo " + rlabel).strip() if cr else "Redo")

    # ----- edit sink (called by EditableContour) ---------------------------

    def record_move(self, contour, index, old_xy, new_xy):
        self.undo_stack.push(undo.FnCommand(
            "move vertex",
            undo=lambda: contour.move_vertex(index, old_xy[0], old_xy[1]),
            redo=lambda: contour.move_vertex(index, new_xy[0], new_xy[1])))

    def record_insert(self, contour, index, xy):
        self.undo_stack.push(undo.FnCommand(
            "add vertex",
            undo=lambda: contour.delete_vertex(index),
            redo=lambda: contour.insert_vertex_at(index, xy[0], xy[1])))

    def record_delete(self, contour, index, xy):
        self.undo_stack.push(undo.FnCommand(
            "delete vertex",
            undo=lambda: contour.insert_vertex_at(index, xy[0], xy[1]),
            redo=lambda: contour.delete_vertex(index)))

    # ----- segmentation ----------------------------------------------------

    def clear_seeds(self):
        self.canvas.clear_seeds()

    def run_segmentation(self):
        if self._loaded is None:
            return
        strokes = [seg.Stroke(label, pts, radius)
                   for (label, pts, radius) in self.canvas.seed_strokes()]
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                mask = seg.grabcut_from_strokes(self._loaded.data, strokes)
            finally:
                QApplication.restoreOverrideCursor()
        except ValueError as exc:
            QMessageBox.warning(self, "Run Segmentation", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(self, "Run Segmentation",
                                 "Segmentation failed: %s" % (exc,))
            return

        # Scale simplification + speckle thresholds to the image size, so big
        # photos don't produce hundreds of vertices or keep tiny specks.
        long_edge = max(self.project.pixel_width, self.project.pixel_height)
        epsilon_px = max(1.5, long_edge * 0.0012)
        min_area_px = max(50.0, 0.0005 * self.project.pixel_width
                          * self.project.pixel_height)
        results = cont.extract_contours(
            mask, epsilon_px=epsilon_px, min_area_px=min_area_px)
        if not results:
            QMessageBox.information(
                self, "Run Segmentation",
                "No object boundary found. Add more inside/outside seeds "
                "and run again.")
            return

        if self._active_index < 0:
            self.new_object()
        self._objects[self._active_index].set_contours(results)
        self.canvas.clear_seeds()
        # Tracing replaces the active object's contours, invalidating any edit
        # commands that referenced the old ones; start the history fresh.
        self.undo_stack.clear()

        self.act_mode_edit.setChecked(True)
        self._mode_edit()
        n_holes = sum(1 for _, role in results if role == cont.ROLE_HOLE)
        self.statusBar().showMessage(
            "Found %d contour(s) (%d hole(s)) for '%s'. Refine, or re-seed and "
            "run again." % (len(results), n_holes,
                            self._objects[self._active_index].name), 6000)

    # ----- object management -----------------------------------------------

    def new_object(self):
        if not self.canvas.has_photo():
            return
        # Monotonic counter so deleting a polygon never reuses a name.
        self._polygon_counter += 1
        name = "Polygon %d" % (self._polygon_counter,)
        layer = ObjectLayer(self.canvas.scene_obj(), name, edit_sink=self)
        self._objects.append(layer)
        self._active_index = len(self._objects) - 1
        self._refresh_object_list()
        self._refresh_editability()

    def delete_active_object(self):
        if self._active_index < 0:
            return
        index = self._active_index
        snapshot = self._objects[index].to_model()
        self._remove_layer_at(index)
        # Structural change: keep the history simple and free of stale targets.
        self.undo_stack.clear()
        self.undo_stack.push(undo.FnCommand(
            "delete object",
            undo=lambda: self._insert_layer_from_model(index, snapshot),
            redo=lambda: self._remove_layer_at(index)))

    def _remove_layer_at(self, index):
        if not (0 <= index < len(self._objects)):
            return
        self._objects[index].remove()
        del self._objects[index]
        if self._active_index >= len(self._objects):
            self._active_index = len(self._objects) - 1
        self._refresh_object_list()
        self._refresh_editability()
        self._update_bbox()

    def _insert_layer_from_model(self, index, model_obj):
        layer = self._build_layer_from_model(model_obj)
        index = max(0, min(index, len(self._objects)))
        self._objects.insert(index, layer)
        self._active_index = index
        self._refresh_object_list()
        self._refresh_editability()
        self._update_bbox()

    def rename_active_object(self):
        if self._active_index < 0:
            return
        current = self._objects[self._active_index].name
        name, ok = QInputDialog.getText(
            self, "Rename Object", "Name:", text=current)
        if ok and name.strip():
            self._objects[self._active_index].name = name.strip()
            self._refresh_object_list()

    def _on_object_row_changed(self, row):
        if 0 <= row < len(self._objects):
            self._active_index = row
            self._refresh_editability()

    def _refresh_object_list(self):
        self._obj_list.blockSignals(True)
        self._obj_list.clear()
        for layer in self._objects:
            item = QListWidgetItem(self._obj_list)
            roww = self._make_object_row(layer)
            item.setSizeHint(roww.sizeHint())
            self._obj_list.setItemWidget(item, roww)
        if 0 <= self._active_index < len(self._objects):
            self._obj_list.setCurrentRow(self._active_index)
        self._obj_list.blockSignals(False)

    def _make_object_row(self, layer):
        """A list row: the polygon name plus a right-aligned show/hide box."""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(4, 1, 4, 1)
        h.addWidget(QLabel(layer.name))
        h.addStretch(1)
        cb = QCheckBox(w)
        cb.setChecked(layer.visible)
        cb.setToolTip("Show / hide this polygon")
        cb.toggled.connect(
            lambda checked, lyr=layer: self._on_visibility_toggled(lyr, checked))
        h.addWidget(cb)
        return w

    def _on_visibility_toggled(self, layer, visible):
        layer.set_visible(visible)
        self._refresh_editability()

    def _refresh_editability(self):
        edit = self._is_edit_mode()
        for i, layer in enumerate(self._objects):
            # Visibility is owned by the per-object toggle; here we only decide
            # which object exposes its draggable vertex handles.
            layer.set_editable(edit and i == self._active_index)

    # ----- marquee group delete --------------------------------------------

    _MIN_VERTICES = 3   # fewer than this is not a polygon -> drop the contour

    def _layer_of_contour(self, contour):
        for layer in self._objects:
            if contour in layer.contours:
                return layer
        return None

    def delete_selected_vertices(self):
        """Delete rubber-band-selected vertices as one undo step.

        If a contour would be left with fewer than 3 vertices it is removed
        entirely (a sub-3 contour is not a polygon); undo restores it.
        """
        if not self._is_edit_mode():
            return
        scene = self.canvas.scene_obj()
        groups = {}
        for item in scene.selectedItems():
            if isinstance(item, VertexHandle):
                contour = item.owner_contour()
                groups.setdefault(contour, []).append(contour.index_of(item))
        if not groups:
            return

        vertex_dels = []   # (contour, index, (x, y)) in deletion order
        removals = []      # mutable records describing removed contours

        for contour, indices in groups.items():
            indices = sorted(set(indices), reverse=True)
            remaining = contour.vertex_count() - len(indices)
            if remaining >= self._MIN_VERTICES:
                for idx in indices:
                    xy = contour.get_point(idx)
                    contour.delete_vertex(idx)
                    vertex_dels.append((contour, idx, xy))
            else:
                layer = self._layer_of_contour(contour)
                if layer is None:
                    continue
                rec = {"layer": layer,
                       "index": layer.contours.index(contour),
                       "points": contour.points(),
                       "role": contour.role,
                       "closed": contour.closed,
                       "contour": contour}
                layer.remove_contour(contour)
                rec["contour"] = None
                removals.append(rec)

        if not vertex_dels and not removals:
            return

        def _undo():
            for rec in reversed(removals):
                rec["contour"] = rec["layer"].insert_contour(
                    rec["index"], rec["points"], rec["role"], rec["closed"])
            for contour, idx, xy in reversed(vertex_dels):
                contour.insert_vertex_at(idx, xy[0], xy[1])
            self._refresh_editability()
            self._update_bbox()

        def _redo():
            for contour, idx, xy in vertex_dels:
                contour.delete_vertex(idx)
            for rec in removals:
                rec["index"] = rec["layer"].remove_contour(rec["contour"])
                rec["contour"] = None
            self._refresh_editability()
            self._update_bbox()

        n = len(vertex_dels) + sum(len(r["points"]) for r in removals)
        if removals:
            # Removing whole contours invalidates older commands that referenced
            # them, so keep the history simple and safe.
            self.undo_stack.clear()
        self.undo_stack.push(undo.FnCommand(
            "delete %d vertices" % n, _undo, _redo))
        scene.clearSelection()
        self._refresh_editability()
        self._update_bbox()
        msg = "Deleted %d vertices" % n
        if removals:
            msg += " (%d contour(s) removed)" % len(removals)
        self.statusBar().showMessage(msg + ".", 4000)

    # ----- bounding box -----------------------------------------------------

    def _all_points(self):
        pts = []
        for layer in self._objects:
            pts.extend(layer.iter_points())
        return pts

    def _update_bbox(self):
        if not self.canvas.has_photo() or not self.act_show_bbox.isChecked():
            self.canvas.clear_bbox()
            self._refresh_size_label()
            return
        pts = self._all_points()
        if not pts:
            self.canvas.clear_bbox()
            self._refresh_size_label()
            return
        box = geo.bbox_of_points(pts)
        c = self.project.calibration
        margin_px = (self.project.margin_mm / c.mm_per_pixel
                     if c.is_calibrated else 0.0)
        box = box.expanded(margin_px)
        self.canvas.set_bbox(box.min_x, box.min_y, box.width, box.height)
        self._refresh_size_label(box)

    def _on_margin_changed(self, value):
        self.project.margin_mm = float(value)
        self._update_bbox()

    def _refresh_size_label(self, box=None):
        if box is None or not self.project.calibration.is_calibrated:
            if not self.project.calibration.is_calibrated and self._objects:
                self._size_label.setText("Traced size: calibrate to show mm.")
            else:
                self._size_label.setText("")
            return
        unit = self.project.calibration.display_unit
        mpp = self.project.calibration.mm_per_pixel
        w = calib.from_mm(box.width * mpp, unit)
        h = calib.from_mm(box.height * mpp, unit)
        self._size_label.setText(
            "Traced size (incl. margin): %.4g × %.4g %s" % (w, h, unit))

    # ----- export ----------------------------------------------------------

    def _sync_model(self):
        self.project.objects = [layer.to_model()
                                for layer in self._objects
                                if not layer.is_empty()]

    def export_svg(self):
        if self._loaded is None:
            return
        if not self.project.calibration.is_calibrated:
            QMessageBox.warning(
                self, "Export SVG",
                "Calibrate the scale first so the SVG can be sized in mm.")
            return
        self._sync_model()
        if not self.project.objects:
            QMessageBox.information(
                self, "Export SVG", "Trace at least one object first.")
            return

        dlg = ExportSvgDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        embed, downscale_max, filled, inkscape = dlg.values()

        base = os.path.splitext(
            os.path.basename(self._loaded.path or "trace"))[0]
        default_path = os.path.join(os.getcwd(), base + ".svg")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export SVG", default_path, "SVG files (*.svg)")
        if not path:
            return

        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                svg = svg_export.build_svg(
                    self.project,
                    image_bgr=self._loaded.data if embed else None,
                    embed_photo=embed,
                    downscale_max=downscale_max,
                    filled=filled,
                    inkscape=inkscape)
            finally:
                QApplication.restoreOverrideCursor()
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(svg)
        except svg_export.ExportError as exc:
            QMessageBox.warning(self, "Export SVG", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(self, "Export SVG",
                                 "Export failed: %s" % (exc,))
            return

        self.statusBar().showMessage("Exported %s" % path, 6000)

    def export_tiles(self):
        if self._loaded is None:
            return
        if not self.project.calibration.is_calibrated:
            QMessageBox.warning(
                self, "Export Print Tiles",
                "Calibrate the scale first so tiles can print at 1:1.")
            return
        self._sync_model()
        if not self.project.objects:
            QMessageBox.information(
                self, "Export Print Tiles", "Trace at least one object first.")
            return

        dlg = TilingDialog(list(tiling.PAGE_SIZES_MM.keys()), self)
        if dlg.exec() != QDialog.Accepted:
            return
        page, landscape, margin_mm, overlap_mm, embed, filled = dlg.values()

        out_dir = QFileDialog.getExistingDirectory(
            self, "Choose a folder for the tile SVGs", os.getcwd())
        if not out_dir:
            return

        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                tiles = tiling.build_tiles(
                    self.project,
                    image_bgr=self._loaded.data if embed else None,
                    page=page, landscape=landscape,
                    margin_mm=margin_mm, overlap_mm=overlap_mm,
                    embed_photo=embed, filled=filled)
                for name, svg in tiles:
                    with open(os.path.join(out_dir, name), "w",
                              encoding="utf-8") as fh:
                        fh.write(svg)
            finally:
                QApplication.restoreOverrideCursor()
        except (svg_export.ExportError, ValueError) as exc:
            QMessageBox.warning(self, "Export Print Tiles", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(self, "Export Print Tiles",
                                 "Tiling failed: %s" % (exc,))
            return

        QMessageBox.information(
            self, "Export Print Tiles",
            "Wrote %d tile(s) to:\n%s" % (len(tiles), out_dir))
        self.statusBar().showMessage(
            "Wrote %d tile(s) to %s" % (len(tiles), out_dir), 6000)

    # ----- display ----------------------------------------------------------

    def set_unit(self, unit):
        self.project.calibration.display_unit = unit
        for u, action in self.act_units.items():
            action.setChecked(u == unit)
        self._refresh_scale_readout()

    def _refresh_scale_readout(self):
        c = self.project.calibration
        if not c.is_calibrated:
            if self.project.source_image_path:
                self._scale_label.setText(
                    "%d × %d px  —  not calibrated"
                    % (self.project.pixel_width, self.project.pixel_height))
            else:
                self._scale_label.setText("No photo loaded")
            return
        unit = c.display_unit
        size = self.project.real_size_mm()
        w = calib.from_mm(size[0], unit)
        h = calib.from_mm(size[1], unit)
        mpp_unit = calib.from_mm(c.mm_per_pixel, unit)
        self._scale_label.setText(
            "%d × %d px   |   %.5g %s/px   |   real size %.4g × %.4g %s"
            % (self.project.pixel_width, self.project.pixel_height,
               mpp_unit, unit, w, h, unit))

    def _on_cursor_moved(self, scene_pt):
        if not self.canvas.has_photo():
            return
        x, y = scene_pt.x(), scene_pt.y()
        c = self.project.calibration
        if c.is_calibrated:
            unit = c.display_unit
            mx = calib.from_mm(x * c.mm_per_pixel, unit)
            my = calib.from_mm(y * c.mm_per_pixel, unit)
            self._cursor_label.setText(
                "x %.0f, y %.0f px  (%.3g, %.3g %s)" % (x, y, mx, my, unit))
        else:
            self._cursor_label.setText("x %.0f, y %.0f px" % (x, y))
