"""Confirm the modem runs the weak-signal preset across baud rates unchanged.

Only ``--baud`` (and matching tone spacing) varies. Everything else — preamble
length, RS block size, repetition count, interleaver, FEC — stays fixed.

Also verifies drift tolerance at 100 ppm relative clock error, which is a
typical soundcard-vs-soundcard mismatch.
"""

from __future__ import annotations

import random
import string
from dataclasses import replace

import numpy as np
import pytest

from weaklink.modem.codec import ModemConfig, decode, encode
from weaklink.modem.waveform import WaveformConfig


BAUDS_TO_TEST = [45, 100, 300, 500, 700]  # SSB channel-friendly: BW = 4 * baud <= 2800 Hz


def _preset(baud: float) -> ModemConfig:
    return ModemConfig(
        waveform=WaveformConfig(baud=baud, tone_spacing_hz=baud),
        preamble_length=64,
        payload_repeats=3,
        rs_data_bytes=16,
        rs_parity_bytes=8,
        rs_crc_enabled=True,
    )


def _payload() -> bytes:
    alphabet = (string.ascii_letters + string.digits + " ").encode("ascii")
    return bytes(random.Random(1).choices(alphabet, k=15))


@pytest.mark.parametrize("baud", BAUDS_TO_TEST)
def test_clean_decode_at_each_baud(baud: int) -> None:
    config = _preset(baud)
    payload = _payload()
    samples = encode(payload, config)
    assert decode(samples, config, payload_length_bytes=15) == payload


@pytest.mark.parametrize("baud", BAUDS_TO_TEST)
def test_survives_100ppm_soundcard_clock_mismatch(baud: int) -> None:
    """TX and RX soundcards differ by 100 ppm — a typical consumer-grade mismatch.

    Simulated by encoding at a slightly higher sample rate than the RX config
    assumes; the received samples then have symbol boundaries 100 ppm off from
    where RX expects them.
    """
    rx_config = _preset(baud)
    tx_config = replace(
        rx_config,
        waveform=replace(rx_config.waveform, sample_rate=rx_config.waveform.sample_rate * (1 + 100e-6)),
    )
    payload = _payload()
    samples = encode(payload, tx_config)
    assert decode(samples, rx_config, payload_length_bytes=15) == payload


def test_4fsk_in_ssb_bandwidth_note() -> None:
    """The 4-FSK stack at 700 baud sits within a 3 kHz SSB passband."""
    config = _preset(700)
    total_tone_stack = 4 * config.waveform.tone_spacing_hz
    assert total_tone_stack <= 2800, (
        f"tone stack {total_tone_stack} Hz overflows SSB passband; above 700 baud "
        "you either need a wider channel or BFSK"
    )
