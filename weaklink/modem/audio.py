"""Audio I/O. WAV via soundfile; live via sounddevice or ``paplay`` /
``parec`` subprocess for named Pulse endpoints (PortAudio's Pulse compat
ignores ``PULSE_*``). Device hints: integer index, name substring, or
Pulse sink/source.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import numpy as np
import sounddevice
import soundfile

from weaklink.modem.exceptions import ConfigError

_log = logging.getLogger("weaklink.audio")


def write_wav(path: Path | str, samples: np.ndarray, sample_rate: float) -> None:
    """Write float32 mono samples to a WAV file."""

    soundfile.write(str(path), np.asarray(samples, dtype=np.float32), int(round(sample_rate)))


def write_wav_stream(path: Path | str, sample_chunks, sample_rate: float) -> None:
    """Streaming sink: consume float32 sample chunks from an iterator and
    append them to a WAV file. Same shape as :func:`play_stream` -- WAV
    output is just another sink at the end of the sample-chunk chain, so
    tx code paths don't branch on target."""

    rate = int(round(sample_rate))
    with soundfile.SoundFile(
        str(path), mode="w", samplerate=rate, channels=1, subtype="FLOAT",
    ) as sf:
        for chunk in sample_chunks:
            sf.write(np.asarray(chunk, dtype=np.float32).reshape(-1))


def read_wav(path: Path | str, *, expected_sample_rate: float | None = None) -> tuple[np.ndarray, int]:
    """Read a WAV file, downmixing to mono if needed.

    Returns ``(samples_float32, sample_rate)``. Raises if
    ``expected_sample_rate`` is given and doesn't match.
    """

    data, sample_rate = soundfile.read(str(path), dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1).astype(np.float32)
    if expected_sample_rate is not None and int(round(expected_sample_rate)) != int(sample_rate):
        raise ConfigError(
            f"WAV sample rate {sample_rate} Hz does not match expected {expected_sample_rate} Hz"
        )
    return data, int(sample_rate)


def read_wav_chunks(
    path: Path | str,
    *,
    chunk_seconds: float = 0.1,
    expected_sample_rate: float | None = None,
) -> Iterator[np.ndarray]:
    """Streaming source: yield float32 mono sample chunks from a WAV.
    Mirrors :class:`LiveInputStream`'s callback signature (chunks land
    at ~``chunk_seconds`` cadence, like a live audio poll), so rx code
    paths don't branch on WAV vs live."""

    with soundfile.SoundFile(str(path)) as sf:
        if expected_sample_rate is not None and int(round(expected_sample_rate)) != sf.samplerate:
            raise ConfigError(
                f"WAV sample rate {sf.samplerate} Hz does not match expected {expected_sample_rate} Hz"
            )
        chunk_frames = max(1, int(chunk_seconds * sf.samplerate))
        while True:
            data = sf.read(chunk_frames, dtype="float32", always_2d=False)
            if data.shape[0] == 0:
                return
            if data.ndim > 1:
                data = data.mean(axis=1).astype(np.float32)
            yield data


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


def _pactl_lookup_id(id_str: str, *, kind: str) -> str | None:
    """Resolve a numeric Pulse sink/source index to its name via ``pactl
    list short``. Returns None if pactl is missing, fails, or has no
    matching row."""
    if not shutil.which("pactl"):
        return None
    subcmd = "sources" if kind == "input" else "sinks"
    try:
        proc = subprocess.run(
            ["pactl", "list", "short", subcmd],
            capture_output=True, text=True, check=True, timeout=5.0,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        _log.warning("pactl %s failed for id %s: %s", subcmd, id_str, exc)
        return None
    for line in proc.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) >= 2 and fields[0] == id_str:
            return fields[1]
    return None


def _resolve_pulse(ref: str, *, kind: str) -> AudioTarget:
    """Handle explicit ``pulse:<x>`` where ``<x>`` is either a Pulse
    sink/source name or a numeric index. Missing pactl / no matching
    row for a numeric ref -> pass raw. Non-numeric ref -> pass raw."""
    if not ref:
        return AudioTarget()
    if ref.lstrip("-").isdigit():
        resolved = _pactl_lookup_id(ref, kind=kind)
        if resolved is not None:
            _log.debug("pulse:%s -> %s (via pactl)", ref, resolved)
            return AudioTarget(pulse_name=resolved)
        _log.warning("no Pulse endpoint at index %s; passing raw", ref)
    return AudioTarget(pulse_name=ref)


def resolve_audio_target(name_hint: str | None, *, kind: str) -> AudioTarget:
    """Turn a user-supplied device hint into a concrete backend target.

    ``kind`` is ``"input"`` or ``"output"``. See module docstring for the
    four permutations.
    """
    if not name_hint:
        return AudioTarget()

    # Permutation 0: ``pulse:<id>`` or ``pulse:<name>`` -- force Pulse path.
    # Numeric IDs get resolved to a sink/source name via ``pactl list short``
    # (paplay / parec don't reliably accept numeric IDs on pipewire-pulse).
    if name_hint.startswith("pulse:"):
        return _resolve_pulse(name_hint[len("pulse:") :], kind=kind)

    # Permutation 1: bare integer. Try Pulse first (via pactl); if pactl
    # is missing or has no such id, fall back to sounddevice index. Lets
    # a user type '47' from `pactl list short sinks` on a Linux box
    # without needing a prefix, while macOS/Windows (no pactl) keeps the
    # sounddevice-index-by-int behavior.
    if name_hint.lstrip("-").isdigit():
        resolved = _pactl_lookup_id(name_hint, kind=kind)
        if resolved is not None:
            _log.debug("hint %s -> pulse:%s (via pactl)", name_hint, resolved)
            return AudioTarget(pulse_name=resolved)
        return AudioTarget(sd_index=int(name_hint))

    sd = sounddevice
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
        # Skip the abstract Pulse/PipeWire compat devices so the subprocess
        # path can claim named Pulse endpoints.
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

    sd = sounddevice
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
    # Let communicate() own stdin + stderr; manual close double-closes on 3.12.
    _, err = proc.communicate(input=samples.tobytes())
    if proc.returncode != 0:
        raise RuntimeError(f"paplay exited {proc.returncode}: {err.decode(errors='replace')}")


def play_stream(
    sample_chunks,
    sample_rate: float,
    *,
    device: str | None = None,
) -> None:
    """Play float32 mono audio as it arrives from ``sample_chunks`` --
    used to stream long tx without buffering the whole encoded signal.
    Pulse targets pipe into ``paplay --raw``; sounddevice targets use an
    OutputStream and ``.write`` chunk by chunk."""
    hint = device if device else os.environ.get("PULSE_SINK")
    target = resolve_audio_target(hint, kind="output")
    rate = int(round(sample_rate))
    if target.pulse_name is not None:
        _play_pulse_stream(sample_chunks, rate, target.pulse_name)
        return
    sd = sounddevice
    stream = sd.OutputStream(
        samplerate=rate,
        channels=1,
        dtype="float32",
        device=target.sd_index,
    )
    stream.start()
    try:
        for chunk in sample_chunks:
            arr = np.asarray(chunk, dtype=np.float32).reshape(-1, 1)
            stream.write(arr)
    finally:
        stream.stop()
        stream.close()


def _play_pulse_stream(sample_chunks, sample_rate: int, sink_name: str) -> None:
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
    try:
        for chunk in sample_chunks:
            proc.stdin.write(np.asarray(chunk, dtype=np.float32).tobytes())
    except BrokenPipeError:
        pass
    finally:
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    err = proc.stderr.read() or b""
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"paplay exited {proc.returncode}: {err.decode(errors='replace')}")


class LiveInputStream:
    """Uniform live-audio input over sounddevice or ``parec``.

    Context manager. Pushes 1-D float32 chunks to ``callback`` from a
    producer thread; caller polls with ``time.sleep`` between checks.
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
        # SIGKILL + walk away. The OS reaps the pipes and the pump thread's
        # blocking read returns immediately once the fd closes. No waiting.
        self._stop_event.set()
        if self._sd_stream is not None:
            try:
                self._sd_stream.close()
            except Exception:
                pass
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass

    def _open_sounddevice(self) -> None:
        sd = sounddevice

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


