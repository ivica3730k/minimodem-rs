"""Streaming rx must decode when the pump is fed silence *before* the
signal arrives -- which is exactly what happens live (rx starts
listening, then tx fires later).

The bug this guards against: the streaming coarse-offset cache in
``decode()`` used to be populated on the first call regardless of
whether preambles were actually found. First call on silence would
FFT the noise floor, cache a garbage offset, and stick to it forever
-- so when real signal arrived on later polls, the correlator was
looking at the wrong tone offsets and never locked on.
"""

from __future__ import annotations

import io

import numpy as np
import pytest

from weaklink.modem.cli import BAUD_PRESETS, _StreamingRxPump
from weaklink.modem.codec import ModemConfig, encode
from weaklink.modem.waveform import WaveformConfig


def _cfg(baud: float) -> ModemConfig:
    preset = BAUD_PRESETS[baud]
    return ModemConfig(
        waveform=WaveformConfig(baud=baud, tone_spacing_hz=preset["tone_spacing_hz"]),
        rs_data_bytes=int(preset["rs_data_bytes"]),
        rs_parity_bytes=int(preset["rs_parity_bytes"]),
        block_repeats=int(preset["block_repeats"]),
    )


def _push_chunks(pump: _StreamingRxPump, audio: np.ndarray, chunk_samples: int) -> None:
    for start in range(0, audio.size, chunk_samples):
        pump.push(audio[start : start + chunk_samples].astype(np.float32))


@pytest.mark.parametrize("baud", [300.0, 1200.0])
def test_pump_decodes_after_leading_silence(baud: float) -> None:
    """Feed the pump seconds of pure silence (mimics rx started before tx),
    then real encoded audio; assert the payload decodes byte-for-byte."""
    config = _cfg(baud)
    payload = b"weaklink starts on silence and still decodes"
    signal = encode(payload, config)
    sr = int(round(config.waveform.sample_rate))

    # 3 seconds of silence before the signal. Plenty for the pump to
    # attempt at least one decode call on silence-only audio.
    silence_seconds = 3.0
    silence = np.zeros(int(silence_seconds * sr), dtype=np.float32)

    out = io.BytesIO()
    pump = _StreamingRxPump(config, output=out)
    chunk = int(0.1 * sr)  # ~100 ms chunks, matches the CLI poll cadence
    _push_chunks(pump, silence, chunk)
    _push_chunks(pump, signal, chunk)
    pump.drain()

    got = out.getvalue()
    assert payload in got, (
        f"baud={baud}: expected payload after leading silence, got {got!r}"
    )


@pytest.mark.parametrize("baud", [300.0, 1200.0])
def test_pump_decodes_after_leading_low_noise(baud: float) -> None:
    """Same as above but with a low-level noise floor instead of pure
    silence -- closer to what a live mic / Pulse monitor delivers when
    no signal is playing."""
    config = _cfg(baud)
    payload = b"weaklink starts on room noise"
    signal = encode(payload, config)
    sr = int(round(config.waveform.sample_rate))

    silence_seconds = 3.0
    rng = np.random.default_rng(int(baud))
    noise = (rng.standard_normal(int(silence_seconds * sr)) * 0.005).astype(np.float32)

    out = io.BytesIO()
    pump = _StreamingRxPump(config, output=out)
    chunk = int(0.1 * sr)
    _push_chunks(pump, noise, chunk)
    _push_chunks(pump, signal, chunk)
    pump.drain()

    got = out.getvalue()
    assert payload in got, (
        f"baud={baud}: expected payload after leading noise, got {got!r}"
    )
