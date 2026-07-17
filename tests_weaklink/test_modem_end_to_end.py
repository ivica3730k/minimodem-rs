"""End-to-end streaming roundtrip via WAV file."""

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


def _strip_trailing_nul(data: bytes) -> bytes:
    return data.rstrip(b"\x00")


def test_short_payload_clean_wav_roundtrip(tmp_path: Path) -> None:
    """CLI TX -> WAV -> CLI RX -> bytes match (with trailing NUL strip)."""
    message = b"weaklink modem streaming hello"
    message_file = tmp_path / "msg.bin"
    wav_file = tmp_path / "signal.wav"
    output_file = tmp_path / "out.bin"
    message_file.write_bytes(message)

    tx_exit = modem_main(["tx", "--input", str(message_file), "--wav", str(wav_file)])
    assert tx_exit == 0
    assert wav_file.exists() and wav_file.stat().st_size > 0

    rx_exit = modem_main(["rx", "--output", str(output_file), "--wav", str(wav_file)])
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
    modem_main(["rx", "--output", str(output_file), "--wav", str(wav_file)])
    assert output_file.read_bytes() == message


def test_wav_file_is_reloadable(tmp_path: Path) -> None:
    """Library-level roundtrip via WAV."""
    config = ModemConfig()
    payload = b"round-trip via WAV"
    samples = encode(payload, config)
    wav_path = tmp_path / "trip.wav"
    write_wav(wav_path, samples, config.waveform.sample_rate)
    reloaded, sample_rate = read_wav(wav_path, expected_sample_rate=config.waveform.sample_rate)
    assert sample_rate == int(round(config.waveform.sample_rate))
    decoded = _strip_trailing_nul(decode(reloaded, config))
    assert decoded == payload


# --- SNR sweep --------------------------------------------------------------


@dataclass
class SweepPoint:
    snr_db: float
    trials: int
    successes: int
    total_bytes: int


def _add_awgn(samples: np.ndarray, snr_db: float, sample_rate: float, *, bandwidth_hz: float, seed: int) -> np.ndarray:
    signal_power = float(np.mean(np.asarray(samples, dtype=np.float64) ** 2))
    noise_variance = signal_power * sample_rate / (2.0 * bandwidth_hz) / (10 ** (snr_db / 10.0))
    rng = np.random.default_rng(seed)
    return samples + rng.normal(0.0, np.sqrt(noise_variance), size=samples.shape).astype(np.float32)


def _sweep(snr_db: float, *, trials: int, config: ModemConfig, payload: bytes) -> SweepPoint:
    samples = encode(payload, config)
    successes = 0
    for trial_index in range(trials):
        noisy = _add_awgn(
            samples,
            snr_db=snr_db,
            sample_rate=config.waveform.sample_rate,
            bandwidth_hz=3_000.0,
            seed=trial_index,
        )
        decoded = _strip_trailing_nul(decode(noisy, config))
        if decoded == payload:
            successes += 1
    return SweepPoint(snr_db=snr_db, trials=trials, successes=successes, total_bytes=trials * len(payload))


@pytest.mark.slow
def test_snr_sweep_prints_baseline() -> None:
    trials = 10
    alphabet = (string.ascii_letters + string.digits + " ").encode("ascii")
    payload = bytes(random.Random(1337).choices(alphabet, k=20))
    config = ModemConfig()  # 300 baud default
    print()
    print(f"{'SNR (dB in 3 kHz)':>18} {'success rate':>15}")
    for snr_db in (5, 0, -3, -5, -8, -10):
        point = _sweep(snr_db, trials=trials, config=config, payload=payload)
        print(f"{point.snr_db:>18.1f} {100 * point.successes / point.trials:>14.1f}%")
    # Sanity: at +5 dB SNR the sweep should be reliable.
    high = _sweep(5.0, trials=trials, config=config, payload=payload)
    assert high.successes == trials, f"expected clean decode at +5 dB, got {high.successes}/{trials}"
