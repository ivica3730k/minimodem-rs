"""End-to-end WAV robustness tests: encode -> corrupt -> decode.

Simulates the acoustic imperfections that keep breaking real live-audio
tx/rx setups, without needing PulseAudio or a running audio server:

* **Head chop** -- RX started after TX did, so the leading pilot + first
  N ms of modem signal never made it into the RX buffer.
* **Tail chop** -- RX Ctrl-C'd or the sink under-ran before the trailing
  pilot flushed; last N ms are missing.
* **Slow fading** -- sinusoidal amplitude envelope varying 10 dB
  peak-to-trough across the transmission; every preamble sees a
  different fade phase.
* **Compound damage** -- head chop + fade at once, the worst-plausible
  combination in a real acoustic loop.

Uses the same pilot-padded live-tx buffer the CLI produces, so what
gets damaged is *exactly* what would hit the wire. Portable across
Linux / macOS / CI -- no audio server required.
"""

from __future__ import annotations

import numpy as np
import pytest

from weaklink.modem.cli import (
    BAUD_PRESETS,
    _LIVE_TX_MIN_SECONDS,
    _LIVE_TX_PILOT_MIN_SECONDS,
    _pilot_signal,
)
from weaklink.modem.codec import ModemConfig, decode, encode
from weaklink.modem.waveform import WaveformConfig


PAYLOAD_1B = b"h"
PAYLOAD_10B = b"helloWorld"


def _live_tx_buffer(baud: int, payload: bytes) -> tuple[np.ndarray, ModemConfig]:
    """Reproduce exactly what the CLI writes to the audio device: pilot +
    encoded modem signal + pilot, at the sample rate of the preset."""
    preset = BAUD_PRESETS[float(baud)]
    config = ModemConfig(
        waveform=WaveformConfig(baud=float(baud), tone_spacing_hz=preset["tone_spacing_hz"]),
        rs_data_bytes=int(preset["rs_data_bytes"]),
        rs_parity_bytes=int(preset["rs_parity_bytes"]),
        block_repeats=int(preset["block_repeats"]),
        sync_every_blocks=int(preset["sync_every_blocks"]),
    )
    samples = encode(payload, config)
    sr = config.waveform.sample_rate
    signal_seconds = len(samples) / sr
    pilot_each_side = max(
        _LIVE_TX_PILOT_MIN_SECONDS,
        (_LIVE_TX_MIN_SECONDS - signal_seconds) / 2.0,
    )
    pilot = _pilot_signal(config, pilot_each_side).astype(np.float32)
    return np.concatenate([pilot, samples, pilot]).astype(np.float32), config


def _apply_head_chop(buf: np.ndarray, chop_ms: float, sample_rate: float) -> np.ndarray:
    return buf[int(chop_ms * sample_rate / 1000.0) :]


def _apply_tail_chop(buf: np.ndarray, chop_ms: float, sample_rate: float) -> np.ndarray:
    n = int(chop_ms * sample_rate / 1000.0)
    return buf[: -n] if n else buf


def _apply_fading(buf: np.ndarray, dB_range: float, cycles: float, sample_rate: float) -> np.ndarray:
    """Sinusoidal amplitude envelope varying from ``10^(-dB/20)`` to 1.0."""
    n = buf.size
    t = np.arange(n) / sample_rate
    duration = n / sample_rate
    trough = 10 ** (-dB_range / 20.0)
    envelope = trough + (1.0 - trough) * (0.5 + 0.5 * np.cos(2 * np.pi * cycles * t / duration))
    return (buf * envelope.astype(np.float32)).astype(np.float32)


# ---------------------------------------------------------------------
# Parametrise (baud, payload, damage-label, damage-fn). ``damage-fn``
# takes (audio, sample_rate) and returns the damaged audio.
# ---------------------------------------------------------------------

CASES: list[tuple[int, bytes, str, "callable"]] = []  # noqa: F821

# Clean baseline for every baud+payload -- catches encode/decode regressions.
for baud in (9, 45, 300, 1200):
    for payload in (PAYLOAD_1B, PAYLOAD_10B):
        if baud == 9 and payload == PAYLOAD_10B:
            continue  # ~4 min encode; single-char covers 9-baud
        CASES.append((baud, payload, "clean", lambda a, sr: a))

# Head chop: RX started late. 100 ms fits inside the 200 ms pilot and
# decode always survives. Head chops longer than the pilot are a
# fundamental loss -- the leading preamble is gone and the decoder can't
# guess where a group starts without an anchor. block_repeats helps only
# when there's a way to align to the copies; a bare trailing preamble
# alone can't tell num_data_blocks. Recovering from >200 ms head chop
# would need a length field or a fixed known payload size at the RS layer.
for baud in (45, 300, 1200):
    CASES.append((
        baud, PAYLOAD_1B,
        "head-chop-100ms",
        lambda a, sr: _apply_head_chop(a, 100.0, sr),
    ))

# Tail chop: sink underrun / Ctrl-C too early.
for baud in (45, 300, 1200):
    CASES.append((
        baud, PAYLOAD_1B,
        "tail-chop-200ms",
        lambda a, sr: _apply_tail_chop(a, 200.0, sr),
    ))

# Slow fading: 10 dB peak-to-trough, ~1 fade cycle across the burst.
for baud in (45, 300, 1200):
    CASES.append((
        baud, PAYLOAD_10B,
        "fade-10dB",
        lambda a, sr: _apply_fading(a, 10.0, 1.0, sr),
    ))

# Compound: head chop + fade at once. Worst plausible real-world combo.
for baud in (45, 300, 1200):
    CASES.append((
        baud, PAYLOAD_10B,
        "head-chop-100ms+fade-6dB",
        lambda a, sr: _apply_fading(_apply_head_chop(a, 100.0, sr), 6.0, 1.5, sr),
    ))


@pytest.mark.parametrize(
    "baud, payload, damage, damage_fn",
    CASES,
    ids=[f"{c[0]}baud_{len(c[1])}b_{c[2]}" for c in CASES],
)
def test_decode_survives_wav_damage(baud: int, payload: bytes, damage: str, damage_fn) -> None:
    buf, config = _live_tx_buffer(baud, payload)
    damaged = damage_fn(buf, config.waveform.sample_rate)
    decoded = decode(damaged, config) or b""
    assert payload in decoded, (
        f"{baud} baud {len(payload)}-byte payload after {damage!r} damage "
        f"was not decoded; got {decoded[:80]!r}"
    )
