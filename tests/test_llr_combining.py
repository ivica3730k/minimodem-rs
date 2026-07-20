"""Soft-LLR combining across ``block_repeats`` copies: two marginal
copies of the same block, deinterleaved with per-copy permutations
and summed before Viterbi, should decode payloads that neither copy
would clear alone.

Each copy uses its own pseudorandom bit-shuffle, so the same channel
burst hits different code positions in each copy. When we add the
soft LLRs before Viterbi, error patterns average out and the good
bits reinforce each other -- classical soft-combining diversity.
"""

from __future__ import annotations

import numpy as np
import pytest

from weaklink.modem.codec import ModemConfig, decode, encode
from weaklink.modem.waveform import WaveformConfig


def _cfg(block_repeats: int) -> ModemConfig:
    return ModemConfig(
        waveform=WaveformConfig(baud=300.0, tone_spacing_hz=300.0),
        rs_data_bytes=16,
        rs_parity_bytes=8,
        block_repeats=block_repeats,
    )


def _awgn(audio: np.ndarray, snr_db: float, seed: int, sample_rate: float = 18_000.0) -> np.ndarray:
    sig_p = float(np.mean(audio.astype(np.float64) ** 2))
    noise_variance = sig_p * sample_rate / (2.0 * 3000.0) / (10 ** (snr_db / 10.0))
    rng = np.random.default_rng(seed)
    return (audio + rng.standard_normal(audio.size).astype(np.float32) * np.sqrt(noise_variance)).astype(np.float32)


@pytest.mark.parametrize("block_repeats", [2, 4])
def test_combined_llrs_decode_below_single_copy_cliff(block_repeats: int) -> None:
    """Payload encoded with ``block_repeats`` copies + AWGN at an SNR that
    a single copy fails at. With per-copy permutations + soft-combining,
    at least some blocks in the message recover -- test that the whole
    payload comes through byte-for-byte."""
    payload = np.random.default_rng(block_repeats).bytes(80)
    config = _cfg(block_repeats)
    audio = encode(payload, config)
    # 3 dB below where R=1 would decode -- forces LLR combining to earn its
    # dB. Deterministic seed for repeatability.
    noisy = _awgn(audio, snr_db=-4.0, seed=block_repeats * 101)
    decoded = decode(noisy, config)
    assert decoded == payload, (
        f"R={block_repeats}: expected {len(payload)} bytes, got {len(decoded)}; "
        f"first diff at byte {next((i for i, (a, b) in enumerate(zip(decoded, payload)) if a != b), 'n/a')}"
    )


# --- e2e streaming variant --------------------------------------------------

from ._streaming import stream_decode


@pytest.mark.parametrize("block_repeats", [2, 4])
def test_combined_llrs_decode_below_single_copy_cliff_e2e_streaming(
    block_repeats: int,
) -> None:
    payload = np.random.default_rng(block_repeats).bytes(80)
    config = _cfg(block_repeats)
    audio = encode(payload, config)
    noisy = _awgn(audio, snr_db=-4.0, seed=block_repeats * 101)
    decoded = stream_decode(noisy, config)
    assert decoded == payload, (
        f"R={block_repeats} (streaming): expected {len(payload)} bytes, got {len(decoded)}"
    )
