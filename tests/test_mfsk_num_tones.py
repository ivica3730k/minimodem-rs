"""``--modem-num-tones`` roundtrip. 2-FSK / 4-FSK / 8-FSK all encode
and decode byte-for-byte at every supported baud."""

from __future__ import annotations

import pytest

from weaklink.modem.codec import ModemConfig, decode, encode
from weaklink.modem.exceptions import ConfigError
from weaklink.modem.waveform import WaveformConfig


@pytest.mark.parametrize("num_tones", [2, 4, 8, 16])
@pytest.mark.parametrize("baud", [45.0, 300.0, 1200.0])
def test_mfsk_roundtrip(num_tones: int, baud: float) -> None:
    """Clean encode → decode roundtrip for every (baud, num_tones) combo
    that actually fits within Nyquist. Combos that don't fit raise at
    ``WaveformConfig`` construction; we skip those."""
    try:
        waveform = WaveformConfig(
            baud=baud, tone_spacing_hz=baud, num_tones=num_tones,
        )
    except (ValueError, ConfigError) as e:
        pytest.skip(str(e))
    config = ModemConfig(
        waveform=waveform,
        rs_data_bytes=16, rs_parity_bytes=8, block_repeats=1,
    )
    payload = b"weaklink at " + str(num_tones).encode() + b"-FSK"
    audio = encode(payload, config)
    decoded = decode(audio, config)
    assert decoded == payload, (
        f"{num_tones}-FSK @ {baud} baud: got {decoded!r}, expected {payload!r}"
    )
