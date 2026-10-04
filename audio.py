"""
Microphone capture, so an event clip has sound.

Two independent pieces have to be there for this to work, and neither is a
hard dependency of the monitor proper:

1. `sounddevice` (PortAudio) to read the mic. Without it there is no audio.
2. `ffmpeg` on PATH to mux. OpenCV's VideoWriter writes video only -- there
   is no API for an audio track -- so the clip is written silent as before
   and the WAV is married to it afterwards.

Either one missing means clips stay silent rather than stop being written:
a dog on the couch is still worth recording without the sound of it.

Timing is the subtle part. The clip's frames are buffered (pre-roll) and
written at a nominal fps that never quite matches what the camera really
delivered, so the audio is cut to the *wall-clock* window the frames cover
and the video's frame rate is restated at mux time (see `mux_command`).
Lining the two up at the ends is the best that can be done without
timestamps inside the video file, and it keeps a minute-long clip from
drifting seconds out.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import wave
from collections import deque
from pathlib import Path

# Audio codec to encode to, per container. The video track is always copied.
_AUDIO_CODEC = {".mp4": "aac", ".webm": "libopus", ".avi": "libmp3lame"}
_DEFAULT_CODEC = "aac"

_SAMPLE_WIDTH = 2  # int16 PCM, the only format this module produces

# Buffer kept beyond the longest clip, so finishing one (writing the WAV,
# running ffmpeg) can't race the audio it still has to cut out.
HISTORY_SLACK_SEC = 15.0


# --------------------------------------------------------------------------
# pure helpers (no device, no ffmpeg -- these are what the tests exercise)
# --------------------------------------------------------------------------

def slice_pcm(blocks, t0: float, t1: float, rate: int, channels: int) -> bytes:
    """Cut [t0, t1) out of recorded blocks, padding gaps with silence.

    `blocks` is an iterable of (end_time, pcm_bytes): the wall-clock time a
    block finished arriving, and its int16 frames. The padding matters more
    than it looks -- a dropped block, or a window reaching back further than
    the buffer holds, must shorten the audio's *content*, never its
    duration, or everything after the gap plays early.
    """
    frame_bytes = _SAMPLE_WIDTH * channels
    total = max(0, int(round((t1 - t0) * rate)))
    out = bytearray(total * frame_bytes)

    for end, pcm in blocks:
        n = len(pcm) // frame_bytes
        if n <= 0:
            continue
        start = end - n / rate
        # Overlap of this block with the requested window, as block frames.
        lo = max(0, int(round((t0 - start) * rate)))
        hi = min(n, int(round((t1 - start) * rate)))
        dest = int(round((start - t0) * rate)) + lo
        if dest < 0:  # shouldn't happen once lo is clamped, but don't wrap
            lo -= dest
            dest = 0
        if hi <= lo or dest >= total:
            continue
        hi = min(hi, lo + (total - dest))
        out[dest * frame_bytes:(dest + hi - lo) * frame_bytes] = \
            pcm[lo * frame_bytes:hi * frame_bytes]

    return bytes(out)


def write_wav(path, pcm: bytes, rate: int, channels: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(_SAMPLE_WIDTH)
        w.setframerate(rate)
        w.writeframes(pcm)


def mux_command(ffmpeg: str, video: str, wav: str, out: str,
                fps: float | None = None) -> list:
    """The ffmpeg invocation that joins one silent clip to one WAV.

    `-r` *before* the video input is the whole point of restating the frame
    rate: it discards the timestamps OpenCV wrote (which assume the nominal
    fps) and regenerates them from the rate the camera actually achieved, so
    the video track ends up as long as the audio instead of drifting. It
    works with `-c:v copy`, so nothing is re-encoded but the audio.
    """
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-nostdin"]
    if fps:
        cmd += ["-r", f"{fps:.6f}"]
    cmd += ["-i", str(video), "-i", str(wav),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", _AUDIO_CODEC.get(Path(out).suffix.lower(), _DEFAULT_CODEC),
            "-b:a", "128k", "-shortest", str(out)]
    return cmd


def ffmpeg_path() -> str | None:
    """ffmpeg, from DFC_FFMPEG or PATH. Looked up per clip, so installing it
    doesn't need the agent restarted."""
    return os.environ.get("DFC_FFMPEG") or shutil.which("ffmpeg")


# --------------------------------------------------------------------------
# the mic
# --------------------------------------------------------------------------

class MicRecorder:
    """A rolling window of microphone audio, cut to order.

    Like the video ring buffer, it is always running: an event's audio
    starts before the event was confirmed, so there is nothing to switch on
    when one fires. `history_sec` is sized from the longest clip that can be
    asked for; at 44.1kHz mono that is ~88KB/s, so a couple of minutes of
    history costs a few megabytes.
    """

    def __init__(self, device=None, sample_rate: int = 44100, channels: int = 1,
                 history_sec: float = 90.0, block_sec: float = 0.1):
        self.device = device
        self.sample_rate = int(sample_rate)
        self.channels = max(1, int(channels))
        self.history_sec = float(history_sec)
        self.block_sec = block_sec
        self.error = None

        self._stream = None
        self._lock = threading.Lock()
        self._blocks = deque(maxlen=max(1, int(self.history_sec / block_sec)))

    @property
    def running(self) -> bool:
        return self._stream is not None

    def ensure_history(self, seconds: float) -> None:
        """Grow the buffer to hold at least `seconds` of audio.

        The longest clip is a dashboard setting and can change while the
        agent runs, so the buffer is sized from it rather than once at
        startup. It only ever grows: shrinking it mid-clip would throw away
        audio the clip being recorded still needs.
        """
        want = max(1, int(seconds / self.block_sec))
        with self._lock:
            if want <= (self._blocks.maxlen or 0):
                return
            self._blocks = deque(self._blocks, maxlen=want)
            self.history_sec = want * self.block_sec

    def start(self) -> bool:
        """Open the input stream. False (with `error` set) if it can't be."""
        if self._stream is not None:
            return True
        try:
            import sounddevice as sd
        except Exception as e:
            self.error = (f"sounddevice not available ({e}); clips will be "
                          "silent. Install it with: pip install sounddevice")
            print(f"[audio] {self.error}", file=sys.stderr)
            return False

        try:
            stream = sd.RawInputStream(
                samplerate=self.sample_rate, channels=self.channels,
                dtype="int16", device=self.device,
                blocksize=int(self.sample_rate * self.block_sec),
                callback=self._on_audio,
            )
            stream.start()
        except Exception as e:
            self.error = f"could not open microphone ({e}); clips will be silent"
            print(f"[audio] {self.error}", file=sys.stderr)
            return False

        self._stream = stream
        self.error = None
        print(f"[audio] recording from {self.device if self.device is not None else 'default input'} "
              f"at {self.sample_rate}Hz, {self.channels}ch")
        return True

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass

    def _on_audio(self, indata, frames, time_info, status) -> None:
        # PortAudio calls this on its own high-priority thread: copy the
        # buffer (it gets reused) and get out. No I/O, no long-held lock.
        with self._lock:
            self._blocks.append((time.time(), bytes(indata)))

    def write_segment(self, path, t0: float, t1: float) -> bool:
        """Write [t0, t1) to `path` as a WAV. False if there was nothing."""
        if t1 <= t0:
            return False
        with self._lock:
            blocks = list(self._blocks)
        if not blocks:
            return False
        pcm = slice_pcm(blocks, t0, t1, self.sample_rate, self.channels)
        if not pcm:
            return False
        write_wav(path, pcm, self.sample_rate, self.channels)
        return True


def from_config(config: dict) -> MicRecorder | None:
    """Build the mic from the `audio` section, or None if it's turned off."""
    cfg = config.get("audio") or {}
    if not cfg.get("enabled", False):
        return None

    video = config.get("alert", {}).get("video", {}) or {}
    longest = (float(video.get("max_clip_sec", 60))
               + float(video.get("pre_roll_sec", 4))
               + float(video.get("post_roll_sec", 3)))
    return MicRecorder(
        device=cfg.get("device"),
        sample_rate=cfg.get("sample_rate", 44100),
        channels=cfg.get("channels", 1),
        # Headroom over the longest clip, so a slow finish can't drop the
        # head of the audio that is about to be cut out of the buffer. The
        # recorder grows this again if the dashboard asks for longer clips.
        history_sec=longest + HISTORY_SLACK_SEC,
    )


def add_audio(video_path: str, wav_path: str, fps: float | None = None) -> bool:
    """Mux `wav_path` into `video_path` in place. False if it couldn't be.

    In place, via a temp file and a rename, so the clip the rest of the
    program knows about -- the event's video, the upload, the Telegram
    message -- keeps the same name whether or not the audio made it.
    """
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        print("[audio] ffmpeg not found on PATH; clip stays silent. Install "
              "ffmpeg (or point DFC_FFMPEG at it) to get sound in clips.",
              file=sys.stderr)
        return False

    video = Path(video_path)
    tmp = video.with_name(video.stem + ".withaudio" + video.suffix)
    try:
        r = subprocess.run(mux_command(ffmpeg, video_path, wav_path, str(tmp), fps),
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            print(f"[audio] ffmpeg failed ({r.returncode}): "
                  f"{(r.stderr or '').strip()[:300]}", file=sys.stderr)
            return False
        os.replace(tmp, video)
        return True
    except subprocess.TimeoutExpired:
        print("[audio] ffmpeg timed out; clip stays silent", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[audio] mux failed: {e}", file=sys.stderr)
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _list_devices() -> int:
    """`python -m audio`: the input devices, and whether ffmpeg is there.

    Both halves of the feature fail quietly at runtime (a silent clip looks
    like any other clip), so there has to be one command that says plainly
    which half is missing and what to put in `audio.device`.
    """
    ff = ffmpeg_path()
    print(f"ffmpeg: {ff or 'NOT FOUND -- clips will stay silent'}")
    try:
        import sounddevice as sd
    except Exception as e:
        print(f"sounddevice: NOT AVAILABLE ({e})")
        print("Install it with: pip install sounddevice")
        return 1

    try:
        default_in = sd.default.device[0]
        devices = sd.query_devices()
    except Exception as e:
        print(f"sounddevice: could not query devices ({e})")
        return 1

    print("\nInput devices (use the index or the name as audio.device):")
    found = False
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) < 1:
            continue
        found = True
        mark = " <- default" if i == default_in else ""
        print(f"  [{i}] {d['name']}  ({d['max_input_channels']}ch, "
              f"{int(d['default_samplerate'])}Hz){mark}")
    if not found:
        print("  (none -- no microphone on this machine)")
    return 0 if (found and ff) else 1


if __name__ == "__main__":
    raise SystemExit(_list_devices())
