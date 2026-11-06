"""
Listen to system audio for a reference sound (an audio "fingerprint") and
press a key (spacebar by default) when it is heard.

How it works
------------
* The reference WAV is turned into a log-band spectrogram (frames x bands).
* Live audio is captured from the default output device via WASAPI loopback
  (what you hear), or from a microphone with --mic.
* Every new spectrogram frame, the most recent N live frames are compared to
  the reference with a Pearson correlation. A score near 1.0 means a match.
* When the score passes --threshold, the key is sent with SendInput using a
  hardware scan code (works in games that ignore virtual-key events).

Usage
-----
    python fish_listener.py                      # uses lure-catch.wav
    python fish_listener.py --ref other.wav --threshold 0.6 --verbose
    python fish_listener.py --record new.wav --seconds 4   # capture a reference
    python fish_listener.py --list-devices

Press Ctrl+C to stop.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import random
import sys
import time
from collections import deque
from math import gcd

import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly

try:
    import pyaudiowpatch as pyaudio
except ImportError:  # pragma: no cover
    pyaudio = None

# ----------------------------------------------------------------------------
# Feature extraction
# ----------------------------------------------------------------------------

N_FFT = 1024
N_BANDS = 48
F_MIN = 150.0
F_MAX = 10000.0


def hop_for(rate: int) -> int:
    # ~11.6 ms hop at 44.1 kHz, scaled so timing is rate independent.
    return max(128, int(round(rate * 512 / 44100)))


class FeatureExtractor:
    """Streaming log-band spectrogram. Feed samples, get frames."""

    def __init__(self, rate: int):
        self.rate = rate
        self.hop = hop_for(rate)
        self.window = np.hanning(N_FFT).astype(np.float32)
        freqs = np.fft.rfftfreq(N_FFT, 1.0 / rate)
        edges = np.geomspace(F_MIN, min(F_MAX, rate / 2 - 1), N_BANDS + 1)
        # Band membership matrix (bins x bands), log-spaced rectangular bands.
        self.fb = np.zeros((len(freqs), N_BANDS), dtype=np.float32)
        for b in range(N_BANDS):
            sel = (freqs >= edges[b]) & (freqs < edges[b + 1])
            if not sel.any():  # very narrow low band: take nearest bin
                sel[np.argmin(np.abs(freqs - (edges[b] + edges[b + 1]) / 2))] = True
            self.fb[sel, b] = 1.0 / sel.sum()
        self.buf = np.zeros(0, dtype=np.float32)

    def push(self, samples: np.ndarray) -> np.ndarray:
        """Append mono float samples; return (n_frames, N_BANDS) new frames."""
        self.buf = np.concatenate([self.buf, samples.astype(np.float32)])
        n = 0 if len(self.buf) < N_FFT else 1 + (len(self.buf) - N_FFT) // self.hop
        if n == 0:
            return np.zeros((0, N_BANDS), dtype=np.float32)
        idx = np.arange(N_FFT)[None, :] + self.hop * np.arange(n)[:, None]
        frames = self.buf[idx] * self.window
        self.buf = self.buf[n * self.hop:]
        mag = np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)
        bands = mag @ self.fb
        return np.log10(bands + 1e-6)


def to_mono_float(data: np.ndarray) -> np.ndarray:
    if data.dtype == np.int16:
        data = data.astype(np.float32) / 32768.0
    elif data.dtype == np.int32:
        data = data.astype(np.float32) / 2147483648.0
    elif data.dtype == np.uint8:
        data = (data.astype(np.float32) - 128.0) / 128.0
    else:
        data = data.astype(np.float32)
    if data.ndim == 2:
        data = data.mean(axis=1)
    return data


def load_reference(path: str, rate: int, max_seconds: float, trim_db: float) -> np.ndarray:
    """Load WAV, resample to `rate`, trim silence, return feature matrix."""
    src_rate, data = wavfile.read(path)
    audio = to_mono_float(data)
    if src_rate != rate:
        g = gcd(src_rate, rate)
        audio = resample_poly(audio, rate // g, src_rate // g).astype(np.float32)

    # Trim leading/trailing quiet parts (relative to the loudest moment).
    hop = hop_for(rate)
    n = max(1, len(audio) // hop)
    rms = np.sqrt(np.mean(audio[: n * hop].reshape(n, hop) ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms / rms.max())
    loud = np.where(db > -abs(trim_db))[0]
    if len(loud):
        audio = audio[loud[0] * hop: (loud[-1] + 1) * hop]
    if max_seconds > 0:
        audio = audio[: int(max_seconds * rate)]

    feats = FeatureExtractor(rate).push(audio)
    if len(feats) < 5:
        sys.exit(f"Reference '{path}' is too short after trimming.")
    return feats


class Matcher:
    """Sliding Pearson correlation between live frames and the reference."""

    def __init__(self, ref: np.ndarray, floor_pct: float = 40.0):
        self.T = len(ref)
        # Values below this floor (quiet bands in the reference) are clamped in
        # both signals, so background noise in those bands is ignored.
        self.floor = float(np.percentile(ref, floor_pct))
        ref = np.maximum(ref, self.floor)
        r = ref - ref.mean()
        self.ref = (r / (np.linalg.norm(r) + 1e-9)).ravel()
        self.live: deque[np.ndarray] = deque(maxlen=self.T)

    def push(self, frame: np.ndarray) -> float:
        self.live.append(np.maximum(frame, self.floor))
        if len(self.live) < self.T:
            return 0.0
        x = np.stack(self.live)
        x = x - x.mean()
        norm = np.linalg.norm(x)
        if norm < 1e-6:
            return 0.0
        return float(self.ref @ (x.ravel() / norm))


# ----------------------------------------------------------------------------
# Key sending (SendInput with scan codes)
# ----------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

SCANCODES = {
    "space": 0x39, "enter": 0x1C, "tab": 0x0F, "esc": 0x01,
    **{str(i): 0x01 + i for i in range(1, 10)}, "0": 0x0B,
    **{c: sc for c, sc in zip("qwertyuiop", range(0x10, 0x1A))},
    **{c: sc for c, sc in zip("asdfghjkl", range(0x1E, 0x27))},
    **{c: sc for c, sc in zip("zxcvbnm", range(0x2C, 0x33))},
    **{f"f{i}": 0x3A + i for i in range(1, 11)},
}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


def _send_scan(scan: int, up: bool) -> None:
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    inp = INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(0, scan, flags, 0, None)))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def press_key(name: str) -> None:
    scan = SCANCODES[name]
    _send_scan(scan, up=False)
    time.sleep(random.uniform(0.04, 0.09))
    _send_scan(scan, up=True)


def foreground_title() -> str:
    hwnd = user32.GetForegroundWindow()
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


# ----------------------------------------------------------------------------
# Audio capture
# ----------------------------------------------------------------------------

def require_pyaudio():
    if pyaudio is None:
        sys.exit("Missing dependency: pip install -r requirements.txt")


def list_devices() -> None:
    require_pyaudio()
    p = pyaudio.PyAudio()
    try:
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["maxInputChannels"] > 0:
                tag = " [loopback]" if d.get("isLoopbackDevice") and "[Loopback]" not in d["name"] else ""
                print(f"{i:3d}  {d['name']}{tag}  ({int(d['defaultSampleRate'])} Hz, "
                      f"{d['maxInputChannels']} ch)")
    finally:
        p.terminate()


def open_stream(p, device: int | None, mic: bool):
    if device is not None:
        info = p.get_device_info_by_index(device)
    elif mic:
        info = p.get_default_input_device_info()
    else:
        info = p.get_default_wasapi_loopback()
    rate = int(info["defaultSampleRate"])
    channels = max(1, min(2, int(info["maxInputChannels"])))
    chunk = hop_for(rate) * 4
    stream = p.open(format=pyaudio.paFloat32, channels=channels, rate=rate,
                    input=True, input_device_index=info["index"],
                    frames_per_buffer=chunk)
    return stream, info, rate, channels, chunk


def read_chunk(stream, chunk: int, channels: int) -> np.ndarray:
    raw = stream.read(chunk, exception_on_overflow=False)
    data = np.frombuffer(raw, dtype=np.float32)
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data


def record(path: str, seconds: float, device: int | None, mic: bool) -> None:
    require_pyaudio()
    p = pyaudio.PyAudio()
    try:
        stream, info, rate, ch, chunk = open_stream(p, device, mic)
        print(f"Recording {seconds:.1f}s from: {info['name']}  ({rate} Hz)")
        parts, need = [], int(seconds * rate)
        while sum(len(x) for x in parts) < need:
            parts.append(read_chunk(stream, chunk, ch))
        stream.stop_stream()
        stream.close()
    finally:
        p.terminate()
    audio = np.concatenate(parts)[:need]
    wavfile.write(path, rate, (np.clip(audio, -1, 1) * 32767).astype(np.int16))
    print(f"Saved {path}. Silence is trimmed automatically when it is used as --ref.")


def listen(args) -> None:
    require_pyaudio()
    key = args.key.lower()
    if key not in SCANCODES:
        sys.exit(f"Unknown key '{args.key}'. Options: {', '.join(sorted(SCANCODES))}")

    p = pyaudio.PyAudio()
    try:
        stream, info, rate, ch, chunk = open_stream(p, args.device, args.mic)
        ref = load_reference(args.ref, rate, args.ref_seconds, args.trim_db)
        matcher = Matcher(ref)
        feats = FeatureExtractor(rate)
        frame_s = feats.hop / rate

        print(f"Listening on: {info['name']}  ({rate} Hz, {ch} ch)")
        print(f"Reference: {args.ref}  ({len(ref)} frames, {len(ref) * frame_s:.2f}s)")
        print(f"Threshold {args.threshold:.2f}, cooldown {args.cooldown:.1f}s, key '{key}'"
              + (f", only when window contains '{args.window}'" if args.window else ""))
        print("Ctrl+C to stop.\n")

        def window_ok(what: str) -> bool:
            title = foreground_title()
            if args.window and args.window.lower() not in title.lower():
                print(f"\n{what} but foreground is '{title}', skipped.")
                return False
            return True

        def send(label: str) -> None:
            if not args.dry_run:
                press_key(key)
            print(f"\n[{time.strftime('%H:%M:%S')}] {label}"
                  f"  -> {'(dry run)' if args.dry_run else key}")

        def miss_wait() -> float:
            return args.timeout + random.uniform(0.0, args.timeout_jitter)

        last_fire = 0.0
        last_action = time.time()  # last cast of either type (or start)
        miss_after = miss_wait()   # re-rolled after every cast
        peak, peak_t = 0.0, time.time()
        hits = 0
        while True:
            for frame in feats.push(read_chunk(stream, chunk, ch)):
                score = matcher.push(frame)
                now = time.time()
                peak = max(peak, score)

                if args.verbose and now - peak_t >= 0.5:
                    bar = "#" * int(max(0.0, peak) * 40)
                    print(f"\rscore {peak:5.2f} |{bar:<40}|", end="", flush=True)
                    peak, peak_t = 0.0, now

                if score >= args.threshold and now - last_fire >= args.cooldown:
                    last_fire = now
                    if not window_ok(f"Match ({score:.2f})"):
                        continue
                    if args.delay_max > 0:
                        time.sleep(random.uniform(args.delay_min, args.delay_max))
                    hits += 1
                    send(f"match #{hits}  score {score:.2f}")
                    if args.recast_delay >= 0:
                        wait = args.recast_delay + random.uniform(0.0, args.recast_jitter)
                        time.sleep(wait)
                        send(f"catch-recast after {wait:.2f}s")
                    last_action = time.time()  # miss timer starts after the cast
                    miss_after = miss_wait()
                    break  # drop the rest of this chunk; it predates the key presses

                if args.timeout > 0 and now - last_action >= miss_after:
                    waited = now - last_action
                    last_action = now
                    miss_after = miss_wait()
                    if window_ok(f"No match for {waited:.1f}s"):
                        send(f"miss-recast after {waited:.1f}s")
                    break
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        p.terminate()


def main() -> None:
    ap = argparse.ArgumentParser(description="Press a key when a reference sound is heard.")
    ap.add_argument("--ref", default="lure-catch.wav", help="reference WAV (default: lure-catch.wav)")
    ap.add_argument("--threshold", type=float, default=0.6,
                    help="match score 0..1 needed to trigger (default 0.6)")
    ap.add_argument("--cooldown", type=float, default=3.0,
                    help="seconds to ignore matches after a trigger (default 3)")
    ap.add_argument("--key", default="space", help="key to press (default space)")
    ap.add_argument("--recast-delay", type=float, default=0.5,
                    help="after a match, wait this long and press the key again "
                         "(default 0.5, negative = don't press again)")
    ap.add_argument("--recast-jitter", type=float, default=1.5,
                    help="random 0..N seconds added to --recast-delay (default 1.5)")
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="press the key if nothing is detected for this many seconds "
                         "(default 30, 0 = off)")
    ap.add_argument("--timeout-jitter", type=float, default=5.0,
                    help="random 0..N seconds added to --timeout (default 5)")
    ap.add_argument("--ref-seconds", type=float, default=0.7,
                    help="use only the first N seconds of the trimmed reference; "
                         "shorter reacts faster but false-triggers more (default 0.7, 0 = all)")
    ap.add_argument("--trim-db", type=float, default=30.0,
                    help="trim reference parts quieter than peak minus this many dB")
    ap.add_argument("--delay-min", type=float, default=0.0, help="min random delay before pressing")
    ap.add_argument("--delay-max", type=float, default=0.0, help="max random delay before pressing")
    ap.add_argument("--window", default="", help="only press when the foreground window title "
                                                 "contains this text, e.g. 'World of Warcraft'")
    ap.add_argument("--device", type=int, help="input device index (see --list-devices)")
    ap.add_argument("--mic", action="store_true", help="use default microphone instead of loopback")
    ap.add_argument("--verbose", "-v", action="store_true", help="show live match score")
    ap.add_argument("--dry-run", action="store_true", help="detect but do not press keys")
    ap.add_argument("--list-devices", action="store_true", help="list capture devices and exit")
    ap.add_argument("--record", metavar="OUT.wav", help="record a new reference clip and exit")
    ap.add_argument("--seconds", type=float, default=3.0, help="length for --record")
    args = ap.parse_args()

    if args.list_devices:
        list_devices()
    elif args.record:
        record(args.record, args.seconds, args.device, args.mic)
    else:
        listen(args)


if __name__ == "__main__":
    main()
