"""Public Python API for weaklink.modem.

Mirrors the CLI 1:1 -- every ``--modem-*`` flag has a matching kwarg
and every runtime mode (batch samples, WAV read/write, live audio
in/out, PTT, tune) is available end-to-end. No caller ever needs to
shuffle audio through their own sounddevice code to reach the modem;
pass the device name/id and the API drives it.

Both ``tx()`` and ``rx()`` route ``weaklink.*`` log records into an
optional ``logger=`` kwarg for callers who want to stream signal-level
events (peak / rms, coarse offset, per-slot decode outcomes, RS
corrections) without wiring their own handlers.
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

import numpy as np

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
from weaklink.modem.waveform import WaveformConfig


@dataclass(frozen=True)
class ModemOptions:
    """Shared modem-layer parameters used by both ``tx`` and ``rx``.
    Fields left as ``None`` fall back to the ``BAUD_PRESETS`` entry
    for the selected baud."""
    baud: float = 300.0
    num_tones: int = 4
    rs_data_bytes: int | None = None
    rs_parity_bytes: int | None = None
    rs_crc_enabled: bool = True
    block_repeats: int | None = None
    sync_every_blocks: int | None = None


@contextmanager
def _routed_loggers(logger: logging.Logger | None) -> Iterator[None]:
    """Temporarily route every ``weaklink.*`` log record into ``logger``.

    Attaches a forwarder to the ``weaklink`` root; children propagate up
    to it by default, so this catches ``weaklink.cli``, ``weaklink.audio``,
    ``weaklink.decode``, and any future descendant without a hard-coded
    name list. When ``logger`` is ``None`` this is a no-op.
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


def build_config(
    *,
    baud: float = 300.0,
    num_tones: int = 4,
    rs_data_bytes: int | None = None,
    rs_parity_bytes: int | None = None,
    rs_crc_enabled: bool = True,
    block_repeats: int | None = None,
    sync_every_blocks: int | None = None,
    tone_spacing_hz: float | None = None,
    tx_volume: int = 100,
) -> ModemConfig:
    """Assemble a ``ModemConfig`` from CLI-equivalent parameters,
    filling unset preset-driven knobs from ``BAUD_PRESETS``. Same
    resolution the CLI does."""
    if baud not in BAUD_PRESETS:
        raise ConfigError(f"baud {baud} is not supported; use one of {sorted(BAUD_PRESETS.keys())}")
    preset = BAUD_PRESETS[baud]
    if not 0 <= tx_volume <= 100:
        raise ConfigError(f"tx_volume must be 0-100 (got {tx_volume})")
    return ModemConfig(
        waveform=WaveformConfig(
            baud=baud,
            tone_spacing_hz=tone_spacing_hz if tone_spacing_hz is not None else preset["tone_spacing_hz"],
            num_tones=num_tones,
            amplitude=tx_volume / 100.0,
        ),
        rs_data_bytes=rs_data_bytes if rs_data_bytes is not None else int(preset["rs_data_bytes"]),
        rs_parity_bytes=rs_parity_bytes if rs_parity_bytes is not None else int(preset["rs_parity_bytes"]),
        rs_crc_enabled=rs_crc_enabled,
        sync_every_blocks=sync_every_blocks if sync_every_blocks is not None else int(preset["sync_every_blocks"]),
        block_repeats=block_repeats if block_repeats is not None else int(preset["block_repeats"]),
    )


def _tx_sample_iterator(config: ModemConfig, data: bytes | Iterable[bytes]) -> Iterator[np.ndarray]:
    """Leading pilot -> encoded blocks -> trailing pilot. Same pilot
    sizing rules as the CLI."""
    leading_pilot_seconds = max(
        LIVE_TX_PILOT_MIN_SECONDS,
        LIVE_TX_PILOT_MIN_SYMBOLS / config.waveform.baud,
    )
    leading_pilot = pilot_signal(config, leading_pilot_seconds).astype(np.float32)
    trailing_pilot = leading_pilot
    if isinstance(data, (bytes, bytearray)):
        chunks: Iterable[bytes] = [bytes(data)]
    else:
        chunks = data
    yield leading_pilot
    for audio in encode_stream(iter(chunks), config):
        yield audio
    yield trailing_pilot


def tx(
    data: bytes | Iterable[bytes] | None = None,
    *,
    baud: float = 300.0,
    num_tones: int = 4,
    rs_data_bytes: int | None = None,
    rs_parity_bytes: int | None = None,
    rs_crc_enabled: bool = True,
    block_repeats: int | None = None,
    sync_every_blocks: int | None = None,
    tone_spacing_hz: float | None = None,
    tx_volume: int = 100,
    wav: str | Path | None = None,
    audio_output: str | None = None,
    ptt: str | None = None,
    tune: bool = False,
    logger: logging.Logger | None = None,
) -> np.ndarray | None:
    """Encode ``data`` and dispatch to the requested sink.

    Sink selection (pick one; mirrors CLI):
    * ``wav=<path>``           write to WAV, return ``None``.
    * ``audio_output=<device>`` stream live to the device, return ``None``.
    * ``tune=True``            ignore ``data``, emit tone cycle to the
                              audio device until interrupted, return ``None``.
    * *(none of the above)*    return the encoded samples as ``ndarray``.

    ``audio_output`` accepts the same syntax as ``--modem-audio-output``:
    sounddevice index, name substring, Pulse sink name, ``pulse:<id>``,
    or a bare numeric Pulse id resolvable by pactl. ``ptt`` takes the
    same rigctld ``host:port`` (or ``None`` to skip PTT) that
    ``--hamlib-ptt`` does.
    """
    if wav is not None and audio_output is not None:
        raise ConfigError("pass either wav= or audio_output=, not both")
    if wav is not None and ptt is not None:
        raise ConfigError("ptt is only valid with live audio TX; drop wav or ptt")
    if tune and wav is not None:
        raise ConfigError("tune=True is a live-audio-only operation; drop wav")

    config = build_config(
        baud=baud,
        num_tones=num_tones,
        rs_data_bytes=rs_data_bytes,
        rs_parity_bytes=rs_parity_bytes,
        rs_crc_enabled=rs_crc_enabled,
        block_repeats=block_repeats,
        sync_every_blocks=sync_every_blocks,
        tone_spacing_hz=tone_spacing_hz,
        tx_volume=tx_volume,
    )
    with _routed_loggers(logger):
        if tune:
            _tx_tune(config, audio_output=audio_output, ptt=ptt)
            return None

        if data is None:
            raise ConfigError("data= is required unless tune=True")

        if wav is not None:
            from weaklink.modem.audio import write_wav_stream

            write_wav_stream(str(wav), _tx_sample_iterator(config, data), config.waveform.sample_rate)
            return None

        if audio_output is not None:
            from weaklink.modem.audio import play_stream

            with hamlib_ptt(ptt):
                play_stream(
                    _tx_sample_iterator(config, data),
                    config.waveform.sample_rate,
                    device=audio_output,
                )
            return None

        # Batch: no sink chosen, return the encoded ndarray.
        payload = data if isinstance(data, (bytes, bytearray)) else b"".join(data)
        return encode(bytes(payload), config)


def _tx_tune(
    config: ModemConfig,
    *,
    audio_output: str | None,
    ptt: str | None,
) -> None:
    """Emit every tone of the mode in round-robin, cycling until Ctrl-C."""
    from weaklink.modem.audio import play_stream
    from weaklink.modem.waveform import modulate

    cycle_symbols = np.arange(config.waveform.num_tones, dtype=np.int64)

    def _cycles() -> Iterator[np.ndarray]:
        while True:
            yield modulate(cycle_symbols, config.waveform).astype(np.float32)

    try:
        with hamlib_ptt(ptt):
            play_stream(_cycles(), config.waveform.sample_rate, device=audio_output)
    except KeyboardInterrupt:
        pass


class _CallbackWriter:
    """Duck-typed file-like: `.write(bytes)` -> callback. Used to feed
    ``_StreamingRxDecoder``'s decoded bytes to an ``on_bytes`` callable."""

    def __init__(self, callback: Callable[[bytes], None]) -> None:
        self._callback = callback

    def write(self, data: bytes) -> int:
        if data:
            self._callback(bytes(data))
        return len(data)

    def flush(self) -> None:  # noqa: D401
        pass


def rx(
    samples: np.ndarray | None = None,
    *,
    baud: float = 300.0,
    num_tones: int = 4,
    rs_data_bytes: int | None = None,
    rs_parity_bytes: int | None = None,
    rs_crc_enabled: bool = True,
    block_repeats: int | None = None,
    sync_every_blocks: int | None = None,
    tone_spacing_hz: float | None = None,
    wav: str | Path | None = None,
    audio_input: str | None = None,
    on_bytes: Callable[[bytes], None] | None = None,
    logger: logging.Logger | None = None,
) -> bytes | None:
    """Decode from the requested source.

    Source selection (pick one; mirrors CLI):
    * ``samples=<ndarray>``    batch decode, return ``bytes``.
    * ``wav=<path>``           read the WAV, decode, return ``bytes``.
                              If ``on_bytes=`` is given, chunks are
                              also fed to the callback as they land.
    * ``audio_input=<device>`` stream live from the device; blocks
                              until KeyboardInterrupt. Decoded bytes
                              go to ``on_bytes(chunk)`` if set, else
                              ``sys.stdout.buffer``. Return value is
                              ``None`` (streaming has no batch return).

    ``audio_input`` accepts the same syntax as ``--modem-audio-input``.
    ``on_bytes`` receives ``bytes`` chunks in stream order.
    """
    config = build_config(
        baud=baud,
        num_tones=num_tones,
        rs_data_bytes=rs_data_bytes,
        rs_parity_bytes=rs_parity_bytes,
        rs_crc_enabled=rs_crc_enabled,
        block_repeats=block_repeats,
        sync_every_blocks=sync_every_blocks,
        tone_spacing_hz=tone_spacing_hz,
    )
    with _routed_loggers(logger):
        if samples is not None:
            if wav is not None or audio_input is not None:
                raise ConfigError("pass at most one of samples=, wav=, audio_input=")
            decoded = decode(samples, config)
            if on_bytes and decoded:
                on_bytes(bytes(decoded))
            return decoded

        if wav is not None:
            if audio_input is not None:
                raise ConfigError("pass at most one of samples=, wav=, audio_input=")
            return _rx_from_wav(config, wav, on_bytes=on_bytes)

        if audio_input is not None:
            sink = _CallbackWriter(on_bytes) if on_bytes is not None else sys.stdout.buffer
            live_stream_decode(config, audio_input=audio_input, output=sink)
            return None

        raise ConfigError("rx() requires one of samples=, wav=, audio_input=")


def _rx_from_wav(
    config: ModemConfig,
    wav_path: str | Path,
    *,
    on_bytes: Callable[[bytes], None] | None,
) -> bytes:
    """WAV read -> streaming pump -> bytes. Also feeds each chunk to
    ``on_bytes`` as it lands so callers get incremental output."""
    from weaklink.modem.audio import read_wav_chunks

    collected = bytearray()

    def _emit(data: bytes) -> None:
        collected.extend(data)
        if on_bytes:
            on_bytes(bytes(data))

    pump = StreamingRxDecoder(config, output=_CallbackWriter(_emit))
    for chunk in read_wav_chunks(
        str(wav_path),
        chunk_seconds=0.1,
        expected_sample_rate=config.waveform.sample_rate,
    ):
        pump.push(chunk)
    pump.drain()
    return bytes(collected)


