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
    coarse_frequency_search_hz: float = 500.0
    """Half-range in Hz for FFT-based coarse LO-offset search before preamble
    sync. Always on by default -- costs ~50 ms per decode and handles typical
    HF LO / dial drift up to a few hundred Hz."""
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


def decode(samples: np.ndarray, config: ModemConfig, *, debug: bool = False) -> bytes:
    """Decode an audio stream to bytes. Missing/undecodable blocks are dropped.

    Frequency-offset tracking is per-preamble: after the global coarse search,
    each detected sync marker gets its own fine-offset estimate, and the data
    group that follows is demodulated using that per-group offset. This tracks
    slow LO drift and satellite Doppler across long transmissions without any
    external AFC.

    ``debug=True`` prints diagnostics to stderr — sample stats, preamble
    correlator peaks, per-group decode results — useful for troubleshooting
    live-audio setups where the modem "just doesn't decode".
    """
    import sys as _sys

    def _dbg(msg: str) -> None:
        if debug:
            print(f"weaklink-rx: {msg}", file=_sys.stderr, flush=True)

    if len(samples) == 0:
        _dbg("empty sample buffer; nothing to decode")
        return b""
    samples_per_symbol = config.waveform.samples_per_symbol

    samples_float = np.asarray(samples, dtype=np.float64)
    duration_s = len(samples_float) / config.waveform.sample_rate
    peak = float(np.max(np.abs(samples_float)))
    rms = float(np.sqrt(np.mean(samples_float ** 2))) if len(samples_float) else 0.0
    peak_db = 20.0 * np.log10(peak) if peak > 0 else -np.inf
    rms_db = 20.0 * np.log10(rms) if rms > 0 else -np.inf
    _dbg(
        f"input: {len(samples_float)} samples, {duration_s:.2f} s, "
        f"peak {peak:.4f} ({peak_db:+.1f} dBFS), rms {rms:.4f} ({rms_db:+.1f} dBFS)"
    )
    if peak_db < -40:
        _dbg("WARNING: peak level below -40 dBFS. Mic input probably too quiet or muted.")
    if rms_db < -60:
        _dbg("WARNING: rms level below -60 dBFS. Likely no signal at all.")

    # 1. Global coarse offset (FFT-based, handles big SSB LO drift).
    coarse_offset = 0.0
    if config.coarse_frequency_search_hz > 0.0:
        coarse_offset = estimate_coarse_frequency_offset(
            samples_float,
            config.waveform,
            search_range_hz=config.coarse_frequency_search_hz,
        )
    _dbg(f"coarse frequency offset: {coarse_offset:+.1f} Hz")

    # 2. Demodulate once with the coarse offset just to find preambles.
    coarse_magnitudes = demodulate_soft(samples, config.waveform, frequency_offset_hz=coarse_offset)
    if coarse_magnitudes.shape[0] == 0:
        _dbg("demodulator returned no symbols; sample count below one symbol")
        return b""

    preamble = preamble_symbols()
    peaks = _find_preamble_peaks(coarse_magnitudes, preamble, config)
    _dbg(f"preamble peaks found: {len(peaks)} at symbol offsets {peaks[:8]}{'...' if len(peaks) > 8 else ''}")
    if not peaks:
        _dbg(
            "no preambles above threshold — either no modem signal in the buffer, "
            "SNR too low, or wrong baud/tone_spacing on RX. Check with a WAV loopback first."
        )
        return b""

    # 3. Per-preamble fine offset. Each peak gets its own estimate so slow
    # drift across the transmission is tracked marker by marker.
    per_peak_offsets: list[float] = []
    for peak in peaks:
        preamble_sample_start = peak * samples_per_symbol
        preamble_sample_end = preamble_sample_start + len(preamble) * samples_per_symbol
        if preamble_sample_end > len(samples):
            per_peak_offsets.append(coarse_offset)
            continue
        preamble_samples = np.asarray(samples[preamble_sample_start:preamble_sample_end], dtype=np.float64)
        if config.frequency_search_hz > 0.0:
            offset = estimate_frequency_offset(
                preamble_samples,
                config.waveform,
                preamble,
                search_range_hz=config.frequency_search_hz,
                resolution_hz=config.frequency_resolution_hz,
                prior_offset_hz=coarse_offset,
            )
        else:
            offset = coarse_offset
        per_peak_offsets.append(offset)

    # 4. For each group, demodulate that region with the per-group offset if
    # it drifted significantly from the coarse baseline; otherwise reuse the
    # already-computed coarse magnitudes.
    peaks_with_end = peaks + [coarse_magnitudes.shape[0]]

    codec = config.rs_codec()
    block_length = _block_symbol_length(config)
    repeats = config.block_repeats
    output = bytearray()
    total_blocks_attempted = 0
    total_blocks_decoded = 0
    for peak_index in range(len(peaks_with_end) - 1):
        group_start = peaks_with_end[peak_index] + len(preamble)
        group_end = peaks_with_end[peak_index + 1]
        span = group_end - group_start
        transmitted_blocks = span // block_length
        if transmitted_blocks == 0:
            continue
        num_data_blocks = transmitted_blocks // repeats
        if num_data_blocks == 0:
            continue

        group_offset = per_peak_offsets[peak_index]
        _dbg(
            f"group {peak_index}: start_sym={group_start}, span_sym={span}, "
            f"data_blocks={num_data_blocks}, fine_offset={group_offset:+.1f} Hz"
        )
        if abs(group_offset - coarse_offset) > 0.5:
            # Re-demodulate just this group's samples with the drifted offset.
            group_samples_start = group_start * samples_per_symbol
            group_samples_end = min(group_end * samples_per_symbol, len(samples))
            group_samples = samples[group_samples_start:group_samples_end]
            group_magnitudes = demodulate_soft(
                group_samples, config.waveform, frequency_offset_hz=group_offset
            )
            base_offset_in_group = 0
        else:
            group_magnitudes = coarse_magnitudes
            base_offset_in_group = group_start

        group_decoded = 0
        for block_index in range(num_data_blocks):
            total_blocks_attempted += 1
            # Sum LLRs across copies rather than magnitudes; max-log-MAP is
            # non-linear, so per-copy LLR extraction then summation gets more
            # of the theoretical combining gain at low SNR.
            combined_soft: np.ndarray | None = None
            for copy_index in range(repeats):
                copy_position = base_offset_in_group + (copy_index * num_data_blocks + block_index) * block_length
                copy_mags = group_magnitudes[copy_position : copy_position + block_length]
                copy_soft = soft_bits_from_magnitudes(copy_mags)
                if combined_soft is None:
                    combined_soft = copy_soft.copy()
                else:
                    combined_soft += copy_soft
            decoded = _decode_one_block_from_soft(combined_soft, config, codec)
            if decoded is not None:
                output.extend(decoded)
                group_decoded += 1
                total_blocks_decoded += 1
        _dbg(f"group {peak_index}: {group_decoded}/{num_data_blocks} blocks decoded")

    _dbg(
        f"totals: {total_blocks_decoded}/{total_blocks_attempted} blocks decoded, "
        f"{len(output)} bytes emitted"
    )
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
