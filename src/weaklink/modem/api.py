"""Public Python API for weaklink.modem.

Mirrors the CLI 1:1: every ``--modem-*`` flag maps to a kwarg and every
runtime mode (WAV read/write, live audio in/out, PTT, tune, batch samples)
is available end-to-end.

``tx()`` and ``rx()`` route ``weaklink.*`` log records into an optional
``logger=`` kwarg for callers who want to stream signal-level events
(peak/rms, coarse offset, per-slot decode outcomes, RS corrections)
without wiring their own handlers.
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

import numpy as np

from weaklink.modem.audio import play_stream, read_wav_chunks, write_wav_stream
from weaklink.modem.codec import ModemConfig, decode, encode, encode_stream
from weaklink.modem.constants import (
    BAUD_PRESETS,
    LIVE_TX_PILOT_MIN_SECONDS,
    LIVE_TX_PILOT_MIN_SYMBOLS,
)
from weaklink.modem.exceptions import ConfigError
from weaklink.modem.ptt import hamlib_ptt
from weaklink.modem.streaming import (
    StreamingRxDecoder,
    live_stream_decode,
    pilot_signal,
)
from weaklink.modem.waveform import WaveformConfig, modulate


@dataclass(frozen=True)
class ModemOptions:
    """Modem-layer parameters shared by both ``tx`` and ``rx``.

    Fields left as ``None`` fall back to the ``BAUD_PRESETS`` entry
    for the selected baud. Same knobs as the CLI's ``--modem-*`` flags.
    """
    baud: float = 300.0
    num_tones: int = 4
    rs_data_bytes: int | None = None
    rs_parity_bytes: int | None = None
    rs_crc_enabled: bool = True
    block_repeats: int | None = None
    sync_every_blocks: int | None = None
    tone_spacing_hz: float | None = None


def build_config(options: ModemOptions = ModemOptions(), *, tx_volume: int = 100) -> ModemConfig:
    """Resolve ``options`` (with preset fallbacks) into a full ``ModemConfig``.
    ``tx_volume`` (0-100) maps to waveform amplitude; RX ignores it."""
    if options.baud not in BAUD_PRESETS:
        raise ConfigError(f"baud {options.baud} is not supported; use one of {sorted(BAUD_PRESETS.keys())}")
    if not 0 <= tx_volume <= 100:
        raise ConfigError(f"tx_volume must be 0-100 (got {tx_volume})")
    preset = BAUD_PRESETS[options.baud]

    def pick(v: int | None, key: str) -> int:
        return v if v is not None else int(preset[key])

    return ModemConfig(
        waveform=WaveformConfig(
            baud=options.baud,
            tone_spacing_hz=options.tone_spacing_hz
            if options.tone_spacing_hz is not None
            else preset["tone_spacing_hz"],
            num_tones=options.num_tones,
            amplitude=tx_volume / 100.0,
        ),
        rs_data_bytes=pick(options.rs_data_bytes, "rs_data_bytes"),
        rs_parity_bytes=pick(options.rs_parity_bytes, "rs_parity_bytes"),
        rs_crc_enabled=options.rs_crc_enabled,
        sync_every_blocks=pick(options.sync_every_blocks, "sync_every_blocks"),
        block_repeats=pick(options.block_repeats, "block_repeats"),
    )


@contextmanager
def _routed_loggers(logger: logging.Logger | None) -> Iterator[None]:
    """Temporarily route every ``weaklink.*`` log record into ``logger``.

    Attaches a forwarder to the ``weaklink`` root; children propagate up
    to it by default, so this catches ``weaklink.cli``, ``weaklink.audio``,
    ``weaklink.decode``, etc. without a hard-coded name list. When
    ``logger`` is ``None`` this is a no-op.
    """
    if logger is None:
        yield
        return

    class _Forwarder(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            logger.handle(record)

    root = logging.getLogger("weaklink")
    handler = _Forwarder()
    prior_handlers = root.handlers.copy()
    prior_propagate = root.propagate
    prior_level = root.level
    try:
        root.handlers = [handler]
        root.propagate = False
        wanted_level = logger.level or logging.DEBUG
        if root.level == logging.NOTSET or root.level > wanted_level:
            root.setLevel(wanted_level)
        yield
    finally:
        root.handlers = prior_handlers
        root.propagate = prior_propagate
        root.setLevel(prior_level)


class _CallbackWriter:
    """File-like adapter: ``.write(bytes)`` -> callback. Feeds a
    ``StreamingRxDecoder``'s decoded bytes to an ``on_bytes`` callable."""

    def __init__(self, callback: Callable[[bytes], None]) -> None:
        self._callback = callback

    def write(self, data: bytes) -> int:
        if data:
            self._callback(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass


def _tx_samples(config: ModemConfig, data: bytes | Iterable[bytes]) -> Iterator[np.ndarray]:
    """Leading pilot -> encoded blocks -> trailing pilot. Same pilot
    sizing rules as the CLI."""
    pilot_seconds = max(
        LIVE_TX_PILOT_MIN_SECONDS,
        LIVE_TX_PILOT_MIN_SYMBOLS / config.waveform.baud,
    )
    pilot = pilot_signal(config, pilot_seconds).astype(np.float32)
    chunks: Iterable[bytes] = [bytes(data)] if isinstance(data, (bytes, bytearray)) else data
    yield pilot
    yield from encode_stream(iter(chunks), config)
    yield pilot


def _tune_samples(config: ModemConfig) -> Iterator[np.ndarray]:
    """Every tone of the mode in round-robin, cycling forever."""
    cycle = np.arange(config.waveform.num_tones, dtype=np.int64)
    while True:
        yield modulate(cycle, config.waveform).astype(np.float32)


def tx(
    data: bytes | Iterable[bytes] | None = None,
    options: ModemOptions = ModemOptions(),
    *,
    tx_volume: int = 100,
    wav: str | Path | None = None,
    audio_output: str | None = None,
    ptt: str | None = None,
    tune: bool = False,
    logger: logging.Logger | None = None,
) -> np.ndarray | None:
    """Encode ``data`` and dispatch to the requested sink.

    Pick one sink (mirrors CLI):
    * ``wav=<path>``           write to WAV, return ``None``.
    * ``audio_output=<dev>``   stream live to the device, return ``None``.
    * ``tune=True``            emit a tone cycle to the audio device
                              until interrupted; ignores ``data``.
    * *(none)*                 return the encoded samples as ``ndarray``.

    ``audio_output`` takes any syntax ``--modem-audio-output`` accepts
    (sounddevice index, name substring, ``pulse:<id>``, pactl-resolvable
    numeric id). ``ptt`` takes the ``host:port`` from ``--hamlib-ptt``.
    """
    if wav is not None and audio_output is not None:
        raise ConfigError("pass either wav= or audio_output=, not both")
    if wav is not None and ptt is not None:
        raise ConfigError("ptt is only valid with live audio TX; drop wav or ptt")
    if tune and wav is not None:
        raise ConfigError("tune=True is a live-audio-only operation; drop wav")

    config = build_config(options, tx_volume=tx_volume)
    with _routed_loggers(logger):
        if tune:
            try:
                with hamlib_ptt(ptt):
                    play_stream(_tune_samples(config), config.waveform.sample_rate, device=audio_output)
            except KeyboardInterrupt:
                pass
            return None

        if data is None:
            raise ConfigError("data= is required unless tune=True")

        samples = _tx_samples(config, data)
        if wav is not None:
            write_wav_stream(str(wav), samples, config.waveform.sample_rate)
            return None

        if audio_output is not None:
            with hamlib_ptt(ptt):
                play_stream(samples, config.waveform.sample_rate, device=audio_output)
            return None

        # Batch: no sink chosen, return the encoded ndarray (no pilots).
        payload = data if isinstance(data, (bytes, bytearray)) else b"".join(data)
        return encode(bytes(payload), config)


def rx(
    samples: np.ndarray | None = None,
    options: ModemOptions = ModemOptions(),
    *,
    wav: str | Path | None = None,
    audio_input: str | None = None,
    on_bytes: Callable[[bytes], None] | None = None,
    logger: logging.Logger | None = None,
) -> bytes | None:
    """Decode from the requested source.

    Pick one (mirrors CLI):
    * ``samples=<ndarray>``    batch decode, return ``bytes``.
    * ``wav=<path>``           read the WAV, decode, return ``bytes``.
                              If ``on_bytes=`` is given, chunks are
                              also fed to the callback as they land.
    * ``audio_input=<dev>``    stream live from the device; blocks
                              until KeyboardInterrupt. Decoded chunks
                              go to ``on_bytes(chunk)`` if set, else
                              ``sys.stdout.buffer``. Return: ``None``.
    """
    sources = sum(x is not None for x in (samples, wav, audio_input))
    if sources == 0:
        raise ConfigError("rx() requires one of samples=, wav=, audio_input=")
    if sources > 1:
        raise ConfigError("pass at most one of samples=, wav=, audio_input=")

    config = build_config(options)
    with _routed_loggers(logger):
        if samples is not None:
            decoded = decode(samples, config)
            if on_bytes and decoded:
                on_bytes(bytes(decoded))
            return decoded

        if wav is not None:
            collected = bytearray()

            def _emit(data: bytes) -> None:
                collected.extend(data)
                if on_bytes:
                    on_bytes(bytes(data))

            pump = StreamingRxDecoder(config, output=_CallbackWriter(_emit))
            for chunk in read_wav_chunks(
                str(wav),
                chunk_seconds=0.1,
                expected_sample_rate=config.waveform.sample_rate,
            ):
                pump.push(chunk)
            pump.drain()
            return bytes(collected)

        # audio_input is not None (validated above).
        sink = _CallbackWriter(on_bytes) if on_bytes is not None else sys.stdout.buffer
        live_stream_decode(config, audio_input=audio_input, output=sink)
        return None
