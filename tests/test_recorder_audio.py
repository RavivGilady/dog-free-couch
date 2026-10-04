"""The recorder's side of clip audio: which window, and which frame rate.

No camera and no ffmpeg: the recorder is driven straight at _finalize with
a fake mic, which is where the window and the retiming are decided.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audio
import recorder


class FakeMic:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []
        self.history = 0.0

    def ensure_history(self, seconds):
        self.history = max(self.history, seconds)

    def write_segment(self, path, t0, t1):
        self.calls.append((str(path), t0, t1))
        if self.ok:
            Path(path).write_bytes(b"wav")
        return self.ok


def _recorder(tmp_path, mic, fps=15.0):
    return recorder.ClipRecorder(out_dir=str(tmp_path), fps=fps, mic=mic)


def test_the_mic_buffer_is_sized_to_outlast_the_longest_clip(tmp_path):
    mic = FakeMic()
    r = recorder.ClipRecorder(out_dir=str(tmp_path), fps=15, pre_roll_sec=4,
                              max_clip_sec=60, post_roll_sec=3, mic=mic)
    assert mic.history > 67

    # A longer clip set in the dashboard has to grow it too, or the start of
    # the audio falls out of the buffer before the clip is finished.
    r.max_clip_sec = 600
    r.size_audio_history()
    assert mic.history > 607


def test_audio_window_covers_the_frames_and_the_last_frames_time_on_screen(
        tmp_path, monkeypatch):
    mic = FakeMic()
    muxed = []
    monkeypatch.setattr(audio, "add_audio",
                        lambda path, wav, fps=None: muxed.append((path, wav, fps)) or True)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video")

    # 100 frames spanning 10s: 9.9s of intervals at 0.1s each.
    _recorder(tmp_path, mic)._mux_audio(str(clip), 100, (1000.0, 1009.9))

    (_, t0, t1) = mic.calls[0]
    assert t0 == 1000.0
    # The last frame is still shown for one interval, so the audio runs past
    # its timestamp -- otherwise the sound ends a frame early every clip.
    assert abs(t1 - 1010.0) < 1e-6
    # And the rate handed to the muxer is the one the camera managed (10),
    # not the 15 the writer was opened with.
    assert abs(muxed[0][2] - 10.0) < 1e-6


def test_the_temporary_wav_is_cleaned_up(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "add_audio", lambda *a, **k: True)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video")

    _recorder(tmp_path, FakeMic())._mux_audio(str(clip), 30, (5.0, 7.0))

    assert list(tmp_path.iterdir()) == [clip]


def test_a_stuttering_feed_is_left_silent_rather_than_retimed(tmp_path, monkeypatch):
    mic = FakeMic()
    monkeypatch.setattr(audio, "add_audio",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("muxed")))
    r = _recorder(tmp_path, mic)

    r._mux_audio("clip.mp4", 2, (1000.0, 1100.0))   # 0.01 fps: a stalled feed
    r._mux_audio("clip.mp4", 1, (1000.0, 1000.1))   # one frame: no interval
    r._mux_audio("clip.mp4", 30, (1000.0, 1000.0))  # no elapsed time at all
    r._mux_audio("clip.mp4", 30, (None, None))      # a clip that never started

    assert mic.calls == []


def test_nothing_recorded_means_no_mux_attempt(tmp_path, monkeypatch):
    mic = FakeMic(ok=False)
    monkeypatch.setattr(audio, "add_audio",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("muxed")))

    _recorder(tmp_path, mic)._mux_audio("clip.mp4", 30, (5.0, 7.0))

    assert len(mic.calls) == 1


def test_the_clip_is_still_announced_when_adding_audio_blows_up(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no ffmpeg today")

    monkeypatch.setattr(recorder.ClipRecorder, "_mux_audio", boom)
    done = []

    _recorder(tmp_path, FakeMic())._finalize("clip.mp4", 42, (5.0, 7.0),
                                            lambda p, n: done.append((p, n)))

    assert done == [("clip.mp4", 42)]


def test_without_a_mic_the_callback_is_all_that_happens(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder.ClipRecorder, "_mux_audio",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("muxed")))
    done = []

    _recorder(tmp_path, None)._finalize("clip.mp4", 42, (5.0, 7.0),
                                        lambda p, n: done.append(p))

    assert done == ["clip.mp4"]


def test_shutdown_waits_for_the_sound_to_be_added(tmp_path, monkeypatch):
    import threading

    started = threading.Event()
    release = threading.Event()
    done = []

    def slow_mux(self, path, n, span):
        started.set()
        release.wait(5)
        done.append(path)

    monkeypatch.setattr(recorder.ClipRecorder, "_mux_audio", slow_mux)
    r = _recorder(tmp_path, FakeMic())
    r._finalizing = threading.Thread(
        target=r._finalize, args=("clip.mp4", 30, (5.0, 7.0), None), daemon=True)
    r._finalizing.start()
    assert started.wait(5)

    # Ctrl+C lands here: the wait has to outlive the mux, not race it.
    release.set()
    r.wait_for_finalize(timeout=5)

    assert done == ["clip.mp4"]
