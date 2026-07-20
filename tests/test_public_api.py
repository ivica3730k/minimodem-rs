"""Direct tests for the ``weaklink.modem`` public API surface. Anything
tested here is part of the 1.x compatibility contract; changing the
signature or the exception type these assert against is a breaking
change."""

from __future__ import annotations

import logging

import numpy as np
import pytest

from weaklink.modem import (
    BAUD_PRESETS,
    ConfigError,
    ModemOptions,
    NyquistError,
    WeaklinkError,
    build_config,
    rx,
    tx,
)


def test_bytes_in_bytes_out_roundtrip() -> None:
    payload = b"weaklink public API roundtrip"
    opts = ModemOptions(baud=300)
    audio = tx(payload, opts)
    assert audio.dtype == np.float32
    assert audio.ndim == 1
    assert rx(audio, opts) == payload


def test_preset_defaults_land() -> None:
    cfg = build_config(ModemOptions(baud=300))
    assert cfg.rs_data_bytes == int(BAUD_PRESETS[300.0]["rs_data_bytes"])
    assert cfg.block_repeats == int(BAUD_PRESETS[300.0]["block_repeats"])
    assert cfg.waveform.tone_spacing_hz == BAUD_PRESETS[300.0]["tone_spacing_hz"]


def test_tx_volume_scales_peak_amplitude() -> None:
    payload = b"loud vs quiet"
    opts = ModemOptions(baud=300)
    loud = tx(payload, opts, tx_volume=100)
    quiet = tx(payload, opts, tx_volume=25)
    assert abs(float(np.max(np.abs(loud))) - 1.0) < 0.01
    assert abs(float(np.max(np.abs(quiet))) - 0.25) < 0.01


def test_unsupported_baud_raises_config_error() -> None:
    with pytest.raises(ConfigError):
        tx(b"x", ModemOptions(baud=888))


def test_infeasible_num_tones_raises_nyquist_error() -> None:
    # 16 tones at 1200 baud puts the top tone above Nyquist.
    with pytest.raises(NyquistError):
        tx(b"x", ModemOptions(baud=1200, num_tones=16))


def test_all_exceptions_share_weaklink_base() -> None:
    with pytest.raises(WeaklinkError):
        tx(b"x", ModemOptions(baud=888))


def test_logger_injection_routes_weaklink_records() -> None:
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    logger = logging.getLogger("test.injected")
    logger.setLevel(logging.DEBUG)
    handler = _Capture()
    logger.addHandler(handler)
    try:
        opts = ModemOptions(baud=300)
        audio = tx(b"logger-injection test", opts, logger=logger)
        rx(audio, opts, logger=logger)
    finally:
        logger.removeHandler(handler)
    assert captured, "expected weaklink diagnostics to route through injected logger"
    assert all(r.name.startswith("weaklink.") for r in captured)


def test_baud_preset_table_is_frozen() -> None:
    # Adding / renaming supported bauds is a breaking change: catch
    # accidental edits.
    assert set(BAUD_PRESETS.keys()) == {45.0, 300.0, 1200.0}
