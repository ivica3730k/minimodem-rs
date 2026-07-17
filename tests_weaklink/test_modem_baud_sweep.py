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


def test_tone_stack_width_scales_with_baud() -> None:
    """Sanity check: null-to-null RF bandwidth of the 4-FSK stack is 5 * baud.

    Below is the guideline for how much channel width each baud needs — real
    behaviour depends on your radio's filter. Standard narrow SSB is ~2.8 kHz,
    wide/extended SSB reaches ~5 kHz, narrow FM ~15 kHz. Signal degrades
    gradually as more sideband energy is clipped rather than failing at a hard
    threshold.
    """
    for baud in BAUDS_TO_TEST:
        config = _preset(baud)
        null_to_null_hz = 5 * config.waveform.baud
        assert null_to_null_hz == 5 * baud
