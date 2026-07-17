"""Transport abstractions.

Two shapes:

* ``BitTransport`` — a streaming bit pipe. Suits async modems like minimodem
  where bits trickle in continuously and framing sits *above* the transport.
* ``PacketTransport`` — a fixed-size byte-packet pipe. Suits the weaklink
  modem, which needs a preamble on every burst so it can only send/receive in
  whole frames.

The two are different enough that a single protocol was more trouble than it
was worth. The framing layer above them chooses one or the other; nothing
mandates that a given deployment uses both.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterable, Iterator, Protocol

import numpy as np


# --- streaming bit interface ----------------------------------------------


class BitTransport(Protocol):
    """One-way streaming bit pipe."""

    def send(self, bits: Iterable[int]) -> None:
        ...

    def recv(self) -> Iterator[int]:
        ...


# --- packet interface -----------------------------------------------------


class PacketTransport(Protocol):
    """One-way pipe carrying fixed-size byte packets."""

    payload_bytes: int

    def send(self, payload: bytes) -> None:
        ...

    def recv(self) -> bytes:
        ...


# --- shared helpers -------------------------------------------------------


def _child_env_without_pyinstaller_leak() -> dict[str, str]:
    env = os.environ.copy()
    for variable_name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        original = env.pop(f"{variable_name}_ORIG", None)
        if original is not None:
            env[variable_name] = original
        else:
            env.pop(variable_name, None)
    return env


# --- minimodem-backed BitTransport ---------------------------------------


class MinimodemTransport:
    """Bit pipe backed by a ``minimodem`` subprocess."""

    def __init__(self, direction: str, baud: int | str, *, minimodem_binary: str = "minimodem", extra_args: list[str] | None = None):
        if direction not in ("tx", "rx"):
            raise ValueError(f"direction must be 'tx' or 'rx', got {direction!r}")
        self._direction = direction
        self._baud = str(baud)
        self._binary = minimodem_binary
        self._extra = list(extra_args or [])
        self._proc: subprocess.Popen | None = None

    def _argv(self) -> list[str]:
        flag = "--tx" if self._direction == "tx" else "--rx"
        return [self._binary, flag, "--binary-output", "1", *self._extra, self._baud]

    def send(self, bits: Iterable[int]) -> None:
        assert self._direction == "tx"
        proc = subprocess.Popen(
            self._argv(), stdin=subprocess.PIPE, env=_child_env_without_pyinstaller_leak(),
        )
        assert proc.stdin is not None
        buffer = bytearray()
        for bit in bits:
            buffer.append(1 if bit else 0)
            if len(buffer) >= 4096:
                proc.stdin.write(bytes(buffer))
                buffer.clear()
        if buffer:
            proc.stdin.write(bytes(buffer))
        proc.stdin.flush()
        proc.stdin.close()
        proc.wait()

    def recv(self) -> Iterator[int]:
        assert self._direction == "rx"
        proc = subprocess.Popen(
            self._argv(), stdout=subprocess.PIPE, env=_child_env_without_pyinstaller_leak(),
        )
        assert proc.stdout is not None
        self._proc = proc
        try:
            while True:
                chunk = proc.stdout.read(1024)
                if not chunk:
                    return
                for byte in chunk:
                    yield 1 if byte else 0
        finally:
            proc.terminate()


# --- weaklink-modem-backed PacketTransport --------------------------------


class AudioModemTransport:
    """Packet pipe using the weaklink 4-FSK+Viterbi modem over audio.

    Sink is either a live audio device (PulseAudio via sounddevice) or a WAV
    file. Source, symmetrically, is a live device or a WAV file. Live paths
    are one-shot per ``send``/``recv``; WAV paths write / read the whole file.
    """

    def __init__(
        self,
        *,
        payload_bytes: int,
        modem_config: "ModemConfig | None" = None,
        wav_path: Path | str | None = None,
        record_duration_seconds: float | None = None,
    ):
        from weaklink.modem.codec import ModemConfig as _ModemConfig

        self.payload_bytes = payload_bytes
        self._config = modem_config or _ModemConfig()
        self._wav_path = Path(wav_path) if wav_path is not None else None
        self._record_duration = record_duration_seconds

    def send(self, payload: bytes) -> None:
        if len(payload) != self.payload_bytes:
            raise ValueError(f"payload must be exactly {self.payload_bytes} bytes, got {len(payload)}")
        from weaklink.modem.codec import encode

        samples = encode(payload, self._config)
        if self._wav_path is not None:
            from weaklink.modem.audio import write_wav

            write_wav(self._wav_path, samples, self._config.waveform.sample_rate)
        else:
            from weaklink.modem.audio import play

            play(samples, self._config.waveform.sample_rate)

    def recv(self) -> bytes:
        from weaklink.modem.codec import decode

        if self._wav_path is not None:
            from weaklink.modem.audio import read_wav

            samples, _ = read_wav(self._wav_path, expected_sample_rate=self._config.waveform.sample_rate)
        else:
            if self._record_duration is None:
                raise ValueError("live recv requires record_duration_seconds")
            from weaklink.modem.audio import record

            samples = record(self._record_duration, self._config.waveform.sample_rate)
        return decode(np.asarray(samples), self._config, payload_length_bytes=self.payload_bytes)
