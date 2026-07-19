"""Streaming modem codec.

Wire format::

    [PREAMBLE] [slot] [PREAMBLE] [slot] ... [PREAMBLE] [slot] [PREAMBLE]

Every slot carries one RS-encoded block. Data area is
``[length][block_index][payload...][zero_pad]`` (1-byte length header
strips trailing NUL; 1-byte index picks the output position and lets
duplicate copies dedupe). Slot: RS(N,K)+CRC -> K=7 rate-1/2 conv ->
interleave -> 4-FSK. Preamble between every slot (13% overhead) so any
slot decodes standalone; message boundaries are inferred from
non-block-length spans between adjacent preambles.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import Iterable, Iterator

import numpy as np

from weaklink.modem import fec
from weaklink.modem.interleaver import (
    InterleaverConfig,
    cycle_size as _interleaver_cycle_size,
    deinterleave_soft,
    interleave,
)
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
    codec = config.rs_codec()
    info_bits = codec.config.block_size * 8
    coded_bits = 2 * (info_bits + fec.CONSTRAINT_LENGTH - 1)
    interleaved = _round_up_multiple(coded_bits, config.interleaver.block_size)
    padded = _round_up_multiple(interleaved, BITS_PER_SYMBOL)
    return padded // BITS_PER_SYMBOL


def _encode_one_block(payload: bytes, config: ModemConfig, seed_slot: int) -> np.ndarray:
    codec = config.rs_codec()
    rs_encoded = codec.encode(payload)
    payload_bits = _bytes_to_bits_msb(rs_encoded)
    coded = fec.encode(payload_bits)
    interleaved = interleave(coded, config.interleaver, block_index=seed_slot)
    padded = _pad_to_multiple(interleaved, BITS_PER_SYMBOL)
    return bits_to_symbols(padded)


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
        raise ValueError(
            f"rs_data_bytes must be >= {_HEADER_BYTES + 1} (header + 1 payload byte)"
        )
    if data_bytes > 256:
        raise ValueError("rs_data_bytes must be <= 256 (length header is 1 byte)")


def _frame_block(chunk: bytes, block_index: int, payload_per_block: int) -> bytes:
    return (
        bytes([len(chunk), (block_index >> 8) & 0xFF, block_index & 0xFF])
        + chunk
        + b"\x00" * (payload_per_block - len(chunk))
    )


def encode_stream(
    byte_iter: "Iterable[bytes]", config: ModemConfig
) -> "Iterator[np.ndarray]":
    """Generator: consume bytes from ``byte_iter``, yield float32 audio
    chunks. Emits one audio chunk per slot (leading preamble + block,
    repeated ``block_repeats`` times per block), then a trailing preamble
    marker. block_index wraps at 65535; anything longer needs another
    tx session.

    Wire layout, per stream: ``[pre][slot 0][pre][slot 0]... x R
    [pre][slot 1]...``. Copies of the same block are adjacent (we don't
    know the total block count upfront), so RX dedupes by block_index.
    """
    codec = config.rs_codec()
    data_bytes = codec.config.data_bytes
    _validate_data_bytes(data_bytes)
    payload_per_block = data_bytes - _HEADER_BYTES

    pre_audio = modulate(PREAMBLE_SYMBOLS, config.waveform)

    def emit_block(chunk_bytes: bytes, block_index: int) -> "Iterator[np.ndarray]":
        framed = _frame_block(chunk_bytes, block_index, payload_per_block)
        for copy_index in range(config.block_repeats):
            seed = _copy_seed_slot(block_index, copy_index, config.block_repeats)
            block_symbols = _encode_one_block(framed, config, seed_slot=seed)
            merged = np.concatenate([PREAMBLE_SYMBOLS, block_symbols])
            yield modulate(merged, config.waveform)

    buffer = bytearray()
    block_index = 0
    emitted_any = False
    for chunk in byte_iter:
        buffer.extend(chunk)
        while len(buffer) >= payload_per_block:
            if block_index > _MAX_BLOCK_INDEX:
                raise ValueError(
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
    streaming_state: dict | None = None,
):
    """Decode audio to bytes; undecodable blocks are dropped.

    Batch mode (``streaming=False``) returns ``bytes``. Streaming mode
    returns ``(bytes, safe_cursor_samples)`` -- cursor is the last real
    preamble; keep it and slice everything before for the next call.

    Cross-call state (``streaming_state``): a mutable dict the caller
    passes in and reuses across successive decode() calls in one live
    session. Used to dedupe blocks whose copies straddle a poll
    boundary -- with ``block_repeats > 1``, a block's copies can land in
    two adjacent RX calls and both calls would otherwise emit it. Reset
    the dict (or pass a fresh one) to start a new logical session.
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

    # 1. Global coarse offset (FFT-based, handles big LO drift). In
    # streaming mode we cache the last estimate across calls -- LO
    # drift is slow and the FFT is one of the more expensive stages.
    # Per-preamble fine tracking still runs on every slot.
    #
    # Invariant: the cache is only trusted after a call that actually
    # found preambles with it. First-call-on-silence would otherwise
    # estimate an FFT peak from noise, cache the garbage, and stick
    # to it forever once real signal arrives. If no peaks are found
    # this call, invalidate the cache so the next one re-estimates.
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

    preamble = PREAMBLE_SYMBOLS
    peaks = _find_preamble_peaks(coarse_magnitudes, preamble, config)
    _log.debug("preamble peaks found: %d at symbol offsets %s", len(peaks), peaks[:8])
    if not peaks:
        _log.debug("no preambles above threshold")
        # Coarse-offset cache stays unproven: drop it so the next call
        # re-runs the FFT against fresher audio.
        if streaming and streaming_state is not None:
            streaming_state.pop("coarse_offset_hz", None)
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

    # 4. Every adjacent preamble pair brackets one slot = one RS block.
    # A span of 0 between preambles is a message boundary (one tx's
    # trailing preamble sitting right against the next tx's leading one)
    # -- flush the assembled prefix and start a new message.
    #
    # For batch mode we also try to project a virtual leading preamble
    # one slot's-worth before the first real one, and a virtual trailing
    # one slot's-worth after the last real one. That recovers single-slot
    # transmissions where the head or tail preamble was chopped off; the
    # missing symbols get zero-padded and RS mops up.
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

    def _flush_message(msg: dict[int, bytes]) -> None:
        # Emit in block_index order. Missing indices leave a gap (a
        # single unrecoverable slot doesn't take the whole tail of a
        # long stream with it). Skip indices we've already emitted in
        # a previous streaming call.
        for i in sorted(msg.keys()):
            if i in emitted_indices:
                continue
            output.extend(msg[i])
            emitted_indices.add(i)

    stride = block_length + len(preamble)
    current_msg: dict[int, bytes] = {}
    # Expected block_index / copies-of-this-block-seen: feeds the seed
    # search's candidate order so the common case (no missed slots) hits
    # on the first try. When block_repeats > 1 we expect the same
    # block_index for R slots in a row, then advance.
    expected_block_index = 0
    copies_seen_this_block = 0
    # Soft LLRs of consecutive slots that failed to decode independently;
    # once we have block_repeats of them we try soft-LLR combining.
    combining_buffer: list[np.ndarray] = []

    def _record_block(
        decoded: bytes, errors: int, header_block_index: int,
        msg_dict: dict[int, bytes],
    ) -> None:
        length = decoded[0]
        payload_area_size = len(decoded) - _HEADER_BYTES
        if length > payload_area_size:
            length = payload_area_size
        if header_block_index not in msg_dict:
            msg_dict[header_block_index] = bytes(
                decoded[_HEADER_BYTES : _HEADER_BYTES + length]
            )

    slot_i = 0
    while slot_i < len(peaks) - 1:
        slot_start = peaks[slot_i] + len(preamble)
        slot_end = peaks[slot_i + 1]
        span = slot_end - slot_start
        if abs(span - block_length) > 4:
            # Not a valid slot span. Two cases:
            # (a) peaks[slot_i + 1] is a spurious mid-message hit --
            #     peaks[slot_i] and peaks[slot_i + 2] then sit at the
            #     usual stride and we can decode across the spurious
            #     peak by skipping it.
            # (b) real message boundary (adjacent preambles / pilot
            #     gap between separate tx sessions) -- flush + advance.
            spurious = False
            if slot_i + 2 < len(peaks):
                two_step = peaks[slot_i + 2] - peaks[slot_i]
                spurious = abs(two_step - stride) <= 4
            if spurious:
                _log.debug("slot %d: dropping spurious peak %d", slot_i, peaks[slot_i + 1])
                del peaks[slot_i + 1]
                del per_peak_offsets[slot_i + 1]
                continue  # retry with the same slot_i
            _log.debug("slot %d span %d: message boundary", slot_i, span)
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
            soft = soft_bits_from_magnitudes(slot_mags)
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
                    _record_block(
                        decoded, errors_corrected, header_block_index,
                        current_msg,
                    )
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
                # Independent decode failed. Buffer the soft LLRs and, once
                # we've got block_repeats copies, try summing their
                # deinterleaved LLRs -- classical soft-combining. This is
                # the diversity gain the per-copy permutation was designed
                # for: two marginal copies together can clear what neither
                # can on its own.
                combining_buffer.append(soft)
                if len(combining_buffer) >= config.block_repeats:
                    dec, errs, seed = _decode_combined_copies(
                        combining_buffer, config, codec, expected_block_index
                    )
                    if dec is not None and len(dec) >= _HEADER_BYTES:
                        header_block_index = (dec[1] << 8) | dec[2]
                        _record_block(dec, errs, header_block_index, current_msg)
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
    # Vectorised correlator: for each tone k, mask marks where the
    # preamble expects that tone. Cross-correlate the tone's magnitude
    # column with the mask; sum contributions across the four tones.
    # ``wanted[offset]`` = Σ_i magnitudes[offset + i, preamble[i]].
    wanted = np.zeros(max_offset + 1, dtype=np.float64)
    preamble_int = preamble.astype(np.int64)
    for tone in range(NUM_TONES):
        mask = (preamble_int == tone).astype(np.float64)
        if mask.any():
            wanted += np.correlate(magnitudes[:, tone], mask, mode="valid")
    # Old inner-loop math simplifies to: raw = (4*wanted - total) / 3.
    raw = (NUM_TONES * wanted - windowed_total) / (NUM_TONES - 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        scores = np.where(windowed_total > 1e-12, raw / windowed_total, 0.0)

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
