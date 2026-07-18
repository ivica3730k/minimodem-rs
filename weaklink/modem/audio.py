"""Audio I/O: WAV files (soundfile) + two live-audio backends (sounddevice /
PortAudio and ``paplay`` / ``parec`` subprocess for explicit Pulse routing).

Device hints go through :func:`resolve_audio_target` and land in one of four
paths:

1. **Integer index**: bare digits -> raw ``sounddevice.query_devices()`` index.
2. **Substring against sounddevice**: any device whose name contains the hint
   or vice versa.
3. **Pulse sink / source name**: contains a ``.`` (e.g. ``virt.monitor``) or
   didn't match any sounddevice, and ``paplay`` / ``parec`` is on PATH. We use
   the subprocess backend and route via ``--device=<name>``. This bypasses
   PortAudio's Pulse compat layer entirely -- necessary because PortAudio
   doesn't reliably honour ``PULSE_SINK`` / ``PULSE_SOURCE`` / ``PIPEWIRE_NODE``
   for streams it opens through pipewire-pulse.
4. **Nothing given / no match**: use PortAudio's default (respecting whatever
   the OS considers the default input / output).

Both dependencies are imported lazily so pure-DSP tests can run without an
audio server or CLI helpers installed.
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

_log = logging.getLogger("weaklink.audio")


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


@dataclass
class AudioTarget:
    """Resolved audio endpoint. Exactly one of ``sd_index`` / ``pulse_name`` set."""

    sd_index: int | None = None
    pulse_name: str | None = None

    def describe(self) -> str:
        if self.pulse_name is not None:
            return f"pulse:{self.pulse_name}"
        if self.sd_index is not None:
            return f"sounddevice[{self.sd_index}]"
        return "default"


def resolve_audio_target(name_hint: str | None, *, kind: str) -> AudioTarget:
    """Turn a user-supplied device hint into a concrete backend target.

    ``kind`` is ``"input"`` or ``"output"``. See module docstring for the
    four permutations.
    """
    if not name_hint:
        return AudioTarget()

    # Permutation 1: bare integer -> raw sounddevice index (skip Pulse path).
    if name_hint.lstrip("-").isdigit():
        return AudioTarget(sd_index=int(name_hint))

    sd = _import_sounddevice()
    channel_attr = "max_input_channels" if kind == "input" else "max_output_channels"
    try:
        devices = sd.query_devices()
    except Exception:
        _log.debug("sounddevice.query_devices() failed while resolving %r", name_hint)
        devices = []

    hint_lower = name_hint.lower()
    # Permutation 2: substring match against a sounddevice name.
    for index, info in enumerate(devices):
        if info.get(channel_attr, 0) <= 0:
            continue
        name = str(info.get("name", "")).lower()
        # Ignore the abstract Pulse/PipeWire compat devices in the substring
        # pass; they'd match anything containing "pulse"/"pipewire" and rob
        # the Pulse subprocess path of its chance.
        if name in ("pulse", "pipewire", "default"):
            continue
        if hint_lower in name or name in hint_lower:
            _log.debug("device hint %r -> sounddevice %d %r", name_hint, index, info["name"])
            return AudioTarget(sd_index=index)

    # Permutation 3: named Pulse endpoint via subprocess.
    tool = "parec" if kind == "input" else "paplay"
    if shutil.which(tool):
        _log.debug("device hint %r -> pulse subprocess (%s --device=%s)",
                   name_hint, tool, name_hint)
        return AudioTarget(pulse_name=name_hint)

    # Nothing matched cleanly; PortAudio picks its default.
    _log.warning(
        "device hint %r did not match any sounddevice %s device and %s "
        "is not on PATH; using OS default", name_hint, kind, tool,
    )
    return AudioTarget()


def play(samples: np.ndarray, sample_rate: float, *, device: str | None = None) -> None:
    """Play ``samples`` blocking, through ``device`` (index / substring / Pulse
    sink) or the OS default."""
    hint = device if device else os.environ.get("PULSE_SINK")
    target = resolve_audio_target(hint, kind="output")
    samples_f32 = np.asarray(samples, dtype=np.float32).reshape(-1)
    rate = int(round(sample_rate))

    if target.pulse_name is not None:
        _play_pulse(samples_f32, rate, target.pulse_name)
        return

    sd = _import_sounddevice()
    sd.play(samples_f32, rate, device=target.sd_index, blocking=True)
    sd.wait()


def _play_pulse(samples: np.ndarray, sample_rate: int, sink_name: str) -> None:
    """Blocking play via ``paplay --device=<sink_name>``."""
    proc = subprocess.Popen(
        [
            "paplay",
            f"--device={sink_name}",
            "--format=float32le",
            f"--rate={sample_rate}",
            "--channels=1",
            "--raw",
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    try:
        proc.stdin.write(samples.tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    _, err = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"paplay exited {proc.returncode}: {err.decode(errors='replace')}")


class LiveInputStream:
    """Uniform live-audio input abstraction over sounddevice or ``parec``.

    Context manager. On entry, opens the underlying stream and starts a
    producer that pushes ``(samples_1d_float32,)`` chunks to ``callback``. On
    exit, cleans up.

    Poll cadence: same on both backends -- the caller can ``sd.sleep(ms)`` or
    ``time.sleep(ms/1000)`` between polls. Both work.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        callback: Callable[[np.ndarray], None],
        target: AudioTarget,
    ) -> None:
        self._sample_rate = sample_rate
        self._callback = callback
        self._target = target
        self._sd_stream = None  # type: ignore[assignment]
        self._proc: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def __enter__(self) -> "LiveInputStream":
        if self._target.pulse_name is not None:
            self._open_parec()
        else:
            self._open_sounddevice()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop_event.set()
        if self._sd_stream is not None:
            self._sd_stream.close()
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1.0)
            except Exception:
                self._proc.kill()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _open_sounddevice(self) -> None:
        sd = _import_sounddevice()

        def _sd_callback(indata: np.ndarray, _frames: int, _time: object, _status: object) -> None:
            self._callback(indata.reshape(-1).astype(np.float32, copy=False))

        self._sd_stream = sd.InputStream(
            samplerate=self._sample_rate,
            channels=1,
            dtype="float32",
            device=self._target.sd_index,
            callback=_sd_callback,
        )
        self._sd_stream.start()

    def _open_parec(self) -> None:
        assert self._target.pulse_name is not None
        self._proc = subprocess.Popen(
            [
                "parec",
                f"--device={self._target.pulse_name}",
                "--format=float32le",
                f"--rate={self._sample_rate}",
                "--channels=1",
                "--raw",
                "--latency-msec=100",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )

        def _pump() -> None:
            chunk_frames = max(1, self._sample_rate // 20)  # ~50 ms chunks
            chunk_bytes = chunk_frames * 4  # 4 bytes / float32
            assert self._proc is not None and self._proc.stdout is not None
            while not self._stop_event.is_set():
                raw = self._proc.stdout.read(chunk_bytes)
                if not raw:
                    break
                self._callback(np.frombuffer(raw, dtype=np.float32).copy())

        self._thread = threading.Thread(target=_pump, name="weaklink-parec", daemon=True)
        self._thread.start()


def _import_sounddevice() -> Any:
    try:
        import sounddevice  # noqa: WPS433 - deferred import is intentional
    except ImportError as exc:
        raise ImportError(
            "sounddevice is required for live audio I/O. Install with `pip install sounddevice` "
            "or (on Debian/Ubuntu) `sudo apt install libportaudio2` first."
        ) from exc
    return sounddevice
