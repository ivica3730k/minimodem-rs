"""Interleaver tests."""

from __future__ import annotations

import numpy as np
import pytest

from weaklink.modem.interleaver import InterleaverConfig, deinterleave_soft, interleave


def test_roundtrip_hard_bits() -> None:
    config = InterleaverConfig(rows=4, cols=8)
    rng = np.random.default_rng(0)
    bits = bytes(rng.integers(0, 2, size=config.block_size).tolist())
    interleaved = interleave(bits, config)
    soft = np.asarray([1.0 if b == 0 else -1.0 for b in interleaved])
    recovered = deinterleave_soft(soft, config, output_length=len(bits))
    hard = (recovered < 0).astype(np.int8)
    np.testing.assert_array_equal(hard, np.frombuffer(bits, dtype=np.int8))


def test_roundtrip_with_padding() -> None:
    config = InterleaverConfig(rows=4, cols=8)
    bits = bytes([1, 0, 1, 1, 0])  # not a multiple of 32
    interleaved = interleave(bits, config)
    assert len(interleaved) == config.block_size
    soft = np.asarray([1.0 if b == 0 else -1.0 for b in interleaved])
    recovered = deinterleave_soft(soft, config, output_length=len(bits))
    hard = (recovered < 0).astype(np.int8)
    np.testing.assert_array_equal(hard, np.frombuffer(bits, dtype=np.int8))


def test_deinterleave_rejects_short_stream() -> None:
    config = InterleaverConfig(rows=4, cols=8)
    with pytest.raises(ValueError):
        deinterleave_soft(np.zeros(10), config, output_length=32)
