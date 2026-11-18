"""The build-up and payoff sounds in assets/music, synthesized here from numpy noise and oscillators (no third-party
audio; CC0 like hit/boom/whoosh). `python -m clipper.sfx [DIR]` writes them again, bit for bit (fixed seeds), each
levelled to its target loudness (the loudest 400 ms, momentary LUFS) with sample peaks at most -1 dBFS, and prints
the library.yaml lines. The recipes are in assets/music/LICENSE.txt too.

    noise-riser    3.0 s  noise swept 300 Hz -> 9 kHz (0.7-octave band) with a speeding tremolo, plus three
                          detuned saws gliding 110 -> 440 Hz; swells (k^2.2) to full level AT the end (the beat)
    reverse-swell  1.6 s  metallic noise (resonances 3.1-12 kHz) decaying like a cymbal, played backwards
    cinematic-impact 2.2 s  a 142 -> 42 Hz sine thump with its 2nd harmonic, a 180 Hz noise body, a 25 ms
                          high crack and a low noise tail, soft-clipped
    sub-drop       2.0 s  a sine gliding 95 -> 32 Hz with 2nd/3rd harmonics (so phone speakers hear it)
    braam          2.6 s  a low power chord (55, 82.4, 110 Hz saws) whose brightness closes from 3 kHz,
                          slight vibrato, soft-clipped
    heartbeat      3.2 s  four lub-dub beats at 75 BPM (52/60 Hz thumps with harmonics); loops seamlessly
"""
import re
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

SR = 44100
TARGETS = {"noise-riser": -11.0, "reverse-swell": -12.0, "cinematic-impact": -10.0, "sub-drop": -12.0, "braam": -10.0,
           "heartbeat": -18.0}
PEAK_DBFS = -1.0


def _t(sec):
    return np.arange(int(round(sec * SR))) / SR


def stft_filter(x, gain, n=2048, hop=512):
    """x shaped by gain(t, freqs) -> weights, frame by frame (Hann overlap-add)."""
    win = np.hanning(n)
    xp = np.concatenate([np.zeros(n), x, np.zeros(n)])
    out, norm = np.zeros(len(xp)), np.zeros(len(xp))
    freqs = np.fft.rfftfreq(n, 1 / SR)
    for i in range(0, len(xp) - n, hop):
        spec = np.fft.rfft(xp[i:i + n] * win) * gain((i + n / 2 - n) / SR, freqs)
        out[i:i + n] += np.fft.irfft(spec, n) * win
        norm[i:i + n] += win ** 2
    return (out / np.maximum(norm, 1e-6))[n:n + len(x)]


def _norm(x):
    return x / (np.abs(x).max() + 1e-12)


def saw(f0):
    """Band-limited saw for a gliding frequency array (additive, harmonics under 8 kHz)."""
    phase = 2 * np.pi * np.cumsum(f0) / SR
    out = np.zeros(len(f0))
    for h in range(1, 80):
        mask = (h * f0) < 8000
        if not mask.any():
            break
        out += np.where(mask, np.sin(h * phase) / h, 0.0)
    return out


def fades(x, attack=0.002, release=0.008):
    a, r = max(1, int(attack * SR)), max(1, int(release * SR))
    x = x.copy()
    x[:a] *= np.linspace(0, 1, a)
    x[-r:] *= np.linspace(1, 0, r)
    return x


def riser():
    rng = np.random.default_rng(101)
    t = _t(3.0)
    k = t / t[-1]
    fc = lambda tc: 300 * (9000 / 300) ** min(1.0, max(0.0, tc / 3.0))
    noise = stft_filter(rng.normal(size=len(t)),
                        lambda tc, f: np.exp(-0.5 * (np.log2((f + 1) / fc(tc)) / 0.7) ** 2))
    rate = 6 + 16 * k
    trem = 1 - 0.35 * (0.5 + 0.5 * np.sin(2 * np.pi * np.cumsum(rate) / SR))
    f0 = 110 * 4 ** k
    tone = sum(saw(f0 * d) for d in (1.0, 1.006, 0.994))
    x = (k ** 2.2) * (0.65 * _norm(noise) * trem + 0.45 * _norm(tone))
    return fades(x, 0.01, 0.006)


def reverse_swell():
    rng = np.random.default_rng(202)
    t = _t(1.6)
    res = (3100, 5300, 7900, 9600, 12000)
    metal = stft_filter(rng.normal(size=len(t)), lambda tc, f: 0.15 + sum(
        np.exp(-0.5 * (np.log2((f + 1) / r) / 0.08) ** 2) for r in res))
    hit = _norm(metal) * np.exp(-3.5 * t)
    hit[:int(0.003 * SR)] *= np.linspace(0, 1, int(0.003 * SR))
    return fades(hit[::-1].copy(), 0.01, 0.006)


def impact():
    rng = np.random.default_rng(303)
    t = _t(2.2)
    f = 42 + 100 * np.exp(-25 * t)
    ph = 2 * np.pi * np.cumsum(f) / SR
    sub = np.sin(ph) * np.exp(-3.0 * t) + 0.3 * np.sin(2 * ph) * np.exp(-4.0 * t)
    body = _norm(stft_filter(rng.normal(size=len(t)),
                             lambda tc, fr: np.exp(-0.5 * (np.log2((fr + 1) / 180) / 1.2) ** 2))) * np.exp(-9 * t)
    crack = _norm(stft_filter(rng.normal(size=len(t)), lambda tc, fr: (fr > 2000) * (fr < 9000) * 1.0)) \
        * np.exp(-70 * t)
    tail = _norm(stft_filter(rng.normal(size=len(t)), lambda tc, fr: 1 / (1 + (fr / 1500) ** 2))) \
        * np.exp(-2.4 * t) * 0.18
    x = np.tanh(1.6 * (0.9 * sub + 0.5 * body + 0.35 * crack + tail))
    return fades(x, 0.002, 0.05)


def sub_drop():
    t = _t(2.0)
    f = 32 + 63 * np.exp(-2.2 * t)
    ph = 2 * np.pi * np.cumsum(f) / SR
    x = (np.sin(ph) + 0.4 * np.sin(2 * ph) + 0.2 * np.sin(3 * ph)) * np.exp(-1.4 * t)
    return fades(x, 0.008, 0.05)


def braam():
    t = _t(2.6)
    vib = 1 + 0.003 * np.sin(2 * np.pi * 5 * t)
    cutoff = 400 + 2600 * np.exp(-2.5 * t)
    x = np.zeros(len(t))
    for f in (55.0, 82.41, 110.0, 110.33):
        ph = 2 * np.pi * np.cumsum(f * vib) / SR
        for h in range(1, 110):
            if h * f > 6000:
                break
            x += np.sin(h * ph) / h / (1 + (h * f / cutoff) ** 4)
    env = np.minimum(1, t / 0.035) * np.exp(-0.9 * t)
    return fades(np.tanh(1.8 * _norm(x) * env), 0.002, 0.3)


def heartbeat():
    period, beats = 0.8, 4
    t = _t(period * beats)
    x = np.zeros(len(t))
    for b in range(beats):
        for at, f0, amp, ln in ((0.0, 52, 1.0, 0.11), (0.28, 60, 0.7, 0.09)):
            s = int((b * period + at) * SR)
            tt = _t(ln + 0.25)
            ph = 2 * np.pi * np.cumsum(f0 + 18 * np.exp(-30 * tt)) / SR
            thump = (np.sin(ph) + 0.35 * np.sin(2 * ph) + 0.15 * np.sin(3 * ph)) * np.exp(-tt / (ln / 2.5))
            thump[:int(0.004 * SR)] *= np.linspace(0, 1, int(0.004 * SR))
            seg = x[s:s + len(thump)]
            seg += amp * thump[:len(seg)]
    return x  # the tail of the last beat is gone before the period ends, so it loops seamlessly


SOUNDS = {"noise-riser": riser, "reverse-swell": reverse_swell, "cinematic-impact": impact, "sub-drop": sub_drop, "braam": braam,
          "heartbeat": heartbeat}


def write_wav(path, x):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.round(np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


def peak_momentary(path):
    res = subprocess.run(["ffmpeg", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True, text=True, check=True)
    return max(float(m) for m in re.findall(r"M:\s*(-?[\d.]+)", res.stderr))


def build(name, out_dir):
    """Write one sound at its target loudness (sample peaks capped at -1 dBFS); returns (peak LUFS, seconds)."""
    x = _norm(SOUNDS[name]())
    path = Path(out_dir) / f"{name}.wav"
    write_wav(path, x * 0.5)
    gain = 10 ** ((TARGETS[name] - peak_momentary(path)) / 20) * 0.5
    gain = min(gain, 10 ** (PEAK_DBFS / 20))  # never past -1 dBFS
    write_wav(path, x * gain)
    return round(peak_momentary(path), 1), round(len(x) / SR, 3)


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "assets" / "music"
    for name in SOUNDS:
        peak, length = build(name, out)
        print(f"{name}: peak_lufs {peak}, length {length}")
