"""Procedural sound effects (whooshes, impacts, risers, ticks) synthesised with numpy.

Everything a bumper needs is generated here, so the project ships no audio files and there is nothing to
license. All functions return float32 stereo arrays shaped (n, 2) at :data:`SR` Hz, deterministic for a
given seed. :class:`Mix` places them on a timeline and writes a WAV.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SR = 48_000


def _t(seconds: float) -> np.ndarray:
    return np.arange(int(seconds * SR), dtype=np.float32) / SR


def _stereo(mono: np.ndarray, pan: float | np.ndarray = 0.0) -> np.ndarray:
    """Equal-power pan: -1 = left, +1 = right. ``pan`` may be an array to move the sound."""
    p = (np.clip(pan, -1, 1) + 1) * (np.pi / 4)
    return np.stack([mono * np.cos(p), mono * np.sin(p)], axis=1).astype(np.float32)


def _env(n: int, attack: float, release: float, curve: float = 2.0) -> np.ndarray:
    """Attack/release envelope over ``n`` samples; ``attack`` is the fraction of the length spent rising."""
    a = max(1, int(n * attack))
    e = np.ones(n, dtype=np.float32)
    e[:a] = np.linspace(0, 1, a, dtype=np.float32) ** (1 / curve)
    r = max(1, int(n * release))
    e[-r:] *= np.linspace(1, 0, r, dtype=np.float32) ** curve
    return e


def _bandpass_sweep(noise: np.ndarray, f0: float, f1: float, q: float = 2.5) -> np.ndarray:
    """State-variable band-pass whose centre glides from f0 to f1 (the classic whoosh filter)."""
    n = len(noise)
    freq = np.geomspace(f0, f1, n)
    f = 2 * np.sin(np.pi * np.minimum(freq, SR / 6.5) / SR)
    damp = 1.0 / q
    out = np.empty(n, dtype=np.float32)
    low = band = 0.0
    for i in range(n):  # ~150k iterations for a 3 s bumper: fine
        low += f[i] * band
        high = noise[i] - low - damp * band
        band += f[i] * high
        out[i] = band
    return out


def _reverb(x: np.ndarray, seconds: float = 0.9, mix: float = 0.25, seed: int = 7) -> np.ndarray:
    """Cheap convolution reverb with an exponentially decaying noise impulse."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    ir = rng.standard_normal((n, 2)).astype(np.float32) * np.exp(-np.linspace(0, 7, n, dtype=np.float32))[:, None]
    ir /= np.abs(ir).sum(axis=0, keepdims=True) / 6
    size = 1 << int(np.ceil(np.log2(len(x) + n)))
    wet = np.stack([np.fft.irfft(np.fft.rfft(x[:, c], size) * np.fft.rfft(ir[:, c], size), size)[: len(x) + n]
                    for c in range(2)], axis=1).astype(np.float32)
    dry = np.zeros_like(wet)
    dry[: len(x)] = x
    return dry + wet * mix


def whoosh(seconds: float = 0.45, *, low: float = 300, high: float = 4200, pan_from: float = -0.8,
           pan_to: float = 0.8, seed: int = 1, rising: bool = True) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    noise = rng.standard_normal(n).astype(np.float32)
    f0, f1 = (low, high) if rising else (high, low)
    body = _bandpass_sweep(noise, f0, f1, q=3.0) * _env(n, 0.55, 0.45, 2.2)
    air = _bandpass_sweep(noise, f0 * 3, min(f1 * 3, 14000), q=1.2) * _env(n, 0.6, 0.4) * 0.35
    return _stereo(body + air, np.linspace(pan_from, pan_to, n)) * 0.9


def impact(*, weight: float = 1.0, seed: int = 2, metal: bool = True) -> np.ndarray:
    """Sub drop + transient + (optional) metallic ring, with a short room tail. Peak at t = 0."""
    rng = np.random.default_rng(seed)
    t = _t(1.1)
    sub_f = 38 + 70 * np.exp(-t * 14)
    sub = np.sin(2 * np.pi * np.cumsum(sub_f) / SR) * np.exp(-t * 4.2) * 1.0
    thump = np.sin(2 * np.pi * np.cumsum(120 + 260 * np.exp(-t * 60)) / SR) * np.exp(-t * 22) * 0.8
    click = rng.standard_normal(len(t)).astype(np.float32) * np.exp(-t * 160) * 0.55
    crunch = _bandpass_sweep(rng.standard_normal(len(t)).astype(np.float32), 2600, 500, q=1.4) * np.exp(-t * 16) * 0.45
    sig = sub + thump + click + crunch
    if metal:
        ring = sum(np.sin(2 * np.pi * f * t + rng.uniform(0, 6.28)) * np.exp(-t * d) * a
                   for f, d, a in ((1830, 9, 0.16), (2740, 12, 0.12), (4120, 16, 0.09), (5630, 22, 0.06),
                                   (967, 7, 0.10)))
        sig = sig + ring
    out = _reverb(_stereo(sig.astype(np.float32) * weight), seconds=1.0, mix=0.22, seed=seed)
    return np.tanh(out * 1.6) * 0.85  # soft clip: denser, louder, no digital overs


def riser(seconds: float = 0.9, *, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = _t(seconds)[:n]
    grow = (t / seconds) ** 2.4
    noise = _bandpass_sweep(rng.standard_normal(n).astype(np.float32), 180, 6500, q=2.0) * grow
    tone = np.sin(2 * np.pi * np.cumsum(np.geomspace(55, 220, n)) / SR) * grow * 0.5
    trem = 1 + 0.35 * np.sin(2 * np.pi * np.cumsum(np.linspace(6, 22, n)) / SR)
    return _stereo(((noise + tone) * trem).astype(np.float32), np.sin(t * 9) * 0.3) * 0.6


def tick(*, freq: float = 2400, seed: int = 4, pan: float = 0.0) -> np.ndarray:
    """Short bright ping, used when an accent lights up."""
    t = _t(0.28)
    sig = (np.sin(2 * np.pi * freq * t) + 0.5 * np.sin(2 * np.pi * freq * 1.5 * t)) * np.exp(-t * 26)
    rng = np.random.default_rng(seed)
    sig += rng.standard_normal(len(t)).astype(np.float32) * np.exp(-t * 220) * 0.4
    return _reverb(_stereo(sig.astype(np.float32) * 0.35, pan), seconds=0.5, mix=0.3, seed=seed)


def sweep_down(seconds: float = 0.5, *, seed: int = 5) -> np.ndarray:
    """Falling whoosh + sub dip: the "cut to footage" / "cut to black" sound."""
    w = whoosh(seconds, low=250, high=5200, pan_from=0.5, pan_to=-0.5, seed=seed, rising=False)
    t = _t(seconds)[: len(w)]
    sub = np.sin(2 * np.pi * np.cumsum(np.geomspace(110, 36, len(t))) / SR) * _env(len(t), 0.1, 0.7) * 0.5
    return w + _stereo(sub.astype(np.float32))


def drone(seconds: float, *, root: float = 55.0, seed: int = 6) -> np.ndarray:
    """Quiet low pad that glues the effects together."""
    t = _t(seconds)
    rng = np.random.default_rng(seed)
    sig = sum(np.sin(2 * np.pi * root * m * t + rng.uniform(0, 6.28)) * a
              for m, a in ((1, 0.5), (1.5, 0.22), (2, 0.18), (3.01, 0.07)))
    sig = sig * (1 + 0.15 * np.sin(2 * np.pi * 0.7 * t)) * _env(len(t), 0.25, 0.35)
    return _stereo(sig.astype(np.float32) * 0.28)


class Mix:
    """A stereo timeline. ``add(sound, at_s, gain)`` then ``write(path)``."""

    def __init__(self, seconds: float):
        self.buf = np.zeros((int(seconds * SR) + SR // 2, 2), dtype=np.float32)
        self.seconds = seconds

    def add(self, sound: np.ndarray, at_s: float, gain: float = 1.0) -> None:
        i = int(at_s * SR)
        if i < 0:
            sound, i = sound[-i:], 0
        n = min(len(sound), len(self.buf) - i)
        if n > 0:
            self.buf[i:i + n] += sound[:n] * gain

    def render(self, peak_db: float = -1.5) -> np.ndarray:
        out = self.buf[: int(self.seconds * SR)].copy()
        fade = min(len(out), int(0.03 * SR))
        out[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)[:, None]
        peak = float(np.abs(out).max()) or 1.0
        return out * (10 ** (peak_db / 20) / peak)

    def write(self, path: Path, peak_db: float = -1.5) -> Path:
        write_wav(path, self.render(peak_db))
        return path


def write_wav(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
