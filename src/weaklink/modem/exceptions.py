"""Public exception hierarchy for weaklink.modem.

Library callers catch these; the CLI wraps them into SystemExit(2)
with the message rendered as ``error: <message>`` so shell users see
a clean line instead of a traceback.
"""

from __future__ import annotations


class WeaklinkError(Exception):
    """Base for anything the modem raises. Catch this to catch them all."""


class ConfigError(WeaklinkError):
    """Invalid configuration -- baud not supported, tone count not a
    power of 2, block_repeats < 1, PTT endpoint malformed, etc."""


class NyquistError(ConfigError):
    """MFSK tone stack won't fit under sample_rate/2, or spacing is
    below the non-coherent orthogonality floor."""


class EncodeError(WeaklinkError):
    """Encoder-side failure -- e.g. stream exceeded MAX_BLOCK_INDEX
    (2^16 slots per session)."""


class PTTError(WeaklinkError):
    """rigctld connect / response failure."""
