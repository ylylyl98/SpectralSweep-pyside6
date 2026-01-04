from __future__ import annotations

import uuid
from typing import List, Dict, Any, Callable, Optional

from .context import RunContext
from .csv_writer import CSVWriter
from app.steps.registry import get_step_class  # registry auto-imports built-ins


class Runner:
    def __init__(self) -> None:
        pass

    def run_recipe(
        self,
        recipe: List[Dict[str, Any]],
        devices: Dict[str, Any],
        wavelength_headers: list,
        extra_scalar_fields_order: List[str],
        out_dir: str,
        file_base: str,
        progress_cb: Optional[Callable[[str], None]] = None,
    
    ) -> None:
        run_id = str(uuid.uuid4())[:8]
        csvw = CSVWriter(out_dir, file_base, wavelength_headers, extra_scalar_fields_order)
        ctx = RunContext(
            run_id=run_id,
            devices=devices,
            csv_writer=csvw,
            progress_cb=progress_cb,
            extra_scalar_fields_order=extra_scalar_fields_order,
        )

        try:
            total = len(recipe)
            for i, step_cfg in enumerate(recipe, start=1):
                step_id = step_cfg.get("step")
                if not step_id:
                    raise ValueError(f"Recipe step missing 'step' key: {step_cfg}")
                step_cls = get_step_class(step_id)
                step = step_cls(step_cfg)

                # Optional validation hook
                if hasattr(step, "validate"):
                    step.validate(ctx)

                if progress_cb:
                    progress_cb(f"Step {i}/{total}: {step_id}")
                step.run(ctx)

            if progress_cb:
                progress_cb("Run complete")

        finally:
            csvw.close()


# Simple singleton used by the UI
runner_singleton = Runner()
