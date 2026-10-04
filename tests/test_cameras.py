"""Camera discovery: the OS name parsers, the choice, and the config edit.

All pure -- no camera, no cv2, no filesystem.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cameras
from cameras import Camera


def _cam(index, *, live=True, opened=True, name=""):
    return Camera(index=index, backend="any", name=name, opened=opened, live=live,
                  width=640, height=480, std=40.0 if live else 0.5)


# --------------------------------------------------------------------------
# names
# --------------------------------------------------------------------------

def test_windows_pnp_names_are_stripped_and_blank_lines_dropped():
    text = "\nIntegrated Camera  \r\n\r\nIR Camera\n   \n"
    assert cameras.parse_windows_pnp_names(text) == ["Integrated Camera", "IR Camera"]


def test_v4l2_name_is_stripped():
    assert cameras.parse_v4l2_name("HD WebCam: HD WebCam\n") == "HD WebCam: HD WebCam"


def test_macos_names_read_the_json_and_tolerate_junk():
    good = '{"SPCameraDataType": [{"_name": "FaceTime HD Camera"}]}'
    assert cameras.parse_macos_camera_names(good) == ["FaceTime HD Camera"]
    assert cameras.parse_macos_camera_names("not json") == []
    assert cameras.parse_macos_camera_names("{}") == []


def test_windows_probes_directshow_before_media_foundation():
    # Order matters: MSMF is the backend that hands back frozen frames, so a
    # working dshow answer must win.
    assert cameras.backends_for("win32")[0] == "dshow"
    assert cameras.backends_for("linux") == ["any"]
    assert cameras.backends_for("some-future-os") == ["any"]


def test_macos_names_are_never_claimed_to_match_indices():
    assert cameras.names_are_authoritative("darwin") is False
    assert cameras.names_are_authoritative("linux") is True


# --------------------------------------------------------------------------
# choosing
# --------------------------------------------------------------------------

def test_default_choice_skips_a_camera_that_opens_but_is_frozen():
    frozen, real = _cam(0, live=False), _cam(1)
    assert cameras.pick_default([frozen, real]) is real


def test_default_choice_falls_back_to_a_frozen_camera_when_thats_all_there_is():
    frozen = _cam(0, live=False)
    assert cameras.pick_default([frozen]) is frozen
    assert cameras.pick_default([]) is None


def test_label_falls_back_to_the_index_when_the_os_gave_no_name():
    assert _cam(2).label == "camera 2"
    assert _cam(2, name="Integrated Camera").label == "Integrated Camera"


def test_status_distinguishes_closed_frozen_and_live():
    assert _cam(0, opened=False, live=False).status == "did not open"
    assert "frozen" in _cam(0, live=False).status
    assert _cam(0).status == "live, 640x480"


# --------------------------------------------------------------------------
# writing config.yaml
# --------------------------------------------------------------------------

def test_existing_index_is_replaced_and_comments_survive():
    text = (
        "camera:\n"
        "  backend: opencv\n"
        "  # which one to use\n"
        "  index: 1\n"
        "  width: 640\n"
        "zone:\n"
        "  overlap_threshold: 0.35\n"
    )
    out = cameras.set_camera_in_config_text(text, 2, "dshow")
    assert "  index: 2\n" in out
    assert "  backend_api: dshow\n" in out
    assert "  # which one to use\n" in out
    assert "  width: 640\n" in out
    # The next top-level block is untouched, and only one camera index exists.
    assert out.count("index:") == 1
    assert out.endswith("zone:\n  overlap_threshold: 0.35\n")


def test_missing_keys_are_inserted_into_the_camera_block():
    text = "camera:\n  backend: opencv\nzone:\n  points: []\n"
    out = cameras.set_camera_in_config_text(text, 3, "msmf")
    lines = out.splitlines()
    assert lines[0] == "camera:"
    assert "  index: 3" in lines[1:4]
    assert "  backend_api: msmf" in lines[1:4]
    assert "zone:" in lines


def test_a_config_with_no_camera_block_gets_one():
    out = cameras.set_camera_in_config_text("zone:\n  points: []\n", 0, "any")
    assert out.startswith("zone:\n  points: []\n")
    assert "camera:" in out
    assert "  index: 0" in out.splitlines()
    assert "  backend_api: any" in out.splitlines()


def test_an_empty_config_still_yields_a_usable_camera_block():
    out = cameras.set_camera_in_config_text("", 1, "dshow")
    assert "camera:" in out
    assert "  backend: opencv" in out.splitlines()
    assert "  index: 1" in out.splitlines()


def test_the_result_parses_as_the_yaml_we_meant():
    yaml = pytest.importorskip("yaml")
    text = Path(__file__).resolve().parent.parent.joinpath("config.yaml").read_text()
    config = yaml.safe_load(cameras.set_camera_in_config_text(text, 4, "msmf"))
    assert config["camera"]["index"] == 4
    assert config["camera"]["backend_api"] == "msmf"
    assert config["camera"]["backend"] == "opencv"
    # Nothing else moved.
    original = yaml.safe_load(text)
    assert config["zone"] == original["zone"]
    assert config["alert"] == original["alert"]
