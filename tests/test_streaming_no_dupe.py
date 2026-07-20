"""Streaming rx must not emit the same block twice when its copies
straddle a poll boundary. Uses ``_StreamingRxDecoder`` (the same class
the CLI drives from both live audio and WAV) so the test exercises
the exact code path.
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest

from weaklink.modem.api import BAUD_PRESETS
from weaklink.modem.codec import ModemConfig, encode
from weaklink.modem.streaming import StreamingRxDecoder as _StreamingRxDecoder
from weaklink.modem.waveform import WaveformConfig


def _cfg(baud: float) -> ModemConfig:
    preset = BAUD_PRESETS[baud]
    return ModemConfig(
        waveform=WaveformConfig(baud=baud, tone_spacing_hz=preset["tone_spacing_hz"]),
        rs_data_bytes=int(preset["rs_data_bytes"]),
        rs_parity_bytes=int(preset["rs_parity_bytes"]),
        block_repeats=int(preset["block_repeats"]),
    )


def _readme_head(n_lines: int) -> bytes:
    readme = Path(__file__).resolve().parent.parent / "README.md"
    lines = readme.read_text().splitlines(keepends=True)
    return "".join(lines[:n_lines]).encode()


def _pump_chunked(
    audio: np.ndarray, config: ModemConfig, chunk_samples: int,
) -> bytes:
    """Drive ``_StreamingRxDecoder`` with fixed-size audio chunks, exactly
    like the CLI's live-rx callback or WAV chunk iterator do."""
    out = io.BytesIO()
    pump = _StreamingRxDecoder(config, output=out)
    for start in range(0, audio.size, chunk_samples):
        pump.push(audio[start : start + chunk_samples].astype(np.float32))
    pump.drain()
    return out.getvalue()


@pytest.mark.parametrize("baud", [300.0, 1200.0])
def test_readme_head_streams_without_dupes(baud: float) -> None:
    """First 10 lines of README fed as encoded audio + chunked pump.
    Must decode to the exact payload -- no duplicated blocks even
    though ``block_repeats > 1`` means multiple copies of each block."""
    payload = _readme_head(10)
    config = _cfg(baud)
    audio = encode(payload, config)
    chunk = int(0.1 * config.waveform.sample_rate)
    got = _pump_chunked(audio, config, chunk_samples=chunk)
    assert got == payload, (
        f"baud={baud}: got {len(got)} B, expected {len(payload)} B; "
        f"first diff at "
        f"{next((i for i, (a, b) in enumerate(zip(got, payload)) if a != b), 'n/a')}"
    )
