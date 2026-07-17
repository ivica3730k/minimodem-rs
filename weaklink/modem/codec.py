"""Streaming modem codec.

Wire format
===========

    [PREAMBLE] [data block] [data block] ... [data block]   \
                                     ^ sync_every_blocks blocks   } repeat
    [PREAMBLE] [data block] [data block] ... [data block]   /
    [PREAMBLE]  <-- trailing marker so the last group decodes

* PREAMBLE is a short fixed PN symbol pattern used for RX symbol alignment;
  RX finds every occurrence by correlation, then extracts the data blocks
  between adjacent preambles.
* Each data block carries ``rs_data_bytes`` payload bytes through:
    RS(N,K)+CRC  →  rate-1/2 K=7 convolutional (per-block, with tail bits)
                 →  block-local interleaver  →  4-FSK.

There is no packet boundary and no length header. TX reads arbitrary bytes,
pads to the RS block boundary with zeros, and streams. RX emits every
successfully-decoded data-block payload concatenated. Missing/undecodable
blocks are silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from weaklink.modem import fec
from weaklink.modem.interleaver import InterleaverConfig, deinterleave_soft, interleave
from weaklink.modem.waveform import (
    BITS_PER_SYMBOL,
    NUM_TONES,
    WaveformConfig,
    bits_to_symbols,
    demodulate_soft,
    estimate_coarse_frequency_offset,
    estimate_frequency_offset,
    modulate,
    soft_bits_from_magnitudes,
)
from weaklink.rs import BlockConfig, RSBlockCodec


PREAMBLE_LENGTH_SYMBOLS: int = 32


def _generate_preamble(length: int, seed: int = 0xC05A) -> tuple[int, ...]:
    """Deterministic 4-ary PN sequence. Same LFSR as the pre-streaming codec."""
    state = seed & 0xFFFF
    if state == 0:
        state = 1
    symbols: list[int] = []
    for _ in range(length):
        pair = 0
        for _ in range(2):
            bit = state & 1
            feedback = ((state >> 15) ^ (state >> 13) ^ (state >> 12) ^ (state >> 10)) & 1
            state = ((state >> 1) | (feedback << 15)) & 0xFFFF
            pair = (pair << 1) | bit
        symbols.append(pair & 0x3)
    return tuple(symbols)


_PREAMBLE_SYMBOLS: tuple[int, ...] = _generate_preamble(PREAMBLE_LENGTH_SYMBOLS)


@dataclass(frozen=True)
class ModemConfig:
    waveform: WaveformConfig = field(default_factory=WaveformConfig)
    interleaver: InterleaverConfig = field(default_factory=lambda: InterleaverConfig(rows=8, cols=32))
    rs_data_bytes: int = 16
    rs_parity_bytes: int = 8
    rs_crc_enabled: bool = True
    sync_every_blocks: int = 4
    """Preamble inserted at the start and every N data blocks thereafter."""
    block_repeats: int = 1
    """Each RS block is transmitted this many times, round-robin across the
    current sync group. RX averages symbol magnitudes across copies before
    Viterbi+RS. Gives ~3 dB per doubling in AWGN; time-diversity across
    ``sync_every_blocks`` positions helps against burst fades too.
    """
    coarse_frequency_search_hz: float = 0.0
    frequency_search_hz: float = 20.0
    frequency_resolution_hz: float = 1.0
    preamble_min_score_ratio: float = 0.7
    """Preamble correlator threshold, as a fraction of the peak preamble score
    on this transmission. Below this, a candidate offset is considered noise.
    Higher = fewer false positives, more risk of missing weak preambles."""

    def __post_init__(self) -> None:
        if self.sync_every_blocks < 1:
            raise ValueError("sync_every_blocks must be >= 1")
        if self.rs_data_bytes < 1:
            raise ValueError("rs_data_bytes must be >= 1")
        if self.block_repeats < 1:
            raise ValueError("block_repeats must be >= 1")

    def rs_codec(self) -> RSBlockCodec:
        return RSBlockCodec(
            BlockConfig(
                data_bytes=self.rs_data_bytes,
                parity_bytes=self.rs_parity_bytes,
                crc_enabled=self.rs_crc_enabled,
            )
        )

    @property
    def block_symbol_length(self) -> int:
        return _block_symbol_length(self)


def preamble_symbols() -> np.ndarray:
    return np.asarray(_PREAMBLE_SYMBOLS, dtype=np.int8)


def _block_symbol_length(config: ModemConfig) -> int:
    codec = config.rs_codec()
    info_bits = codec.config.block_size * 8
    coded_bits = 2 * (info_bits + fec.CONSTRAINT_LENGTH - 1)
    interleaved = _round_up_multiple(coded_bits, config.interleaver.block_size)
    padded = _round_up_multiple(interleaved, BITS_PER_SYMBOL)
    return padded // BITS_PER_SYMBOL


def _encode_one_block(payload: bytes, config: ModemConfig) -> np.ndarray:
    codec = config.rs_codec()
    rs_encoded = codec.encode(payload)
    payload_bits = _bytes_to_bits_msb(rs_encoded)
    coded = fec.encode(payload_bits)
    interleaved = interleave(coded, config.interleaver)
    padded = _pad_to_multiple(interleaved, BITS_PER_SYMBOL)
    return bits_to_symbols(padded)


def _decode_one_block(magnitudes: np.ndarray, config: ModemConfig, codec: RSBlockCodec) -> bytes | None:
    """Given demodulated magnitudes for exactly one block, run the pipeline in reverse."""
    if magnitudes.shape[0] != config.block_symbol_length:
        return None
    soft_bits = soft_bits_from_magnitudes(magnitudes)
    coded_bits_count = 2 * (codec.config.block_size * 8 + fec.CONSTRAINT_LENGTH - 1)
    deinterleaved = deinterleave_soft(soft_bits, config.interleaver, coded_bits_count)
    payload_bits = fec.decode(deinterleaved, num_output_bits=codec.config.block_size * 8)
    wire_bytes = _bits_to_bytes_msb(payload_bits)
    return codec.try_decode(wire_bytes)


def encode(input_bytes: bytes, config: ModemConfig) -> np.ndarray:
    """Encode arbitrary-length bytes into a float32 audio stream.

    Input is padded up to the next ``rs_data_bytes`` boundary with zeros.
    Emits ``[preamble][group of M data blocks, round-robin repeated R times]``
    per sync period; where M = sync_every_blocks (or fewer for the trailing
    group) and R = block_repeats. Round-robin order interleaves copies so a
    burst affecting one copy's time-slot leaves the other copies intact.

    Frame::

        [preamble] [b1 b2 ... bM b1 b2 ... bM ...]    <- R copies, round-robin
                    group repeated R times back-to-back
        [preamble] [b(M+1) ... b(2M) ...]
        ...
        [preamble] (trailing)
    """
    codec = config.rs_codec()
    data_bytes = codec.config.data_bytes
    remainder = len(input_bytes) % data_bytes
    if remainder:
        input_bytes = input_bytes + b"\x00" * (data_bytes - remainder)

    pre = preamble_symbols()
    total_blocks = len(input_bytes) // data_bytes
    group_size = config.sync_every_blocks
    repeats = config.block_repeats

    symbol_pieces: list[np.ndarray] = []
    for group_start in range(0, total_blocks, group_size):
        group_end = min(group_start + group_size, total_blocks)
        group_symbols = [
            _encode_one_block(
                input_bytes[block_index * data_bytes : (block_index + 1) * data_bytes],
                config,
            )
            for block_index in range(group_start, group_end)
        ]
        symbol_pieces.append(pre)
        for _copy in range(repeats):
            symbol_pieces.extend(group_symbols)
    symbol_pieces.append(pre)  # trailing marker

    all_symbols = np.concatenate(symbol_pieces) if symbol_pieces else np.zeros(0, dtype=np.int8)
    return modulate(all_symbols, config.waveform)


def decode(samples: np.ndarray, config: ModemConfig) -> bytes:
    """Decode an audio stream to bytes. Missing/undecodable blocks are dropped."""
    magnitudes = demodulate_soft(samples, config.waveform)
    if magnitudes.shape[0] == 0:
        return b""

    # Optional coarse frequency offset (FFT-based) for big SSB LO drift.
    if config.coarse_frequency_search_hz > 0.0:
        coarse_offset = estimate_coarse_frequency_offset(
            np.asarray(samples, dtype=np.float64),
            config.waveform,
            search_range_hz=config.coarse_frequency_search_hz,
        )
        if coarse_offset != 0.0:
            magnitudes = demodulate_soft(samples, config.waveform, frequency_offset_hz=coarse_offset)

    preamble = preamble_symbols()
    peaks = _find_preamble_peaks(magnitudes, preamble, config)
    if not peaks:
        return b""
    # Treat end-of-signal as an implicit trailing sync boundary so the last
    # group is decoded even if its trailing preamble was corrupted.
    peaks_with_end = peaks + [magnitudes.shape[0]]

    codec = config.rs_codec()
    block_length = _block_symbol_length(config)
    repeats = config.block_repeats
    output = bytearray()
    for peak_index in range(len(peaks_with_end) - 1):
        group_start = peaks_with_end[peak_index] + len(preamble)
        group_end = peaks_with_end[peak_index + 1]
        span = group_end - group_start
        transmitted_blocks = span // block_length
        if transmitted_blocks == 0:
            continue
        # Round-robin: M logical blocks repeated R times. transmitted = M*R.
        num_data_blocks = transmitted_blocks // repeats
        if num_data_blocks == 0:
            continue
        for block_index in range(num_data_blocks):
            # Sum LLRs across copies rather than magnitudes -- max-log-MAP is
            # non-linear, so per-copy LLR extraction then summation is
            # noticeably better at low SNR than averaging magnitudes first.
            combined_soft: np.ndarray | None = None
            for copy_index in range(repeats):
                copy_position = group_start + (copy_index * num_data_blocks + block_index) * block_length
                copy_mags = magnitudes[copy_position : copy_position + block_length]
                copy_soft = soft_bits_from_magnitudes(copy_mags)
                if combined_soft is None:
                    combined_soft = copy_soft.copy()
                else:
                    combined_soft += copy_soft
            decoded = _decode_one_block_from_soft(combined_soft, config, codec)
            if decoded is not None:
                output.extend(decoded)
    return bytes(output)


def _decode_one_block_from_soft(soft_bits: np.ndarray, config: ModemConfig, codec: RSBlockCodec) -> bytes | None:
    """Decode a single block from combined soft LLR bits."""
    coded_bits_count = 2 * (codec.config.block_size * 8 + fec.CONSTRAINT_LENGTH - 1)
    if soft_bits.shape[0] < coded_bits_count:
        return None
    deinterleaved = deinterleave_soft(soft_bits, config.interleaver, coded_bits_count)
    payload_bits = fec.decode(deinterleaved, num_output_bits=codec.config.block_size * 8)
    wire_bytes = _bits_to_bytes_msb(payload_bits)
    return codec.try_decode(wire_bytes)


def _find_preamble_peaks(
    magnitudes: np.ndarray, preamble: np.ndarray, config: ModemConfig
) -> list[int]:
    """Return preamble positions above threshold with non-max suppression."""
    preamble_length = len(preamble)
    if magnitudes.shape[0] < preamble_length:
        return []
    tone_indices = preamble.astype(np.int64)
    positions = np.arange(preamble_length)
    max_offset = magnitudes.shape[0] - preamble_length
    scores = np.empty(max_offset + 1, dtype=np.float64)
    for offset in range(max_offset + 1):
        window = magnitudes[offset : offset + preamble_length]
        wanted = window[positions, tone_indices]
        others = (window.sum(axis=1) - wanted) / (NUM_TONES - 1)
        scores[offset] = float(np.sum(wanted - others))

    if scores.size == 0:
        return []
    peak_score = float(scores.max())
    if peak_score <= 0.0:
        return []
    threshold = peak_score * config.preamble_min_score_ratio

    peaks: list[int] = []
    guard = preamble_length
    order = np.argsort(-scores)
    taken = np.zeros(scores.size, dtype=bool)
    for candidate in order:
        if scores[candidate] < threshold:
            break
        lo = max(0, int(candidate) - guard)
        hi = min(scores.size, int(candidate) + guard + 1)
        if taken[lo:hi].any():
            continue
        peaks.append(int(candidate))
        taken[lo:hi] = True
    peaks.sort()
    return peaks


# --- bit/byte helpers ------------------------------------------------------


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
    return bits + bytes(multiple - (len(bits) % multiple))


def _round_up_multiple(value: int, multiple: int) -> int:
    remainder = value % multiple
    if remainder == 0:
        return value
    return value + (multiple - remainder)
