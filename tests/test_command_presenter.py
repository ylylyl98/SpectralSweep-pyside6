"""Independent command contract: no widgets, drivers, config or SDK imports."""
import pytest

from app.command_presenter import CommandPresenter


def field(key="exposure", *, value=10.0, enabled=True, integer=False):
    return dict(id=key, label="Exposure", value=value, enabled=enabled,
                minimum=1.0, maximum=100.0, integer=integer, decimals=0 if integer else 2)


def actions(*, enabled=True):
    return [dict(id="run", text="Run", enabled=enabled, cancel=False, tooltip=""),
            dict(id="stop", text="Stop", enabled=enabled, cancel=True, tooltip="")]


def presenter_with_field(**kwargs):
    presenter = CommandPresenter()
    presenter.publish(title="Acquisition", fields=[field(**kwargs)], actions=actions())
    return presenter


@pytest.mark.parametrize("text", ["", ".", "NaN", "inf", "-inf", "0", "101", "1e999", "abc"])
def test_invalid_numeric_edits_never_reach_backend(text):
    presenter = presenter_with_field()
    edits, commands = [], []
    presenter.fieldEdited.connect(lambda *args: edits.append(args))
    presenter.commandRequested.connect(commands.append)
    assert not presenter.editField("exposure", text)
    assert not edits
    assert presenter.validationError
    assert not presenter.invoke("run")
    assert presenter.invoke("stop")
    assert commands == ["stop"]


def test_integer_validation_and_corrected_value_restore_commands():
    presenter = presenter_with_field(integer=True)
    edits = []
    presenter.fieldEdited.connect(lambda *args: edits.append(args))
    assert not presenter.editField("exposure", "2.5")
    assert presenter.editField("exposure", " 3 ")
    assert edits == [("exposure", 3.0)]
    assert not presenter.validationError
    assert presenter.invoke("run")
    # The view is updated only from the backend's subsequent publication.
    assert presenter.snapshot["fields"][0]["value"] == 10.0


def test_disabled_unknown_and_closed_edits_do_not_dispatch():
    presenter = presenter_with_field(enabled=False)
    edits = []
    presenter.fieldEdited.connect(lambda *args: edits.append(args))
    assert not presenter.editField("exposure", "5")
    assert not presenter.editField("missing", "5")
    presenter.publish(title="Acquisition", fields=[field()], actions=actions())
    presenter.close()
    assert not presenter.editField("exposure", "5")
    assert not presenter.invoke("run")
    assert not edits


def test_status_and_value_refresh_keep_item_identity_for_focused_editor():
    presenter = presenter_with_field()
    original_field, original_action = presenter.fields[0], presenter.actions[0]
    structural, field_updates = [], []
    presenter.fieldsChanged.connect(lambda: structural.append(True))
    original_field.changed.connect(lambda: field_updates.append(True))
    presenter.publish(title="Acquisition", status="Ready", fields=[field(value=12.0)], actions=actions())
    assert presenter.fields[0] is original_field
    assert presenter.actions[0] is original_action
    assert not structural
    assert field_updates == [True]
    presenter.publish(title="Acquisition", status="Ready", fields=[field(value=12.0)], actions=actions())
    assert field_updates == [True]
    assert not structural


def test_public_snapshot_and_item_info_cannot_mutate_command_contract():
    presenter = presenter_with_field()
    snapshot = presenter.snapshot
    snapshot["fields"][0]["enabled"] = False
    info = presenter.fields[0].info
    info["maximum"] = 0
    assert presenter.editField("exposure", "5")
    assert presenter.fields[0].info["maximum"] == 100.0


def test_action_structure_change_does_not_reset_field_model():
    presenter = presenter_with_field()
    original_field = presenter.fields[0]
    field_structure_changes = []
    presenter.fieldsChanged.connect(lambda: field_structure_changes.append(True))
    presenter.publish(title="Acquisition", fields=[field()], actions=actions()[:1])
    assert presenter.fields[0] is original_field
    assert not field_structure_changes


def test_reentrant_invoke_does_not_emit_second_command():
    presenter = presenter_with_field()
    requests, nested = [], []

    def dispatch(key):
        requests.append(key)
        nested.append(presenter.invoke("run"))

    presenter.commandRequested.connect(dispatch)
    assert presenter.invoke("run")
    assert requests == ["run"]
    assert nested == [False]


def test_command_availability_uses_latest_backend_publication():
    presenter = presenter_with_field()
    requests = []
    presenter.commandRequested.connect(requests.append)
    presenter.publish(title="Acquisition", status="External owner", fields=[field(enabled=False)],
                      actions=actions(enabled=False))
    assert not presenter.invoke("run")
    assert not presenter.invoke("stop")
    assert not presenter.invoke("missing")
    assert not requests


def test_removed_invalid_field_does_not_leave_invisible_permanent_command_block():
    presenter = presenter_with_field()
    assert not presenter.editField("exposure", ".")
    presenter.publish(title="Workflow", fields=[], actions=actions())
    assert not presenter.validationError
    assert presenter.invoke("run")


def test_disabled_invalid_field_does_not_block_unrelated_enabled_actions():
    presenter = presenter_with_field()
    assert not presenter.editField("exposure", ".")
    presenter.publish(title="Acquisition", fields=[field(enabled=False)], actions=actions())
    assert not presenter.validationError
    assert presenter.invoke("run")


def test_discard_invalid_edits_keeps_backend_value_and_reopens_admission():
    presenter = presenter_with_field()
    assert not presenter.editField("exposure", "-5")
    presenter.discardInvalidEdits()
    assert not presenter.validationError
    assert presenter.snapshot["fields"][0]["value"] == 10.0
    assert presenter.invoke("run")
