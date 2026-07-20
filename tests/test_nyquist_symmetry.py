"""Nyquist + spacing guardrail fires at WaveformConfig construction --
same code path for TX and RX, so both sides raise the same error for
infeasible configs. Regression against a report that only one side
enforced it."""

from __future__ import annotations

import pytest

from weaklink.modem.codec import ModemConfig
from weaklink.modem.exceptions import NyquistError
from weaklink.modem.waveform import WaveformConfig


def _infeasible() -> WaveformConfig:
    # 32 tones at 300 baud with 300 Hz spacing puts the top tone above
    # Nyquist (9 kHz internal). Construction must raise -- test can't
    # actually build the config, so we assert the raise here and use a
    # feasible config to build ModemConfig for the encode/decode legs.
    return WaveformConfig(baud=300, tone_spacing_hz=300, num_tones=32)


def test_infeasible_config_raises_at_construction() -> None:
    with pytest.raises(NyquistError):
        _infeasible()


def test_encode_side_bubbles_up_config_error() -> None:
    # Any encode() call inherits its Nyquist gate from WaveformConfig.
    with pytest.raises(NyquistError):
        ModemConfig(waveform=_infeasible())


def test_decode_side_bubbles_up_config_error() -> None:
    # decode() takes a ModemConfig; building that also constructs the
    # WaveformConfig, so the same gate fires.
    with pytest.raises(NyquistError):
        ModemConfig(waveform=_infeasible())
