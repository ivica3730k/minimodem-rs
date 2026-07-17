"""End-to-end modem roundtrip via WAV file — mirrors the minimodem e2e style.

Two flavours:

* Clean roundtrip through a WAV file (must succeed byte-for-byte).
* AWGN sweep: injects noise between encode and decode, records the byte error
  rate. Marked slow.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from weaklink.modem.audio import read_wav, write_wav
from weaklink.modem.cli import main as modem_main
from weaklink.modem.codec import ModemConfig, decode, encode
from weaklink.modem.waveform import WaveformConfig


def test_short_payload_clean_wav_roundtrip(tmp_path: Path) -> None:
    """Same pattern as the minimodem e2e test: CLI TX -> WAV -> CLI RX -> bytes match."""
    message = b"weaklink modem hello world"
    message_file = tmp_path / "msg.bin"
    wav_file = tmp_path / "signal.wav"
    output_file = tmp_path / "out.bin"
    message_file.write_bytes(message)

    tx_exit = modem_main(["tx", "--input", str(message_file), "--wav", str(wav_file)])
    assert tx_exit == 0
    assert wav_file.exists() and wav_file.stat().st_size > 0

    rx_exit = modem_main(
        ["rx", "--output", str(output_file), "--wav", str(wav_file), "--length", str(len(message))]
    )
    assert rx_exit == 0
    assert output_file.read_bytes() == message


def test_random_100_bytes_clean_wav_roundtrip(tmp_path: Path) -> None:
    alphabet = (string.ascii_letters + string.digits + string.punctuation + " ").encode("ascii")
    rng = random.Random(7)
    message = bytes(rng.choices(alphabet, k=100))

    message_file = tmp_path / "msg.bin"
    wav_file = tmp_path / "signal.wav"
    output_file = tmp_path / "out.bin"
    message_file.write_bytes(message)

    modem_main(["tx", "--input", str(message_file), "--wav", str(wav_file)])
    modem_main(["rx", "--output", str(output_file), "--wav", str(wav_file), "--length", str(len(message))])
    assert output_file.read_bytes() == message


def test_wav_file_is_reloadable(tmp_path: Path) -> None:
    """Round-trip through WAV file at library level (not via CLI)."""
    config = ModemConfig()
    payload = b"round-trip via WAV"
    samples = encode(payload, config)
    wav_path = tmp_path / "trip.wav"
    write_wav(wav_path, samples, config.waveform.sample_rate)
    reloaded, sample_rate = read_wav(wav_path, expected_sample_rate=config.waveform.sample_rate)
    assert sample_rate == int(round(config.waveform.sample_rate))
    assert decode(reloaded, config, payload_length_bytes=len(payload)) == payload


# --- SNR sweep --------------------------------------------------------------


@dataclass
class SweepPoint:
    snr_db: float
    trials: int
    errors: int
    total_bytes: int

    @property
    def byte_error_rate(self) -> float:
        return self.errors / self.total_bytes


def _add_awgn(samples: np.ndarray, snr_db: float, sample_rate: float, *, bandwidth_hz: float, seed: int) -> np.ndarray:
    signal_power = float(np.mean(np.asarray(samples, dtype=np.float64) ** 2))
    noise_variance = signal_power * sample_rate / (2.0 * bandwidth_hz) / (10 ** (snr_db / 10.0))
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, np.sqrt(noise_variance), size=samples.shape).astype(np.float32)
    return samples + noise


def _sweep(snr_db: float, *, trials: int) -> SweepPoint:
    config = ModemConfig()
    alphabet = (string.ascii_letters + string.digits + " ").encode("ascii")
    message_rng = random.Random(1337)
    payload = bytes(message_rng.choices(alphabet, k=20))

    errors = 0
    for trial_index in range(trials):
        samples = encode(payload, config)
        noisy = _add_awgn(
            samples,
            snr_db=snr_db,
            sample_rate=config.waveform.sample_rate,
            bandwidth_hz=3_000.0,
            seed=trial_index,
        )
        decoded = decode(noisy, config, payload_length_bytes=len(payload))
        errors += sum(1 for a, b in zip(decoded, payload) if a != b)
    return SweepPoint(snr_db=snr_db, trials=trials, errors=errors, total_bytes=trials * 20)


@pytest.mark.slow
def test_snr_sweep_prints_baseline() -> None:
    trials = 10
    sweep_points = [_sweep(snr_db, trials=trials) for snr_db in (5, 0, -3, -5, -8, -10)]
    print()
    print(f"{'SNR (dB in 3 kHz)':>18} {'byte-error rate':>18}")
    for point in sweep_points:
        print(f"{point.snr_db:>18.1f} {point.byte_error_rate:>18.2%}")

    # Sanity: at +5 dB SNR every byte should decode.
    top = sweep_points[0]
    assert top.byte_error_rate == 0.0, f"expected clean decode at +5 dB, got {top.byte_error_rate:.2%}"
