"""Audio I/O: WAV files (soundfile) and live PulseAudio (sounddevice).

sounddevice uses PortAudio, which on Linux picks the PulseAudio backend by
default when PulseAudio is running — so this is the "pulse audio backend in
python" the plan asks for without hard-coupling to Pulse's own API.

Both dependencies are imported lazily so that the modem's pure-DSP tests can
run in environments without libsndfile or an audio server.

Device selection honours ``PULSE_SINK``/``PULSE_SOURCE`` env vars. PortAudio
usually takes the ALSA-plug path to Pulse, so the env vars don't reach libpulse
in a way that changes the "default" device — we resolve them ourselves against
``sounddevice.query_devices()`` and pass an explicit index.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np


def write_wav(path: Path | str, samples: np.ndarray, sample_rate: float) -> None:
    """Write float32 mono samples to a WAV file."""
    import soundfile

    soundfile.write(str(path), np.asarray(samples, dtype=np.float32), int(round(sample_rate)))


def read_wav(path: Path | str, *, expected_sample_rate: float | None = None) -> tuple[np.ndarray, int]:
    """Read a WAV file, downmixing to mono if needed.

    Returns ``(samples_float32, sample_rate)``. Raises if
    ``expected_sample_rate`` is given and doesn't match.
    """
    import soundfile

    data, sample_rate = soundfile.read(str(path), dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1).astype(np.float32)
    if expected_sample_rate is not None and int(round(expected_sample_rate)) != int(sample_rate):
        raise ValueError(
            f"WAV sample rate {sample_rate} Hz does not match expected {expected_sample_rate} Hz"
        )
    return data, int(sample_rate)


def play(samples: np.ndarray, sample_rate: float, *, blocking: bool = True) -> None:
    """Play ``samples`` through the default audio device (PulseAudio on Linux)."""
    sd = _import_sounddevice()
    device = _resolve_device(sd, os.environ.get("PULSE_SINK"), kind="output")
    sd.play(
        np.asarray(samples, dtype=np.float32),
        int(round(sample_rate)),
        device=device,
        blocking=blocking,
    )
    if blocking:
        sd.wait()


def record_until_interrupted(sample_rate: float) -> np.ndarray:
    """Record mono audio from the default input device until Ctrl-C.

    Returns the accumulated samples as a 1-D float32 array. The stream stays
    open across the entire recording; callers pass the whole buffer to
    ``decode()`` once the user has interrupted.
    """
    import sys

    sd = _import_sounddevice()
    device = _resolve_device(sd, os.environ.get("PULSE_SOURCE"), kind="input")
    chunks: list[np.ndarray] = []

    def _callback(indata, _frames, _time, _status):
        chunks.append(indata.copy())

    print("recording — press Ctrl-C to stop and decode", file=sys.stderr, flush=True)
    try:
        with sd.InputStream(
            samplerate=int(round(sample_rate)),
            channels=1,
            dtype="float32",
            device=device,
            callback=_callback,
        ):
            while True:
                sd.sleep(500)
    except KeyboardInterrupt:
        print("stopped, decoding…", file=sys.stderr, flush=True)

    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).reshape(-1)


def _import_sounddevice() -> Any:
    try:
        import sounddevice  # noqa: WPS433 - deferred import is intentional
    except ImportError as exc:
        raise ImportError(
            "sounddevice is required for live audio I/O. Install with `pip install sounddevice` "
            "or (on Debian/Ubuntu) `sudo apt install libportaudio2` first."
        ) from exc
    return sounddevice


def _resolve_device(sd: Any, name_hint: str | None, *, kind: str) -> int | None:
    """Match ``name_hint`` against ``sounddevice.query_devices()`` names.

    Returns the device index for the best match, or ``None`` to let PortAudio
    pick its own default. Substring match is intentional -- Pulse sink names
    like ``alsa_output.usb-Focusrite_Scarlett_Solo_...`` don't line up with
    PortAudio's shorter human-readable strings, so we accept either direction.

    If the hint is set but no sounddevice name matches (typical case: the hint
    is a Pulse sink or source name that PortAudio doesn't enumerate
    individually), fall back to the ``pulse`` PortAudio device. PulseAudio
    itself will then honour the ``PULSE_SINK`` / ``PULSE_SOURCE`` env var for
    that stream, routing it to the actual named sink or source.
    """
    if not name_hint:
        return None
    channel_attr = "max_input_channels" if kind == "input" else "max_output_channels"
    try:
        devices = sd.query_devices()
    except Exception:
        return None
    hint_lower = name_hint.lower()
    # First: direct substring match against a sounddevice device name.
    for index, info in enumerate(devices):
        if info.get(channel_attr, 0) <= 0:
            continue
        name = str(info.get("name", "")).lower()
        if hint_lower in name or name in hint_lower:
            return index
    # Second: use the generic "pulse" PortAudio device so libpulse honours the
    # ``PULSE_SINK`` / ``PULSE_SOURCE`` env var.
    for index, info in enumerate(devices):
        if info.get(channel_attr, 0) <= 0:
            continue
        if str(info.get("name", "")).lower() == "pulse":
            return index
    return None
