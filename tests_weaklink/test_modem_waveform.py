"""4-FSK waveform + soft demod unit tests."""

from __future__ import annotations

import numpy as np
import pytest

from weaklink.modem.waveform import (
    BITS_PER_SYMBOL,
    NUM_TONES,
    WaveformConfig,
    bits_to_symbols,
    demodulate_soft,
    modulate,
    soft_bits_from_magnitudes,
    symbols_to_bits,
)


def test_bits_symbols_roundtrip() -> None:
    rng = np.random.default_rng(0)
    bits = bytes(rng.integers(0, 2, size=200 * BITS_PER_SYMBOL).tolist())
    symbols = bits_to_symbols(bits)
    assert symbols.max() < NUM_TONES
    assert symbols.min() >= 0
    assert symbols_to_bits(symbols) == bits


def test_bits_to_symbols_rejects_odd_length() -> None:
    with pytest.raises(ValueError):
        bits_to_symbols(bytes([1, 0, 1]))


def test_modulate_produces_expected_length() -> None:
    config = WaveformConfig(baud=300.0, sample_rate=48_000.0)
    symbols = np.asarray([0, 1, 2, 3, 0], dtype=np.int8)
    samples = modulate(symbols, config)
    assert len(samples) == len(symbols) * config.samples_per_symbol
    assert samples.dtype == np.float32
    assert np.max(np.abs(samples)) <= config.amplitude + 1e-6


def test_demodulate_recovers_symbols_clean_channel() -> None:
    config = WaveformConfig(baud=300.0, sample_rate=48_000.0)
    original_symbols = np.asarray([0, 1, 2, 3, 3, 2, 1, 0, 2, 1, 3, 0], dtype=np.int8)
    samples = modulate(original_symbols, config)
    magnitudes = demodulate_soft(samples, config)
    assert magnitudes.shape == (len(original_symbols), NUM_TONES)
    recovered = np.argmax(magnitudes, axis=1)
    np.testing.assert_array_equal(recovered, original_symbols)


def test_soft_bits_sign_matches_transmitted_bits() -> None:
    """Positive LLR-shape means bit 0; negative means bit 1."""
    config = WaveformConfig(baud=300.0, sample_rate=48_000.0)
    # 4 symbols cover all 4 bit patterns: 00, 01, 11, 10.
    symbols = np.asarray([0, 1, 2, 3], dtype=np.int8)
    samples = modulate(symbols, config)
    magnitudes = demodulate_soft(samples, config)
    soft = soft_bits_from_magnitudes(magnitudes)
    hard = (soft < 0).astype(int)
    expected = np.asarray([0, 0, 0, 1, 1, 1, 1, 0], dtype=int)  # bits for symbols 0..3 in Gray order
    np.testing.assert_array_equal(hard, expected)
