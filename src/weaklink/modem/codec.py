"""Streaming modem codec. Wire format: ``[pre][slot][pre][slot]...[pre]``.
Each slot = one RS-block wrapping ``[length][block_index][payload][pad]``,
routed through RS+CRC → conv(K=7, r=1/2) → per-block interleave → 4-FSK.
Message boundaries fall on non-block-length spans between preambles.
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

import numpy as np

from weaklink.modem import fec
from weaklink.modem.exceptions import ConfigError, EncodeError
from weaklink.modem.interleaver import (
    InterleaverConfig,
    deinterleave_soft,
    interleave,
)
from weaklink.modem.interleaver import (
    cycle_size as _interleaver_cycle_size,
)
from weaklink.modem.rs import BlockConfig, RSBlockCodec
from weaklink.modem.waveform import (
    WaveformConfig,
    _bits_per_symbol,
    _num_symbols,
    bits_to_symbols,
    demodulate_soft,
    estimate_coarse_frequency_offset,
    estimate_frequency_offset,
    modulate,
    soft_bits_from_magnitudes,
)


@functools.lru_cache(maxsize=32)
def _cached_rs_codec(
    data_bytes: int, parity_bytes: int, crc_enabled: bool,
) -> RSBlockCodec:
    """Building a ``reedsolo`` codec shows up in profiles at ~1 ms each,
    and we build one per encode / decode / seed-search try. Cache them."""
    return RSBlockCodec(
        BlockConfig(
            data_bytes=data_bytes,
            parity_bytes=parity_bytes,
            crc_enabled=crc_enabled,
        )
    )


PREAMBLE_LENGTH_SYMBOLS: int = 32


def _generate_preamble(length: int, num_tones: int = 4, seed: int = 0xC05A) -> tuple[int, ...]:
    """Deterministic PN sequence over the mode's symbol alphabet. Same
    LFSR at every mode; consumes ``bits_per_symbol`` bits per symbol.
    For OOK (``num_tones=1``) the alphabet is {0, 1}."""
    bits_per_symbol = _bits_per_symbol(num_tones)
    mask = _num_symbols(num_tones) - 1
    state = seed & 0xFFFF
    if state == 0:
        state = 1
    symbols: list[int] = []
    for _ in range(length):
        acc = 0
        for _ in range(bits_per_symbol):
            bit = state & 1
            feedback = ((state >> 15) ^ (state >> 13) ^ (state >> 12) ^ (state >> 10)) & 1
            state = ((state >> 1) | (feedback << 15)) & 0xFFFF
            acc = (acc << 1) | bit
        symbols.append(acc & mask)
    return tuple(symbols)


@functools.lru_cache(maxsize=8)
def _preamble_for(num_tones: int) -> np.ndarray:
    return np.asarray(
        _generate_preamble(PREAMBLE_LENGTH_SYMBOLS, num_tones=num_tones), dtype=np.int8,
    )


@functools.lru_cache(maxsize=8)
def _preamble_deterministic_sidelobe(num_tones: int) -> float:
    """Peak-normalised max sidelobe of the correlator run over the
    preamble sequence at every aperiodic offset ≠ 0, assuming the
    positions outside the overlap carry balanced random data
    (mean 0.5 of tone amplitude). Depends only on the fixed PN
    sequence -- computed once at import time, no fudge factor."""
    pre = _preamble_for(num_tones).astype(np.int64)
    length = pre.size
    max_score = 0.0
    if num_tones == 1:
        # OOK correlator: score = (tone_matches - silence_matches) / total.
        tone_mask = (pre == 1).astype(np.float64)
        silence_mask = (pre == 0).astype(np.float64)
        for shift in range(1, length):
            overlap = length - shift
            in_overlap = pre[shift:].astype(np.float64)
            det_num = float(in_overlap @ tone_mask[:overlap] - in_overlap @ silence_mask[:overlap])
            det_total = float(in_overlap.sum())
            out_tone = float(tone_mask[overlap:].sum())
            out_silence = float(silence_mask[overlap:].sum())
            # Outside overlap: random data at mean amplitude 0.5.
            numerator = det_num + 0.5 * (out_tone - out_silence)
            denominator = det_total + 0.5 * (out_tone + out_silence)
            if denominator > 0:
                max_score = max(max_score, abs(numerator / denominator))
        return max_score

    # N-FSK correlator: score = (M * wanted - total) / ((M-1) * total).
    for shift in range(1, length):
        magnitudes = np.zeros((length, num_tones), dtype=np.float64)
        # Positions inside the overlap carry the shifted preamble's tone.
        overlap = length - shift
        magnitudes[np.arange(overlap), pre[shift:]] = 1.0
        # Positions outside the overlap: mean-amplitude across all tones.
        magnitudes[overlap:] = 1.0 / num_tones
        wanted = magnitudes[np.arange(length), pre].sum()
        total = magnitudes.sum()
        raw = (num_tones * wanted - total) / (num_tones - 1)
        if total > 0:
            max_score = max(max_score, abs(raw / total))
    return max_score


# 4-FSK preamble kept as a module constant so tests importing it directly
# still work; use ``_preamble_for(num_tones)`` in encode / decode paths.
PREAMBLE_SYMBOLS: np.ndarray = _preamble_for(4)


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
            raise ConfigError("sync_every_blocks must be >= 1")
        if self.rs_data_bytes < 1:
            raise ConfigError("rs_data_bytes must be >= 1")
        if self.block_repeats < 1:
            raise ConfigError("block_repeats must be >= 1")

    def rs_codec(self) -> RSBlockCodec:
        return _cached_rs_codec(
            self.rs_data_bytes, self.rs_parity_bytes, self.rs_crc_enabled,
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
    bps = config.waveform.bits_per_symbol
    codec = config.rs_codec()
    info_bits = codec.config.block_size * 8
    coded_bits = 2 * (info_bits + fec.CONSTRAINT_LENGTH - 1)
    interleaved = _round_up_multiple(coded_bits, config.interleaver.block_size)
    padded = _round_up_multiple(interleaved, bps)
    return padded // bps


def _encode_one_block(payload: bytes, config: ModemConfig, seed_slot: int) -> np.ndarray:
    bps = config.waveform.bits_per_symbol
    codec = config.rs_codec()
    rs_encoded = codec.encode(payload)
    payload_bits = _bytes_to_bits_msb(rs_encoded)
    coded = fec.encode(payload_bits)
    interleaved = interleave(coded, config.interleaver, block_index=seed_slot)
    padded = _pad_to_multiple(interleaved, bps)
    return bits_to_symbols(padded, num_tones=config.waveform.num_tones)


def _copy_seed_slot(block_index: int, copy_index: int, block_repeats: int) -> int:
    """Copy K of block B uses a different permutation from copy K-1: seeds
    are spread ``cycle_size / block_repeats`` apart so successive copies
    look uncorrelated to the RX bit-error pattern (real diversity when
    combining soft LLRs, not just retry-until-success)."""
    cycle = _interleaver_cycle_size()
    step = max(1, cycle // max(1, block_repeats))
    return (block_index + copy_index * step) % cycle


def _valid_seed_for_block(
    found_seed_slot: int, block_index: int, block_repeats: int
) -> bool:
    """Does ``found_seed_slot`` correspond to *any* of the ``block_repeats``
    copies of ``block_index``? Sanity check so a spurious RS+CRC hit on
    the wrong permutation doesn't get accepted."""
    cycle = _interleaver_cycle_size()
    step = max(1, cycle // max(1, block_repeats))
    for copy_i in range(block_repeats):
        if (block_index + copy_i * step) % cycle == found_seed_slot:
            return True
    return False


#: Per-slot data-area header: ``[length 1B][block_index 2B]``. Length
#: strips trailing NUL padding; block_index dedupes copies (see
#: block_repeats) and picks the RX output slot.
_HEADER_BYTES: int = 3
_MAX_BLOCK_INDEX: int = 0xFFFF


def _validate_data_bytes(data_bytes: int) -> None:
    if data_bytes < _HEADER_BYTES + 1:
        raise ConfigError(
            f"rs_data_bytes must be >= {_HEADER_BYTES + 1} (header + 1 payload byte)"
        )
    if data_bytes > 256:
        raise ConfigError("rs_data_bytes must be <= 256 (length header is 1 byte)")


def _frame_block(chunk: bytes, block_index: int, payload_per_block: int) -> bytes:
    return (
        bytes([len(chunk), (block_index >> 8) & 0xFF, block_index & 0xFF])
        + chunk
        + b"\x00" * (payload_per_block - len(chunk))
    )


def encode_stream(
    byte_iter: "Iterable[bytes]", config: ModemConfig
) -> "Iterator[np.ndarray]":
    """Generator: consume bytes, yield float32 audio one slot at a time,
    then a trailing preamble. Copies of the same block are adjacent
    (no future-lookahead needed); RX dedupes by block_index. Cap:
    65535 slots per session (2-byte index).
    """
    codec = config.rs_codec()
    data_bytes = codec.config.data_bytes
    _validate_data_bytes(data_bytes)
    payload_per_block = data_bytes - _HEADER_BYTES
    preamble = _preamble_for(config.waveform.num_tones)

    pre_audio = modulate(preamble, config.waveform)

    def emit_block(chunk_bytes: bytes, block_index: int) -> "Iterator[np.ndarray]":
        framed = _frame_block(chunk_bytes, block_index, payload_per_block)
        for copy_index in range(config.block_repeats):
            seed = _copy_seed_slot(block_index, copy_index, config.block_repeats)
            block_symbols = _encode_one_block(framed, config, seed_slot=seed)
            merged = np.concatenate([preamble, block_symbols])
            yield modulate(merged, config.waveform)

    buffer = bytearray()
    block_index = 0
    emitted_any = False
    for chunk in byte_iter:
        buffer.extend(chunk)
        while len(buffer) >= payload_per_block:
            if block_index > _MAX_BLOCK_INDEX:
                raise EncodeError(
                    f"stream too long: block_index exceeded {_MAX_BLOCK_INDEX}"
                )
            yield from emit_block(bytes(buffer[:payload_per_block]), block_index)
            del buffer[:payload_per_block]
            block_index += 1
            emitted_any = True

    if buffer or not emitted_any:
        if block_index > _MAX_BLOCK_INDEX:
            raise ValueError(
                f"stream too long: block_index exceeded {_MAX_BLOCK_INDEX}"
            )
        yield from emit_block(bytes(buffer), block_index)

    yield pre_audio  # trailing marker


def encode(input_bytes: bytes, config: ModemConfig) -> np.ndarray:
    """Batch-encode ``input_bytes`` to float32 audio. Thin wrapper around
    :func:`encode_stream` for tests / WAV output."""
    parts = list(encode_stream(iter([input_bytes]), config))
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts)


import logging as _logging

_log = _logging.getLogger("weaklink.decode")


def decode(
    samples: np.ndarray,
    config: ModemConfig,
    *,
    streaming: bool = False,
    streaming_state: dict[str, Any] | None = None,
) -> bytes | tuple[bytes, int]:
    """Decode audio to bytes; undecodable blocks dropped.

    Batch mode returns ``bytes``. Streaming mode returns
    ``(bytes, safe_cursor_samples)`` -- keep audio from the cursor
    onward for the next call. ``streaming_state`` is a mutable dict
    the caller reuses across polls; carries cross-call dedup and
    the cached coarse LO offset. Logger: ``weaklink.decode``.
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

    # Coarse LO offset via FFT. Cached across streaming calls but
    # only trusted after a call that found preambles -- otherwise a
    # first-call-on-silence would cache a noise-floor peak and stick.
    coarse_offset = 0.0
    cached_offset = (
        streaming_state.get("coarse_offset_hz")
        if streaming and streaming_state is not None else None
    )
    fresh_estimate = False
    if cached_offset is not None:
        coarse_offset = float(cached_offset)
    elif config.coarse_frequency_search_hz > 0.0:
        coarse_offset = estimate_coarse_frequency_offset(
            samples_float,
            config.waveform,
            search_range_hz=config.coarse_frequency_search_hz,
        )
        fresh_estimate = True
    _log.debug("coarse frequency offset: %+.1f Hz", coarse_offset)

    # 2. Demodulate once with the coarse offset just to find preambles.
    coarse_magnitudes = demodulate_soft(samples, config.waveform, frequency_offset_hz=coarse_offset)
    if coarse_magnitudes.shape[0] == 0:
        _log.debug("demodulator returned no symbols; sample count below one symbol")
        return (b"", 0) if streaming else b""

    preamble = _preamble_for(config.waveform.num_tones)
    peaks = _find_preamble_peaks(coarse_magnitudes, preamble, config)
    _log.debug("preamble peaks found: %d at symbol offsets %s", len(peaks), peaks[:8])
    if not peaks:
        _log.debug("no preambles above threshold")
        # Coarse-offset cache stays unproven: drop it so the next call
        # re-runs the FFT against fresher audio. Signal end-of-session
        # only if we HAD lock (cache existed): silence-before-first-TX
        # shouldn't fire the flush.
        if streaming and streaming_state is not None:
            if streaming_state.pop("coarse_offset_hz", None) is not None:
                streaming_state["session_ended"] = True
                # Session is over -- flush any pending (unfinalised)
                # blocks. The last block may have < R copies but we
                # take what we've got.
                pending = streaming_state.get("pending_blocks", {})
                emitted = streaming_state.setdefault("emitted", set())
                tail = bytearray()
                for i in sorted(pending.keys()):
                    if i not in emitted:
                        tail.extend(pending[i])
                        emitted.add(i)
                streaming_state["pending_blocks"] = {}
                streaming_state["copies_seen"] = {}
                streaming_state["expected_block_index"] = 0
                if tail:
                    return (bytes(tail), 0) if streaming else bytes(tail)
        return (b"", 0) if streaming else b""
    # Peaks found: the offset estimate is proven. Cache it if fresh so
    # subsequent calls skip the FFT.
    if fresh_estimate and streaming and streaming_state is not None:
        streaming_state["coarse_offset_hz"] = coarse_offset
    if streaming and len(peaks) < 2:
        # Need two preambles to bracket a slot. Keep the one we have as
        # anchor; stale if it's been sitting past two block durations.
        block_len = _block_symbol_length(config)
        symbols_past = coarse_magnitudes.shape[0] - peaks[0]
        if symbols_past > 2 * (block_len + len(preamble)):
            return b"", (peaks[0] + len(preamble)) * samples_per_symbol
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

    # Adjacent preamble pair = one slot. Span 0 = message boundary
    # (adjacent txs' trailing / leading preambles). Batch mode also
    # projects a virtual leading / trailing preamble to recover head-
    # or tail-chopped signals via zero-padding + RS.
    codec = config.rs_codec()
    block_length = _block_symbol_length(config)
    output = bytearray()
    total_rs_errors_corrected = 0
    slot_attempted = 0
    slot_decoded = 0

    if not streaming:
        samples = np.asarray(samples)
        # Head padding: pretend a virtual leading preamble sits one
        # slot's-worth before the first real one.
        if peaks[0] > len(preamble):
            virtual = peaks[0] - block_length - len(preamble)
            if virtual < 0:
                pad_symbols = -virtual
                coarse_magnitudes, samples = _pad_zeros(
                    coarse_magnitudes, samples, samples_per_symbol,
                    n_symbols=pad_symbols, side="head",
                )
                peaks = [p + pad_symbols for p in peaks]
                virtual = 0
            per_peak_offsets = [coarse_offset] + per_peak_offsets
            peaks = [virtual] + peaks
        # Tail padding: project a virtual trailing preamble only when
        # there's meaningful signal after the last real peak (i.e. the
        # tx trailing preamble was chopped off, leaving a bare slot).
        # If the buffer already ends right at a real trailing preamble,
        # projecting would decode zero-padded silence and log a bogus
        # CRC failure.
        tail_symbols_after_last_peak = coarse_magnitudes.shape[0] - peaks[-1] - len(preamble)
        if tail_symbols_after_last_peak > block_length // 2:
            virtual_end = peaks[-1] + len(preamble) + block_length
            if virtual_end > coarse_magnitudes.shape[0]:
                coarse_magnitudes, samples = _pad_zeros(
                    coarse_magnitudes, samples, samples_per_symbol,
                    n_symbols=virtual_end - coarse_magnitudes.shape[0], side="tail",
                )
            per_peak_offsets = per_peak_offsets + [coarse_offset]
            peaks = peaks + [virtual_end]

    # Cross-call dedup set: block_indices we've already emitted in this
    # streaming session. Honoured whenever ``streaming_state`` is
    # passed, regardless of the ``streaming`` flag -- so a final
    # batch-mode decode over the tail (drain) still dedups against
    # blocks the earlier streaming calls already emitted.
    emitted_indices: set[int] = (
        streaming_state.setdefault("emitted", set())
        if streaming_state is not None
        else set()
    )
    # Blocks decoded but not yet R-copy-confirmed. Persists across
    # streaming calls so a block whose R copies straddle two decode
    # windows still finalises correctly. On message boundary or
    # session end these are flushed unconditionally.
    pending_blocks: dict[int, bytes] = (
        streaming_state.setdefault("pending_blocks", {})
        if streaming_state is not None
        else {}
    )
    copies_seen: dict[int, int] = (
        streaming_state.setdefault("copies_seen", {})
        if streaming_state is not None
        else {}
    )

    def _flush_message(msg: dict[int, bytes]) -> None:
        # Emit in block_index order. Missing indices leave a gap (a
        # single unrecoverable slot doesn't take the whole tail of a
        # long stream with it). Skip indices we've already emitted in
        # a previous streaming call. The caller (the streaming decoder) is responsible
        # for clearing ``emitted_indices`` at session boundaries.
        for i in sorted(msg.keys()):
            if i in emitted_indices:
                continue
            output.extend(msg[i])
            emitted_indices.add(i)

    def _flush_pending_all(reason: str) -> None:
        # Emit every buffered block, R-confirmed or not. Called on
        # message boundary / session end -- the last block(s) may have
        # fewer than R observed copies, but the message is over so we
        # take what we've got.
        if not pending_blocks:
            return
        _log.debug("flushing %d unfinalised block(s) (%s)", len(pending_blocks), reason)
        for i in sorted(pending_blocks.keys()):
            if i not in emitted_indices:
                output.extend(pending_blocks[i])
                emitted_indices.add(i)
        pending_blocks.clear()
        copies_seen.clear()

    stride = block_length + len(preamble)
    current_msg: dict[int, bytes] = {}
    # Expected block_index / copies-of-this-block-seen: feeds the seed
    # search's candidate order so the common case (no missed slots) hits
    # on the first try. When block_repeats > 1 we expect the same
    # block_index for R slots in a row, then advance.
    expected_block_index = (
        streaming_state.get("expected_block_index", 0)
        if streaming_state is not None else 0
    )
    copies_seen_this_block = 0
    # Soft LLRs of consecutive slots that failed to decode independently;
    # once we have block_repeats of them we try soft-LLR combining.
    combining_buffer: list[np.ndarray] = []

    def _observe_copy(header_block_index: int, content: bytes, errors: int) -> None:
        """Record one copy of a block; commit to output when R copies seen."""
        if header_block_index in emitted_indices:
            return
        if header_block_index not in pending_blocks:
            pending_blocks[header_block_index] = content
        copies_seen[header_block_index] = copies_seen.get(header_block_index, 0) + 1
        if copies_seen[header_block_index] >= config.block_repeats:
            current_msg[header_block_index] = pending_blocks.pop(header_block_index)
            copies_seen.pop(header_block_index, None)

    def _record_block(
        decoded: bytes, errors: int, header_block_index: int,
    ) -> bytes:
        length = decoded[0]
        payload_area_size = len(decoded) - _HEADER_BYTES
        if length > payload_area_size:
            length = payload_area_size
        return bytes(decoded[_HEADER_BYTES : _HEADER_BYTES + length])

    slot_i = 0
    while slot_i < len(peaks) - 1:
        slot_start = peaks[slot_i] + len(preamble)
        slot_end = peaks[slot_i + 1]
        span = slot_end - slot_start
        if abs(span - block_length) > 4:
            # Bad span: either a spurious peak (dropping peaks[i+1] leaves
            # a stride-consistent chain) or a real message boundary.
            # Peaks[i+1] is spurious when it's off-stride AND ignoring
            # it lets us reach a stride-consistent successor: any peak
            # at peaks[i] + K*stride (K >= 1). No such successor means
            # peaks[i+1] genuinely marks the end of stride-consistent
            # data -- a real boundary or the tail of the audio buffer.
            spurious = False
            for j in range(slot_i + 2, len(peaks)):
                offset_from_anchor = peaks[j] - peaks[slot_i]
                if offset_from_anchor <= 0:
                    continue
                nearest_k = round(offset_from_anchor / stride)
                if nearest_k >= 1 and abs(offset_from_anchor - nearest_k * stride) <= 4:
                    spurious = True
                    break
            if spurious:
                _log.debug("slot %d: dropping spurious peak %d", slot_i, peaks[slot_i + 1])
                del peaks[slot_i + 1]
                del per_peak_offsets[slot_i + 1]
                continue  # retry with the same slot_i
            _log.debug("slot %d span %d: message boundary", slot_i, span)
            _flush_pending_all("message boundary")
            _flush_message(current_msg)
            current_msg = {}
            # New message starts fresh -- previous message's block_indices
            # no longer act as dedup keys against the new one.
            emitted_indices.clear()
            expected_block_index = 0
            copies_seen_this_block = 0
            combining_buffer.clear()
            slot_i += 1
            continue
        slot_offset = per_peak_offsets[slot_i]
        if abs(slot_offset - coarse_offset) > 0.5:
            sample_start = slot_start * samples_per_symbol
            sample_end = min(slot_end * samples_per_symbol, len(samples))
            slot_mags = demodulate_soft(
                samples[sample_start:sample_end], config.waveform, frequency_offset_hz=slot_offset,
            )[:block_length]
        else:
            slot_mags = coarse_magnitudes[slot_start : slot_start + block_length]
        if slot_mags.shape[0] >= block_length:
            slot_attempted += 1
            soft = soft_bits_from_magnitudes(slot_mags, num_tones=config.waveform.num_tones)
            decoded, errors_corrected, found_seed_slot = _decode_slot_with_seed_search(
                soft, config, codec, expected_block_index
            )
            accepted = False
            if decoded is not None and len(decoded) >= _HEADER_BYTES:
                header_block_index = (decoded[1] << 8) | decoded[2]
                if _valid_seed_for_block(
                    found_seed_slot, header_block_index, config.block_repeats
                ):
                    accepted = True
                    content = _record_block(decoded, errors_corrected, header_block_index)
                    _observe_copy(header_block_index, content, errors_corrected)
                    slot_decoded += 1
                    if errors_corrected > 0:
                        total_rs_errors_corrected += errors_corrected
                    copies_seen_this_block += 1
                    if copies_seen_this_block >= config.block_repeats:
                        expected_block_index = header_block_index + 1
                        copies_seen_this_block = 0
                    else:
                        expected_block_index = header_block_index
                    combining_buffer.clear()
            if not accepted and config.block_repeats > 1:
                # Independent decode failed. Buffer soft LLRs; once we
                # have block_repeats copies, sum their deinterleaved
                # LLRs (classical soft-combining diversity).
                combining_buffer.append(soft)
                if len(combining_buffer) >= config.block_repeats:
                    dec, errs, seed = _decode_combined_copies(
                        combining_buffer, config, codec, expected_block_index
                    )
                    if dec is not None and len(dec) >= _HEADER_BYTES:
                        header_block_index = (dec[1] << 8) | dec[2]
                        # Combined decode consumed R copies at once ->
                        # block is finalised immediately regardless of
                        # copies_seen so far.
                        if header_block_index not in emitted_indices:
                            content = _record_block(dec, errs, header_block_index)
                            current_msg[header_block_index] = content
                            pending_blocks.pop(header_block_index, None)
                            copies_seen.pop(header_block_index, None)
                        slot_decoded += 1
                        if errs > 0:
                            total_rs_errors_corrected += errs
                        expected_block_index = header_block_index + 1
                        copies_seen_this_block = 0
                    # Whether combined worked or not, we've spent our shot
                    # on this block -- clear the buffer and advance.
                    combining_buffer.clear()
        slot_i += 1

    _flush_message(current_msg)
    if streaming and streaming_state is not None:
        # Persist unfinalised state so a block whose R copies straddle
        # two streaming calls picks up where it left off.
        streaming_state["expected_block_index"] = expected_block_index
    else:
        # Batch mode (drain / WAV rx): no more calls coming, flush
        # whatever is buffered.
        _flush_pending_all("batch mode end")

    if total_rs_errors_corrected > 0:
        _log.warning("RS corrected %d byte-symbol(s) total", total_rs_errors_corrected)
    slot_failed = slot_attempted - slot_decoded
    if slot_failed > 0:
        _log.error("%d slot(s) failed CRC/RS -- copies elsewhere may recover", slot_failed)
    _log.debug("slots: %d attempted, %d decoded; %d bytes emitted",
               slot_attempted, slot_decoded, len(output))

    if streaming:
        safe_cursor_samples = peaks[-1] * samples_per_symbol
        return bytes(output), safe_cursor_samples
    return bytes(output)


def _decode_one_block_from_soft(
    soft_bits: np.ndarray,
    config: ModemConfig,
    codec: RSBlockCodec,
    seed_slot: int,
) -> tuple[bytes | None, int]:
    """Decode a single block from soft LLR bits using the interleaver
    permutation identified by ``seed_slot``.

    Returns ``(payload, errors_corrected)`` or ``(None, 0)`` if RS/CRC failed.
    """
    coded_bits_count = 2 * (codec.config.block_size * 8 + fec.CONSTRAINT_LENGTH - 1)
    if soft_bits.shape[0] < coded_bits_count:
        return None, 0
    deinterleaved = deinterleave_soft(
        soft_bits, config.interleaver, coded_bits_count, block_index=seed_slot
    )
    payload_bits = fec.decode(deinterleaved, num_output_bits=codec.config.block_size * 8)
    wire_bytes = _bits_to_bytes_msb(payload_bits)
    return codec.try_decode_with_stats(wire_bytes)


def _decode_combined_copies(
    copies_soft: list[np.ndarray],
    config: ModemConfig,
    codec: RSBlockCodec,
    expected_block_index: int,
) -> tuple[bytes | None, int, int]:
    """Soft-LLR combine ``len(copies_soft)`` back-to-back copies of the same
    block, then Viterbi + RS + CRC. Each copy is deinterleaved with the
    per-copy seed slot before summing; we brute-force the candidate block
    seed (expected first, radiating outward mod cycle_size) since the copies
    might not decode individually. Returns ``(payload, errors, seed_slot)``.
    """
    coded_bits_count = 2 * (codec.config.block_size * 8 + fec.CONSTRAINT_LENGTH - 1)
    for soft in copies_soft:
        if soft.shape[0] < coded_bits_count:
            return None, 0, -1
    cycle = _interleaver_cycle_size()
    expected_slot = expected_block_index % cycle
    step = max(1, cycle // max(1, config.block_repeats))
    order: list[int] = [expected_slot]
    for delta in range(1, cycle):
        order.append((expected_slot + delta) % cycle)
        if len(order) >= cycle:
            break
        order.append((expected_slot - delta) % cycle)
    for candidate in order:
        combined = np.zeros(coded_bits_count, dtype=np.float64)
        for copy_i, soft in enumerate(copies_soft):
            per_copy_seed = (candidate + copy_i * step) % cycle
            combined += deinterleave_soft(
                soft, config.interleaver, coded_bits_count, block_index=per_copy_seed,
            )
        payload_bits = fec.decode(combined, num_output_bits=codec.config.block_size * 8)
        wire_bytes = _bits_to_bytes_msb(payload_bits)
        result, errors = codec.try_decode_with_stats(wire_bytes)
        if result is not None:
            header_block_index = (result[1] << 8) | result[2]
            # The combined decode's ``candidate`` IS the block_index-seed
            # base (copy 0's seed); header should mod-agree with it.
            if header_block_index % cycle == candidate:
                return result, errors, candidate
    return None, 0, -1


def _decode_slot_with_seed_search(
    soft_bits: np.ndarray,
    config: ModemConfig,
    codec: RSBlockCodec,
    expected_block_index: int,
) -> tuple[bytes | None, int, int]:
    """Try each of the ``cycle_size`` permutation slots (expected first,
    then radiating outward mod cycle_size) until RS+CRC clears. Returns
    ``(payload, errors_corrected, seed_slot)``. Since only ``cycle_size``
    distinct permutations exist, worst case is a fixed bound of tries
    covering every possible bit ordering."""
    cycle = _interleaver_cycle_size()
    expected_slot = expected_block_index % cycle
    # Deterministic radiating order over the cycle: 0, +1, -1, +2, -2, ...
    # bounded by cycle_size, no repeats.
    order: list[int] = [expected_slot]
    for step in range(1, cycle):
        order.append((expected_slot + step) % cycle)
        if len(order) >= cycle:
            break
        order.append((expected_slot - step) % cycle)
    for seed_slot in order:
        payload, errors = _decode_one_block_from_soft(soft_bits, config, codec, seed_slot)
        if payload is not None:
            return payload, errors, seed_slot
    return None, 0, -1


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
    max_offset = magnitudes.shape[0] - preamble_length
    # Rolling sum of per-symbol total energy across the preamble-length
    # window; used to amplitude-normalise the raw correlation score.
    total_per_symbol = magnitudes.sum(axis=1)
    windowed_total = np.convolve(total_per_symbol, np.ones(preamble_length), mode="valid")
    preamble_int = preamble.astype(np.int64)
    num_tones = magnitudes.shape[1]

    if num_tones == 1:
        # OOK: preamble symbols are {0, 1}. Score = (tone-symbol energy)
        # minus (silence-symbol energy), normalised by total window
        # energy. Perfect alignment -> 1.0; noise-only -> ~0.
        tone_mask = (preamble_int == 1).astype(np.float64)
        silence_mask = (preamble_int == 0).astype(np.float64)
        mag_col = magnitudes[:, 0]
        raw = np.correlate(mag_col, tone_mask, mode="valid") - np.correlate(mag_col, silence_mask, mode="valid")
    else:
        # N-FSK: for each tone k, mask marks where the preamble expects
        # that tone; sum the wanted energy across all N tones.
        wanted = np.zeros(max_offset + 1, dtype=np.float64)
        for tone in range(num_tones):
            mask = (preamble_int == tone).astype(np.float64)
            if mask.any():
                wanted += np.correlate(magnitudes[:, tone], mask, mode="valid")
        # Old inner-loop math simplifies to: raw = (M * wanted - total) / (M - 1).
        raw = (num_tones * wanted - windowed_total) / (num_tones - 1)

    with np.errstate(divide="ignore", invalid="ignore"):
        scores = np.where(windowed_total > 1e-12, raw / windowed_total, 0.0)

    if scores.size == 0:
        return []
    peak_score = float(scores.max())

    if num_tones == 1:
        # OOK scores are symmetric in [-1, 1] -- anti-correlated windows
        # look as different from a real peak as unrelated noise does. Use
        # |score| to estimate the spread of the noise / sidelobe cloud
        # around 0; the top 25% of |scores| is the correlated tail.
        abs_scores = np.abs(scores)
        cutoff = float(np.quantile(abs_scores, 0.75))
        noise_pool = scores[abs_scores <= cutoff]
    else:
        # N-FSK noise floor: bottom (2/M) of scores (adaptive quantile).
        # At M=2 sidelobes fill the lower half, so we go lower (Q0.25).
        # At higher M sidelobes are tighter and the lower half is fine.
        q = max(0.25, 1.0 - 2.0 / num_tones)
        noise_pool = scores[scores <= float(np.quantile(scores, q))]
    if noise_pool.size < 4:
        return []
    noise_centre = float(np.median(noise_pool))
    noise_mad = float(np.median(np.abs(noise_pool - noise_centre)))
    noise_sigma = max(2.0 * 1.4826 * noise_mad, 1e-9)

    # Peak-vs-noise gate. OOK's 2-symbol alphabet makes near-matches
    # unavoidable, so the sigma-based gate would reject valid peaks;
    # the sidelobe-based threshold below is the only filter we need
    # for that mode.
    if num_tones > 1 and peak_score < noise_centre + 6.0 * noise_sigma:
        return []

    # Sidelobe-based threshold. In the noise-only limit, correlator
    # scores at wrong alignments are ~Gaussian with std
    # ``1 / sqrt((M-1) * L)`` (M tones, L-symbol preamble). Expected
    # max of N such samples ≈ std * sqrt(2 * ln N). We scale that by
    # ``peak_score`` (amplitude-normalisation aside, peak is our
    # observed 1.0 reference). ``max(sigma-based, sidelobe-based)``
    # covers low-SNR (Gaussian noise dominates) and high-SNR (pattern-
    # vs-data sidelobes dominate) regimes without any M-specific
    # hard-coded constants.
    n_positions = max(scores.size, 2)
    sidelobe_max = math.sqrt(2.0 * math.log(n_positions)) / math.sqrt(
        max(1, num_tones - 1) * preamble_length
    )
    # ``sidelobe_max`` (above) is the sqrt(2 ln N) Gaussian-noise bound
    # -- valid when the correlator's off-peak scores are ~normal noise.
    # For low-M / short-alphabet modes, the specific PN preamble also
    # produces deterministic autocorrelation sidelobes above that noise
    # floor. Compute both, take the larger.
    det_sidelobe = _preamble_deterministic_sidelobe(num_tones)
    sidelobe_bound = max(sidelobe_max, det_sidelobe)
    if num_tones == 1:
        # OOK sidelobes aren't Gaussian; the sigma floor is unreliable.
        # Rely purely on the sidelobe-vs-peak bound.
        candidate_threshold = sidelobe_bound * peak_score
    else:
        candidate_threshold = max(
            noise_centre + 5.0 * noise_sigma,
            sidelobe_bound * peak_score,
        )

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
