"""Public Python API for weaklink.modem.

Two functions, bytes-in / samples-out and samples-in / bytes-out:

    from weaklink.modem import tx, rx

    audio = tx(b"hello", baud=300)              # ndarray float32
    payload = rx(audio, baud=300)                # bytes

All CLI ``--modem-*`` knobs are exposed as kwargs; unset ones fall
through to the same per-baud presets the CLI uses. Optional ``logger=``
lets callers subscribe to signal-level events (peak/rms, coarse offset,
per-slot decode outcomes, RS corrections) without wiring a file
handler -- when ``None``, the module's default logger is used.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

import numpy as np

from weaklink.modem.codec import ModemConfig, decode as _codec_decode, encode as _codec_encode
from weaklink.modem.exceptions import ConfigError
from weaklink.modem.waveform import WaveformConfig

# Per-baud presets. Kept in sync with weaklink.modem.cli.BAUD_PRESETS
# on purpose -- library callers get the same defaults as CLI users.
BAUD_PRESETS: dict[float, dict[str, float]] = {
    45.0:   dict(tone_spacing_hz=200.0, rs_data_bytes=16, rs_parity_bytes=8,  block_repeats=4, sync_every_blocks=4),
    300.0:  dict(tone_spacing_hz=300.0, rs_data_bytes=16, rs_parity_bytes=8,  block_repeats=2, sync_every_blocks=4),
    1200.0: dict(tone_spacing_hz=1200.0, rs_data_bytes=16, rs_parity_bytes=8, block_repeats=2, sync_every_blocks=4),
}

_LOGGER_NAMES = (
    "weaklink.cli",
    "weaklink.audio",
    "weaklink.modem",
)


@contextmanager
def _routed_loggers(logger: logging.Logger | None) -> Iterator[None]:
    """Temporarily forward the codec/CLI/audio loggers into ``logger``
    while yielding. When ``logger`` is ``None``, this is a no-op."""
    if logger is None:
        yield
        return

    class _Forwarder(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            logger.handle(record)

    handler = _Forwarder()
    targets = [logging.getLogger(name) for name in _LOGGER_NAMES]
    prior = [(t, t.handlers.copy(), t.propagate, t.level) for t in targets]
    try:
        for t in targets:
            t.handlers = [handler]
            t.propagate = False
            if t.level == logging.NOTSET or t.level > logger.level:
                t.setLevel(logger.level or logging.DEBUG)
        yield
    finally:
        for t, orig_handlers, orig_propagate, orig_level in prior:
            t.handlers = orig_handlers
            t.propagate = orig_propagate
            t.setLevel(orig_level)


def _resolve_preset(baud: float) -> dict[str, float]:
    if baud not in BAUD_PRESETS:
        raise ConfigError(
            f"baud {baud} is not supported; use one of {sorted(BAUD_PRESETS.keys())}"
        )
    return BAUD_PRESETS[baud]


def build_config(
    *,
    baud: float = 300.0,
    num_tones: int = 4,
    rs_data_bytes: int | None = None,
    rs_parity_bytes: int | None = None,
    rs_crc_enabled: bool = True,
    block_repeats: int | None = None,
    sync_every_blocks: int | None = None,
    tone_spacing_hz: float | None = None,
    tx_volume: int = 100,
) -> ModemConfig:
    """Assemble a ``ModemConfig`` from CLI-equivalent parameters,
    filling unset preset-driven knobs from ``BAUD_PRESETS``. Same
    resolution the CLI does."""
    preset = _resolve_preset(baud)
    if not 0 <= tx_volume <= 100:
        raise ConfigError(f"tx_volume must be 0-100 (got {tx_volume})")
    return ModemConfig(
        waveform=WaveformConfig(
            baud=baud,
            tone_spacing_hz=tone_spacing_hz if tone_spacing_hz is not None else preset["tone_spacing_hz"],
            num_tones=num_tones,
            amplitude=tx_volume / 100.0,
        ),
        rs_data_bytes=rs_data_bytes if rs_data_bytes is not None else int(preset["rs_data_bytes"]),
        rs_parity_bytes=rs_parity_bytes if rs_parity_bytes is not None else int(preset["rs_parity_bytes"]),
        rs_crc_enabled=rs_crc_enabled,
        sync_every_blocks=sync_every_blocks if sync_every_blocks is not None else int(preset["sync_every_blocks"]),
        block_repeats=block_repeats if block_repeats is not None else int(preset["block_repeats"]),
    )


def tx(
    data: bytes,
    *,
    baud: float = 300.0,
    num_tones: int = 4,
    rs_data_bytes: int | None = None,
    rs_parity_bytes: int | None = None,
    rs_crc_enabled: bool = True,
    block_repeats: int | None = None,
    sync_every_blocks: int | None = None,
    tone_spacing_hz: float | None = None,
    tx_volume: int = 100,
    logger: logging.Logger | None = None,
) -> np.ndarray:
    """Encode ``data`` (bytes) to float32 audio samples.

    All keyword arguments mirror CLI ``--modem-*`` flags. ``logger``,
    if provided, receives diagnostic events for this call.

    Returns a 1-D ``numpy.ndarray`` of ``float32``, peak in
    ``[-tx_volume/100, +tx_volume/100]``. Sample rate is
    ``config.waveform.sample_rate`` (18 kHz).
    """
    config = build_config(
        baud=baud,
        num_tones=num_tones,
        rs_data_bytes=rs_data_bytes,
        rs_parity_bytes=rs_parity_bytes,
        rs_crc_enabled=rs_crc_enabled,
        block_repeats=block_repeats,
        sync_every_blocks=sync_every_blocks,
        tone_spacing_hz=tone_spacing_hz,
        tx_volume=tx_volume,
    )
    with _routed_loggers(logger):
        return _codec_encode(data, config)


def rx(
    samples: np.ndarray,
    *,
    baud: float = 300.0,
    num_tones: int = 4,
    rs_data_bytes: int | None = None,
    rs_parity_bytes: int | None = None,
    rs_crc_enabled: bool = True,
    block_repeats: int | None = None,
    sync_every_blocks: int | None = None,
    tone_spacing_hz: float | None = None,
    logger: logging.Logger | None = None,
) -> bytes:
    """Decode float32 audio ``samples`` to bytes.

    Same parameters as :func:`tx` less ``tx_volume`` (amplitude is
    irrelevant on the RX side -- the correlator is amplitude-normalised).
    ``logger``, if provided, receives diagnostic events for this call.
    """
    config = build_config(
        baud=baud,
        num_tones=num_tones,
        rs_data_bytes=rs_data_bytes,
        rs_parity_bytes=rs_parity_bytes,
        rs_crc_enabled=rs_crc_enabled,
        block_repeats=block_repeats,
        sync_every_blocks=sync_every_blocks,
        tone_spacing_hz=tone_spacing_hz,
    )
    with _routed_loggers(logger):
        return _codec_decode(samples, config)
