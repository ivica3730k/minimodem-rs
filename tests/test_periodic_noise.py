"""Periodic man-made noise (SMPS harmonics, mains hum, alternator whine)
tends to hit the same bit positions in every block when the interleaver
is fixed. Per-block pseudorandom permutation (seeded by block_index)
scrambles the periodicity so RS sees random errors across blocks.

Tests here inject a periodic amplitude notch that repeats once per
block boundary -- a synthetic worst-case for a fixed interleaver. With
the per-block permutation this decodes; if we regressed to a fixed
shuffle, it would systematically corrupt the same bits every block.
"""

from __future__ import annotations

import numpy as np
import pytest

from weaklink.modem.codec import ModemConfig, decode, encode
from weaklink.modem.waveform import WaveformConfig


def _config(baud: float = 300.0) -> ModemConfig:
    return ModemConfig(
        waveform=WaveformConfig(baud=baud, tone_spacing_hz=baud),
        rs_data_bytes=16,
        rs_parity_bytes=8,
        block_repeats=1,
    )


def _inject_periodic_notch(
    audio: np.ndarray,
    sample_rate: float,
    period_seconds: float,
    notch_width_seconds: float,
    depth_db: float,
) -> np.ndarray:
    """Multiply the signal by an envelope that drops by ``depth_db`` for
    ``notch_width_seconds`` once every ``period_seconds``. Mimics a car
    alternator / SMPS harmonic that periodically knocks the signal down."""
    n = audio.size
    t = np.arange(n) / sample_rate
    phase = (t % period_seconds) / period_seconds
    notch_fraction = notch_width_seconds / period_seconds
    trough = 10 ** (-depth_db / 20.0)
    envelope = np.where(phase < notch_fraction, trough, 1.0).astype(np.float32)
    return (audio * envelope).astype(np.float32)


@pytest.mark.parametrize("payload_size", [200, 500])
def test_decodes_through_periodic_notches(payload_size: int) -> None:
    """Signal + periodic ~15 dB notches that repeat about once per block --
    the exact alignment pattern that would nail a fixed interleaver."""
    config = _config()
    payload = np.random.default_rng(payload_size).bytes(payload_size)
    audio = encode(payload, config)
    sample_rate = config.waveform.sample_rate

    # ~1 notch per block (block ≈ 0.85 s at 300 baud, RS(16,8))
    notched = _inject_periodic_notch(
        audio,
        sample_rate,
        period_seconds=0.85,
        notch_width_seconds=0.08,  # ~10% duty
        depth_db=15.0,
    )
    decoded = decode(notched, config)
    assert decoded == payload, (
        f"periodic-notch decode failed: {len(decoded)}/{len(payload)} bytes"
    )


# --- e2e streaming variant --------------------------------------------------

from ._streaming import stream_decode


@pytest.mark.parametrize("payload_size", [200, 500])
def test_decodes_through_periodic_notches_e2e_streaming(payload_size: int) -> None:
    config = _config()
    payload = np.random.default_rng(payload_size).bytes(payload_size)
    audio = encode(payload, config)
    sample_rate = config.waveform.sample_rate
    notched = _inject_periodic_notch(
        audio, sample_rate, period_seconds=0.85, notch_width_seconds=0.08, depth_db=15.0,
    )
    decoded = stream_decode(notched, config)
    assert decoded == payload, (
        f"periodic-notch e2e streaming decode failed: {len(decoded)}/{len(payload)} bytes"
    )
