"""One rx session watches while 10 tx sessions fire in sequence.

Reproduces what a live listener sees when the same rx pipe is left
open while multiple independent ``tx`` invocations run one after
another. Each tx buffer carries its own pilot padding (matching what
the CLI writes to the audio device), so preambles between adjacent
sessions are separated by real silence in the sample stream, not
smashed together.

Assertion: every payload decodes, in order, with no missing bytes.
"""

from __future__ import annotations

import random
import string

import numpy as np
import pytest

from tests.test_wav_damage import _live_tx_buffer
from weaklink.modem.api import BAUD_PRESETS
from weaklink.modem.codec import ModemConfig, decode
from weaklink.modem.waveform import WaveformConfig

from ._streaming import stream_decode


def _config_for(baud: int) -> ModemConfig:
    preset = BAUD_PRESETS[float(baud)]
    return ModemConfig(
        waveform=WaveformConfig(baud=float(baud), tone_spacing_hz=preset["tone_spacing_hz"]),
        rs_data_bytes=int(preset["rs_data_bytes"]),
        rs_parity_bytes=int(preset["rs_parity_bytes"]),
        block_repeats=int(preset["block_repeats"]),
        sync_every_blocks=int(preset["sync_every_blocks"]),
    )


@pytest.mark.parametrize("baud", [45, 300, 1200])
def test_10_sequential_tx_all_decode_in_order(baud: int) -> None:
    """10 sequential tx sessions of random 1-20-char payloads all decode, in order."""
    config = _config_for(baud)
    rng = random.Random(42 + baud)
    alphabet = (string.ascii_letters + string.digits + " ").encode("ascii")

    payloads = [
        bytes(rng.choice(alphabet) for _ in range(rng.randint(1, 20)))
        for _ in range(10)
    ]
    expected = b"".join(payloads)

    # Each tx buffer includes the CLI's pilot-padding, so back-to-back
    # concatenation has natural silence between preambles.
    audio_pieces = [_live_tx_buffer(baud, p)[0] for p in payloads]
    combined = np.concatenate(audio_pieces).astype(np.float32)

    decoded = decode(combined, config)
    assert decoded == expected, (
        f"{baud} baud: expected {expected!r}, got {decoded!r}\n"
        f"payloads were: {payloads}"
    )


@pytest.mark.parametrize("baud", [300, 1200])
def test_10_sequential_tx_all_decode_in_order_e2e_streaming(baud: int) -> None:
    """Same as above but fed through ``_StreamingRxDecoder`` -- catches
    streaming-only bugs like the cross-call block-dedup regression that
    the batch test can't see. 45 baud omitted (too slow for CI)."""
    config = _config_for(baud)
    rng = random.Random(42 + baud)
    alphabet = (string.ascii_letters + string.digits + " ").encode("ascii")

    payloads = [
        bytes(rng.choice(alphabet) for _ in range(rng.randint(1, 20)))
        for _ in range(10)
    ]
    expected = b"".join(payloads)

    audio_pieces = [_live_tx_buffer(baud, p)[0] for p in payloads]
    combined = np.concatenate(audio_pieces).astype(np.float32)

    decoded = stream_decode(combined, config)
    assert decoded == expected, (
        f"{baud} baud (streaming): expected {expected!r}, got {decoded!r}\n"
        f"payloads were: {payloads}"
    )
