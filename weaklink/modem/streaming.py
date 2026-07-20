"""Streaming helpers shared by the CLI and the Python API: pilot
signal generation, the streaming decoder, and the live-rx poll loop.
PTT lives in :mod:`weaklink.modem.ptt`.
"""

from __future__ import annotations

import logging
import time

import numpy as np

from weaklink.modem.audio import LiveInputStream, resolve_audio_target
from weaklink.modem.codec import ModemConfig, _block_symbol_length, decode
from weaklink.modem.constants import (
    LIVE_RX_POLL_MS,
    LIVE_RX_SNAPSHOT_EVERY_POLLS,
)
from weaklink.modem.waveform import modulate

_log = logging.getLogger("weaklink.streaming")


def pilot_signal(config: ModemConfig, duration_seconds: float) -> np.ndarray:
    """Random N-FSK symbols for ``duration_seconds``. All tones exercised
    uniformly so the coarse-offset FFT locks cleanly."""
    symbols_needed = max(1, int(round(duration_seconds * config.waveform.baud)))
    rng = np.random.default_rng(0xC0DE)
    symbols = rng.integers(0, config.waveform.num_tones, size=symbols_needed, dtype=np.int64)
    return modulate(symbols, config.waveform)


class StreamingRxDecoder:
    """Chunk-in, decoded-bytes-out streaming rx state. Same code path
    for live audio and WAV -- callers push audio chunks as they arrive
    and stdout-like ``output`` receives decoded bytes."""

    def __init__(self, config: ModemConfig, output) -> None:
        self.config = config
        self.output = output
        self.sample_rate = int(round(config.waveform.sample_rate))
        self.chunks: list = []
        self.samples_before_buffer = 0
        self.cursor = 0
        # Cross-call dedup so block copies that straddle two decode()
        # calls don't emit the block twice.
        self.streaming_state: dict = {}
        self._block_symbol_length = _block_symbol_length

        max_group_symbols = (
            config.sync_every_blocks * _block_symbol_length(config) * config.block_repeats
            + 32  # preamble length
        )
        max_group_seconds = max_group_symbols / config.waveform.baud
        self.max_window_samples = int(max(60.0, 3.0 * max_group_seconds) * self.sample_rate)

    def push(self, chunk) -> None:
        self.chunks.append(chunk)
        self.try_emit()

    def _write_out(self, decoded: bytes) -> None:
        if not decoded:
            return
        self.output.write(decoded)
        try:
            self.output.flush()
        except (AttributeError, OSError):
            pass

    def on_session_end(self) -> None:
        # Codec sets ``session_ended`` when it loses lock after having had
        # it. Reset the block-index dedup so the next TX starts fresh.
        if self.streaming_state.pop("session_ended", False):
            self.streaming_state.pop("emitted", None)

    def drain(self) -> None:
        """Flush at end-of-stream (WAV mode). Streaming decode until
        progress stalls, then batch decode over the tail so end-of-
        buffer slots still emit."""
        while self.try_emit():
            pass
        if not self.chunks:
            return
        buffer = np.concatenate(self.chunks).reshape(-1)
        cursor_in_buffer = max(0, self.cursor - self.samples_before_buffer)
        tail = buffer[cursor_in_buffer:]
        if tail.size == 0:
            return
        decoded = decode(
            tail, self.config, streaming=False,
            streaming_state=self.streaming_state,
        )
        self._write_out(decoded)
        self.chunks.clear()
        self.samples_before_buffer += buffer.size

    def _total_buffered(self) -> int:
        return self.samples_before_buffer + sum(c.size for c in self.chunks)

    def try_emit(self) -> bool:
        """Attempt one decode pass over the currently buffered audio.
        Returns True if the buffer actually shrank (i.e. progress made),
        so callers can loop drain() until it returns False."""
        if not self.chunks:
            return False
        total_samples_seen = self._total_buffered()
        preamble_length_symbols = 32
        block_symbols = self._block_symbol_length(self.config)
        min_group_symbols = 2 * preamble_length_symbols + block_symbols
        min_wait_samples = int(
            min_group_symbols / self.config.waveform.baud * self.sample_rate
        )
        if total_samples_seen - self.cursor < min_wait_samples:
            return False
        buffer = np.concatenate(self.chunks).reshape(-1)
        buffer_start_stream_pos = self.samples_before_buffer
        cursor_in_buffer = max(0, self.cursor - buffer_start_stream_pos)
        window = buffer[cursor_in_buffer:]
        if window.size < self.sample_rate:
            return False
        decoded, safe_cursor_offset = decode(
            window, self.config, streaming=True, streaming_state=self.streaming_state,
        )
        progress = safe_cursor_offset > 0 or bool(decoded)
        self._write_out(decoded)
        self.cursor = buffer_start_stream_pos + cursor_in_buffer + safe_cursor_offset

        while self.chunks:
            first_chunk_end = self.samples_before_buffer + self.chunks[0].size
            if first_chunk_end <= self.cursor:
                self.samples_before_buffer += self.chunks[0].size
                self.chunks.pop(0)
            else:
                break
        overflow = (
            self._total_buffered() - self.samples_before_buffer - self.max_window_samples
        )
        while overflow > 0 and self.chunks:
            drop = min(overflow, self.chunks[0].size)
            if drop >= self.chunks[0].size:
                self.samples_before_buffer += self.chunks[0].size
                self.chunks.pop(0)
            else:
                self.chunks[0] = self.chunks[0][drop:]
                self.samples_before_buffer += drop
            overflow = (
                self._total_buffered() - self.samples_before_buffer - self.max_window_samples
            )
            if self.cursor < self.samples_before_buffer:
                self.cursor = self.samples_before_buffer
        return progress


def audio_level_snapshot(pump: StreamingRxDecoder) -> None:
    """One-second peak + RMS snapshot; logs to ``weaklink.streaming``."""
    if not pump.chunks:
        return
    recent_needed = pump.sample_rate  # 1 second
    recent: list[np.ndarray] = []
    recent_len = 0
    for chunk in reversed(pump.chunks):
        recent.append(chunk)
        recent_len += chunk.size
        if recent_len >= recent_needed:
            break
    window = np.concatenate(list(reversed(recent))).reshape(-1)[-recent_needed:]
    window_float = window.astype(np.float64)
    peak = float(np.max(np.abs(window_float))) if window_float.size else 0.0
    rms = float(np.sqrt(np.mean(window_float ** 2))) if window_float.size else 0.0
    peak_db = 20.0 * np.log10(peak) if peak > 0 else float("-inf")
    rms_db = 20.0 * np.log10(rms) if rms > 0 else float("-inf")
    _log.info("audio: peak %+.1f dBFS, rms %+.1f dBFS", peak_db, rms_db)


def live_stream_decode(
    config: ModemConfig,
    *,
    audio_input: str,
    output,
) -> None:
    """Live streaming decode loop. Blocks until KeyboardInterrupt.
    Decoded bytes go to ``output`` (any object with ``.write(bytes)``
    and ``.flush()``)."""
    target = resolve_audio_target(audio_input, kind="input")
    if audio_input:
        _log.debug("audio input hint %r -> %s", audio_input, target.describe())
    sample_rate = int(round(config.waveform.sample_rate))

    pump = StreamingRxDecoder(config, output=output)
    _log.debug("live rx buffer cap: %.1f s", pump.max_window_samples / sample_rate)

    def _callback(indata_1d: np.ndarray) -> None:
        # Don't call pump.push -- try_emit runs numpy-heavy work and
        # mustn't block the audio callback. Just buffer.
        pump.chunks.append(indata_1d)

    _log.debug("live rx: polling every %d ms, source %s", LIVE_RX_POLL_MS, target.describe())

    poll_counter = 0
    try:
        with LiveInputStream(sample_rate=sample_rate, callback=_callback, target=target):
            while True:
                time.sleep(LIVE_RX_POLL_MS / 1000.0)
                poll_counter += 1
                pump.try_emit()
                pump.on_session_end()
                if poll_counter % LIVE_RX_SNAPSHOT_EVERY_POLLS == 0:
                    audio_level_snapshot(pump)
    except KeyboardInterrupt:
        _log.debug("live rx: keyboard interrupt, draining tail")
    pump.drain()
    try:
        output.flush()
    except (AttributeError, OSError):
        pass
