"""The couch zone drawn in the dashboard, on its way to the detection loop.

The dashboard stores it as fractions of the frame; only the agent knows the
camera's real pixel size. These tests cover that handover -- no camera, no
network, no server.
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cloud_client import AgentLink, RemoteSettings
from service import MonitorService

FRAME = (480, 640, 3)  # (h, w, c), as OpenCV reports it


class FakeSettings:
    def all(self):
        return {"video": {}}


def make_service(zone=None):
    tmp = Path(tempfile.mkdtemp())
    config = {"alert": {"video_dir": str(tmp)}}
    if zone is not None:
        config["zone"] = zone
    return MonitorService(config, store=None, settings=FakeSettings()), tmp


def test_dashboard_zone_is_scaled_to_the_camera_frame():
    svc, tmp = make_service()
    try:
        svc.update_zone([(0.0, 0.5), (0.5, 0.5), (1.0, 1.0)], 0.6)
        # Nothing scales until a frame has told us the camera's real size.
        assert svc.zone_points == []

        svc._resolve_zone(FRAME)
        assert svc.zone_points == [(0, 240), (320, 240), (639, 479)]
        assert svc.overlap_threshold == 0.6

        # A camera that comes back at a different resolution needs no redraw.
        svc._resolve_zone((240, 320, 3))
        assert svc.zone_points == [(0, 120), (160, 120), (319, 239)]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_config_zone_is_published_as_fractions_for_the_editor():
    """A zone from calibrate.py has to make it back to the dashboard, or the
    editor would have nothing to start from but an empty polygon."""
    svc, tmp = make_service({"points": [[64, 48], [576, 48], [576, 432]]})
    try:
        assert svc.zone_points == [(64, 48), (576, 48), (576, 432)]
        svc._resolve_zone(FRAME)
        assert svc.status["zone_points"] == [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]]

        svc.update_zone([(0.2, 0.2), (0.8, 0.2), (0.8, 0.8)])
        svc._resolve_zone(FRAME)
        assert svc.status["zone_points"] == [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8]]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_remote_settings_keeps_the_local_zone_when_the_server_sends_none():
    """An empty zone from the server means "not set there", not "no zone":
    clearing it would leave the camera unable to alert at all."""
    rs = RemoteSettings()
    old = rs.replace({"alert": {}, "video": {}}, "builtin")
    assert old["zone"] == rs.all()["zone"] == {"points": [], "overlap_threshold": 0.35}

    zone = {"points": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]], "overlap_threshold": 0.5}
    old = rs.replace({"alert": {}, "video": {}, "zone": zone}, "builtin")
    assert old["zone"]["points"] == []        # the caller can see it changed
    assert rs.all()["zone"] == zone


class FakeMonitor:
    def __init__(self):
        self.status = {}
        self.zones = []

    def apply_video_settings(self, v):
        pass

    def update_zone(self, points, overlap_threshold=None):
        self.zones.append((points, overlap_threshold))


def test_heartbeat_pushes_a_changed_zone_into_the_monitor_once():
    monitor = FakeMonitor()
    link = AgentLink(client=None, monitor=monitor, settings=RemoteSettings(),
                     uploader=None)
    zone = {"points": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]], "overlap_threshold": 0.5}
    reply = {"settings": {"alert": {"active_sound": "builtin"}, "video": {},
                          "zone": zone}}

    link._apply(reply)
    assert monitor.zones == [(zone["points"], 0.5)]

    # A zone that has not changed is not pushed again, twice a second.
    link._apply(reply)
    assert len(monitor.zones) == 1
