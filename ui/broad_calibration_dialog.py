"""WinSpec broad calibration collection and validation; explicit center acquisitions."""
import copy
import threading
from PySide6.QtCore import Signal, QTimer, QObject
from PySide6.QtWidgets import QDialog,QVBoxLayout,QLabel,QComboBox,QPushButton,QTableWidget,QCheckBox,QDoubleSpinBox,QHBoxLayout
from app.wavelength_calibration import fit_broad_calibration
from utils.config import cfg

class _AnalysisSignals(QObject):
    progress=Signal(str)
    completed=Signal(object)


class BroadCalibrationDialog(QDialog):
    capture_center_requested=Signal(float)
    calibration_saved=Signal(object)
    capture_settings_requested=Signal(float,float)
    automatic_finished=Signal(bool)
    grating_requested=Signal(str)

    def __init__(self,parent=None):
        super().__init__(parent)
        self.setWindowTitle('WinSpec Broad calibration — target 900–1700 nm')
        self.resize(900,650)
        layout=QVBoxLayout(self)
        note=QLabel('One grating / detector / exit per model. PIXIS calibration is untouched.\n'
                    'Acquire anchor centers 900, 1000, …1700 nm; independent check centers 950, 1050, …1650 nm.\n'
                    'Calibration automatically matches Ne/Ar peaks and validates independent center positions.\n'
                    'A broad model is enabled only in independently checked intervals, within the shared fitted pixel range.\n'
                    'Center coverage is not the same as complete wavelength coverage. No extrapolation.')
        note.setWordWrap(True);layout.addWidget(note)
        self.confirm=QCheckBox('Ne/Ar lamp is on; WinSpec and current grating are selected');layout.addWidget(self.confirm)
        row=QHBoxLayout();self.center=QDoubleSpinBox();self.center.setRange(900,1700);self.center.setValue(900);self.center.setSuffix(' nm')
        row.addWidget(QLabel('Center to acquire:'));row.addWidget(self.center)
        capture=QPushButton('Acquire this center');capture.clicked.connect(self.capture);row.addWidget(capture)
        layout.addLayout(row)
        self.sweep_centers=[];self.sweep_expected=None;self.sweep_identity=None
        self.sweep_active=False;self.sweep_generation=0
        sweep=QPushButton('Collect 900–1700 nm automatically (50 nm steps)')
        sweep.clicked.connect(self.start_collection);layout.addWidget(sweep)
        stop=QPushButton('Stop after current exposure');stop.clicked.connect(self.stop_collection);layout.addWidget(stop)
        self.table=QTableWidget(0,2);self.table.setHorizontalHeaderLabels(['Role','Saved local calibration']);self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        refresh=QPushButton('Refresh saved local calibrations');refresh.clicked.connect(self.refresh);layout.addWidget(refresh)
        self.tolerance=QDoubleSpinBox();self.tolerance.setRange(.001,10);self.tolerance.setDecimals(3);self.tolerance.setValue(.2)
        layout.addWidget(QLabel('Maximum independent-center error (nm):'));layout.addWidget(self.tolerance)
        self.result=QLabel('No broad calibration validated.');self.result.setWordWrap(True);layout.addWidget(self.result)
        fit=QPushButton('Validate selected anchor / check centers');fit.clicked.connect(self.fit);layout.addWidget(fit)
        self.save=QPushButton('Save separate WinSpec broad calibration');self.save.setEnabled(False);self.save.clicked.connect(self.save_record);layout.addWidget(self.save)
        self.record=None;self.tolerance.valueChanged.connect(self.invalidate);self.refresh()
        self.batch_active=False;self.batch_expected=None
        self.automatic=False;self.analysis_running=False
        self._cancel_analysis=threading.Event()
        self._analysis_signals=_AnalysisSignals(self)
        self._analysis_signals.progress.connect(self._analysis_progress)
        self._analysis_signals.completed.connect(self._analysis_done)

    def set_simple_view(self):
        # Inline progress only; collection/fit controls remain in advanced tools.
        layout = self.layout()
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item.widget() is not None and item.widget() is not self.result:
                item.widget().hide()
            if item.layout() is not None:
                for j in range(item.layout().count()):
                    widget = item.layout().itemAt(j).widget()
                    if widget is not None:widget.hide()
        self.setMinimumSize(0, 0)
        self.resize(500, 100)

    def start_all_gratings(self, gratings, exposure_ms=1000.):
        if self.batch_active or self.sweep_active or self.analysis_running:return
        gratings=list(dict.fromkeys(str(g) for g in gratings if g is not None))
        if not gratings:
            self.result.setText('Cannot enumerate installed gratings. Refresh the LightField connection.')
            self.automatic_finished.emit(False);return
        self.batch_active=True;self.batch_queue=gratings;self.batch_results=[]
        self.batch_exposure=exposure_ms
        self._next_grating()

    def _next_grating(self):
        if not self.batch_active:return
        if not self.batch_queue:
            self.batch_active=False;self.batch_expected=None
            self.result.setText('All gratings finished. Saved models are retained separately.\n'+'\n'.join('%s: %s'%(g,msg) for g,ok,msg in self.batch_results))
            self.automatic_finished.emit(any(ok for g,ok,msg in self.batch_results));return
        self.batch_expected=self.batch_queue.pop(0)
        self.result.setText('Switching grating: '+self.batch_expected)
        self.grating_requested.emit(self.batch_expected)

    def grating_ready(self, grating):
        if not self.batch_active or str(grating)!=self.batch_expected:return
        self.start_automatic_collection(self.batch_exposure)

    def _finish_automatic(self, success):
        if self.batch_active:
            self.batch_results.append((self.batch_expected,success,self.result.text()))
            self._next_grating()
        else:self.automatic_finished.emit(success)

    def start_automatic_collection(self, exposure_ms=1000.):
        if self.sweep_active or self.analysis_running:return
        if not self.confirm.isChecked():return
        self.automatic=True;self.auto_exposure=float(exposure_ms)
        self.auto_frames=[];self.auto_attempt=0
        self._cancel_analysis=threading.Event()
        self.start_collection()

    def _request_current(self):
        if self.automatic:
            self.capture_settings_requested.emit(self.sweep_expected,self.auto_exposure)
        else:
            self.capture_center_requested.emit(self.sweep_expected)

    def _analysis_progress(self, text):
        if self.analysis_running:self.result.setText(text)

    def _start_analysis(self):
        from app.auto_wavelength_calibration import build_broad
        from app.wavelength_calibration import reference_lines
        from ui.wavelength_calibration_dialog import REFERENCE
        self.analysis_running=True
        frames=copy.deepcopy(self.auto_frames)
        try:reference=reference_lines(REFERENCE)
        except Exception as exc:
            self._analysis_done({'generation':self.sweep_generation,'error':str(exc)});return
        tolerance=self.tolerance.value();generation=self.sweep_generation
        cancel=self._cancel_analysis;signals=self._analysis_signals
        self.result.setText('Matching Ne/Ar and validating independent center positions…')
        def run():
            try:
                record=build_broad(frames,reference,tolerance,cancel.is_set,lambda text: signals.progress.emit(text) if not cancel.is_set() else None)
                result={'generation':generation,'record':record}
            except Exception as exc:result={'generation':generation,'error':str(exc)}
            if not cancel.is_set():
                try:signals.completed.emit(result)
                except RuntimeError:pass  # Widget closed; pure analysis has no side effects.
        threading.Thread(target=run,daemon=True).start()

    def _analysis_done(self, payload):
        if payload['generation']!=self.sweep_generation or not self.analysis_running:return
        self.analysis_running=False;self.automatic=False
        if 'error' in payload:
            self.result.setText('Not calibrated: '+payload['error']+'\nPrevious calibration retained; raw captures remain available.')
            self._finish_automatic(False);return
        self.record=payload['record']
        try:
            saved = bool(getattr(self, 'save_callback', lambda record: False)(self.record))
        except Exception:
            saved = False
        if not saved:
            self.result.setText('Validation passed, but save failed. Previous calibration retained.')
            self._finish_automatic(False);return
        ranges=', '.join('%g–%g'%tuple(s['center_range']) for s in self.record['segments'])
        self.result.setText('Validation passed. Center intervals: '+ranges+' nm\n'
                            'Independent RMS: %.5f nm. Only validated pixels/intervals are enabled.'%self.record['rms_nm'])
        self._finish_automatic(True)

    def invalidate(self,*args):
        self.record=None;self.save.setEnabled(False)

    def capture(self):
        if self.batch_active:return
        if self.sweep_active or self.analysis_running:return
        if not self.confirm.isChecked():self.result.setText('Confirm Ne/Ar source and WinSpec first.');return
        self.result.setText('Requesting center; wait for capture in the local calibration window. Center remains at the selected value.')
        self.capture_center_requested.emit(self.center.value())

    def start_collection(self):
        if self.batch_active and not self.automatic:return
        if not self.confirm.isChecked():self.result.setText('Confirm Ne/Ar source and WinSpec first.');return
        if self.sweep_active or self.analysis_running:return
        self.sweep_active=True;self.sweep_generation+=1
        self.sweep_centers=list(range(900,1701,50));self.sweep_identity=None
        self._next_center()

    def _next_center(self):
        if not self.sweep_active or not self.sweep_centers:return
        self.sweep_expected=float(self.sweep_centers.pop(0))
        self.auto_attempt=0
        self.center.setValue(self.sweep_expected)
        self.result.setText(f'Collecting {self.sweep_expected:g} nm; {len(self.sweep_centers)} positions remain. Raw captures are not yet a valid calibration.')
        self._request_current()

    def frame_collected(self,frame):
        if self.sweep_expected is None:return
        from app.wavelength_calibration import valid_context, _fixed_identity
        context=frame.get('context')
        if not valid_context(context) or abs(context['center_nm']-self.sweep_expected)>.001:
            self.stop_collection('Stopped: missing or unexpected center readback');return
        if self.batch_active and str(context['grating'])!=self.batch_expected:
            self.stop_collection('Stopped: unexpected grating readback');return
        identity=_fixed_identity(context)
        if self.sweep_identity is not None and identity!=self.sweep_identity:
            self.stop_collection('Stopped: grating, detector or exit changed');return
        self.sweep_identity=identity
        if self.automatic:
            from app.auto_wavelength_calibration import exposure_retry
            try:retry=exposure_retry(frame,self.auto_exposure,self.auto_attempt)
            except ValueError as exc:self.stop_collection(str(exc));return
            if retry is not None:
                self.auto_exposure=retry;self.auto_attempt+=1
                self.result.setText('Adjusting exposure at %g nm to %g ms (%d/4)'%(self.sweep_expected,retry,self.auto_attempt))
                generation=self.sweep_generation
                QTimer.singleShot(100,lambda:self._request_current() if generation==self.sweep_generation else None)
                return
            self.auto_frames.append(copy.deepcopy(frame))
        self.sweep_expected=None
        if self.sweep_centers:
            generation=self.sweep_generation
            QTimer.singleShot(100,lambda: self._next_center() if generation==self.sweep_generation else None)
        else:
            self.sweep_active=False
            if self.automatic:self._start_analysis()
            else:self.result.setText('17 center positions captured. Center remains at 1700 nm. Reference-line matching and independent validation are still required.')

    def stop_collection(self, message='Collection stopped; current exposure may finish. Center is not restored automatically.'):
        automatic=getattr(self,'automatic',False) or self.batch_active
        saved=any(ok for g,ok,msg in getattr(self,'batch_results',[])) if self.batch_active else False
        self.batch_active=False;self.batch_expected=None
        if hasattr(self,'_cancel_analysis'):self._cancel_analysis.set()
        self.analysis_running=False;self.automatic=False
        self.sweep_centers=[];self.sweep_expected=None
        self.sweep_active=False;self.sweep_generation+=1
        self.result.setText(message if isinstance(message,str) else 'Collection stopped after current exposure.')
        if automatic:self.automatic_finished.emit(saved)

    def closeEvent(self,event):
        self.stop_collection();super().closeEvent(event)

    def hideEvent(self,event):
        if not getattr(self, 'embedded', False):
            self.stop_collection()
        super().hideEvent(event)

    def refresh(self):
        self.invalidate();self.table.setRowCount(0)
        self.records=[r for r in cfg.lf6.winspec_wavelength_calibrations if r.get('kind')!='broad_piecewise']
        for r in self.records:
            i=self.table.rowCount();self.table.insertRow(i)
            role=QComboBox();role.addItems(['Ignore','Anchor','Check']);role.currentIndexChanged.connect(self.invalidate)
            self.table.setCellWidget(i,0,role)
            c=r.get('context',{})
            label=QLabel(f"{c.get('profile')} | grating {c.get('grating')} | {c.get('center_nm')} nm | {c.get('output_port')} | {r.get('created_utc')}")
            self.table.setCellWidget(i,1,label)

    def fit(self):
        self.invalidate()
        try:
            anchors=[];checks=[]
            for i,r in enumerate(self.records):
                role=self.table.cellWidget(i,0).currentText()
                if role=='Anchor':anchors.append(r)
                elif role=='Check':checks.append(r)
            self.record=fit_broad_calibration(anchors,checks,self.tolerance.value())
            ranges=', '.join(f"{s['center_range'][0]:g}–{s['center_range'][1]:g}" for s in self.record['segments'])
            self.result.setText(f"Validated center intervals (nm): {ranges}. Independent RMS {self.record['rms_nm']:.5f} nm. Unlisted intervals remain pixels.")
            self.save.setEnabled(True)
        except Exception as exc:self.result.setText(str(exc))

    def save_record(self):
        if self.record is not None:self.calibration_saved.emit(self.record)
