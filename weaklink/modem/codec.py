"""Streaming modem codec.

Wire format::

    [PREAMBLE] [data]...[data]   } sync_every_blocks per group, repeat
    [PREAMBLE] [data]...[data]
    [PREAMBLE]                     trailing marker

Data block: RS(N,K)+CRC -> K=7 rate-1/2 conv -> interleave -> 4-FSK.
No length header; missing blocks silently dropped.
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


PREAMBLE_SYMBOLS: np.ndarray = np.asarray(
    _generate_preamble(PREAMBLE_LENGTH_SYMBOLS), dtype=np.int8
)


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
    """Each RS block sent this many times, round-robin. RX combines soft
    LLRs across copies before Viterbi+RS. ~3 dB per doubling in AWGN."""
    coarse_frequency_search_hz: float = 500.0
    """Half-range for pre-sync FFT LO-offset search, in Hz. ~50 ms per
    decode; covers typical HF dial drift."""
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


def _pad_zeros(
    magnitudes: np.ndarray,
    samples: np.ndarray,
    samples_per_symbol: int,
    *,
    n_symbols: int,
    side: str,
) -> tuple[np.ndarray, np.ndarray]:
    """Zero-pad magnitudes + samples on ``side`` ("head" or "tail")."""
    pad_rows = np.zeros((n_symbols, magnitudes.shape[1]), dtype=magnitudes.dtype)
    pad_samples = np.zeros(n_symbols * samples_per_symbol, dtype=samples.dtype)
    if side == "head":
        return np.vstack([pad_rows, magnitudes]), np.concatenate([pad_samples, samples])
    return np.vstack([magnitudes, pad_rows]), np.concatenate([samples, pad_samples])


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


def encode(input_bytes: bytes, config: ModemConfig) -> np.ndarray:
    """Encode bytes to float32 audio.

    Each block carries ``[length_byte, payload..., zero_pad]`` inside the
    RS data area, so the receiver knows exactly how many bytes are real
    payload and how many are padding. Length byte counts real payload
    within the block (0 .. data_bytes-1).
    """
    codec = config.rs_codec()
    data_bytes = codec.config.data_bytes
    if data_bytes < 2:
        raise ValueError("rs_data_bytes must be >= 2 (1 header byte + 1 payload byte)")
    if data_bytes > 256:
        raise ValueError("rs_data_bytes must be <= 256 for a single-byte length header")

    payload_per_block = data_bytes - 1
    block_payloads: list[bytes] = []
    for start in range(0, max(len(input_bytes), 1), payload_per_block):
        chunk = input_bytes[start : start + payload_per_block]
        header = bytes([len(chunk)])
        block_payloads.append(header + chunk + b"\x00" * (payload_per_block - len(chunk)))
    if not block_payloads:  # zero-length input still gets one empty block
        block_payloads = [bytes([0]) + b"\x00" * payload_per_block]

    pre = PREAMBLE_SYMBOLS
    group_size = config.sync_every_blocks
    repeats = config.block_repeats
    symbol_pieces: list[np.ndarray] = []
    for group_start in range(0, len(block_payloads), group_size):
        group_end = min(group_start + group_size, len(block_payloads))
        group_symbols = [
            _encode_one_block(block_payloads[i], config)
            for i in range(group_start, group_end)
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
    """Decode audio to bytes; undecodable blocks are dropped.

    Batch mode (``streaming=False``) returns ``bytes``. Streaming mode
    returns ``(bytes, safe_cursor_samples)`` -- cursor is the last real
    preamble; keep it and slice everything before for the next call.
    Coarse offset then per-preamble fine offset. Logger: ``weaklink.decode``.
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

    preamble = PREAMBLE_SYMBOLS
    peaks = _find_preamble_peaks(coarse_magnitudes, preamble, config)
    _log.debug("preamble peaks found: %d at symbol offsets %s", len(peaks), peaks[:8])
    if not peaks:
        _log.debug("no preambles above threshold")
        return (b"", 0) if streaming else b""
    if streaming and len(peaks) < 2:
        # Keep the lone preamble as anchor for next call, unless enough
        # audio has passed that the trailing one should already have
        # arrived -- then it's stale (truncated tx or false peak), skip it.
        preamble_length = len(preamble)
        max_group_symbols = config.sync_every_blocks * _block_symbol_length(config) + preamble_length
        symbols_past_preamble = coarse_magnitudes.shape[0] - peaks[0]
        if symbols_past_preamble > 2 * max_group_symbols:
            _log.debug(
                "single preamble at %d is stale (%d symbols past, threshold %d); dropping",
                peaks[0], symbols_past_preamble, 2 * max_group_symbols,
            )
            return b"", (peaks[0] + preamble_length) * samples_per_symbol
        return b"", peaks[0] * samples_per_symbol

    # 3. Per-preamble fine offset -- tracks drift marker by marker.
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
        # Group is one preamble pair; tail after the last one may be
        # incomplete, so leave it for the next call.
        peaks_with_end = peaks
    else:
        # Virtual leading/trailing preambles one group's-worth from the
        # nearest real peak recover head/tail chops. Zero-pad magnitudes +
        # samples if the projection is outside the buffer; RS mops up.
        # Pin virtual offsets to coarse so we skip re-demod (which would
        # index un-padded samples with padded coords).
        group_symbol_span = config.block_repeats * _block_symbol_length(config)
        samples = np.asarray(samples)

        # Virtual leading: pair before first_real. Pad head if it lands < 0.
        if peaks[0] > len(preamble):
            virtual_leading = peaks[0] - group_symbol_span - len(preamble)
            if virtual_leading < 0:
                coarse_magnitudes, samples = _pad_zeros(
                    coarse_magnitudes, samples, samples_per_symbol,
                    n_symbols=-virtual_leading, side="head",
                )
                peaks = [p + -virtual_leading for p in peaks]
                virtual_leading = 0
            per_peak_offsets = [coarse_offset] + per_peak_offsets
            peaks = [virtual_leading] + peaks

        # Virtual trailing: pair after last_real. Pad tail if past buffer end.
        virtual_trailing = peaks[-1] + len(preamble) + group_symbol_span
        if virtual_trailing > coarse_magnitudes.shape[0]:
            coarse_magnitudes, samples = _pad_zeros(
                coarse_magnitudes, samples, samples_per_symbol,
                n_symbols=virtual_trailing - coarse_magnitudes.shape[0], side="tail",
            )
        per_peak_offsets = per_peak_offsets + [coarse_offset]
        peaks = peaks + [virtual_trailing]

        peaks_with_end = peaks + [coarse_magnitudes.shape[0]]

    codec = config.rs_codec()
    block_length = _block_symbol_length(config)
    repeats = config.block_repeats
    output = bytearray()
    total_blocks_attempted = 0
    total_blocks_decoded = 0
    total_rs_errors_corrected = 0
    # Max legit group span = sync_every_blocks * block_repeats slots.
    # Wider means the two preambles are from different transmissions with
    # silence between -- skip so we don't Viterbi garbage into RS.
    max_group_span_symbols = config.sync_every_blocks * repeats * block_length
    for peak_index in range(len(peaks_with_end) - 1):
        group_start = peaks_with_end[peak_index] + len(preamble)
        group_end = peaks_with_end[peak_index + 1]
        span = group_end - group_start
        transmitted_blocks = span // block_length
        if transmitted_blocks == 0:
            continue
        if span > max_group_span_symbols + block_length:
            _log.debug(
                "group %d skipped: span %d symbols exceeds max group %d (unrelated preambles)",
                peak_index, span, max_group_span_symbols,
            )
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
            # Sum LLRs (not magnitudes) across copies: max-log-MAP is
            # non-linear, so per-copy LLR + sum gets more combining gain.
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
                # First byte is the block-local length header; next
                # ``length`` bytes are real payload, rest is zero padding.
                length = decoded[0] if decoded else 0
                if length > len(decoded) - 1:
                    length = len(decoded) - 1  # tolerate corrupt header
                output.extend(decoded[1 : 1 + length])
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


def _find_preamble_peaks(
    magnitudes: np.ndarray, preamble: np.ndarray, config: ModemConfig
) -> list[int]:
    """Amplitude-normalised correlator: score / window-total-energy.

    Fade-invariant. Real preambles land at ~1.0; noise at ~0; sidelobes
    around 0.3. Gate: peak >= median + 6σ (else noise-only, return []).
    Accept anything >= median + 5σ; CRC + RS filter anything that slips.
    """
    preamble_length = len(preamble)
    if magnitudes.shape[0] < preamble_length:
        return []
    tone_indices = preamble.astype(np.int64)
    positions = np.arange(preamble_length)
    max_offset = magnitudes.shape[0] - preamble_length
    scores = np.empty(max_offset + 1, dtype=np.float64)
    # Rolling sum of per-symbol total energy across the preamble-length
    # window; used to amplitude-normalise the raw correlation score.
    total_per_symbol = magnitudes.sum(axis=1)
    windowed_total = np.convolve(total_per_symbol, np.ones(preamble_length), mode="valid")
    assert windowed_total.size == max_offset + 1
    for offset in range(max_offset + 1):
        window = magnitudes[offset : offset + preamble_length]
        wanted = window[positions, tone_indices]
        others = (window.sum(axis=1) - wanted) / (NUM_TONES - 1)
        raw = float(np.sum(wanted - others))
        denom = float(windowed_total[offset])
        # denom = 0 only on a genuinely silent window; anything else has
        # non-trivial magnitudes even at very low SNR.
        scores[offset] = raw / denom if denom > 1e-12 else 0.0

    if scores.size == 0:
        return []
    peak_score = float(scores.max())

    # Robust noise floor from the lower half -- real preambles + their PN
    # autocorrelation sidelobes live in the upper half of scores; anything
    # <= the overall median is approximately noise-only.
    overall_median = float(np.median(scores))
    lower_half = scores[scores <= overall_median]
    if lower_half.size < 4:
        return []
    noise_centre = float(np.median(lower_half))
    noise_mad = float(np.median(np.abs(lower_half - noise_centre)))
    # MAD -> gaussian sigma. 2.0x factor because half-distribution MAD
    # underestimates the full σ.
    noise_sigma = max(2.0 * 1.4826 * noise_mad, 1e-9)

    if peak_score < noise_centre + 6.0 * noise_sigma:
        return []

    candidate_threshold = noise_centre + 5.0 * noise_sigma

    peaks: list[int] = []
    guard = preamble_length
    order = np.argsort(-scores)
    taken = np.zeros(scores.size, dtype=bool)
    for candidate in order:
        if scores[candidate] < candidate_threshold:
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
