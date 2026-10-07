"""WinSpec-only fixed-position calibration; no LightField calibration writes."""
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QCheckBox,
    QTableWidget,QTableWidgetItem,QComboBox,QDoubleSpinBox,QPlainTextEdit,
    QWidget,QTabWidget,QScrollArea,QHeaderView,QToolButton)
from app.wavelength_calibration import fit_calibration, reference_lines, valid_context

REFERENCE = Path(__file__).resolve().parents[1]/'calibrations/ingaas-2026-09-26/LightField-SourceSpectra.xml'

class WavelengthCalibrationDialog(QDialog):
    capture_requested = Signal()
    broad_capture_requested = Signal(float)
    calibration_saved = Signal(object)
    broad_settings_requested = Signal(float,float)
    automatic_started = Signal()
    grating_requested = Signal(str)
    automatic_finished = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('WinSpec InGaAs — Wavelength Calibration')
        self.resize(1000,800)
        self.frame=None
        self.lines=[]
        layout=QVBoxLayout(self)
        note=QLabel('WinSpec only · fixed grating / center / exit · PIXIS calibration is unchanged.\n'
                    'Assign known reference lines explicitly. Use ≥3 Fit + ≥1 Check lines for linear; ≥4 Fit + ≥1 Check for quadratic.\n'
                    'Only the fitted pixel interval will display nm; pixels outside it are not extrapolated.')
        note.setWordWrap(True); layout.addWidget(note)
        self.context_label=QLabel('Acquire a WinSpec lamp spectrum first.'); self.context_label.setWordWrap(True)
        layout.addWidget(self.context_label)
        self.source_confirm=QCheckBox('Ne/Ar lamp is on; same physical InGaAs detector and mounting as this setup profile')
        layout.addWidget(self.source_confirm)
        capture=QPushButton('Acquire lamp spectrum using Spectrum settings')
        capture.clicked.connect(self.capture_requested.emit); layout.addWidget(capture)
        broad=QPushButton('Broad calibration: 900–1700 nm…');broad.clicked.connect(self.open_broad);layout.addWidget(broad)
        self.broad_dialog=None
        load=QPushButton('Load saved raw calibration capture…');load.clicked.connect(self.load_raw);layout.addWidget(load)
        self.plot=pg.PlotWidget(); self.plot.setLabel('bottom','Pixel'); self.plot.setLabel('left','Counts')
        self.plot.getPlotItem().layout.setContentsMargins(8, 12, 8, 8)
        layout.addWidget(self.plot,2)
        self.table=QTableWidget(0,3); self.table.setHorizontalHeaderLabels(['Use','Peak pixel (1-based)','Known wavelength nm'])
        self.table.horizontalHeader().setStretchLastSection(True); layout.addWidget(self.table,2)
        add=QPushButton('Add peak manually'); add.clicked.connect(lambda: self.add_peak(1.)); layout.addWidget(add)
        options=QHBoxLayout()
        self.degree=QComboBox(); self.degree.addItems(['Linear','Quadratic'])
        self.tolerance=QDoubleSpinBox(); self.tolerance.setRange(.001,10); self.tolerance.setDecimals(3); self.tolerance.setValue(.2)
        options.addWidget(self.degree); options.addWidget(QLabel('Maximum fit / check error (nm):')); options.addWidget(self.tolerance)
        layout.addLayout(options)
        self.reference=QPlainTextEdit(); self.reference.setReadOnly(True); self.reference.setMaximumHeight(85)
        try:
            self.lines=reference_lines(REFERENCE)
            self.reference.setPlainText('LightField NIR PI Neon / NeAr reference (nm; species):\n' + '\n'.join(f'{w:.5f}  {s}' for w,s,_ in self.lines))
        except Exception as exc:
            self.reference.setPlainText(f'Reference unavailable: {exc}. Enter independently verified wavelengths manually.')
        layout.addWidget(self.reference)
        from utils.config import cfg
        self.history=QComboBox()
        self.history.addItem('Saved WinSpec calibrations (read-only history)')
        for record in reversed(cfg.lf6.winspec_wavelength_calibrations):
            context=record.get('context',{})
            self.history.addItem(f"{record.get('created_utc','')} | {context.get('grating','')} | {context.get('center_nm','')} nm | {context.get('output_port','')} | RMS {record.get('rms_nm',0):.4f} nm")
        layout.addWidget(self.history)
        self.result=QLabel('No calibration fitted.'); self.result.setWordWrap(True); layout.addWidget(self.result)
        fit=QPushButton('Fit and validate'); fit.clicked.connect(self.fit)
        self.save=QPushButton('Save WinSpec calibration'); self.save.setEnabled(False); self.save.clicked.connect(self.save_result)
        row=QHBoxLayout(); row.addWidget(fit); row.addWidget(self.save); layout.addLayout(row)
        self.record=None
        self.table.itemChanged.connect(self.invalidate)
        self.degree.currentIndexChanged.connect(self.invalidate)
        self.tolerance.valueChanged.connect(self.invalidate)
        self.source_confirm.toggled.connect(self.invalidate)
        self.embedded = False
        # Reuse the tested fitting controls, but present them as a guided flow.
        while layout.count():
            layout.takeAt(0)
        self.steps = QTabWidget()
        layout.addWidget(self.steps)
        page_layouts = []
        for title in ('1 · Prepare', '2 · Review lines', '3 · Results'):
            scroll = QScrollArea(); scroll.setWidgetResizable(True)
            content = QWidget(); flow = QVBoxLayout(content)
            flow.setAlignment(Qt.AlignmentFlag.AlignTop)
            scroll.setWidget(content); self.steps.addTab(scroll, title)
            page_layouts.append(flow)
        prepare, review, results = page_layouts
        note.setText('WinSpec InGaAs wavelength calibration\n'
                     'Connect WinSpec and LightField, select the grating and exit, then turn on the PI Ne/Ar lamp. '
                     'PIXIS calibration is unchanged.')
        prepare.addWidget(note); prepare.addWidget(self.context_label)
        self.source_confirm.setText('Ne/Ar lamp on; detector and mounting confirmed')
        prepare.addWidget(self.source_confirm)
        self.mode = QComboBox(); self.mode.addItems(['Fixed — current center', 'Broad — multiple centers'])
        self.mode.setCurrentIndex(1)
        prepare.addWidget(QLabel('Calibration range')); prepare.addWidget(self.mode)
        self.capture_settings_layout = QVBoxLayout(); prepare.addLayout(self.capture_settings_layout)
        self.capture_button = capture
        capture.setText('Start calibration'); capture.clicked.disconnect()
        capture.clicked.connect(self.start_calibration); prepare.addWidget(capture)
        prepare.addWidget(load)
        manual_capture = QPushButton('Acquire current center for manual calibration')
        manual_capture.clicked.connect(self.capture_requested.emit); prepare.addWidget(manual_capture)
        hint = QLabel('Fixed: acquire → confirm reference lines → validate → save.\n'
                      'Broad: automatically acquire 900-1700 nm centers, match Ne/Ar, independently validate and save. All installed gratings; unvalidated intervals stay pixels.')
        hint.setWordWrap(True); prepare.addWidget(hint)
        self.display_settings_layout = QVBoxLayout(); prepare.addLayout(self.display_settings_layout)
        review_note = QLabel('Confirm the wavelength for each usable peak. Assign at least 3 Fit lines and 1 independent Check line for a linear fit.')
        review_note.setWordWrap(True); review.addWidget(review_note)
        self.plot.setMinimumHeight(180); self.plot.setMaximumHeight(280); review.addWidget(self.plot)
        self.table.setMinimumHeight(180)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        review.addWidget(self.table); review.addWidget(add)
        advanced = QWidget(); advanced_layout = QVBoxLayout(advanced)
        options.setParent(None); advanced_layout.addLayout(options)
        advanced_layout.addWidget(self.reference); advanced_layout.addWidget(self.history)
        advanced_layout.addWidget(broad)
        toggle = QToolButton(); toggle.setText('Advanced fit settings and history'); toggle.setCheckable(True)
        toggle.toggled.connect(advanced.setVisible); review.addWidget(toggle); review.addWidget(advanced); advanced.hide()
        row.removeWidget(fit); row.removeWidget(self.save)
        row.setParent(None)
        fit.setText('Validate calibration'); review.addWidget(fit)
        results.addWidget(self.result); results.addWidget(self.save)
        back = QPushButton('Review reference lines'); back.clicked.connect(lambda: self.steps.setCurrentIndex(1))
        results.addWidget(back)
        # Daily operation is one page; the existing manual tools remain optional.
        layout.removeWidget(self.steps)
        self.simple_panel = QWidget()
        simple = QVBoxLayout(self.simple_panel)
        simple.setContentsMargins(0, 0, 0, 0)
        simple.setSpacing(12)
        title = QLabel('InGaAs calibration')
        font = title.font(); font.setPointSize(font.pointSize() + 4); font.setBold(True); title.setFont(font)
        simple.addWidget(title)
        description = QLabel('Connect the Ne/Ar lamp and select the InGaAs setup. The app calibrates all installed gratings and saves each result automatically.')
        description.setWordWrap(True); simple.addWidget(description)
        simple.addWidget(self.source_confirm)
        actions = QHBoxLayout()
        capture.setText('Calibrate all gratings')
        capture.clicked.disconnect(); capture.clicked.connect(self._start_simple_calibration)
        actions.addWidget(capture)
        self.stop_button = QPushButton('Stop')
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(lambda: self.broad_dialog.stop_collection() if self.broad_dialog else None)
        actions.addWidget(self.stop_button); actions.addStretch()
        self.load_model_button = QPushButton('Load calibration…')
        self.load_model_button.clicked.connect(self.load_calibration_model)
        actions.addWidget(self.load_model_button)
        simple.addLayout(actions)
        self.progress_host = QVBoxLayout(); simple.addLayout(self.progress_host)
        simple.addWidget(self.result)
        self.result.setText('Ready. Temperature: Locked or <= -100 C. Target: 900-1700 nm.')
        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText('Advanced settings and manual calibration')
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.toggled.connect(self.steps.setVisible)
        simple.addWidget(self.advanced_toggle)
        layout.addWidget(self.simple_panel); layout.addWidget(self.steps)
        self.steps.hide(); layout.addStretch()
        self.automatic_started.connect(lambda: self.result.setText('Calibration in progress. Results are saved automatically.'))
        self.automatic_started.connect(lambda: self.stop_button.setEnabled(True))
        self.automatic_finished.connect(lambda ok: self.stop_button.setEnabled(False))
        self.automatic_finished.connect(lambda ok: self.result.setText('Finished. See grating results above.' if ok else 'Stopped or validation failed. Previous calibrations retained.'))


    def _start_simple_calibration(self):
        self.mode.setCurrentIndex(1)
        self.start_calibration()

    def load_calibration_model(self):
        broad = self.broad_dialog
        if broad is not None and (broad.batch_active or broad.sweep_active or broad.analysis_running):
            self.result.setText('Finish or stop automatic calibration before loading a model.')
            return
        from PySide6.QtWidgets import QFileDialog
        from app.wavelength_calibration import validate_imported_calibration
        path, _ = QFileDialog.getOpenFileName(self, 'Load validated WinSpec calibration',
                                             str(REFERENCE.parent.parent), 'JSON (*.json)')
        if not path:
            return
        try:
            record = validate_imported_calibration(json.loads(Path(path).read_text(encoding='utf-8')))
            if not getattr(self, 'save_callback', lambda r: False)(record):
                raise ValueError('Could not save imported calibration')
            self.record = record
            self.result.setText('Calibration loaded and validation recomputed. It applies only to matching WinSpec optics and validated intervals.')
        except Exception as exc:
            self.result.setText('Calibration not loaded: ' + str(exc))

    def start_calibration(self):
        if not self.source_confirm.isChecked():
            self.result.setText('Confirm that the Ne/Ar lamp is on and the detector is mounted before starting.')
            self.context_label.setText(self.result.text())
            return
        if self.mode.currentIndex() == 1:
            if self.broad_dialog is not None and (self.broad_dialog.batch_active or self.broad_dialog.sweep_active or self.broad_dialog.analysis_running):return
            self.open_broad()
            self.broad_dialog.confirm.setChecked(True)
            self.automatic_started.emit()
            exposure=1000.  # Automatic calibration chooses its own bounded exposure.
            self.broad_dialog.start_all_gratings(getattr(self,'gratings_provider',lambda:[])(),exposure)
        else:
            self.capture_requested.emit()

    def invalidate(self, *args):
        self.record=None; self.save.setEnabled(False)

    def reject(self):
        if not self.embedded:
            super().reject()

    def hideEvent(self,event):
        if not self.embedded and self.broad_dialog is not None:
            self.broad_dialog.stop_collection()
            self.broad_dialog.hide()
        super().hideEvent(event)

    def open_broad(self):
        from ui.broad_calibration_dialog import BroadCalibrationDialog
        if self.broad_dialog is None:
            self.broad_dialog=BroadCalibrationDialog(self)
            self.broad_dialog.setWindowFlags(Qt.WindowType.Widget)
            self.broad_dialog.embedded = self.embedded
            self.broad_dialog.set_simple_view()
            self.progress_host.addWidget(self.broad_dialog)
            self.broad_dialog.grating_requested.connect(self.grating_requested.emit)
            self.broad_dialog.capture_center_requested.connect(self.broad_capture_requested.emit)
            self.broad_dialog.capture_settings_requested.connect(self.broad_settings_requested.emit)
            self.broad_dialog.automatic_finished.connect(self.automatic_finished.emit)
            self.broad_dialog.calibration_saved.connect(self.calibration_saved.emit)
        self.broad_dialog.save_callback = getattr(self, 'save_callback', lambda record: False)
        if not self.broad_dialog.sweep_active and not self.broad_dialog.analysis_running:self.broad_dialog.refresh()
        self.broad_dialog.show()

    def load_raw(self):
        from PySide6.QtWidgets import QFileDialog
        from utils.config import _CONFIG_FILE
        path,_=QFileDialog.getOpenFileName(self,'Load WinSpec lamp capture',str(Path(_CONFIG_FILE).parent/'winspec-calibration-captures'),'JSON (*.json)')
        if not path:return
        try:
            frame=json.loads(Path(path).read_text(encoding='utf8'))
            if np.asarray(frame['counts']).shape!=(512,) or not valid_context(frame.get('context')):
                raise ValueError('Not a 512-pixel capture with live optics context')
            self.set_frame(frame)
        except Exception as exc:self.result.setText(str(exc))

    def add_peak(self, pixel):
        row=self.table.rowCount(); self.table.insertRow(row)
        role=QComboBox(); role.addItems(['Ignore','Fit','Check']); role.currentIndexChanged.connect(self.invalidate)
        self.table.setCellWidget(row,0,role); self.table.setItem(row,1,QTableWidgetItem(f'{pixel:.4f}'))
        line=QComboBox(); line.setEditable(True); line.addItem('')
        for w,s,_ in self.lines: line.addItem(f'{w:.5f}  {s}',w)
        line.currentTextChanged.connect(self.invalidate); self.table.setCellWidget(row,2,line)
        self.invalidate()

    def set_frame(self, frame):
        self.frame=copy.deepcopy(frame); self.invalidate(); self.table.setRowCount(0)
        y=np.asarray(frame['counts']); self.plot.clear(); self.plot.plot(np.arange(1,513),y)
        context=frame.get('context')
        self.context_label.setText(
            f"Captured center: {context['center_nm']:g} nm · Grating: {context['grating']}\nExit: {context['output_port']} · Detector: WinSpec InGaAs"
            if valid_context(context) else 'Live optics identity unavailable / changed during exposure. This frame cannot be calibrated.')
        broad = self.broad_dialog
        if broad is not None and (broad.batch_active or broad.sweep_active or broad.analysis_running):
            self.result.setText('Lamp spectrum captured. Automatic matching and validation follow the scan.')
            return
        self.steps.setCurrentIndex(1)
        noise=max(1.,float(np.median(np.abs(np.diff(y)-np.median(np.diff(y))))/0.6745/np.sqrt(2)))
        peaks=np.where((y[1:-1]>y[:-2])&(y[1:-1]>y[2:])&(y[1:-1]>np.median(y)+6*noise))[0]+1
        selected=[]
        for i in sorted(peaks,key=lambda i:y[i],reverse=True):
            if all(abs(i-j)>3 for j in selected): selected.append(i)
            if len(selected)>=18: break
        for i in sorted(selected):
            denominator=y[i-1]-2*y[i]+y[i+1]
            offset=.5*(y[i-1]-y[i+1])/denominator if denominator else 0
            self.add_peak(i+1+float(np.clip(offset,-.5,.5)))
        self.result.setText(f'{len(selected)} candidate peaks; no wavelengths assigned automatically. Verify identities and avoid saturated/blended peaks.')

    def save_raw_frame(self):
        """Save evidence before fitting so it can be independently inspected."""
        if self.frame is None: return
        from utils.config import _CONFIG_FILE
        folder=Path(_CONFIG_FILE).parent/'winspec-calibration-captures'
        folder.mkdir(parents=True,exist_ok=True)
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        path=folder/f'near-{stamp}.json'
        path.write_text(json.dumps(self.frame,indent=2,allow_nan=False),encoding='utf8')
        np.savetxt(path.with_suffix('.csv'),np.column_stack((np.arange(1,513),self.frame['counts'])),
                   delimiter=',',header='pixel,intensity_counts',comments='')
        self.result.setText(self.result.text()+f'\nRaw capture saved: {path}')

    def fit(self):
        self.invalidate()
        self.steps.setCurrentIndex(2)
        try:
            if self.frame is None or not self.source_confirm.isChecked(): raise ValueError('Acquire a lamp spectrum and confirm source / physical detector')
            p,w,cp,cw=[],[],[],[]
            for row in range(self.table.rowCount()):
                role=self.table.cellWidget(row,0).currentText()
                if role=='Ignore': continue
                pixel=float(self.table.item(row,1).text())
                wavelength=float(self.table.cellWidget(row,2).currentText().split()[0])
                (p if role=='Fit' else cp).append(pixel); (w if role=='Fit' else cw).append(wavelength)
            record=fit_calibration(p,w,cp,cw,self.frame['context'],self.degree.currentIndex()+1,self.tolerance.value())
            record['raw_counts']=list(self.frame['counts']); record['captured_utc']=self.frame.get('captured_utc')
            record['reference_sha256']=hashlib.sha256(REFERENCE.read_bytes()).hexdigest() if REFERENCE.exists() else None
            self.record=record; self.save.setEnabled(True)
            residuals=np.asarray(record['check_residual_nm'],dtype=float)
            self.result.setText(f"Validation passed\nFit RMS: {record['rms_nm']:.5f} nm\n"
                                f"Independent check RMS: {np.sqrt(np.mean(residuals**2)):.5f} nm\n"
                                f"Largest check error: {np.max(np.abs(residuals)):.5f} nm\n"
                                f"Valid pixel interval: {record['pixel_range']}\n"
                                'Save to use this WinSpec calibration. Reference-line identities must be correct.')
        except Exception as exc: self.result.setText(str(exc))

    def save_result(self):
        if self.record is not None: self.calibration_saved.emit(copy.deepcopy(self.record))
