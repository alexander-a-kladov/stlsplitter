"""STL Splitter - cut an STL figure into colour-printable parts with pin/hole connectors.

Run:  python stl_splitter.py [model.stl]

Mouse:  left-drag rotate | right-drag / wheel zoom | middle-drag (or shift+left) pan
        left-CLICK on the model places the 3D cursor (red dot + cyan cut plane)
"""
import os
import sys

os.environ.setdefault("QT_API", "pyside6")

import numpy as np
import pyvista as pv
import traceback

import vtk
from pyvistaqt import QtInteractor
from PySide6.QtCore import QEvent, QObject, QSettings, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDockWidget, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QSlider, QToolButton, QVBoxLayout, QWidget)

import splitter_core as core

PALETTE = [(231, 76, 60), (52, 152, 219), (46, 204, 113), (241, 196, 15),
           (155, 89, 182), (230, 126, 34), (26, 188, 156), (236, 240, 241)]
BASE_COLOR = (176, 184, 192)


def to_poly(m):
    faces = np.hstack([np.full((len(m.faces), 1), 3), m.faces]).astype(np.int64).ravel()
    return pv.PolyData(np.asarray(m.vertices, dtype=float), faces)


def hexcol(c):
    return "#%02x%02x%02x" % tuple(c)


class ClickSpinBox(QDoubleSpinBox):
    """Changes with the mouse wheel only after a direct left click into it (and until it loses
    focus), so scrolling the tool panel never changes values by accident."""

    def __init__(self):
        super().__init__()
        self._armed = False
        self.setFocusPolicy(Qt.StrongFocus)
        self.setKeyboardTracking(False)
        self.lineEdit().installEventFilter(self)

    def eventFilter(self, obj, ev):
        if obj is self.lineEdit():
            if ev.type() == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton:
                self._armed = True
            elif ev.type() == QEvent.FocusOut:
                self._armed = False
        return super().eventFilter(obj, ev)

    def mousePressEvent(self, e):             # the up/down arrow buttons
        if e.button() == Qt.LeftButton:
            self._armed = True
        super().mousePressEvent(e)

    def focusOutEvent(self, e):
        self._armed = False
        super().focusOutEvent(e)

    def wheelEvent(self, e):
        if self._armed:
            super().wheelEvent(e)
        else:
            e.ignore()  # let the scroll area scroll


class ClickSlider(QSlider):
    def __init__(self, orientation):
        super().__init__(orientation)
        self._armed = False
        self.setFocusPolicy(Qt.StrongFocus)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._armed = True
        super().mousePressEvent(e)

    def focusOutEvent(self, e):
        self._armed = False
        super().focusOutEvent(e)

    def wheelEvent(self, e):
        if self._armed:
            super().wheelEvent(e)
        else:
            e.ignore()


class ClickComboBox(QComboBox):
    def __init__(self):
        super().__init__()
        self._armed = False
        self.setFocusPolicy(Qt.StrongFocus)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._armed = True
        super().mousePressEvent(e)

    def focusOutEvent(self, e):
        self._armed = False
        super().focusOutEvent(e)

    def wheelEvent(self, e):
        if self._armed:
            super().wheelEvent(e)
        else:
            e.ignore()


class SliderRow(QWidget):
    """Label + slider + spin box (+ fine nudge buttons) kept in sync.
    `step` is the fine step used by the arrow buttons, the spin box arrows and the wheel."""
    changed = Signal()

    def __init__(self, label, lo, hi, value, decimals=1, suffix="", step=None):
        super().__init__()
        self._scale = 10 ** decimals
        step = step if step is not None else 1.0 / self._scale
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        self.slider = ClickSlider(Qt.Horizontal)
        self.slider.setSingleStep(max(1, round(step * self._scale)))
        self.slider.setPageStep(max(1, round(step * self._scale * 10)))
        self.spin = ClickSpinBox()
        self.spin.setDecimals(decimals)
        self.spin.setSuffix(suffix)
        self.spin.setSingleStep(step)
        self._step = step
        lbl = QLabel(label)
        lbl.setMinimumWidth(48)
        lay.addWidget(lbl)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.spin)
        for txt, sign in (("◀", -1), ("▶", +1)):
            b = QToolButton()
            b.setText(txt)
            b.setAutoRepeat(True)          # hold to keep nudging
            b.setAutoRepeatInterval(60)
            b.setFixedWidth(24)
            b.setToolTip(f"Nudge by {sign * step:+g}")
            b.clicked.connect(lambda _=False, sg=sign: self.set_value(self.spin.value() + sg * self._step))
            lay.addWidget(b)
        self.set_range(lo, hi)
        self.set_value(value, emit=False)
        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)

    def set_range(self, lo, hi, slider_lo=None, slider_hi=None):
        """The box accepts lo..hi; the slider can cover a narrower span for finer mouse resolution."""
        self.spin.setRange(lo, hi)
        slo = lo if slider_lo is None else max(lo, slider_lo)
        shi = hi if slider_hi is None else min(hi, slider_hi)
        self.slider.setRange(round(slo * self._scale), round(shi * self._scale))

    def _from_slider(self, v):
        self.spin.blockSignals(True)
        self.spin.setValue(v / self._scale)
        self.spin.blockSignals(False)
        self.changed.emit()

    def _from_spin(self, v):
        self.slider.blockSignals(True)
        self.slider.setValue(round(v * self._scale))
        self.slider.blockSignals(False)
        self.changed.emit()

    def value(self):
        return self.spin.value()

    def set_value(self, v, emit=True):
        self.spin.blockSignals(True)
        self.slider.blockSignals(True)
        self.spin.setValue(v)
        self.slider.setValue(round(self.spin.value() * self._scale))
        self.spin.blockSignals(False)
        self.slider.blockSignals(False)
        if emit:
            self.changed.emit()


def dspin(lo, hi, val, step=0.1, dec=2, suffix=" mm"):
    s = ClickSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setSingleStep(step)
    s.setValue(val)
    s.setSuffix(suffix)
    return s


class Section(QWidget):
    """Collapsible panel ("spoiler"). Put your layout on `.body`. The open/closed state is remembered."""

    def __init__(self, title, expanded=True):
        super().__init__()
        self._key = "section/" + title.split(" (")[0]
        self._settings = QSettings("StlSplitter", "StlSplitter")
        expanded = self._settings.value(self._key, expanded, type=bool)
        self.btn = QToolButton()
        self.btn.setText(title)
        self.btn.setCheckable(True)
        self.btn.setChecked(expanded)
        self.btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn.setStyleSheet("QToolButton { border: none; font-weight: bold; text-align: left; padding: 5px 2px; }")
        self.btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.body = QFrame()
        self.body.setFrameShape(QFrame.StyledPanel)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.btn)
        lay.addWidget(self.body)
        self.btn.toggled.connect(self._toggled)
        self._toggled(expanded)

    def _toggled(self, on):
        self.btn.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.body.setVisible(on)
        self._settings.setValue(self._key, on)


class ClickFilter(QObject):
    """Detects a left-button CLICK (press+release without dragging) on the 3D widget at Qt level.
    (The VTK widget does not reliably forward the release event to VTK observers.)"""

    def __init__(self, widget, on_click):
        super().__init__(widget)
        self.on_click = on_click
        self._p = None

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton:
            self._p = ev.position()
        elif t == QEvent.MouseButtonRelease and ev.button() == Qt.LeftButton and self._p is not None:
            q = ev.position()
            moved = abs(q.x() - self._p.x()) + abs(q.y() - self._p.y())
            self._p = None
            if moved <= 4:
                self.on_click(q.x(), q.y())
        return False  # never swallow the event: rotate / zoom keep working


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("STL Splitter")
        self.resize(1450, 900)
        self.parts = []
        self.history = []
        self._uid = 0
        self._name_n = 1
        self._color_i = 0
        self.diag = 100.0
        self.pick = None          # cursor point (in part-local coordinates)
        self.pick_part = None
        self.n0 = np.array([0.0, 0.0, 1.0])
        self._loop_names = []

        self.plotter = QtInteractor(self)
        self.plotter.set_background("#2b2d31")
        self.plotter.add_axes()
        self.setCentralWidget(self.plotter.interactor)
        self._build_panel()
        self._build_menu()
        self._click_filter = ClickFilter(self.plotter.interactor, self._on_click)
        self.plotter.interactor.installEventFilter(self._click_filter)
        self.statusBar().showMessage("Open an STL (File > Open STL), then click on the model to place the cut.")

    # ------------------------------------------------------------------ UI
    def _build_menu(self):
        m = self.menuBar().addMenu("&File")
        for text, fn, key in [("Open STL...", self.open_stl, "Ctrl+O"),
                              ("Open collection folder...", self.open_collection, None),
                              ("Save collection (STL per part)...", self.save_collection, "Ctrl+S"),
                              ("Export coloured 3MF...", self.export_3mf, None)]:
            a = QAction(text, self)
            if key:
                a.setShortcut(key)
            a.triggered.connect(fn)
            m.addAction(a)
        e = self.menuBar().addMenu("&Edit")
        u = QAction("Undo", self)
        u.setShortcut(QKeySequence.Undo)
        u.triggered.connect(self.undo)
        e.addAction(u)

    def _btn(self, text, fn):
        b = QPushButton(text)
        b.clicked.connect(fn)
        return b

    def _build_panel(self):
        root = QWidget()
        lay = QVBoxLayout(root)

        # file
        g = Section("File", expanded=False)
        gl = QGridLayout(g.body)
        gl.addWidget(self._btn("Open STL", self.open_stl), 0, 0)
        gl.addWidget(self._btn("Open collection", self.open_collection), 0, 1)
        gl.addWidget(self._btn("Save collection", self.save_collection), 1, 0)
        gl.addWidget(self._btn("Export 3MF", self.export_3mf), 1, 1)
        lay.addWidget(g)

        # parts
        g = Section("Parts (tick = visible, double-click = rename)")
        gl = QVBoxLayout(g.body)
        self.list = QListWidget()
        self.list.setMinimumHeight(140)
        self.list.currentRowChanged.connect(lambda _: self._highlight())
        self.list.itemChanged.connect(self._item_changed)
        self.list.itemDoubleClicked.connect(lambda _: self.rename_part())
        gl.addWidget(self.list)
        row = QHBoxLayout()
        for t, f in [("Colour...", self.color_part), ("Rename", self.rename_part),
                     ("Delete", self.delete_part), ("Undo", self.undo)]:
            row.addWidget(self._btn(t, f))
        gl.addLayout(row)
        lay.addWidget(g)

        # cut plane
        g = Section("Cut plane (click the model to place the 3D cursor)")
        gl = QVBoxLayout(g.body)
        self.spin_row = SliderRow("Spin", 0, 360, 0, 1, "°", step=0.5)
        self.tilt_row = SliderRow("Tilt", -80, 80, 0, 1, "°", step=0.5)
        self.off_row = SliderRow("Offset", -50, 50, 0, 2, " mm", step=0.1)
        for r in (self.spin_row, self.tilt_row, self.off_row):
            r.changed.connect(self.update_preview)
            gl.addWidget(r)
        self.chk_snap = QCheckBox("Auto-snap to the thinnest cross-section on click")
        self.chk_snap.setChecked(True)
        self.chk_plane = QCheckBox("Show cut plane")
        self.chk_plane.setChecked(True)
        self.chk_plane.toggled.connect(self.update_preview)
        gl.addWidget(self.chk_snap)
        gl.addWidget(self.chk_plane)
        row = QHBoxLayout()
        row.addWidget(self._btn("Snap now", self.snap_now))
        row.addWidget(self._btn("Clear cursor", self.clear_pick))
        gl.addLayout(row)
        lay.addWidget(g)

        # cut mode
        g = Section("Cut mode", expanded=False)
        fl = QFormLayout(g.body)
        self.c_mode = ClickComboBox()
        self.c_mode.addItems(["Local loop - only the outline under the cursor",
                              "Infinite plane - everything the plane touches"])
        self.c_mode.setToolTip("Local loop: like a thread pulled tight around ONE limb. Other parts the "
                               "same plane passes through are not cut.")
        self.c_mode.currentIndexChanged.connect(self._mode_changed)
        self.s_kerf = dspin(0.005, 0.5, 0.02, 0.01, 3)
        self.s_kerf.setToolTip("Local loop leaves this tiny saw-gap between the two halves.")
        self.s_margin = dspin(0.1, 3, 0.5, 0.1, 2)
        self.s_margin.setToolTip("How far the cutting disc extends beyond the outline. Lower it if "
                                 "another body part is touching closely.")
        fl.addRow(self.c_mode)
        fl.addRow("Saw gap", self.s_kerf)
        fl.addRow("Cutter margin", self.s_margin)
        lay.addWidget(g)

        # connectors
        g = Section("Connectors (pin + hole)")
        fl = QFormLayout(g.body)
        self.chk_conn = QCheckBox("Add connectors when cutting")
        self.chk_conn.setChecked(True)
        self.s_diam = dspin(0.8, 30, 3.0)
        self.s_len = dspin(1, 40, 6.0)
        self.s_tol = dspin(0, 1.5, 0.15, 0.05)
        self.s_extra = dspin(0, 5, 0.5)
        self.s_chamfer = dspin(0, 3, 0.8, 0.1)
        self.c_pin = ClickComboBox()
        self.c_pin.addItems(["Straight pin", "Straight pin + flat key (fits one way)",
                             "Snap-in pin (rotating joint)",
                             "Snap-in pin + flat key (fixed, no rotation)"])
        self.c_pin.setToolTip("Snap-in: the pin has a thicker head that is pressed past a narrower neck "
                              "into a cavity in the hole. It holds by friction and rotates freely. "
                              "Snap-in + flat key holds the same way but cannot turn, for parts that "
                              "must stay in one orientation.")
        self.s_bulge = dspin(0.1, 2.0, 0.6, 0.1)
        self.s_bulge.setToolTip("How much thicker the head is than the shaft (per side).")
        self.s_headlen = dspin(1.0, 10.0, 2.6, 0.1)
        self.s_headlen.setToolTip("Length of the head. The pin length must be at least about head + 1 mm.")
        self.s_interf = dspin(0.05, 1.0, 0.3, 0.05)
        self.s_interf.setToolTip("How much narrower the hole's neck is than the head (per side). "
                                 "More = harder to push in and tighter hold; less = looser.")
        self.snap_widgets = [self.s_bulge, self.s_headlen, self.s_interf]
        self.c_pin.currentIndexChanged.connect(self._pin_type_changed)
        self._pin_type_changed()
        self.c_center = ClickComboBox()
        self.c_center.addItems(["Centre of section", "At cursor position"])
        self.c_sites = ClickComboBox()
        self.c_sites.addItems(["This section only", "All sections on the plane"])
        self.c_sites.setToolTip("Only used in 'Infinite plane' mode.")
        self.c_sites.setEnabled(False)
        self.chk_swap = QCheckBox("Pin on the other half")
        fl.addRow(self.chk_conn)
        fl.addRow("Pin diameter", self.s_diam)
        fl.addRow("Pin length", self.s_len)
        fl.addRow("Hole clearance", self.s_tol)
        fl.addRow("Extra hole depth", self.s_extra)
        fl.addRow("Pin tip chamfer", self.s_chamfer)
        fl.addRow("Pin type", self.c_pin)
        fl.addRow("  Head bulge", self.s_bulge)
        fl.addRow("  Head length", self.s_headlen)
        fl.addRow("  Neck interference", self.s_interf)
        fl.addRow("Position", self.c_center)
        fl.addRow("Sites", self.c_sites)
        fl.addRow(self.chk_swap)
        lay.addWidget(g)

        self.btn_cut = QPushButton("✂  CUT selected part")
        self.btn_cut.setStyleSheet("font-weight:bold; padding:8px;")
        self.btn_cut.setEnabled(False)
        self.btn_cut.clicked.connect(self.do_cut)

        # view
        g = Section("View", expanded=False)
        gl = QGridLayout(g.body)
        views = [("Front", lambda: self._view(self.plotter.view_xz)),
                 ("Back", lambda: self._view(self.plotter.view_xz, negative=True)),
                 ("Left", lambda: self._view(self.plotter.view_yz, negative=True)),
                 ("Right", lambda: self._view(self.plotter.view_yz)),
                 ("Top", lambda: self._view(self.plotter.view_xy)),
                 ("Iso", lambda: self._view(self.plotter.view_isometric))]
        for i, (t, f) in enumerate(views):
            gl.addWidget(self._btn(t, f), i // 3, i % 3)
        self.chk_edges = QCheckBox("Show mesh edges")
        self.chk_edges.toggled.connect(self._edges)
        gl.addWidget(self.chk_edges, 2, 0, 1, 2)
        gl.addWidget(self._btn("Fit", self._fit), 2, 2)
        self.explode = SliderRow("Explode", 0, 100, 0, 0, " %")
        self.explode.changed.connect(self._explode_changed)
        gl.addWidget(self.explode, 3, 0, 1, 3)
        lay.addWidget(g)
        lay.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(root)
        dock = QDockWidget("Tools", self)
        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(scroll, 1)
        hl.addWidget(self.btn_cut)
        dock.setWidget(holder)
        dock.setMinimumWidth(400)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)

    # ------------------------------------------------------------------ scene helpers
    def _new_uid(self):
        self._uid += 1
        return self._uid

    def _new_name(self):
        self._name_n += 1
        return f"part_{self._name_n}"

    def _next_color(self):
        c = PALETTE[self._color_i % len(PALETTE)]
        self._color_i += 1
        return c

    def _add_actor(self, part):
        part.poly = to_poly(part.mesh)
        part.actor = self.plotter.add_mesh(
            part.poly, color=hexcol(part.color), name=f"part{part.uid}", smooth_shading=True,
            specular=0.25, pickable=True, reset_camera=False, render=False)
        part.actor.SetVisibility(part.visible)
        part.actor.GetProperty().SetEdgeVisibility(self.chk_edges.isChecked())
        part.actor.SetPosition(*part.offset)

    def _remove_actor(self, part):
        if part.actor is not None:
            self.plotter.remove_actor(part.actor, render=False)
            part.actor = None

    def _icon(self, c):
        pm = QPixmap(16, 16)
        pm.fill(QColor(*c))
        return QIcon(pm)

    def _rebuild_list(self, select=None):
        self.list.blockSignals(True)
        self.list.clear()
        for p in self.parts:
            it = QListWidgetItem(self._icon(p.color), p.name)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if p.visible else Qt.Unchecked)
            self.list.addItem(it)
        self.list.blockSignals(False)
        if select is not None and 0 <= select < len(self.parts):
            self.list.setCurrentRow(select)
        self._highlight()

    def _selected(self):
        r = self.list.currentRow()
        return self.parts[r] if 0 <= r < len(self.parts) else None

    def _highlight(self):
        p = self._selected()
        if p is not None and p.poly is not None and p.visible:
            o = p.poly.outline()
            o.translate(p.offset, inplace=True)
            self.plotter.add_mesh(o, color="yellow", line_width=2, name="selbox", pickable=False,
                                  reset_camera=False, render=False)
        else:
            self.plotter.remove_actor("selbox", render=False)
        self.plotter.render()

    def _item_changed(self, item):
        r = self.list.row(item)
        if 0 <= r < len(self.parts):
            p = self.parts[r]
            p.visible = item.checkState() == Qt.Checked
            p.actor.SetVisibility(p.visible)
            self._highlight()

    def _set_model(self, entries):
        """entries: [(name, mesh, color, visible)] - replaces the whole scene."""
        for p in self.parts:
            self._remove_actor(p)
        self.parts, self.history = [], []
        for name, mesh, color, vis in entries:
            self.parts.append(core.Part(self._new_uid(), name, mesh, tuple(color), vis))
        lo = np.min([p.mesh.bounds[0] for p in self.parts], axis=0)
        hi = np.max([p.mesh.bounds[1] for p in self.parts], axis=0)
        self.diag = float(np.linalg.norm(hi - lo)) or 100.0
        half = self.diag / 2
        self.off_row.set_range(-half, half, -min(25.0, half), min(25.0, half))  # slider = fine range
        self.explode.set_value(0, emit=False)
        for p in self.parts:
            self._add_actor(p)
        self.clear_pick()
        self._rebuild_list(select=0)
        self.plotter.reset_camera()
        self._view(self.plotter.view_xz)

    # ------------------------------------------------------------------ views
    def _view(self, fn, **kw):
        fn(**kw)
        self.plotter.reset_camera()
        self.plotter.render()

    def _fit(self):
        self.plotter.reset_camera()
        self.plotter.render()

    def _edges(self, on):
        for p in self.parts:
            p.actor.GetProperty().SetEdgeVisibility(on)
        self.plotter.render()

    def _explode_changed(self):
        self._apply_explode()
        self._highlight()
        self.update_preview()

    def _apply_explode(self):
        if not self.parts:
            return
        f = self.explode.value() / 100.0
        lo = np.min([p.mesh.bounds[0] for p in self.parts], axis=0)
        hi = np.max([p.mesh.bounds[1] for p in self.parts], axis=0)
        c = (lo + hi) / 2
        for p in self.parts:
            p.offset = (p.mesh.bounds.mean(axis=0) - c) * f if len(self.parts) > 1 else np.zeros(3)
            if p.actor is not None:
                p.actor.SetPosition(*p.offset)
        self.plotter.render()

    # ------------------------------------------------------------------ picking / 3D cursor
    def _on_click(self, x, y):
        try:
            w = self.plotter.interactor
            dpr = w.devicePixelRatioF()
            vx, vy = x * dpr, w.height() * dpr - y * dpr - 1  # VTK's origin is bottom-left
            picker = vtk.vtkCellPicker()
            picker.SetTolerance(0.0005)
            picker.Pick(vx, vy, 0, self.plotter.renderer)
            actor = picker.GetActor()
            if actor is None:
                self.statusBar().showMessage("Click landed on empty space - click directly on the model.")
                return
            part = next((p for p in self.parts if p.actor is actor), None)
            if part is None:
                return
            world = np.array(picker.GetPickPosition())
            local = world - part.offset
            fi = int(np.argmin(np.linalg.norm(part.mesh.triangles_center - local, axis=1)))
            self._set_pick(part, world, part.mesh.face_normals[fi])
        except Exception as e:  # never fail silently inside an event handler
            traceback.print_exc()
            self.statusBar().showMessage(f"Pick failed: {e}")

    def _set_pick(self, part, world_point, normal):
        self.pick_part = part
        self.pick = world_point - part.offset
        self.n0 = core.unit(normal)
        self.tilt_row.set_value(0, emit=False)
        self.off_row.set_value(0, emit=False)
        self.spin_row.set_value(0, emit=False)
        self.list.setCurrentRow(self.parts.index(part))
        if self.chk_snap.isChecked():
            self.snap_now(quiet=True)
        self.btn_cut.setEnabled(True)
        self.update_preview()
        self.statusBar().showMessage(
            "Cursor placed. Adjust Spin / Tilt / Offset, set the connector size, then press CUT.")

    def snap_now(self, quiet=False):
        if self.pick is None:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            a = core.snap_spin(self.pick_part.mesh, self.pick, self.n0)
            self.spin_row.set_value(a, emit=False)
            self.tilt_row.set_value(0, emit=False)
        except Exception as e:  # snapping is a convenience only
            if not quiet:
                QMessageBox.warning(self, "Snap failed", str(e))
        finally:
            QApplication.restoreOverrideCursor()
        self.update_preview()

    def clear_pick(self):
        self.pick = None
        self.pick_part = None
        self.btn_cut.setEnabled(False)
        for n in ["cutplane", "cursor"] + self._loop_names:
            self.plotter.remove_actor(n, render=False)
        self._loop_names = []
        self.plotter.render()

    def _plane(self):
        n = core.plane_normal(self.n0, self.spin_row.value(), self.tilt_row.value())
        origin = self.pick + n * self.off_row.value()
        return origin, n

    def _local_mode(self):
        return self.c_mode.currentIndex() == 0

    def update_preview(self):
        if self.pick is None or self.pick_part is None:
            return
        origin, n = self._plane()
        off = self.pick_part.offset
        wo = origin + off
        self.plotter.remove_actor("cutplane", render=False)
        if self.chk_plane.isChecked():
            size = self.diag * 0.35
            self.plotter.add_mesh(pv.Plane(center=wo, direction=n, i_size=size, j_size=size),
                                  color="#00d0ff", opacity=0.25 if self._local_mode() else 0.35,
                                  name="cutplane", pickable=False, lighting=False,
                                  reset_camera=False, render=False)
        self.plotter.add_mesh(pv.Sphere(radius=self.diag * 0.008, center=wo), color="red",
                              name="cursor", pickable=False, reset_camera=False, render=False)
        # the "thread": the outline(s) that this cut will actually separate
        for nm in self._loop_names:
            self.plotter.remove_actor(nm, render=False)
        self._loop_names = []
        try:
            loops = core.section_loops(self.pick_part.mesh, origin, n)
            if loops and self._local_mode():
                dist = [np.min(np.linalg.norm(l - origin, axis=1)) for l in loops]
                loops = [loops[int(np.argmin(dist))]]
            for i, l in enumerate(loops):
                line = pv.lines_from_points(l)
                line.translate(off, inplace=True)
                nm = f"loop{i}"
                self.plotter.add_mesh(line, color="#ffd400", line_width=4, render_lines_as_tubes=True,
                                      name=nm, pickable=False, reset_camera=False, render=False)
                self._loop_names.append(nm)
        except Exception:
            traceback.print_exc()
        self.plotter.render()

    # ------------------------------------------------------------------ cutting
    def _pin_type_changed(self):
        snap = self.c_pin.currentIndex() in (2, 3)
        for w_ in self.snap_widgets:
            w_.setEnabled(snap)

    def _mode_changed(self):
        self.c_sites.setEnabled(not self._local_mode())
        self.update_preview()

    def _pick_state(self):
        if self.pick is None or self.pick_part is None:
            return None
        return dict(uid=self.pick_part.uid, pick=self.pick.copy(), n0=self.n0.copy(),
                    spin=self.spin_row.value(), tilt=self.tilt_row.value(), off=self.off_row.value())

    def _restore_pick(self, st):
        """Re-attach cursor + plane settings to the part with the same uid. False if it is gone."""
        part = next((p for p in self.parts if p.uid == st["uid"]), None)
        if part is None:
            return False
        self.pick_part = part
        self.pick = st["pick"].copy()
        self.n0 = st["n0"].copy()
        self.spin_row.set_value(st["spin"], emit=False)
        self.tilt_row.set_value(st["tilt"], emit=False)
        self.off_row.set_value(st["off"], emit=False)
        self.btn_cut.setEnabled(True)
        self.update_preview()
        return True

    def _snapshot(self):
        self.history.append({
            "parts": [core.Part(p.uid, p.name, p.mesh, p.color, p.visible) for p in self.parts],
            "pick": self._pick_state(), "color_i": self._color_i, "name_n": self._name_n})
        del self.history[:-20]

    def do_cut(self):
        part = self.pick_part
        if part is None or self.pick is None or part not in self.parts:
            return
        origin, n = self._plane()
        cp = None
        if self.chk_conn.isChecked():
            cp = core.ConnectorParams(
                diameter=self.s_diam.value(), length=self.s_len.value(), tolerance=self.s_tol.value(),
                extra_depth=self.s_extra.value(),
                center_mode="cursor" if self.c_center.currentIndex() == 1 else "section",
                all_sections=self.c_sites.currentIndex() == 1, swap=self.chk_swap.isChecked(),
                chamfer=self.s_chamfer.value(), key=self.c_pin.currentIndex() in (1, 3),
                snap=self.c_pin.currentIndex() in (2, 3), head_bulge=self.s_bulge.value(),
                head_length=self.s_headlen.value(), interference=self.s_interf.value())
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            neg, pos, note = core.cut_part(
                part.mesh, origin, n, self.pick, cp,
                mode="local" if self._local_mode() else "plane",
                kerf=self.s_kerf.value(), margin=self.s_margin.value())
        except Exception as e:
            QApplication.restoreOverrideCursor()
            traceback.print_exc()
            QMessageBox.warning(self, "Cut failed", str(e))
            return
        QApplication.restoreOverrideCursor()

        self._snapshot()
        idx = self.parts.index(part)
        self._remove_actor(part)
        new_color = self._next_color()
        new = []
        for i, m in enumerate(neg):
            new.append(core.Part(part.uid if i == 0 else self._new_uid(),
                                 part.name if i == 0 else self._new_name(), m, part.color, part.visible))
        for m in pos:
            new.append(core.Part(self._new_uid(), self._new_name(), m, new_color, True))
        self.parts[idx:idx + 1] = new
        for p in new:
            self._add_actor(p)
        self._apply_explode()
        self.clear_pick()
        self._rebuild_list(select=idx)
        msg = f"Cut done: {len(new)} piece(s)."
        if not self._local_mode() and len(neg) + len(pos) > 2:
            msg += " The plane produced extra separate bodies - Undo and adjust it if that is not what you want."
        self.statusBar().showMessage(msg + (" " + note if note else ""))

    def undo(self):
        if not self.history:
            return
        snap = self.history.pop()
        current = self._pick_state()          # what the user has set up right now
        sel = self._selected()
        sel_uid = sel.uid if sel else None
        for p in self.parts:
            self._remove_actor(p)
        self.parts = snap["parts"]
        self._color_i = snap["color_i"]      # the retried cut gets the same colour and names again
        self._name_n = snap["name_n"]
        for p in self.parts:
            self._add_actor(p)
        self._apply_explode()
        row = next((i for i, p in enumerate(self.parts) if p.uid == sel_uid), 0)
        self._rebuild_list(select=row)
        # Keep the cursor/plane the user has now; if there is none (a cut clears it), bring back the
        # one that was active when the undone action was made, so the cut can be retried with new options.
        if not (current and self._restore_pick(current)):
            self.clear_pick()
            if snap["pick"]:
                self._restore_pick(snap["pick"])
        self.statusBar().showMessage("Undone - cursor and plane settings kept.")

    # ------------------------------------------------------------------ part actions
    def color_part(self):
        p = self._selected()
        if p is None:
            return
        c = QColorDialog.getColor(QColor(*p.color), self, "Part colour")
        if c.isValid():
            p.color = (c.red(), c.green(), c.blue())
            p.actor.GetProperty().SetColor(c.redF(), c.greenF(), c.blueF())
            self._rebuild_list(select=self.parts.index(p))

    def rename_part(self):
        p = self._selected()
        if p is None:
            return
        name, ok = QInputDialog.getText(self, "Rename part", "Name:", text=p.name)
        if ok and name.strip():
            p.name = name.strip()
            self._rebuild_list(select=self.parts.index(p))

    def delete_part(self):
        p = self._selected()
        if p is None:
            return
        self._snapshot()
        self._remove_actor(p)
        self.parts.remove(p)
        self.clear_pick()
        self._rebuild_list(select=0)

    # ------------------------------------------------------------------ files
    def open_stl(self, path=None):
        if not isinstance(path, str):
            path, _ = QFileDialog.getOpenFileName(self, "Open STL", "", "STL files (*.stl *.STL)")
        if not path:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            mesh, ok = core.load_mesh(path)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Cannot open", str(e))
            return
        QApplication.restoreOverrideCursor()
        self._set_model([(os.path.splitext(os.path.basename(path))[0], mesh, BASE_COLOR, True)])
        if not ok:
            QMessageBox.warning(self, "Mesh is not watertight",
                                "The model has holes or self-intersections that could not be repaired. "
                                "Cutting may fail; try repairing it first (e.g. 3D Builder, Meshmixer, "
                                "or Blender's 3D-Print toolbox).")
        self.statusBar().showMessage(f"Loaded {path}: {len(mesh.faces)} triangles, "
                                     f"size {mesh.extents.round(1).tolist()} mm")

    def open_collection(self):
        folder = QFileDialog.getExistingDirectory(self, "Open collection folder")
        if not folder:
            return
        try:
            entries = core.load_collection(folder)
        except Exception as e:
            QMessageBox.critical(self, "Cannot open", str(e))
            return
        if not entries:
            QMessageBox.information(self, "Empty", "No STL files found in that folder.")
            return
        self._set_model(entries)

    def save_collection(self):
        if not self.parts:
            return
        folder = QFileDialog.getExistingDirectory(self, "Choose / create the folder to save the parts in")
        if not folder:
            return
        planned = core.plan_collection_files(self.parts) + ["collection.json"]
        existing = [f for f in planned if os.path.exists(os.path.join(folder, f))]
        if existing:
            shown = ", ".join(existing[:6]) + (f" ... and {len(existing) - 6} more" if len(existing) > 6 else "")
            r = QMessageBox.question(
                self, "Replace existing files?",
                f"{len(existing)} file(s) already exist in this folder and will be replaced:\n\n{shown}\n\n"
                "Replace them?", QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if r != QMessageBox.Yes:
                self.statusBar().showMessage("Save cancelled.")
                return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            n = core.save_collection(folder, self.parts)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            traceback.print_exc()
            QMessageBox.critical(self, "Save failed", str(e))
            return
        QApplication.restoreOverrideCursor()
        self.statusBar().showMessage(f"All was saved: {n} STL file(s) + collection.json in {folder}")
        QMessageBox.information(self, "Saved", f"All was saved.\n\n{n} STL file(s) + collection.json\n{folder}")

    def export_3mf(self):
        if not self.parts:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export 3MF", "figure.3mf", "3MF (*.3mf)")
        if not path:
            return
        if not path.lower().endswith(".3mf"):
            path += ".3mf"
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            core.export_3mf(path, self.parts)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            traceback.print_exc()
            QMessageBox.critical(self, "Export failed", str(e))
            return
        QApplication.restoreOverrideCursor()
        self.statusBar().showMessage(f"All was saved: {path}")
        QMessageBox.information(self, "Saved", f"All was saved.\n\n{path}")

    def closeEvent(self, e):
        self.plotter.close()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        w.open_stl(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
