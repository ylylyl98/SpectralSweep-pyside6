"""Python dependencies refreshed with each supported Qt Quick workspace.

Keep helpers before the workspace builders that import them. Shared helpers
also serve unsupported pages, whose existing instances still need a restart.
"""

QUICK_PAGE_MODULES = {
    "dual_gate": (
        "ui.quick_sweep_calculator", "ui.quick_gate_sections",
        "ui.quick_workspace_layouts", "ui.quick_table_editor",
        "ui.quick_parameter_forms",
    ),
    "spectrum": (
        "ui.quick_acquisition_sections", "ui.quick_workspace_layouts",
        "ui.quick_table_editor", "ui.quick_parameter_forms",
        "ui.quick_reference_list",
    ),
    "settings": (
        "ui.quick_settings_layout", "ui.quick_workspace_layouts",
        "ui.quick_table_editor", "ui.quick_parameter_forms",
    ),
}

QUICK_SHARED_RESTART_SOURCES = frozenset({
    "ui/quick_gate_sections.py",
    "ui/quick_acquisition_sections.py",
    "ui/quick_workspace_layouts.py",
    "ui/quick_table_editor.py",
    "ui/quick_parameter_forms.py",
})
