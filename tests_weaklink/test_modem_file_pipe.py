"""Streaming: pipe arbitrary-length files through the modem end-to-end."""

from __future__ import annotations

import io
import random
import string
from pathlib import Path

import numpy as np

from weaklink.modem.cli import main as modem_main
from weaklink.modem.codec import ModemConfig, decode, encode
from weaklink.modem.waveform import WaveformConfig

from ._streaming import pump_decode


class _FakeStdio:
    def __init__(self, initial: bytes = b"") -> None:
        self.buffer = io.BytesIO(initial)


def _random_text(size: int, seed: int) -> bytes:
    alphabet = (string.ascii_letters + string.digits + string.punctuation + " \n").encode("ascii")
    return bytes(random.Random(seed).choices(alphabet, k=size))


def _strip(data: bytes) -> bytes:
    return data.rstrip(b"\x00")


def _pipe_config(baud: float = 300.0) -> ModemConfig:
    return ModemConfig(waveform=WaveformConfig(baud=baud, tone_spacing_hz=baud))


def test_500_byte_file_clean_roundtrip() -> None:
    payload = _random_text(500, seed=1)
    config = _pipe_config()
    samples = encode(payload, config)
    assert _strip(decode(samples, config)) == payload


def test_1000_byte_file_clean_roundtrip() -> None:
    payload = _random_text(1000, seed=2)
    config = _pipe_config()
    samples = encode(payload, config)
    assert _strip(decode(samples, config)) == payload


def test_odd_sized_file_pads_and_strips_correctly() -> None:
    """Non-multiple-of-rs_data_bytes payload; TX pads, RX-caller strips trailing NULs."""
    payload = _random_text(97, seed=3)
    config = _pipe_config()
    samples = encode(payload, config)
    decoded = _strip(decode(samples, config))
    assert decoded == payload
    assert len(decoded) == 97


def test_e2e_cli_pipes_a_file(tmp_path: Path, monkeypatch) -> None:
    payload = _random_text(400, seed=5)
    wav_file = tmp_path / "signal.wav"

    monkeypatch.setattr("sys.stdin", _FakeStdio(payload))
    assert modem_main(["tx", "--modem-wav", str(wav_file)]) == 0

    fake_out = _FakeStdio()
    monkeypatch.setattr("sys.stdout", fake_out)
    assert modem_main(["rx", "--modem-wav", str(wav_file)]) == 0
    assert fake_out.buffer.getvalue() == payload


# --- e2e streaming variants -------------------------------------------------


def test_500_byte_file_e2e_streaming() -> None:
    payload = _random_text(500, seed=1)
    config = _pipe_config()
    samples = encode(payload, config)
    assert _strip(pump_decode(samples, config)) == payload


def test_1000_byte_file_e2e_streaming() -> None:
    payload = _random_text(1000, seed=2)
    config = _pipe_config()
    samples = encode(payload, config)
    assert _strip(pump_decode(samples, config)) == payload


def test_odd_sized_file_e2e_streaming() -> None:
    payload = _random_text(97, seed=3)
    config = _pipe_config()
    samples = encode(payload, config)
    assert _strip(pump_decode(samples, config)) == payload
