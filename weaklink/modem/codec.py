"""End-to-end modem: bytes ↔ audio samples.

Signal chain
============

TX side::

    payload bytes
        └─ pad ──▶ [preamble tones][conv-encoded + interleaved payload bits]
                       └─ 4-FSK CPFSK modulate ──▶ float32 audio samples

RX side::

    float32 audio samples
        ├─ non-coherent 4-FSK demod (soft magnitudes per symbol)
        ├─ preamble search (correlate the known preamble tone pattern in the
        │  magnitude domain to find symbol timing)
        ├─ deinterleave
        └─ soft Viterbi ──▶ decoded payload bytes

The preamble is a fixed sequence of tone indices — not conv-encoded — so RX
can find it without knowing anything about the code. Correlation runs on the
demodulated soft magnitudes, which is the right domain: it's frequency-only
(non-coherent), matched to how symbols are received.

There are no headers on the wire. TX and RX must agree on the preamble length,
interleaver geometry, and payload length in bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

from weaklink.modem import fec
from weaklink.modem.interleaver import InterleaverConfig, deinterleave_soft, interleave
from weaklink.modem.waveform import (
    BITS_PER_SYMBOL,
    NUM_TONES,
    WaveformConfig,
    bits_to_symbols,
    demodulate_soft,
    modulate,
    soft_bits_from_magnitudes,
)


# A short deterministic 4-ary preamble. Chosen to hit all four tones several
# times so the correlator sees energy on every filter, and to have low
# auto-correlation sidelobes. Not an optimised sequence — good enough for a
# baseline that we can iterate on when we're measuring.
_PREAMBLE_SYMBOLS: tuple[int, ...] = (
    0, 1, 2, 3, 0, 2, 3, 1,
    1, 3, 0, 2, 2, 0, 1, 3,
    3, 2, 1, 0, 1, 0, 3, 2,
    2, 1, 0, 3, 0, 3, 2, 1,
)


@dataclass(frozen=True)
class ModemConfig:
    waveform: WaveformConfig = WaveformConfig()
    interleaver: InterleaverConfig = InterleaverConfig(rows=8, cols=32)
    preamble_repeats: int = 1
    """Number of times to send the fixed preamble. Longer preambles buy sync
    reliability at the cost of overhead."""

    @property
    def preamble_length_symbols(self) -> int:
        return self.preamble_repeats * len(_PREAMBLE_SYMBOLS)


def preamble_symbols(config: ModemConfig) -> np.ndarray:
    """Return the preamble as an array of symbol indices."""
    return np.tile(np.asarray(_PREAMBLE_SYMBOLS, dtype=np.int8), config.preamble_repeats)


def encode(payload: bytes, config: ModemConfig) -> np.ndarray:
    """Encode a byte payload into a float32 audio sample stream."""
    payload_bits = _bytes_to_bits_msb(payload)
    coded = fec.encode(payload_bits)
    interleaved = interleave(coded, config.interleaver)
    payload_symbols = bits_to_symbols(_pad_to_multiple(interleaved, BITS_PER_SYMBOL))
    all_symbols = np.concatenate([preamble_symbols(config), payload_symbols])
    return modulate(all_symbols, config.waveform)


def decode(samples: np.ndarray, config: ModemConfig, payload_length_bytes: int) -> bytes:
    """Decode an audio sample stream into ``payload_length_bytes`` bytes."""
    magnitudes = demodulate_soft(samples, config.waveform)
    if magnitudes.shape[0] == 0:
        return b"\x00" * payload_length_bytes

    sync_offset = _find_preamble(magnitudes, config)
    payload_symbols_start = sync_offset + config.preamble_length_symbols

    # How many payload symbols do we expect?
    payload_bits_count = payload_length_bytes * 8
    coded_bits_count = 2 * (payload_bits_count + fec.CONSTRAINT_LENGTH - 1)
    interleaved_length = _round_up_multiple(coded_bits_count, config.interleaver.block_size)
    expected_payload_symbols = _round_up_multiple(interleaved_length, BITS_PER_SYMBOL) // BITS_PER_SYMBOL

    end = payload_symbols_start + expected_payload_symbols
    if end > magnitudes.shape[0]:
        # Not enough samples — return zeros. Real streaming will read more.
        return b"\x00" * payload_length_bytes

    payload_mags = magnitudes[payload_symbols_start:end]
    soft_bits = soft_bits_from_magnitudes(payload_mags)
    deinterleaved = deinterleave_soft(soft_bits, config.interleaver, coded_bits_count)
    payload_bits = fec.decode(deinterleaved, num_output_bits=payload_bits_count)
    return _bits_to_bytes_msb(payload_bits)


def _find_preamble(magnitudes: np.ndarray, config: ModemConfig) -> int:
    """Cross-correlate the preamble tone pattern against the received magnitudes.

    The reference pattern for symbol ``s`` has 1 at column ``preamble[s]``
    and 0 elsewhere. The score at offset ``off`` is the sum, over preamble
    positions, of the magnitude at the "correct" tone minus the mean of the
    other three tones. That penalises broadband noise events that light up
    every tone equally.
    """
    preamble = preamble_symbols(config)
    preamble_length = len(preamble)
    if magnitudes.shape[0] < preamble_length:
        return 0

    tone_indices = preamble.astype(np.int64)
    positions = np.arange(preamble_length)

    max_offset = magnitudes.shape[0] - preamble_length
    scores = np.empty(max_offset + 1, dtype=np.float64)
    for offset in range(max_offset + 1):
        window = magnitudes[offset : offset + preamble_length]
        wanted = window[positions, tone_indices]
        others = (window.sum(axis=1) - wanted) / (NUM_TONES - 1)
        scores[offset] = float(np.sum(wanted - others))
    return int(np.argmax(scores))


def _bytes_to_bits_msb(data: bytes) -> bytes:
    out = bytearray(len(data) * 8)
    for byte_index, byte_value in enumerate(data):
        for bit_index in range(8):
            out[byte_index * 8 + bit_index] = (byte_value >> (7 - bit_index)) & 1
    return bytes(out)


def _bits_to_bytes_msb(bits: bytes) -> bytes:
    if len(bits) % 8 != 0:
        raise ValueError(f"bit length {len(bits)} not a multiple of 8")
    out = bytearray(len(bits) // 8)
    for index, bit in enumerate(bits):
        out[index // 8] |= (bit & 1) << (7 - (index % 8))
    return bytes(out)


def _pad_to_multiple(bits: bytes, multiple: int) -> bytes:
    if len(bits) % multiple == 0:
        return bits
    padding_count = multiple - (len(bits) % multiple)
    return bits + bytes(padding_count)


def _round_up_multiple(value: int, multiple: int) -> int:
    remainder = value % multiple
    if remainder == 0:
        return value
    return value + (multiple - remainder)
