"""Shared helper: drive audio through ``_StreamingRxPump`` in fixed-size
chunks and return the decoded bytes.

Every batch-mode ``decode(samples, config)`` test has a companion e2e
test elsewhere in this suite that uses ``pump_decode`` instead -- same
audio, same assertion, but the audio flows through the live pipeline
(chunked, cross-call state, drain). Bugs that only surface in the
live path get caught by CI without needing a real audio device.
"""

from __future__ import annotations

import io

import numpy as np

from weaklink.modem.cli import _StreamingRxPump
from weaklink.modem.codec import ModemConfig


def pump_decode(
    audio: np.ndarray,
    config: ModemConfig,
    *,
    chunk_seconds: float = 0.1,
) -> bytes:
    """Push ``audio`` through ``_StreamingRxPump`` in ``chunk_seconds``-
    sized chunks, drain at end, return the decoded byte stream."""
    out = io.BytesIO()
    pump = _StreamingRxPump(config, output=out)
    audio32 = np.asarray(audio, dtype=np.float32)
    chunk_samples = max(1, int(chunk_seconds * config.waveform.sample_rate))
    for start in range(0, audio32.size, chunk_samples):
        pump.push(audio32[start : start + chunk_samples])
    pump.drain()
    return out.getvalue()
