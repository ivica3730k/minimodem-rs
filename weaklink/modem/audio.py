"""Audio I/O: WAV files (soundfile) and live PulseAudio (sounddevice).

sounddevice uses PortAudio, which on Linux picks the PulseAudio backend by
default when PulseAudio is running — so this is the "pulse audio backend in
python" the plan asks for without hard-coupling to Pulse's own API.

Both dependencies are imported lazily so that the modem's pure-DSP tests can
run in environments without libsndfile or an audio server.
"""

from __future__ import annotations

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
    sd.play(np.asarray(samples, dtype=np.float32), int(round(sample_rate)), blocking=blocking)
    if blocking:
        sd.wait()


def record(duration_seconds: float, sample_rate: float) -> np.ndarray:
    """Record ``duration_seconds`` of mono audio from the default input device."""
    sd = _import_sounddevice()
    frame_count = int(round(duration_seconds * sample_rate))
    recording = sd.rec(frame_count, samplerate=int(round(sample_rate)), channels=1, dtype="float32")
    sd.wait()
    return recording.reshape(-1)


def _import_sounddevice() -> Any:
    try:
        import sounddevice  # noqa: WPS433 - deferred import is intentional
    except ImportError as exc:
        raise ImportError(
            "sounddevice is required for live audio I/O. Install with `pip install sounddevice` "
            "or (on Debian/Ubuntu) `sudo apt install libportaudio2` first."
        ) from exc
    return sounddevice
