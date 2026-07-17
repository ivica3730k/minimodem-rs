"""Multi-block RS: pipe arbitrary-length files through the modem end-to-end.

The RS layer chunks the payload into ceil(N/data_bytes) blocks, each protected
independently, all carried inside one modem packet under one preamble. Mirrors
what the original minimodem_rs wrapper did over minimodem's byte stream, but
adapted to the packet-oriented weaklink modem.
"""

from __future__ import annotations

import random
import string
from pathlib import Path

import numpy as np
import pytest

from weaklink.modem.cli import main as modem_main
from weaklink.modem.codec import ModemConfig, decode, encode
from weaklink.modem.waveform import WaveformConfig


def _pipe_config(baud: float = 100.0, repeats: int = 1) -> ModemConfig:
    return ModemConfig(
        waveform=WaveformConfig(baud=baud, tone_spacing_hz=baud),
        preamble_length=64,
        payload_repeats=repeats,
        rs_data_bytes=32,
        rs_parity_bytes=8,
        rs_crc_enabled=True,
    )


def _random_text(size: int, seed: int) -> bytes:
    alphabet = (string.ascii_letters + string.digits + string.punctuation + " \n").encode("ascii")
    return bytes(random.Random(seed).choices(alphabet, k=size))


def _add_awgn(samples: np.ndarray, snr_db: float, sample_rate: float, seed: int) -> np.ndarray:
    signal_power = float(np.mean(np.asarray(samples, dtype=np.float64) ** 2))
    noise_variance = signal_power * sample_rate / (2.0 * 3000.0) / (10 ** (snr_db / 10.0))
    rng = np.random.default_rng(seed)
    return samples + rng.normal(0.0, np.sqrt(noise_variance), size=samples.shape).astype(np.float32)


def test_500_byte_file_clean_roundtrip() -> None:
    """Roughly 16 RS blocks in one packet."""
    payload = _random_text(500, seed=1)
    config = _pipe_config()
    samples = encode(payload, config)
    assert decode(samples, config, payload_length_bytes=len(payload)) == payload


def test_1000_byte_file_clean_roundtrip() -> None:
    """Roughly 32 RS blocks in one packet."""
    payload = _random_text(1000, seed=2)
    config = _pipe_config()
    samples = encode(payload, config)
    assert decode(samples, config, payload_length_bytes=len(payload)) == payload


def test_odd_sized_file_pads_and_strips_correctly() -> None:
    """Non-multiple-of-data_bytes payload should pad on TX, strip on RX."""
    payload = _random_text(97, seed=3)  # 97 isn't a multiple of 32
    config = _pipe_config()
    samples = encode(payload, config)
    decoded = decode(samples, config, payload_length_bytes=len(payload))
    assert decoded == payload
    assert len(decoded) == 97


def test_500_bytes_survives_minus_5_db_snr() -> None:
    """Weak-signal survival: 500 bytes at 100 baud with 1x repeat."""
    payload = _random_text(500, seed=4)
    config = _pipe_config()
    samples = encode(payload, config)
    noisy = _add_awgn(samples, snr_db=-5.0, sample_rate=config.waveform.sample_rate, seed=1)
    assert decode(noisy, config, payload_length_bytes=len(payload)) == payload


def test_e2e_cli_pipes_a_file(tmp_path: Path) -> None:
    """CLI-level file pipe: bytes in through --input, bytes out through --output."""
    payload = _random_text(400, seed=5)
    input_file = tmp_path / "message.txt"
    wav_file = tmp_path / "signal.wav"
    output_file = tmp_path / "received.txt"
    input_file.write_bytes(payload)

    common_args = [
        "--baud", "100",
        "--tone-spacing", "100",
        "--preamble-length", "64",
        "--payload-repeats", "1",
        "--rs-data-bytes", "32",
        "--rs-parity-bytes", "8",
    ]
    tx_exit = modem_main(["tx", *common_args, "--input", str(input_file), "--wav", str(wav_file)])
    assert tx_exit == 0
    rx_exit = modem_main(
        ["rx", *common_args, "--output", str(output_file), "--wav", str(wav_file), "--length", str(len(payload))]
    )
    assert rx_exit == 0
    assert output_file.read_bytes() == payload
