import copy
import pytest
from PyQt6.QtWidgets import QCheckBox, QDialog
from sc_studio.studio_tools import PublishDialog
from sc_studio.agent_gateway import AgentGateway
from sc_studio.document import Document
from test_gui import window, application
from test_document import populated


def test_publish_fields_and_preset_values(window):
    window.add_doc(populated())
    dialog = PublishDialog(window)
    settings = dialog.values()
    assert settings.version == 6 and settings.repack
    assert dialog.sources.count() == 1
    assert dialog.sources.item(0).checkState().value == 2
    dialog.fields["encoding"].setCurrentText("KTX")
    dialog.fields["external"].setChecked(True)
    assert dialog.values().external
    dialog.fields["encoding"].setCurrentText("RGBA8")
    with pytest.raises(ValueError, match="require KTX"):
        dialog.values()
    dialog.deleteLater()


def test_new_actions_are_registered(window):
    required = {
        "publish",
        "svg",
        "primitive",
        "parenting",
        "mask_range",
        "morph",
        "color_tween",
        "text_style",
        "custom_curve",
        "rendered_preview",
        "palette",
        "batch_exports",
        "nine_slice",
        "group_tracks",
        "repack_atlases",
    }
    assert required <= window.actions.keys()
    assert window.actions["publish"].shortcut().toString() == "Ctrl+Alt+P"


def test_agent_authoring_undo_and_render(window):
    window.add_doc(populated())
    gateway = AgentGateway(window)
    before = copy.deepcopy(window.current().doc.tables)
    result = gateway.execute(
        "author",
        {"action": "parenting", "parameters": {"clip_id": 7, "parents": {1: 0}}},
    )
    assert result == {"result": None}
    image = gateway.execute("render_resource", {"resource_id": 7})
    assert image["width"] > 0 and image["png"].startswith("iVBOR")
    gateway.execute("undo", {})
    assert window.current().doc.tables == before


def test_failed_agent_authoring_rolls_back(window):
    window.add_doc(populated())
    gateway = AgentGateway(window)
    before = copy.deepcopy(window.current().doc.tables)
    with pytest.raises(ValueError, match="Mask must"):
        gateway.execute(
            "author",
            {
                "action": "mask_range",
                "parameters": {"clip_id": 7, "mask_child": 1, "content_children": [0]},
            },
        )
    assert window.current().doc.tables == before and not window.current().history


def test_agent_publish_preserves_source_and_checks_overwrite(window, tmp_path):
    window.add_doc(populated())
    gateway = AgentGateway(window)
    ed = window.current()
    before = copy.deepcopy(ed.doc.tables)
    path = tmp_path / "out.sc"
    report = gateway.execute("publish", {"path": str(path)})
    assert report["exports"] == 1 and ed.doc.tables == before
    assert Document.load(path).validate() == []
    with pytest.raises(ValueError, match="Output exists"):
        gateway.execute("publish", {"path": str(path)})
    ed.doc.path = path
    with pytest.raises(ValueError, match="separate output"):
        gateway.execute("publish", {"path": str(path), "overwrite": True})
