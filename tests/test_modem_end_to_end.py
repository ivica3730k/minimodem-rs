"""End-to-end streaming roundtrip via WAV file."""

from __future__ import annotations

import io
import random
import string
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from weaklink.modem.audio import read_wav, write_wav
from weaklink.modem.cli import main as modem_main
from weaklink.modem.codec import ModemConfig, decode, encode

from ._streaming import stream_decode


def _strip_trailing_nul(data: bytes) -> bytes:
    return data.rstrip(b"\x00")


class _FakeStdio:
    def __init__(self, initial: bytes = b"") -> None:
        self.buffer = io.BytesIO(initial)


def _cli_roundtrip(monkeypatch, message: bytes, wav_file: Path) -> bytes:
    monkeypatch.setattr("sys.stdin", _FakeStdio(message))
    tx_exit = modem_main(["tx", "--modem-wav", str(wav_file)])
    assert tx_exit == 0
    assert wav_file.exists() and wav_file.stat().st_size > 0

    fake_out = _FakeStdio()
    monkeypatch.setattr("sys.stdout", fake_out)
    rx_exit = modem_main(["rx", "--modem-wav", str(wav_file)])
    assert rx_exit == 0
    return fake_out.buffer.getvalue()


def test_short_payload_clean_wav_roundtrip(tmp_path: Path, monkeypatch) -> None:
    """CLI TX (stdin) -> WAV -> CLI RX (stdout) -> bytes match."""
    message = b"weaklink modem streaming hello"
    wav_file = tmp_path / "signal.wav"
    assert _cli_roundtrip(monkeypatch, message, wav_file) == message


def test_random_100_bytes_clean_wav_roundtrip(tmp_path: Path, monkeypatch) -> None:
    alphabet = (string.ascii_letters + string.digits + string.punctuation + " ").encode("ascii")
    rng = random.Random(7)
    message = bytes(rng.choices(alphabet, k=100))
    wav_file = tmp_path / "signal.wav"
    assert _cli_roundtrip(monkeypatch, message, wav_file) == message


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


# --- e2e streaming variants -------------------------------------------------


def test_short_payload_clean_e2e_streaming() -> None:
    """Same roundtrip as ``test_short_payload_clean_wav_roundtrip`` but
    through ``_StreamingRxDecoder`` -- exercises the live-rx code path."""
    config = ModemConfig()
    message = b"weaklink modem streaming hello"
    samples = encode(message, config)
    assert _strip_trailing_nul(stream_decode(samples, config)) == message


def test_random_100_bytes_clean_e2e_streaming() -> None:
    config = ModemConfig()
    alphabet = (string.ascii_letters + string.digits + string.punctuation + " ").encode("ascii")
    rng = random.Random(7)
    message = bytes(rng.choices(alphabet, k=100))
    samples = encode(message, config)
    assert _strip_trailing_nul(stream_decode(samples, config)) == message


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
