"""Regression tests for the preamble correlator + signal-presence gate.

Guards against three bugs we've hit:
  1. On pure-noise buffers, the old ``threshold = 0.7 * max(scores)`` rule
     accepted a single noise extremum, returning ``[1 peak at 0]``. Live rx
     then got stuck at the same cursor position and never advanced.
  2. A strong buffer-edge transient (e.g. mic startup click) would raise the
     ratio threshold above every real preamble that followed, so nothing
     could be decoded until the transient rolled out of the buffer.
  3. A short synthetic buffer with only random data (no preamble) must not
     produce false-positive "sync markers".
"""

from __future__ import annotations

import numpy as np
import pytest

from weaklink.modem.codec import (
    ModemConfig,
    _find_preamble_peaks,
    decode,
    encode,
    preamble_symbols,
)
from weaklink.modem.waveform import WaveformConfig, demodulate_soft


@pytest.fixture()
def config() -> ModemConfig:
    return ModemConfig(waveform=WaveformConfig(baud=300.0, tone_spacing_hz=300.0))


def test_pure_noise_finds_no_preambles(config: ModemConfig) -> None:
    rng = np.random.default_rng(0)
    # 5 s of Gaussian noise at typical mic-input amplitude.
    noise = rng.standard_normal(int(5 * config.waveform.sample_rate)).astype(np.float32) * 0.05
    magnitudes = demodulate_soft(noise, config.waveform)
    peaks = _find_preamble_peaks(magnitudes, preamble_symbols(), config)
    assert peaks == []


def test_streaming_pure_noise_returns_empty_and_no_cursor_advance(config: ModemConfig) -> None:
    """Live rx invariant: a pure-noise buffer must not advance the cursor by
    a nonzero amount that would trim real audio in the next call."""
    rng = np.random.default_rng(1)
    noise = rng.standard_normal(int(5 * config.waveform.sample_rate)).astype(np.float32) * 0.05
    decoded, safe_cursor = decode(noise, config, streaming=True)
    assert decoded == b""
    assert safe_cursor == 0


def test_edge_transient_before_signal_does_not_mask_it(config: ModemConfig) -> None:
    """A loud transient at buffer start (mic click, keyboard bump) must not
    stop real preambles that follow from being detected."""
    rng = np.random.default_rng(2)
    real = encode(b"hello weaklink", config)
    # 0.3 s of loud broadband noise, then the real signal, then trailing silence.
    lead_len = int(0.3 * config.waveform.sample_rate)
    lead = rng.standard_normal(lead_len).astype(np.float32) * 0.9  # loud transient
    tail = np.zeros(int(0.5 * config.waveform.sample_rate), dtype=np.float32)
    buffer = np.concatenate([lead, real, tail])
    decoded = decode(buffer, config)
    assert b"hello weaklink" in decoded


def test_random_data_without_preamble_produces_no_false_peaks(config: ModemConfig) -> None:
    """Modulated random symbols (i.e. valid audio that isn't a preamble)
    must not correlate as a preamble somewhere."""
    from weaklink.modem.waveform import modulate

    rng = np.random.default_rng(3)
    fake_symbols = rng.integers(0, 4, size=1000)
    audio = modulate(fake_symbols, config.waveform)
    magnitudes = demodulate_soft(audio, config.waveform)
    peaks = _find_preamble_peaks(magnitudes, preamble_symbols(), config)
    # Random data has zero expected correlation to the fixed preamble PN
    # sequence, but variance is nonzero -- allow at most one spurious hit.
    assert len(peaks) <= 1, f"expected <=1 spurious peak on random data, got {peaks}"


@pytest.mark.parametrize("baud", [45, 300])
def test_decode_under_10db_slow_fading(baud: int) -> None:
    """10 dB peak-to-trough sinusoidal fading -- amplitude ranges 0.316× to 1.0×
    across the transmission. The amplitude-normalised correlator must still
    find every real preamble because the score is scale-invariant.

    Preset lifted from ``weaklink.modem.cli.BAUD_PRESETS``."""
    presets = {
        45:  dict(rs_data_bytes=32, rs_parity_bytes=8, block_repeats=2, sync_every_blocks=4),
        300: dict(rs_data_bytes=16, rs_parity_bytes=8, block_repeats=1, sync_every_blocks=4),
    }
    from weaklink.modem.codec import encode, decode

    config = ModemConfig(
        waveform=WaveformConfig(baud=float(baud), tone_spacing_hz=float(baud)),
        **presets[baud],
    )
    payload = b"weaklink fading test payload 12345678 abcdef"
    signal = encode(payload, config)
    duration = len(signal) / config.waveform.sample_rate
    t = np.arange(len(signal)) / config.waveform.sample_rate
    # A few fade cycles across the burst so different preambles hit different
    # fade phases.
    period = max(duration / 2.5, 1.0)
    envelope = 0.316 + 0.684 * (0.5 + 0.5 * np.cos(2 * np.pi * t / period))
    faded = (signal * envelope).astype(np.float32)
    rng = np.random.default_rng(0)
    sig_p = float(np.mean(faded.astype(np.float64) ** 2))
    noise = rng.standard_normal(len(faded)).astype(np.float32) * np.sqrt(sig_p * 10 ** (-5 / 10))
    decoded = decode(faded + noise, config)
    assert payload in decoded, f"{baud} baud + 10 dB fade + 5 dB SNR: {decoded[:80]!r}"
