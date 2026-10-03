"""
The built-in alert sounds: a small pack of synthesized sirens.

Synthesized rather than shipped as audio files, for the same reason
alert.py never calls MessageBeep(): a generated waveform is loud, identical
on every machine, needs no licence and adds nothing to the repo or to what
the agent has to download. Each siren renders once to a .wav in the temp
dir and is then played straight off disk by winsound/aplay.

A device's alert.active_sound holds either one of these ids or the numeric
id of a sound recorded in the dashboard. "builtin" is the original two-tone
square-wave alarm and stays the default, so settings saved before this pack
existed keep working.
"""
from __future__ import annotations

import io
import math
import tempfile
import wave
from array import array
from pathlib import Path

RATE = 44100
DEFAULT = "builtin"

# Click-free edges: ~5ms of fade at the start and end of every rendering.
_FADE = int(RATE * 0.005)


# --------------------------------------------------------------------------
# oscillators -- phase is a 0..1 ramp, so sweeping the frequency never
# produces a discontinuity the way sin(2*pi*f*t) with a varying f would.
# --------------------------------------------------------------------------

def _square(phase: float) -> float:
    return 1.0 if phase < 0.5 else -1.0


def _saw(phase: float) -> float:
    return 2.0 * phase - 1.0


# --------------------------------------------------------------------------
# frequency envelopes: seconds -> Hz
# --------------------------------------------------------------------------

def _triangle(t: float, period: float, lo: float, hi: float) -> float:
    """Up and back down once per period -- the classic wail."""
    x = (t % period) / period
    return lo + (hi - lo) * (2 * x if x < 0.5 else 2 * (1 - x))


def _ramp_up(t: float, period: float, lo: float, hi: float) -> float:
    """Rise, then snap back: the ascending 'whoop'. Exponential, because
    pitch is heard logarithmically and a linear sweep sags at the top."""
    return lo * (hi / lo) ** ((t % period) / period)


def _step(t: float, period: float, lo: float, hi: float) -> float:
    """Alternating steady notes -- a European hi-lo horn."""
    return hi if (t % period) < period / 2 else lo


# --------------------------------------------------------------------------
# the pack
# --------------------------------------------------------------------------

class Siren:
    """One built-in sound: an oscillator driven by a frequency envelope."""

    def __init__(self, label, blurb, dur, shape, envelope,
                 period=1.0, lo=440.0, hi=1200.0, volume=0.85, gap=0.0):
        self.label = label
        self.blurb = blurb
        self.dur = dur
        self.shape = shape
        self.envelope = envelope
        self.period = period
        self.lo = lo
        self.hi = hi
        self.volume = volume
        # Silence after each envelope period, for sounds that pulse.
        self.gap = gap

    def render(self) -> bytes:
        """Build the PCM samples. Pure and deterministic -- no I/O."""
        amp = 32767 * max(0.0, min(1.0, self.volume))
        n = int(RATE * self.dur)
        buf = array("h", bytes(2 * n))
        phase = 0.0
        for i in range(n):
            t = i / RATE
            if self.gap and (t % (self.period + self.gap)) >= self.period:
                phase = 0.0  # resting between pulses
                continue
            f = self.envelope(t, self.period, self.lo, self.hi)
            phase = (phase + f / RATE) % 1.0
            fade = min(1.0, (i + 1) / _FADE, (n - i) / _FADE)
            buf[i] = int(self.shape(phase) * amp * fade)
        return buf.tobytes()


class TwoTone(Siren):
    """The original alarm: a run of alternating square-wave beeps.

    Kept exactly as it sounded before the pack existed -- it is the default,
    and an alarm that changes pitch after an update is a support question.
    """

    def __init__(self, label, blurb, beeps=3, freq=1400.0, beep_ms=160,
                 gap_ms=90, volume=0.85):
        dur = (beeps * beep_ms + (beeps - 1) * gap_ms) / 1000.0
        super().__init__(label, blurb, dur, _square, _step, volume=volume)
        self.beeps, self.freq = beeps, freq
        self.beep_ms, self.gap_ms = beep_ms, gap_ms

    def render(self) -> bytes:
        amp = int(32767 * max(0.0, min(1.0, self.volume)))
        buf = array("h")

        def tone(f, ms):
            n = int(RATE * ms / 1000)
            for i in range(n):
                v = amp if math.sin(2 * math.pi * f * i / RATE) >= 0 else -amp
                fade = min(1.0, i / 200.0, (n - i) / 200.0)
                buf.append(int(v * fade))

        for i in range(self.beeps):
            # Alternate two pitches -- a warble carries better than one note.
            tone(self.freq if i % 2 == 0 else self.freq * 0.75, self.beep_ms)
            if i < self.beeps - 1:
                buf.extend(array("h", bytes(2 * int(RATE * self.gap_ms / 1000))))
        return buf.tobytes()


SIRENS: dict[str, Siren] = {
    "builtin": TwoTone(
        "Built-in alarm", "Three piercing square-wave beeps. The original."),
    "wail": Siren(
        "Police wail", "Slow rise and fall, like a patrol car holding station.",
        dur=3.0, shape=_saw, envelope=_triangle, period=1.5, lo=520.0, hi=1500.0),
    "yelp": Siren(
        "Police yelp", "The same sweep five times faster -- urgent, hard to ignore.",
        dur=2.4, shape=_saw, envelope=_triangle, period=0.3, lo=520.0, hi=1500.0),
    "hi_lo": Siren(
        "Hi-lo horn", "Two alternating notes, the European ambulance horn.",
        dur=2.8, shape=_square, envelope=_step, period=0.9, lo=680.0, hi=910.0,
        volume=0.75),
    "whoop": Siren(
        "Whoop", "Short rising sweeps with a beat of silence between them.",
        dur=2.6, shape=_square, envelope=_ramp_up, period=0.28, lo=400.0,
        hi=1600.0, gap=0.12, volume=0.8),
}


def is_builtin(name) -> bool:
    return isinstance(name, str) and name in SIRENS


def catalog() -> list[dict]:
    """What the dashboard lists in its sound library."""
    return [{"id": sid, "label": s.label, "blurb": s.blurb,
             "duration_sec": round(s.dur, 2), "builtin": True}
            for sid, s in SIRENS.items()]


def wav_bytes(name: str = DEFAULT) -> bytes:
    """A complete .wav file for one siren; an unknown name gives the default."""
    siren = SIRENS.get(name) or SIRENS[DEFAULT]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(siren.render())
    return buf.getvalue()


def wav_path(name: str = DEFAULT) -> str:
    """Render to a cached file and return its path. Rendering runs a Python
    loop per sample, so it happens once per siren and not per alert."""
    if name not in SIRENS:
        name = DEFAULT
    dest = Path(tempfile.gettempdir()) / f"dog_couch_siren_{name}.wav"
    if not dest.exists() or dest.stat().st_size == 0:
        dest.write_bytes(wav_bytes(name))
    return str(dest)


if __name__ == "__main__":
    # `python -m sirens [name ...]` -- play the pack, or just the named ones.
    import sys
    import time

    from alert import play_alert_sound

    for sid in (sys.argv[1:] or list(SIRENS)):
        siren = SIRENS.get(sid)
        if siren is None:
            print(f"no such siren: {sid} (have: {', '.join(SIRENS)})")
            continue
        print(f"{sid:9} {siren.label} -- {siren.blurb}")
        play_alert_sound(builtin=sid)
        time.sleep(siren.dur + 0.4)
