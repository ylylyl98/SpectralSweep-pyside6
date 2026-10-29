"""Disposable sequence review examples; no controller or production planner."""
from PySide6.QtWidgets import QComboBox, QLabel, QVBoxLayout, QWidget

from ui.quick_dual_gate_sequence import QuickDualGateSequencePreview


def example_plan(nested=True):
    contexts = [{"Center Wavelength (nm)": center, "Rotation1 Angle (deg)": angle}
                for center in (750, 810) for angle in (0, 45, 90)]
    conditions = [dict(Run=True, condition_label=label, frames=5, repeat=2,
                       Vtg_start=start, Vtg_stop=stop, Vbg_start=-1, Vbg_stop=1,
                       Vbias_start=0, Vbias_stop=0, MeasurePower=False, When=when)
                  for label, start, stop, when in (("Forward", -2, 2, ""),
                                                  ("Return", 2, -2, "Center_Wavelength > 800"))]
    tasks = []
    order = [dict(kind="conditions", id="conditions", label="Gate conditions"),
             dict(kind="points", id="points", label="Gate points"),
             dict(kind="group", id="group:1", label="Wavelength + rotation")]
    if nested:
        for row_i, row in enumerate(conditions):
            for repeat_i in range(row["repeat"]):
                for point_i in range(row["frames"]):
                    for context_i, context in enumerate(contexts):
                        if row_i == 1 and context["Center Wavelength (nm)"] <= 800:
                            continue
                        t = point_i / (row["frames"] - 1)
                        point = dict(point_i=point_i, point_total=row["frames"],
                                     Vtg=row["Vtg_start"] + t * (row["Vtg_stop"] - row["Vtg_start"]),
                                     Vbg=-1 + 2*t, Vbias=0)
                        tasks.append(dict(nested=True, seq_i=len(tasks), row_i=row_i, ctx=context,
                                          row=dict(row, frames=1, repeat=1), repeat_i=repeat_i,
                                          repeat_total=row["repeat"], gate_point=point, execution_order=order,
                                          group_values={"group:1": context}, group_instances={"group:1": context_i}))
    else:
        for row_i, row in enumerate(conditions):
            for context_i, context in enumerate(contexts):
                if row_i == 1 and context["Center Wavelength (nm)"] <= 800:
                    continue
                tasks.append(dict(seq_i=context_i, row_i=row_i, ctx=context, row=row))
    return dict(final_sequence=contexts, df_batch=conditions, acquisition_schedule=tasks,
                acquisition_grouping="batch_first", total_acq=18,
                preview_state="Draft sequence · simulated", run_outcome="idle")


class DualGateSequenceDemo(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        label = QLabel("Synthetic Dual Gate plans · reviewing these examples sends no device commands.")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.mode = QComboBox()
        self.mode.addItems(["Interleaved gate points", "Sweeps with When filtering"])
        layout.addWidget(self.mode)
        self.preview = QuickDualGateSequencePreview(self)
        layout.addWidget(self.preview, 1)
        self.mode.currentIndexChanged.connect(self._load)
        self._load(0)

    def _load(self, index):
        self.preview.update_plan(**example_plan(nested=index == 0))

    def shutdown(self):
        self.preview.shutdown()
