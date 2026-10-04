"""Cutting microphone audio to a clip's window, and the mux invocation.

No microphone and no ffmpeg involved: slice_pcm is where A/V sync is won or
lost, so it is tested against synthesized blocks whose every sample says
which block and offset it came from.
"""
import io
import sys
import wave
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audio

RATE = 1000       # 1 sample per millisecond keeps the arithmetic readable
BLOCK = 0.1       # 100 frames per block, like the real block_sec


def _block(end, value, n=int(RATE * BLOCK), channels=1):
    """One block of constant-valued int16 frames, ending at time `end`."""
    return (end, array("h", [value] * (n * channels)).tobytes())


def _samples(pcm):
    return array("h", pcm).tolist()


def test_window_inside_the_buffer_is_cut_exactly():
    blocks = [_block(1.0 + i * BLOCK, i + 1) for i in range(10)]  # 1.0 .. 1.9

    pcm = audio.slice_pcm(blocks, 1.1, 1.4, RATE, 1)

    s = _samples(pcm)
    assert len(s) == 300  # 0.3s at 1kHz, to the sample
    # A block's time is when it *finished*, so the one valued i+1 covers
    # [0.9 + i*0.1, 1.0 + i*0.1): the window opens on the 3rd and the three
    # blocks must land in order, not reversed.
    assert s[:100] == [3] * 100
    assert s[100:200] == [4] * 100
    assert s[200:] == [5] * 100


def test_requesting_more_than_was_recorded_pads_instead_of_shifting():
    # Only the tail is in the buffer: the missing head has to come back as
    # silence, or the sound of the dog landing plays before it happens.
    blocks = [_block(2.0, 7), _block(2.1, 8)]

    pcm = audio.slice_pcm(blocks, 1.5, 2.1, RATE, 1)

    s = _samples(pcm)
    assert len(s) == 600
    assert s[:400] == [0] * 400      # 1.5 .. 1.9 was never recorded
    assert s[400:500] == [7] * 100
    assert s[500:] == [8] * 100


def test_a_dropped_block_leaves_a_hole_of_the_right_length():
    blocks = [_block(1.1, 1), _block(1.3, 3)]  # the 1.1-1.2 block never arrived

    s = _samples(audio.slice_pcm(blocks, 1.0, 1.3, RATE, 1))

    assert len(s) == 300
    assert s[:100] == [1] * 100
    assert s[100:200] == [0] * 100
    assert s[200:] == [3] * 100


def test_blocks_outside_the_window_are_ignored_and_never_overrun():
    blocks = [_block(0.5, 9), _block(5.0, 9), _block(1.1, 4)]

    s = _samples(audio.slice_pcm(blocks, 1.0, 1.1, RATE, 1))

    assert s == [4] * 100


def test_stereo_frames_are_sliced_on_frame_boundaries():
    # Interleaved L/R: a slice that cut mid-frame would swap the channels
    # for the rest of the clip.
    frames = array("h", [v for i in range(200) for v in (i, -i)]).tobytes()
    blocks = [(1.2, frames)]  # 200 frames ending at 1.2, so starting at 1.0

    s = _samples(audio.slice_pcm(blocks, 1.1, 1.2, RATE, 2))

    assert len(s) == 200  # 100 stereo frames
    assert s[0:2] == [100, -100]
    assert s[-2:] == [199, -199]


def test_empty_buffer_gives_a_silent_window_of_the_right_length():
    assert audio.slice_pcm([], 1.0, 1.25, RATE, 1) == b"\x00" * 500


def test_write_wav_round_trips_rate_and_channels(tmp_path):
    pcm = array("h", [100, -100, 200, -200]).tobytes()
    path = tmp_path / "seg.wav"

    audio.write_wav(path, pcm, 8000, 2)

    with wave.open(str(path), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (2, 2, 8000)
        assert w.readframes(w.getnframes()) == pcm


def test_mux_restates_the_real_frame_rate_before_the_video_input():
    cmd = audio.mux_command("ffmpeg", "clip.mp4", "clip.wav", "out.mp4", fps=11.5)

    # -r has to come before its own -i to retime the input rather than set
    # an output rate, which with -c:v copy would do nothing at all.
    assert cmd.index("-r") < cmd.index("-i")
    assert cmd[cmd.index("-r") + 1].startswith("11.5")
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert "-shortest" in cmd


def test_mux_picks_an_audio_codec_the_container_accepts():
    def acodec(out):
        cmd = audio.mux_command("ffmpeg", "in" + Path(out).suffix, "a.wav", out)
        return cmd[cmd.index("-c:a") + 1]

    # Muxing AAC into WebM would fail outright, so each container that
    # probe_codec can fall back to needs its own answer.
    assert acodec("clip.mp4") == "aac"
    assert acodec("clip.webm") == "libopus"
    assert acodec("clip.avi") == "libmp3lame"


def test_unknown_fps_leaves_the_clips_own_timestamps_alone():
    cmd = audio.mux_command("ffmpeg", "clip.mp4", "clip.wav", "out.mp4", fps=None)

    assert "-r" not in cmd


def test_from_config_is_off_unless_asked_for():
    assert audio.from_config({}) is None
    assert audio.from_config({"audio": {"enabled": False}}) is None

    mic = audio.from_config({"audio": {"enabled": True, "channels": 2,
                                       "sample_rate": 16000, "device": 3}})
    assert (mic.device, mic.sample_rate, mic.channels) == (3, 16000, 2)
    # History must outlast the longest clip it can be asked to cut.
    assert mic.history_sec > 60 + 4 + 3


def test_history_follows_the_configured_clip_length():
    mic = audio.from_config({"audio": {"enabled": True},
                             "alert": {"video": {"max_clip_sec": 300,
                                                 "pre_roll_sec": 10,
                                                 "post_roll_sec": 5}}})

    assert mic.history_sec > 315


def test_history_grows_on_demand_but_never_shrinks():
    mic = audio.MicRecorder(sample_rate=RATE, history_sec=10, block_sec=BLOCK)
    mic._blocks.extend([_block(1.0 + i * BLOCK, i + 1) for i in range(5)])

    mic.ensure_history(60)
    assert mic.history_sec >= 60
    assert len(mic._blocks) == 5  # audio already buffered survives the resize

    # Shrinking mid-clip would drop audio the clip in progress still needs.
    mic.ensure_history(1)
    assert mic.history_sec >= 60


def test_write_segment_of_an_empty_mic_writes_nothing(tmp_path):
    mic = audio.MicRecorder(sample_rate=RATE)
    path = tmp_path / "seg.wav"

    assert mic.write_segment(path, 1.0, 2.0) is False
    assert not path.exists()


def test_write_segment_writes_the_requested_window(tmp_path):
    mic = audio.MicRecorder(sample_rate=RATE, channels=1)
    mic._blocks.extend([_block(1.0 + i * BLOCK, i + 1) for i in range(5)])
    path = tmp_path / "seg.wav"

    assert mic.write_segment(path, 1.0, 1.2) is True

    with wave.open(str(path), "rb") as w:
        assert w.getnframes() == 200
        assert _samples(w.readframes(w.getnframes())) == [2] * 100 + [3] * 100


def test_write_segment_rejects_a_backwards_window(tmp_path):
    mic = audio.MicRecorder(sample_rate=RATE)
    mic._blocks.append(_block(1.0, 1))

    assert mic.write_segment(tmp_path / "seg.wav", 2.0, 1.0) is False


def test_add_audio_without_ffmpeg_leaves_the_clip_untouched(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"silent video")
    monkeypatch.setattr(audio, "ffmpeg_path", lambda: None)

    assert audio.add_audio(str(clip), str(tmp_path / "clip.wav")) is False
    assert clip.read_bytes() == b"silent video"


def test_add_audio_keeps_the_clip_when_ffmpeg_fails(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"silent video")
    monkeypatch.setattr(audio, "ffmpeg_path", lambda: "ffmpeg")

    class Result:
        returncode = 1
        stderr = "Invalid data found"
        stdout = ""

    monkeypatch.setattr(audio.subprocess, "run", lambda *a, **k: Result())

    assert audio.add_audio(str(clip), str(tmp_path / "clip.wav")) is False
    # The original clip survives, and no half-written temp file is left.
    assert clip.read_bytes() == b"silent video"
    assert list(tmp_path.iterdir()) == [clip]


def test_add_audio_replaces_the_clip_in_place_on_success(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"silent video")
    monkeypatch.setattr(audio, "ffmpeg_path", lambda: "ffmpeg")

    class Result:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"video with sound")
        return Result()

    monkeypatch.setattr(audio.subprocess, "run", fake_run)

    assert audio.add_audio(str(clip), str(tmp_path / "clip.wav"), fps=12.0) is True
    # Same name, so the event's video and the upload don't need to know.
    assert clip.read_bytes() == b"video with sound"
    assert list(tmp_path.iterdir()) == [clip]
