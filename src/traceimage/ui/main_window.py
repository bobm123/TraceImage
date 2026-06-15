"""Main window: photo loading (Phase 0), calibration (Phase 1), the seed ->
GrabCut -> editable-contour workflow (Phase 2), multi-object management with a
margin/bounding-box preview (Phase 3), and true-scale SVG export (Phase 4).
"""

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QDockWidget, QDoubleSpinBox, QFileDialog,
    QHBoxLayout, QInputDialog, QLabel, QListWidget, QMainWindow, QMessageBox,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from ..core import calibration as calib
from ..core import contours as cont
from ..core import geometry as geo
from ..core import image_io
from ..core import segmentation as seg
from ..core import svg_export
from ..core import tiling
from ..model import Project
from .canvas import Canvas
from .dialogs import CalibrationDialog, ExportSvgDialog, TilingDialog
from .objects import ObjectLayer

_IMAGE_FILTER = (
    "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp);;All files (*)")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TraceImage")
        self.resize(1200, 820)

        self.project = Project()
        self._loaded = None            # image_io.LoadedImage (BGR array source)
        self._objects = []             # list[ObjectLayer]
        self._active_index = -1

        self.canvas = Canvas(self)
        self.setCentralWidget(self.canvas)
        self.canvas.calibrationPicked.connect(self._on_calibration_picked)
        self.canvas.cursorMoved.connect(self._on_cursor_moved)

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_object_dock()
        self._build_statusbar()
        self._set_tools_enabled(False)
        self._refresh_scale_readout()

    # ----- construction ----------------------------------------------------

    def _build_actions(self):
        self.act_open = QAction("&Open Photo…", self)
        self.act_open.setShortcut(QKeySequence.Open)
        self.act_open.triggered.connect(self.open_photo)

        self.act_export = QAction("&Export SVG…", self)
        self.act_export.setShortcut(QKeySequence.Save)
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

        self.act_run_seg = QAction("&Run Segmentation", self)
        self.act_run_seg.triggered.connect(self.run_segmentation)
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
        m_file.addAction(self.act_open)
        m_file.addAction(self.act_export)
        m_file.addAction(self.act_export_tiles)
        m_file.addSeparator()
        m_file.addAction(self.act_quit)

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
                  self.act_export_tiles, self.act_show_bbox):
            a.setEnabled(enabled)

    # ----- photo loading ---------------------------------------------------

    def open_photo(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Photo", "", _IMAGE_FILTER)
        if not path:
            return
        try:
            loaded = image_io.load_image(path)
        except IOError as exc:
            QMessageBox.critical(self, "Open Photo", str(exc))
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            QMessageBox.critical(
                self, "Open Photo", "Qt could not display this image.")
            return

        self.project = Project()
        self.project.set_source_image(loaded)
        self._loaded = loaded
        self._objects = []             # scene.clear() in set_photo drops items
        self._active_index = -1

        self.canvas.set_photo(pixmap)
        # Scale the default brush to the image so seeds are visible on large
        # photos (an 8px brush is invisible on a multi-megapixel image).
        long_edge = max(loaded.pixel_width, loaded.pixel_height)
        default_brush = int(min(150, max(6, round(long_edge * 0.01))))
        self._brush_spin.setValue(default_brush)   # signals canvas.set_brush_radius
        self._margin_spin.setValue(self.project.margin_mm)
        self.act_mode_pan.setChecked(True)
        self._mode_pan()
        self._set_tools_enabled(True)
        self._refresh_object_list()
        self.set_unit(self.project.calibration.display_unit)
        self.setWindowTitle("TraceImage — %s" % os.path.basename(path))
        self._refresh_scale_readout()

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

    def _mode_seed_fg(self):
        self.canvas.start_seed_mode("fg")
        self._refresh_editability()
        self.statusBar().showMessage(
            "Paint inside the object (foreground seeds).", 4000)

    def _mode_seed_bg(self):
        self.canvas.start_seed_mode("bg")
        self._refresh_editability()
        self.statusBar().showMessage(
            "Paint outside the object (background seeds).", 4000)

    def _mode_edit(self):
        self.canvas.enter_edit_mode()
        self._refresh_editability()
        self.statusBar().showMessage(
            "Edit: drag handles to move, right-click to delete, "
            "double-click an edge to add a vertex.", 6000)

    def _is_edit_mode(self):
        return self.act_mode_edit.isChecked()

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
        name = "Object %d" % (len(self._objects) + 1)
        layer = ObjectLayer(self.canvas.scene_obj(), name)
        self._objects.append(layer)
        self._active_index = len(self._objects) - 1
        self._refresh_object_list()
        self._refresh_editability()

    def delete_active_object(self):
        if self._active_index < 0:
            return
        self._objects[self._active_index].remove()
        del self._objects[self._active_index]
        self._active_index = min(self._active_index, len(self._objects) - 1)
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
            self._obj_list.addItem(layer.name)
        if 0 <= self._active_index < len(self._objects):
            self._obj_list.setCurrentRow(self._active_index)
        self._obj_list.blockSignals(False)

    def _refresh_editability(self):
        edit = self._is_edit_mode()
        for i, layer in enumerate(self._objects):
            layer.set_visible(True)
            layer.set_editable(edit and i == self._active_index)

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
