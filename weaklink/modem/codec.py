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


import logging as _logging

_log = _logging.getLogger("weaklink.decode")


def decode(samples: np.ndarray, config: ModemConfig, *, streaming: bool = False):
    """Decode an audio stream to bytes. Missing/undecodable blocks are dropped.

    ``streaming=False`` (default): batch mode. Treats end-of-signal as an
    implicit trailing preamble so the last group is decoded even if its
    trailing preamble was corrupted. Returns ``bytes``.

    ``streaming=True``: incremental mode. Only decodes groups fully bracketed
    by two real preambles; the tail after the last preamble is left for the
    next call. Returns ``(bytes, safe_cursor_samples)`` where
    ``safe_cursor_samples`` is the sample offset of the last real preamble --
    callers should keep this preamble in the next decode window and slice
    everything before it off.

    Frequency-offset tracking is per-preamble: after the global coarse search,
    each detected sync marker gets its own fine-offset estimate, and the data
    group that follows is demodulated using that per-group offset.

    Diagnostics go through the standard ``logging`` module (logger name
    ``weaklink.decode``). At default INFO level the codec is silent aside
    from ``WARN`` on successful RS corrections and ``ERROR`` on RS failures.
    Set the CLI ``--modem-debug`` flag to see the per-decode chatter.
    """
    if len(samples) == 0:
        _log.debug("empty sample buffer; nothing to decode")
        return (b"", 0) if streaming else b""
    samples_per_symbol = config.waveform.samples_per_symbol

    samples_float = np.asarray(samples, dtype=np.float64)
    duration_s = len(samples_float) / config.waveform.sample_rate
    peak = float(np.max(np.abs(samples_float)))
    rms = float(np.sqrt(np.mean(samples_float ** 2))) if len(samples_float) else 0.0
    peak_db = 20.0 * np.log10(peak) if peak > 0 else -np.inf
    rms_db = 20.0 * np.log10(rms) if rms > 0 else -np.inf
    _log.debug(
        "input: %d samples, %.2f s, peak %.4f (%+.1f dBFS), rms %.4f (%+.1f dBFS)",
        len(samples_float), duration_s, peak, peak_db, rms, rms_db,
    )
    if peak_db < -40:
        _log.debug("peak level below -40 dBFS")
    if rms_db < -60:
        _log.debug("rms level below -60 dBFS")

    # 1. Global coarse offset (FFT-based, handles big SSB LO drift).
    coarse_offset = 0.0
    if config.coarse_frequency_search_hz > 0.0:
        coarse_offset = estimate_coarse_frequency_offset(
            samples_float,
            config.waveform,
            search_range_hz=config.coarse_frequency_search_hz,
        )
    _log.debug("coarse frequency offset: %+.1f Hz", coarse_offset)

    # 2. Demodulate once with the coarse offset just to find preambles.
    coarse_magnitudes = demodulate_soft(samples, config.waveform, frequency_offset_hz=coarse_offset)
    if coarse_magnitudes.shape[0] == 0:
        _log.debug("demodulator returned no symbols; sample count below one symbol")
        return (b"", 0) if streaming else b""

    preamble = preamble_symbols()
    peaks = _find_preamble_peaks(coarse_magnitudes, preamble, config)
    _log.debug("preamble peaks found: %d at symbol offsets %s", len(peaks), peaks[:8])
    if not peaks:
        _log.debug("no preambles above threshold")
        return (b"", 0) if streaming else b""
    if streaming and len(peaks) < 2:
        # Only one preamble seen -- we can't be sure the trailing preamble has
        # arrived yet. Leave everything for the next call.
        return b"", peaks[0] * samples_per_symbol

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
    if streaming:
        # Only decode groups bounded by two real preambles; the tail after the
        # last preamble might be an incomplete group and should wait.
        peaks_with_end = peaks
    else:
        peaks_with_end = peaks + [coarse_magnitudes.shape[0]]

    codec = config.rs_codec()
    block_length = _block_symbol_length(config)
    repeats = config.block_repeats
    output = bytearray()
    total_blocks_attempted = 0
    total_blocks_decoded = 0
    total_rs_errors_corrected = 0
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
        _log.debug(
            "group %d: start_sym=%d, span_sym=%d, data_blocks=%d, fine_offset=%+.1f Hz",
            peak_index, group_start, span, num_data_blocks, group_offset,
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
        group_failed = 0
        group_rs_errors = 0
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
            decoded, errors_corrected = _decode_one_block_from_soft(combined_soft, config, codec)
            if decoded is not None:
                output.extend(decoded)
                group_decoded += 1
                total_blocks_decoded += 1
                if errors_corrected > 0:
                    group_rs_errors += errors_corrected
                    total_rs_errors_corrected += errors_corrected
            else:
                group_failed += 1
        if group_rs_errors > 0:
            _log.warning(
                "RS corrected %d byte-symbol(s) across %d block(s) in group %d",
                group_rs_errors, group_decoded, peak_index,
            )
        if group_failed > 0:
            _log.error(
                "RS failed on %d block(s) in group %d -- data lost",
                group_failed, peak_index,
            )
        _log.debug("group %d: %d/%d blocks decoded", peak_index, group_decoded, num_data_blocks)

    _log.debug(
        "totals: %d/%d blocks decoded, %d bytes emitted, %d RS corrections",
        total_blocks_decoded, total_blocks_attempted, len(output), total_rs_errors_corrected,
    )
    if streaming:
        # Advance the cursor to the last real preamble so the next call keeps
        # that preamble as the anchor for the group that follows.
        safe_cursor_samples = peaks[-1] * samples_per_symbol
        return bytes(output), safe_cursor_samples
    return bytes(output)


def _decode_one_block_from_soft(
    soft_bits: np.ndarray, config: ModemConfig, codec: RSBlockCodec
) -> tuple[bytes | None, int]:
    """Decode a single block from combined soft LLR bits.

    Returns ``(payload, errors_corrected)``. ``errors_corrected`` is the count
    of byte-symbols the RS outer code had to fix; zero when the block arrived
    clean, positive when RS intervened.
    """
    coded_bits_count = 2 * (codec.config.block_size * 8 + fec.CONSTRAINT_LENGTH - 1)
    if soft_bits.shape[0] < coded_bits_count:
        return None, 0
    deinterleaved = deinterleave_soft(soft_bits, config.interleaver, coded_bits_count)
    payload_bits = fec.decode(deinterleaved, num_output_bits=codec.config.block_size * 8)
    wire_bytes = _bits_to_bytes_msb(payload_bits)
    return codec.try_decode_with_stats(wire_bytes)


_PREAMBLE_SCORE_RATIO = 0.7
"""Preamble correlator threshold: candidates must score ``>= ratio * peak``."""


def _find_preamble_peaks(
    magnitudes: np.ndarray, preamble: np.ndarray, config: ModemConfig
) -> list[int]:
    """Return preamble positions above a ratio-of-peak threshold, with a
    signal-presence gate that rejects noise-only buffers outright.

    Two-stage design:

    1. **Signal-presence gate.** On pure noise, ``peak / robust-sigma`` sits at
       just a few σ (extreme-value statistics for a few thousand samples).
       Real preambles push the peak-vs-noise ratio much higher. If the peak
       isn't at least ~6 robust-σ above the noise centre, we treat the buffer
       as "no signal here yet" and return [].

       The noise centre + robust-σ are estimated from the *lower half* of
       scores. This avoids contamination from real-preamble outliers and their
       PN autocorrelation sidelobes -- both of which sit in the upper half.

    2. **Candidate selection.** Keep offsets scoring ``>= 0.7 * peak``. On a
       clean signal, real preambles score ~100% of peak and sidelobes score
       ~33% -- the 0.7 line cleanly separates them without dropping real
       peaks that faded modestly.
    """
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

    # Estimate noise floor from the lower half of scores -- signal peaks and
    # their sidelobes live in the upper half, so this gives a clean noise
    # estimate even for signal-rich buffers.
    overall_median = float(np.median(scores))
    lower_half = scores[scores <= overall_median]
    if lower_half.size < 4:
        return []
    noise_centre = float(np.median(lower_half))
    noise_mad = float(np.median(np.abs(lower_half - noise_centre)))
    # MAD -> gaussian sigma. The 2.0x factor is because we're computing MAD on
    # a half-distribution (values <= median), which underestimates true σ.
    noise_sigma = max(2.0 * 1.4826 * noise_mad, 1e-9)

    if peak_score < noise_centre + 6.0 * noise_sigma:
        return []

    threshold = peak_score * _PREAMBLE_SCORE_RATIO

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
