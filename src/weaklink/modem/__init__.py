"""Weaklink modem: MFSK + convolutional FEC + soft Viterbi, audio I/O."""

from weaklink.modem.api import ModemOptions, build_config, rx, tx
from weaklink.modem.codec import ModemConfig
from weaklink.modem.constants import BAUD_PRESETS
from weaklink.modem.exceptions import (
    ConfigError,
    EncodeError,
    NyquistError,
    PTTError,
    WeaklinkError,
)
from weaklink.modem.waveform import WaveformConfig

__all__ = [
    # Public API
    "tx",
    "rx",
    "build_config",
    "BAUD_PRESETS",
    # Config objects
    "ModemOptions",
    "ModemConfig",
    "WaveformConfig",
    # Exceptions
    "WeaklinkError",
    "ConfigError",
    "NyquistError",
    "EncodeError",
    "PTTError",
]
