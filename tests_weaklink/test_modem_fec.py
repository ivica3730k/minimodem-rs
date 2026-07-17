"""Convolutional encoder + soft Viterbi tests."""

from __future__ import annotations

import numpy as np
import pytest

from weaklink.modem import fec


def _soft_from_bits(bits: bytes) -> np.ndarray:
    return np.asarray([1.0 if b == 0 else -1.0 for b in bits], dtype=np.float64)


def test_encode_produces_expected_length() -> None:
    bits = bytes([1, 0, 1, 1, 0, 0, 1, 0])
    coded = fec.encode(bits)
    assert len(coded) == 2 * (len(bits) + fec.CONSTRAINT_LENGTH - 1)


def test_encode_all_zeros_gives_all_zeros() -> None:
    bits = bytes(20)
    assert fec.encode(bits) == bytes(2 * (20 + fec.CONSTRAINT_LENGTH - 1))


@pytest.mark.parametrize("length", [1, 8, 32, 128])
def test_viterbi_roundtrip_clean(length: int) -> None:
    rng = np.random.default_rng(length)
    bits = bytes(rng.integers(0, 2, size=length).tolist())
    soft = _soft_from_bits(fec.encode(bits))
    assert fec.decode(soft, num_output_bits=length) == bits


def test_viterbi_corrects_a_few_bit_flips() -> None:
    """Rate-1/2 K=7 should shrug off scattered errors easily."""
    rng = np.random.default_rng(42)
    bits = bytes(rng.integers(0, 2, size=64).tolist())
    coded = fec.encode(bits)
    coded_list = list(coded)
    # Flip 3 random coded bits out of ~140.
    for pos in rng.choice(len(coded_list), size=3, replace=False):
        coded_list[pos] ^= 1
    soft = _soft_from_bits(bytes(coded_list))
    assert fec.decode(soft, num_output_bits=64) == bits


def test_decode_rejects_mismatched_length() -> None:
    with pytest.raises(ValueError):
        fec.decode(np.zeros(10), num_output_bits=8)
