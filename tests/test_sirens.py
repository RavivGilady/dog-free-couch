"""The built-in siren pack: every entry renders to a playable WAV.

Pure synthesis, so this runs anywhere -- no audio device involved.
"""
import io
import sys
import wave
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sirens


def _samples(name):
    """Decode one siren to (wav header fields, signed 16-bit samples)."""
    with wave.open(io.BytesIO(sirens.wav_bytes(name)), "rb") as w:
        meta = (w.getnchannels(), w.getsampwidth(), w.getframerate())
        frames = w.readframes(w.getnframes())
    return meta, array("h", frames)


def _crossings(samples):
    """Zero crossings -- a cheap stand-in for pitch."""
    return sum(1 for a, b in zip(samples, samples[1:]) if (a < 0) != (b < 0))


def test_every_siren_renders_audible_mono_pcm():
    assert "builtin" in sirens.SIRENS  # the default must never disappear
    for name, siren in sirens.SIRENS.items():
        meta, s = _samples(name)
        assert meta == (1, 2, sirens.RATE), (name, meta)
        assert abs(len(s) / sirens.RATE - siren.dur) < 0.05, name
        # Loud enough to be an alarm, and never clipped into distortion.
        peak = max(max(s), -min(s))
        assert 16000 < peak <= 32767, (name, peak)
        # It must start and end near silence, or the player clicks.
        assert abs(s[0]) < 2000 and abs(s[-1]) < 2000, name


def test_sirens_sweep_in_pitch_unlike_the_two_tone_default():
    """A siren is a siren because the pitch moves.

    Measured in 20ms windows, short enough to sit inside one sweep of even
    the fastest siren here -- windows as long as a siren's own period all
    average out to the same pitch and would hide the sweep entirely.
    The narrowest spread is hi_lo, whose two notes are a fourth apart.
    """
    for name in ("wail", "yelp", "hi_lo", "whoop"):
        _, s = _samples(name)
        win = int(sirens.RATE * 0.02)
        step = (len(s) - win) // 59
        pitches = [_crossings(s[i * step:i * step + win]) for i in range(60)]
        assert max(pitches) > min(pitches) * 1.25, (name, pitches)


def test_unknown_names_fall_back_to_the_default():
    assert sirens.is_builtin("wail") and sirens.is_builtin("builtin")
    assert not sirens.is_builtin("nope")
    assert not sirens.is_builtin(7)  # a recording's id, not a siren
    assert not sirens.is_builtin(None)
    assert sirens.wav_bytes("nope") == sirens.wav_bytes(sirens.DEFAULT)
    assert sirens.wav_path("../evil") == sirens.wav_path(sirens.DEFAULT)


def test_wav_path_caches_the_rendering():
    p = Path(sirens.wav_path("yelp"))
    assert p.exists() and p.read_bytes().startswith(b"RIFF")
    assert sirens.wav_path("yelp") == str(p)


def test_catalog_describes_the_whole_pack():
    cat = sirens.catalog()
    assert [c["id"] for c in cat] == list(sirens.SIRENS)
    assert all(c["builtin"] and c["label"] and c["blurb"] for c in cat)
