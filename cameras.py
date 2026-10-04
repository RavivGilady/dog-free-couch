"""
Camera discovery: which cameras does this computer actually have, and which
index is which?

OpenCV addresses cameras by a bare integer index, which is no help at all on
a laptop that registers three of them (built-in webcam, the infrared Windows
Hello sensor, and whatever virtual camera a meeting app installed). This
module puts names to those indices by asking the OS, and probes each one to
say whether it opens and produces a live picture.

The name lookup is best-effort and per-platform; the probe is not. So the
picker (`list_cameras.py`) always shows the probe result, and treats a name
as a label that makes the list readable.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Index/backend combinations worth trying. Windows is the awkward one: its
# default Media Foundation backend can hand back a frozen placeholder frame,
# so DirectShow is probed first there (see camera.OpenCVCamera).
BACKENDS = {
    "win32": ["dshow", "msmf"],
    "default": ["any"],
}
MAX_INDEX = 5

# Probing indices that do not exist makes OpenCV grumble on stderr about
# backends that "can't be used to capture by index". That is the expected
# answer here, not a problem, so it is kept out of the user's way. Must be
# set before cv2 is first imported.
os.environ.setdefault("OPENCV_LOG_LEVEL", "FATAL")

# A real frame varies pixel to pixel; a "no signal" placeholder is flat.
DEAD_FRAME_STD = 3.0


@dataclass
class Camera:
    """One probed index/backend pair."""

    index: int
    backend: str
    name: str = ""
    opened: bool = False
    live: bool = False
    width: int = 0
    height: int = 0
    std: float = 0.0

    @property
    def label(self) -> str:
        return self.name or f"camera {self.index}"

    @property
    def status(self) -> str:
        if not self.opened:
            return "did not open"
        if not self.live:
            return "opens, but the picture looks frozen"
        return f"live, {self.width}x{self.height}"

    def describe(self) -> str:
        return f"{self.label}  (index={self.index} backend={self.backend}) -- {self.status}"


def backends_for(platform: str = sys.platform) -> list[str]:
    return BACKENDS.get(platform, BACKENDS["default"])


# --------------------------------------------------------------------------
# names from the OS -- one parser per platform, each pure and tested
# --------------------------------------------------------------------------

def parse_v4l2_name(text: str) -> str:
    """Contents of /sys/class/video4linux/videoN/name."""
    return text.strip()


def parse_windows_pnp_names(text: str) -> list[str]:
    """Lines of `Get-CimInstance Win32_PnPEntity | ... Name`.

    Enumeration order here is the OS's, which is usually but not certainly
    OpenCV's index order -- so these names are only used when the
    authoritative DirectShow list (pygrabber) is not installed.
    """
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_macos_camera_names(text: str) -> list[str]:
    """`system_profiler SPCameraDataType -json` output."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    names = []
    for cam in data.get("SPCameraDataType") or []:
        name = cam.get("_name") or cam.get("spcamera_model-id")
        if name:
            names.append(str(name).strip())
    return names


def _run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout or ""


def _windows_names() -> dict[int, str]:
    # pygrabber enumerates DirectShow devices in the same order OpenCV's
    # CAP_DSHOW indexes them, so when it is available the mapping is exact.
    try:
        from pygrabber.dshow_graph import FilterGraph  # type: ignore

        return dict(enumerate(FilterGraph().get_input_devices()))
    except Exception:
        pass

    names = parse_windows_pnp_names(_run([
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "Get-CimInstance Win32_PnPEntity | "
        "Where-Object { $_.PNPClass -eq 'Camera' -or $_.PNPClass -eq 'Image' } | "
        "ForEach-Object { $_.Name }",
    ]))
    return dict(enumerate(names))


def _linux_names() -> dict[int, str]:
    names: dict[int, str] = {}
    for path in sorted(Path("/sys/class/video4linux").glob("video*")):
        try:
            index = int(path.name[len("video"):])
            names[index] = parse_v4l2_name((path / "name").read_text())
        except (OSError, ValueError):
            continue
    return names


def _macos_names() -> dict[int, str]:
    names = parse_macos_camera_names(_run(["system_profiler", "SPCameraDataType", "-json"]))
    return dict(enumerate(names))


def device_names(platform: str = sys.platform) -> dict[int, str]:
    """Best-effort {index: human name} for the cameras the OS knows about."""
    try:
        if platform == "win32":
            return _windows_names()
        if platform == "darwin":
            return _macos_names()
        return _linux_names()
    except Exception:
        return {}


def names_are_authoritative(platform: str = sys.platform) -> bool:
    """Whether the index->name mapping can be trusted to match OpenCV's.

    On Linux, /dev/videoN is index N, so it can. On Windows it can only when
    pygrabber is installed to read the DirectShow device order; otherwise the
    names come from the OS device list and may be shuffled relative to the
    indices. macOS offers no ordered enumeration at all.
    """
    if platform == "win32":
        try:
            import pygrabber.dshow_graph  # type: ignore  # noqa: F401

            return True
        except Exception:
            return False
    return platform != "darwin"


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------

def probe(index: int, backend: str, frames: int = 4, name: str = "") -> Camera:
    """Open one index/backend pair and see what comes out of it."""
    import cv2

    flags = {
        "any": cv2.CAP_ANY,
        "dshow": getattr(cv2, "CAP_DSHOW", cv2.CAP_ANY),
        "msmf": getattr(cv2, "CAP_MSMF", cv2.CAP_ANY),
    }
    cam = Camera(index=index, backend=backend, name=name)

    cap = cv2.VideoCapture(index, flags.get(backend, cv2.CAP_ANY))
    try:
        if not cap.isOpened():
            return cam
        cam.opened = True
        frame = None
        for _ in range(max(1, frames)):
            ok, got = cap.read()
            if ok and got is not None:
                frame = got
            time.sleep(0.03)
        if frame is not None:
            cam.height, cam.width = frame.shape[:2]
            cam.std = float(frame.std())
            cam.live = cam.std >= DEAD_FRAME_STD
    finally:
        cap.release()
    return cam


def discover(max_index: int = MAX_INDEX, platform: str = sys.platform,
             on_progress=None) -> list[Camera]:
    """Probe every index/backend pair, keeping the ones that opened.

    Each index is reported once, under the first backend that gave a live
    picture (falling back to one that merely opened), so the result reads as
    "these are your cameras" rather than as a matrix of attempts.
    """
    names = device_names(platform)
    found: list[Camera] = []

    for index in range(max_index + 1):
        best: Camera | None = None
        for backend in backends_for(platform):
            if on_progress:
                on_progress(index, backend)
            cam = probe(index, backend, name=names.get(index, ""))
            if cam.live:
                best = cam
                break
            if cam.opened and best is None:
                best = cam
        if best is not None:
            found.append(best)
    return found


def pick_default(cameras: list[Camera]) -> Camera | None:
    """The camera to offer as the suggested choice: the first live one."""
    for cam in cameras:
        if cam.live:
            return cam
    return cameras[0] if cameras else None


# --------------------------------------------------------------------------
# writing the choice back to config.yaml
# --------------------------------------------------------------------------

def set_camera_in_config_text(text: str, index: int, backend_api: str) -> str:
    """Return `text` (a config.yaml) with the camera index/backend set.

    This edits the two lines in place rather than re-dumping the parsed YAML,
    so the comments in config.yaml survive being told which camera to use.
    """
    lines = text.splitlines()
    values = {"index": str(index), "backend_api": backend_api}

    start = next((i for i, l in enumerate(lines) if l.rstrip() == "camera:"), None)
    if start is None:
        block = ["camera:", "  backend: opencv"]
        block += [f"  {k}: {v}" for k, v in values.items()]
        return "\n".join(lines + block) + "\n"

    # The camera block runs until the next line that starts in column 0.
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i] and not lines[i][0].isspace():
            end = i
            break

    for key, value in values.items():
        for i in range(start + 1, end):
            stripped = lines[i].strip()
            if stripped.startswith(f"{key}:") or stripped.startswith(f"#{key}:"):
                indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
                lines[i] = f"{indent}{key}: {value}"
                break
        else:
            lines.insert(start + 1, f"  {key}: {value}")
            end += 1

    return "\n".join(lines) + "\n"


def save_choice(config_path: str, index: int, backend_api: str) -> None:
    path = Path(config_path)
    text = path.read_text() if path.exists() else ""
    path.write_text(set_camera_in_config_text(text, index, backend_api))
