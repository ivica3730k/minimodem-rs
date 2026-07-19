"""Streaming modem CLI. Bytes on stdin/stdout, samples via WAV or live audio.
Baud presets in ``BAUD_PRESETS`` (45/300/1200); ``--modem-*`` flags override.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Sequence

from weaklink.modem.codec import ModemConfig, decode, encode, encode_stream
from weaklink.modem.waveform import WaveformConfig

DEFAULT_LOG_PATH = Path("log.txt")
_log = logging.getLogger("weaklink.cli")


# Per-baud presets. ``tone_spacing_hz`` widened at low bauds so the four
# tones spread across enough Hz to survive room modes and mic roll-off.
# Only these bauds are supported; anything else raises NotImplementedError.
BAUD_PRESETS: dict[float, dict[str, float]] = {
    45.0:   dict(tone_spacing_hz=200.0, rs_data_bytes=16, rs_parity_bytes=8,  block_repeats=4, sync_every_blocks=4),
    300.0:  dict(tone_spacing_hz=300.0, rs_data_bytes=16, rs_parity_bytes=8,  block_repeats=2, sync_every_blocks=4),
    1200.0: dict(tone_spacing_hz=1200.0, rs_data_bytes=16, rs_parity_bytes=8, block_repeats=2, sync_every_blocks=4),
}


def _add_modem_args(sub: argparse.ArgumentParser) -> None:
    """All modem-side knobs. ``--modem-*``.

    Presetable knobs default to ``None`` at the CLI layer so we can detect
    "user didn't set this" and fill from ``BAUD_PRESETS`` instead. Explicit
    ``--modem-*`` values still win.
    """
    modem = sub.add_argument_group("modem", "modem-layer configuration + sample-side I/O")
    modem.add_argument("--modem-baud", type=float, default=300.0, dest="modem_baud",
                       help=f"Symbol rate. Supported values: {sorted(BAUD_PRESETS.keys())}.")
    modem.add_argument(
        "--modem-rs-data-bytes",
        type=int,
        default=None,
        dest="modem_rs_data_bytes",
        help="RS data bytes per block. Preset default depends on --modem-baud.",
    )
    modem.add_argument(
        "--modem-rs-parity-bytes",
        type=int,
        default=None,
        dest="modem_rs_parity_bytes",
        help="RS parity bytes per block. Preset default depends on --modem-baud.",
    )
    modem.add_argument(
        "--modem-no-rs-crc",
        dest="modem_rs_crc_enabled",
        action="store_false",
        default=True,
        help="Skip the CRC-32 inside the RS-protected region.",
    )
    modem.add_argument(
        "--modem-sync-every-blocks",
        type=int,
        default=None,
        dest="modem_sync_every_blocks",
        help="Preamble inserted every N data blocks. Preset default: 4.",
    )
    modem.add_argument(
        "--modem-num-tones",
        type=int,
        default=None,
        dest="modem_num_tones",
        choices=(2, 4, 8, 16, 32),
        help="Number of FSK tones (power of 2). 4 (default) is standard; "
        "8/16/32 pack more bits per symbol at wider bandwidth and worse "
        "cliff. 2 halves throughput but fits narrow audio paths. TX / RX "
        "must match.",
    )
    modem.add_argument(
        "--modem-block-repeats",
        type=int,
        default=None,
        dest="modem_block_repeats",
        help="Each block transmitted N times, round-robin. Preset default depends on --modem-baud.",
    )
    modem.add_argument(
        "--modem-wav",
        type=Path,
        default=None,
        dest="modem_wav",
        help="Read from / write to a WAV file instead of the live audio device.",
    )
    modem.add_argument(
        "--modem-audio-output",
        type=str,
        default=None,
        dest="modem_audio_output",
        help="Audio output device for tx. Accepts a sounddevice index (e.g. '4'), "
        "a substring of a device name (e.g. 'USB'), or a Pulse/PipeWire sink name "
        "(e.g. 'virt') -- see `sounddevice.query_devices()` and `pactl list short sinks`. "
        "Default: OS default output.",
    )
    modem.add_argument(
        "--modem-audio-input",
        type=str,
        default=None,
        dest="modem_audio_input",
        help="Audio input device for rx. Same syntax as --modem-audio-output but "
        "matches against input devices / Pulse source names (e.g. 'virt.monitor'). "
        "Default: OS default input.",
    )
    modem.add_argument(
        "--modem-debug",
        dest="modem_debug",
        action="store_true",
        default=False,
        help="Verbose diagnostics (DEBUG level) in the log file: per-group decode "
        "results, offset estimates, etc.",
    )
    modem.add_argument(
        "--modem-log-file",
        type=Path,
        default=DEFAULT_LOG_PATH,
        dest="modem_log_file",
        help=f"Path to the log file (default: ./{DEFAULT_LOG_PATH}). "
        "stdout/stderr are never used for diagnostics.",
    )


def _resolve_version() -> str:
    """Read installed package version. Baked in at binary build time."""
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("weaklink-9a3ice")
    except Exception:
        return "unknown"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="weaklink-9a3ice", description="Streaming 4-FSK modem.")
    parser.add_argument("--version", action="version", version=f"weaklink-9a3ice {_resolve_version()}")
    subparsers = parser.add_subparsers(dest="direction", required=True)
    tx_parser = subparsers.add_parser("tx", help="Encode stdin bytes and transmit (or write to WAV).")
    _add_modem_args(tx_parser)
    rx_parser = subparsers.add_parser("rx", help="Receive (or read WAV) and decode to stdout bytes.")
    _add_modem_args(rx_parser)
    return parser


def _pick_preset(baud: float) -> dict[str, float]:
    """Look up the preset for ``baud``. Only the four tested bauds are supported."""
    if baud not in BAUD_PRESETS:
        raise NotImplementedError(
            f"baud {baud} is not supported; use one of {sorted(BAUD_PRESETS.keys())}"
        )
    return BAUD_PRESETS[baud]


def _make_config(args: argparse.Namespace) -> ModemConfig:
    preset = _pick_preset(args.modem_baud)
    rs_data_bytes = args.modem_rs_data_bytes if args.modem_rs_data_bytes is not None else int(preset["rs_data_bytes"])
    rs_parity_bytes = args.modem_rs_parity_bytes if args.modem_rs_parity_bytes is not None else int(preset["rs_parity_bytes"])
    sync_every = args.modem_sync_every_blocks if args.modem_sync_every_blocks is not None else int(preset["sync_every_blocks"])
    block_repeats = args.modem_block_repeats if args.modem_block_repeats is not None else int(preset["block_repeats"])
    num_tones = args.modem_num_tones if args.modem_num_tones is not None else 4
    return ModemConfig(
        waveform=WaveformConfig(
            baud=args.modem_baud,
            tone_spacing_hz=preset["tone_spacing_hz"],
            num_tones=num_tones,
        ),
        rs_data_bytes=rs_data_bytes,
        rs_parity_bytes=rs_parity_bytes,
        rs_crc_enabled=args.modem_rs_crc_enabled,
        sync_every_blocks=sync_every,
        block_repeats=block_repeats,
    )


#: Pilot each side of live-tx: wakes the sink from IDLE (~50 ms) and
#: gives the coarse-offset FFT real 4-FSK tone energy to lock onto.
_LIVE_TX_PILOT_MIN_SECONDS: float = 0.2

#: Pilot must also exceed the preamble in symbol space so back-to-back
#: tx buffers keep > 2 * preamble_length between adjacent preambles.
#: Matters at low baud where 0.2 s is only ~9 symbols.
_LIVE_TX_PILOT_MIN_SYMBOLS: int = 40

#: Floor on total live-tx duration. 1200-baud single-char is ~250 ms of
#: signal -- too short to give RX two clean poll windows. Pad to 1 s.
_LIVE_TX_MIN_SECONDS: float = 1.0


def _pilot_signal(config: ModemConfig, duration_seconds: float) -> "np.ndarray":  # noqa: F821
    """Random M-FSK symbols for ``duration_seconds``. All tones exercised
    uniformly so the coarse-offset FFT locks cleanly."""
    import numpy as np

    from weaklink.modem.waveform import modulate

    symbols_needed = max(1, int(round(duration_seconds * config.waveform.baud)))
    rng = np.random.default_rng(0xC0DE)
    symbols = rng.integers(0, config.waveform.num_tones, size=symbols_needed, dtype=np.int64)
    return modulate(symbols, config.waveform)


def _run_tx(args: argparse.Namespace) -> int:
    import numpy as np

    from weaklink.modem.audio import play_stream, write_wav_stream

    config = _make_config(args)
    sample_rate = config.waveform.sample_rate

    # Same encoder + same pilot padding regardless of output. WAV sinks
    # and live-audio sinks both consume the same sample-chunk iterator;
    # only the last hop differs. Keeping one path means WAV tests
    # exercise exactly what live rx sees.
    leading_pilot_seconds = max(
        _LIVE_TX_PILOT_MIN_SECONDS,
        _LIVE_TX_PILOT_MIN_SYMBOLS / config.waveform.baud,
    )
    leading_pilot = _pilot_signal(config, leading_pilot_seconds).astype(np.float32)
    trailing_pilot = leading_pilot  # same duration each side

    def stdin_chunks() -> "Iterable[bytes]":  # noqa: F821
        # Read modest-sized chunks so the encoder can start emitting
        # audio before the whole input arrives (matters for pipes like
        # ``tail -f | tx`` or slow-generating commands).
        while True:
            block = sys.stdin.buffer.read(4096)
            if not block:
                return
            yield block

    def sample_chunks():
        yield leading_pilot
        for audio in encode_stream(stdin_chunks(), config):
            yield audio
        yield trailing_pilot

    if args.modem_wav is not None:
        write_wav_stream(args.modem_wav, sample_chunks(), sample_rate)
    else:
        play_stream(sample_chunks(), sample_rate, device=args.modem_audio_output)
    return 0


def _run_rx(args: argparse.Namespace) -> int:
    config = _make_config(args)
    if args.modem_wav is not None:
        # WAV mode drives the same streaming decoder as live rx, just
        # from a WAV chunk iterator instead of a mic callback -- same
        # code path exercised by tests.
        from weaklink.modem.audio import read_wav_chunks

        pump = _StreamingRxPump(config, output=sys.stdout.buffer)
        for chunk in read_wav_chunks(
            args.modem_wav, chunk_seconds=0.1,
            expected_sample_rate=config.waveform.sample_rate,
        ):
            pump.push(chunk)
        pump.drain()
        sys.stdout.buffer.flush()
        return 0

    # Live mode: streaming decode. As samples come in from the audio device we
    # re-decode the growing buffer once per second and print any newly-decoded
    # bytes to stdout immediately. Ctrl-C stops recording.
    return _live_stream_decode(config, audio_input=args.modem_audio_input)


class _StreamingRxPump:
    """Chunk-in, decoded-bytes-out streaming rx state. Same code path
    for live audio and WAV -- callers push audio chunks as they arrive
    and stdout-like ``output`` receives decoded bytes."""

    def __init__(self, config: ModemConfig, output) -> None:
        import numpy as np

        from weaklink.modem.codec import _block_symbol_length  # noqa: WPS433

        self._np = np
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

    def drain(self) -> None:
        """Flush at end-of-stream (WAV mode). Streaming decode until
        progress stalls, then batch decode over the tail so end-of-
        buffer slots still emit."""
        np = self._np
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
        if decoded:
            self.output.write(decoded)
            try:
                self.output.flush()
            except (AttributeError, OSError):
                pass
        self.chunks.clear()
        self.samples_before_buffer += buffer.size

    def _total_buffered(self) -> int:
        return self.samples_before_buffer + sum(c.size for c in self.chunks)

    def try_emit(self) -> bool:
        """Attempt one decode pass over the currently buffered audio.
        Returns True if the buffer actually shrank (i.e. progress made),
        so callers can loop drain() until it returns False."""
        np = self._np
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
        if decoded:
            self.output.write(decoded)
            try:
                self.output.flush()
            except (AttributeError, OSError):
                pass
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


def _live_stream_decode(config: ModemConfig, *, audio_input: str | None = None) -> int:
    import os
    import time

    import numpy as np

    from weaklink.modem.audio import LiveInputStream, resolve_audio_target

    hint = audio_input if audio_input else os.environ.get("PULSE_SOURCE")
    target = resolve_audio_target(hint, kind="input")
    if hint:
        _log.debug("audio input hint %r -> %s", hint, target.describe())
    sample_rate = int(round(config.waveform.sample_rate))

    pump = _StreamingRxPump(config, output=sys.stdout.buffer)
    _log.debug(
        "live rx buffer cap: %.1f s",
        pump.max_window_samples / sample_rate,
    )

    def _callback(indata_1d: np.ndarray) -> None:
        # Don't call pump.push here -- try_emit runs numpy-heavy work
        # and mustn't block the audio callback. Just buffer the chunk.
        pump.chunks.append(indata_1d)

    _log.debug("live rx: polling every 100 ms, source %s", target.describe())

    def _log_audio_snapshot() -> None:
        """One-second audio-level snapshot: peak + RMS."""
        if not pump.chunks:
            return
        recent_needed = sample_rate  # 1 second
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

    poll_ms = 100
    snapshot_every_polls = 10  # 1 s at 100 ms poll
    poll_counter = 0
    try:
        with LiveInputStream(sample_rate=sample_rate, callback=_callback, target=target):
            while True:
                time.sleep(poll_ms / 1000.0)
                poll_counter += 1
                pump.try_emit()
                if poll_counter % snapshot_every_polls == 0:
                    _log_audio_snapshot()
    except KeyboardInterrupt:
        _log.debug("live rx: keyboard interrupt, exiting")
    return 0


def _configure_logging(log_path: Path, debug: bool) -> None:
    """Send all diagnostics to ``log_path``. stdout/stderr stay clean."""
    handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("weaklink")
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    # Clear any handlers a previous main() call added.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.propagate = False


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.modem_log_file, args.modem_debug)
    _log.debug("weaklink-9a3ice %s starting", args.direction)
    try:
        if args.direction == "tx":
            return _run_tx(args)
        return _run_rx(args)
    finally:
        logging.shutdown()


if __name__ == "__main__":
    sys.exit(main())
