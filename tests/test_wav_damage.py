"""WAV robustness tests: encode -> damage -> decode.

Damages the same pilot-padded buffer the CLI writes to the wire:
head/tail chop, slow fading, compound. Portable, no audio server needed.
"""

from __future__ import annotations

import numpy as np
import pytest

from weaklink.modem.codec import ModemConfig, decode, encode
from weaklink.modem.constants import (
    BAUD_PRESETS,
    LIVE_TX_MIN_SECONDS,
    LIVE_TX_PILOT_MIN_SECONDS,
    LIVE_TX_PILOT_MIN_SYMBOLS,
)
from weaklink.modem.streaming import pilot_signal
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
        LIVE_TX_PILOT_MIN_SECONDS,
        (LIVE_TX_MIN_SECONDS - signal_seconds) / 2.0,
        LIVE_TX_PILOT_MIN_SYMBOLS / config.waveform.baud,
    )
    pilot = pilot_signal(config, pilot_each_side).astype(np.float32)
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


def _apply_awgn(buf: np.ndarray, snr_db: float, seed: int, sample_rate: float = 18_000.0) -> np.ndarray:
    """Add white Gaussian noise so noise power in a 3 kHz reference band
    is ``snr_db`` below signal power -- sample-rate-invariant, matches
    the benchmark's convention."""
    sig_p = float(np.mean(buf.astype(np.float64) ** 2))
    noise_variance = sig_p * sample_rate / (2.0 * 3000.0) / (10 ** (snr_db / 10.0))
    rng = np.random.default_rng(seed)
    return (buf + rng.standard_normal(buf.size).astype(np.float32) * np.sqrt(noise_variance)).astype(np.float32)


# ---------------------------------------------------------------------
# Parametrise (baud, payload, damage-label, damage-fn). ``damage-fn``
# takes (audio, sample_rate) and returns the damaged audio.
# ---------------------------------------------------------------------

CASES: list[tuple[int, bytes, str, "callable"]] = []  # noqa: F821

# Clean baseline for every baud+payload -- catches encode/decode regressions.
for baud in (45, 300, 1200):
    for payload in (PAYLOAD_1B, PAYLOAD_10B):
        CASES.append((baud, payload, "clean", lambda a, sr: a))

# Head chop: RX started late. Decoder projects a virtual leading
# preamble; if it lands past buffer start, magnitudes get zero-padded
# and RS corrects the resulting errors. Test up to 500 ms.
for baud in (45, 300, 1200):
    for chop_ms in (100.0, 300.0, 500.0):
        CASES.append((
            baud, PAYLOAD_1B,
            f"head-chop-{int(chop_ms)}ms",
            lambda a, sr, c=chop_ms: _apply_head_chop(a, c, sr),
        ))

# Tail chop: sink underrun / Ctrl-C too early. The trailing pilot is
# 200 ms; anything up to that duration is a no-op on decode.
for baud in (45, 300, 1200):
    for chop_ms in (200.0, 400.0):
        CASES.append((
            baud, PAYLOAD_1B,
            f"tail-chop-{int(chop_ms)}ms",
            lambda a, sr, c=chop_ms: _apply_tail_chop(a, c, sr),
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

# AWGN at ~3 dB above each preset's cliff (regression guard: below-noise
# operation must keep working). SNRs are in the 3 kHz reference band --
# see ``_apply_awgn`` for the noise formula.
_AWGN_TARGETS = {45: -11.0, 300: -2.0, 1200: 5.0}
for baud, snr in _AWGN_TARGETS.items():
    CASES.append((
        baud, PAYLOAD_10B,
        f"awgn-{int(snr):+d}dB",
        lambda a, sr, s=snr: _apply_awgn(a, s, seed=1),
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


# --- e2e streaming variant --------------------------------------------------

from ._streaming import stream_decode


@pytest.mark.parametrize(
    "baud, payload, damage, damage_fn",
    CASES,
    ids=[f"{c[0]}baud_{len(c[1])}b_{c[2]}" for c in CASES],
)
def test_decode_survives_wav_damage_e2e_streaming(
    baud: int, payload: bytes, damage: str, damage_fn,
) -> None:
    """Same damage cases fed through ``StreamingRxDecoder`` -- exercises
    the live-rx code path (chunk-by-chunk decode + cross-call state)."""
    buf, config = _live_tx_buffer(baud, payload)
    damaged = damage_fn(buf, config.waveform.sample_rate)
    decoded = stream_decode(damaged, config) or b""
    assert payload in decoded, (
        f"{baud} baud {len(payload)}-byte payload after {damage!r} damage "
        f"(streaming) was not decoded; got {decoded[:80]!r}"
    )
