"""One commit boundary for commands, session capture and page replacement."""


def commit_quick_edits(panel):
    if not any(hasattr(panel, name) for name in ("_quick_table_editors", "_quick_reference_list", "_quick_parameter_forms")):
        return True
    from ui.quick_parameter_forms import commit_parameter_edits
    for view in getattr(panel, "_quick_table_editors", []):
        if not view.model._closed and view.source_table.isEnabled() and not view.model.commitEdits():
            return False
    reference = getattr(panel, "_quick_reference_list", None)
    commit = getattr(reference, "commitEdits", None)
    if callable(commit) and not commit():
        return False
    return commit_parameter_edits(panel)


def discard_quick_edits(panel):
    """Discard presentation drafts before an explicit reset/cancel operation."""
    from shiboken6 import isValid
    for view in getattr(panel, "_quick_table_editors", []):
        if isValid(view) and not view.model._closed:
            view.model.discardEdits()
    reference = getattr(panel, "_quick_reference_list", None)
    discard = getattr(reference, "discardEdits", None)
    if callable(discard):
        discard()
    for form in getattr(panel, "_quick_parameter_forms", {}).get("forms", []):
        if not isValid(form) or form.adapter._closed:
            continue
        form.adapter._drafts.clear()
        for field in form.adapter._fields:
            field.publish(invalid=False, error="")
        form.adapter.refresh()


def require_committed_quick_edits(panel):
    if not commit_quick_edits(panel):
        raise RuntimeError("Correct the highlighted Quick editor value before reloading this page. Your current page is unchanged.")
