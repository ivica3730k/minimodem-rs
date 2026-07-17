"""Paragraph-sized payloads work by scaling the RS block size.

RS supports up to 255 bytes per block over GF(2^8), so a single-block payload
tops out around ~240 chars once you subtract parity + CRC. Beyond that, users
would concatenate multiple packets — the modem itself stays single-block.

The trade-off vs. the 15-char preset is SNR sensitivity: sending 10x more
information in the same duration costs ~10 dB of SNR margin (Shannon).
"""

from __future__ import annotations

import numpy as np
import pytest

from weaklink.modem.codec import ModemConfig, decode, encode
from weaklink.modem.waveform import WaveformConfig


PARAGRAPH = (
    "CQ CQ CQ this is a test paragraph transmitted over the weaklink modem. "
    "The quick brown fox jumps over the lazy dog. Signal report 5-9 in EM70."
)[:142]


def _paragraph_config(baud: float, repeats: int, data_bytes: int) -> ModemConfig:
    return ModemConfig(
        waveform=WaveformConfig(baud=baud, tone_spacing_hz=baud),
        preamble_length=64,
        payload_repeats=repeats,
        rs_data_bytes=data_bytes,
        rs_parity_bytes=32,
        rs_crc_enabled=True,
    )


def _add_awgn(samples: np.ndarray, snr_db: float, sample_rate: float, *, seed: int) -> np.ndarray:
    signal_power = float(np.mean(np.asarray(samples, dtype=np.float64) ** 2))
    noise_variance = signal_power * sample_rate / (2.0 * 3000.0) / (10 ** (snr_db / 10.0))
    rng = np.random.default_rng(seed)
    return samples + rng.normal(0.0, np.sqrt(noise_variance), size=samples.shape).astype(np.float32)


def test_paragraph_encodes_and_decodes_clean() -> None:
    config = _paragraph_config(baud=100.0, repeats=1, data_bytes=150)
    payload = PARAGRAPH.encode("ascii")
    samples = encode(payload, config)
    duration = len(samples) / config.waveform.sample_rate
    assert duration < 30.0, f"paragraph packet is {duration:.1f}s, over 30s budget"
    assert decode(samples, config, payload_length_bytes=len(payload)) == payload


def test_paragraph_survives_minus_8_db_snr_at_100_baud() -> None:
    """100 baud + 1x repeat + RS(174,142) fits in ~16s and holds down to -8 dB SNR."""
    config = _paragraph_config(baud=100.0, repeats=1, data_bytes=150)
    payload = PARAGRAPH.encode("ascii")
    samples = encode(payload, config)
    noisy = _add_awgn(samples, snr_db=-8.0, sample_rate=config.waveform.sample_rate, seed=1)
    assert decode(noisy, config, payload_length_bytes=len(payload)) == payload


def test_paragraph_survives_minus_10_db_snr_at_100_baud_with_2x_repeat() -> None:
    """Doubling the repeat count buys ~3 dB at the cost of the 30s budget."""
    config = _paragraph_config(baud=100.0, repeats=2, data_bytes=150)
    payload = PARAGRAPH.encode("ascii")
    samples = encode(payload, config)
    noisy = _add_awgn(samples, snr_db=-10.0, sample_rate=config.waveform.sample_rate, seed=2)
    assert decode(noisy, config, payload_length_bytes=len(payload)) == payload


def test_rs_block_size_ceiling() -> None:
    """The 255-byte GF(2^8) block limit puts a soft cap on single-packet paragraph length."""
    max_data_bytes = 255 - 32 - 4  # parity + CRC
    config = _paragraph_config(baud=100.0, repeats=1, data_bytes=max_data_bytes)
    payload = b"x" * max_data_bytes
    samples = encode(payload, config)
    assert decode(samples, config, payload_length_bytes=len(payload)) == payload
