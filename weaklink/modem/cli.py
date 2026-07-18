"""Streaming modem CLI.

Byte-side I/O is always stdin/stdout — use shell redirection for files or
pipes. Sample-side I/O is either a WAV file (``--modem-wav``) or the default
live audio device.

Presets: for each tested baud in ``BAUD_PRESETS`` (9, 45, 300, 1200) the RS
config, block-repeat count, and sync marker density default to the values
that measured best in the AWGN benchmark. Any explicit ``--modem-*`` flag
overrides the preset. Off-preset baud rates fall back to the 300-baud
preset with a stderr warning.

    echo -n "hello over air" | weaklink-9a3ice tx --modem-wav out.wav
    cat message.txt         | weaklink-9a3ice tx                     # live TX
    weaklink-9a3ice rx --modem-wav out.wav > received.bin
    weaklink-9a3ice rx                                                # live RX, Ctrl-C to stop
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Sequence

from weaklink.modem.codec import ModemConfig, decode, encode
from weaklink.modem.waveform import WaveformConfig

DEFAULT_LOG_PATH = Path("log.txt")
_log = logging.getLogger("weaklink.cli")


# Per-baud preset: RS parameters, block repetition, sync marker density
# picked from the measured AWGN benchmark cliff-optimum for each tested baud.
# Off-preset baud rates fall back to the 300-baud preset with a warning.
#: Hard-coded per-baud presets. ``tone_spacing_hz`` widens the tone stack at
#: low bauds (default ``spacing = baud`` clusters the tones inside <200 Hz,
#: which hits room modes and mic-response variation hard on acoustic paths).
#: Only these bauds are supported; anything else raises NotImplementedError.
BAUD_PRESETS: dict[float, dict[str, float]] = {
    9.0:    dict(tone_spacing_hz=100.0, rs_data_bytes=16, rs_parity_bytes=8,  block_repeats=2, sync_every_blocks=4),
    45.0:   dict(tone_spacing_hz=200.0, rs_data_bytes=32, rs_parity_bytes=8,  block_repeats=2, sync_every_blocks=4),
    300.0:  dict(tone_spacing_hz=300.0, rs_data_bytes=16, rs_parity_bytes=8,  block_repeats=1, sync_every_blocks=4),
    1200.0: dict(tone_spacing_hz=1200.0, rs_data_bytes=16, rs_parity_bytes=8, block_repeats=1, sync_every_blocks=4),
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
    modem.add_argument("--modem-sample-rate", type=float, default=48_000.0, dest="modem_sample_rate")
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
    return ModemConfig(
        waveform=WaveformConfig(
            baud=args.modem_baud,
            sample_rate=args.modem_sample_rate,
            tone_spacing_hz=preset["tone_spacing_hz"],
        ),
        rs_data_bytes=rs_data_bytes,
        rs_parity_bytes=rs_parity_bytes,
        rs_crc_enabled=args.modem_rs_crc_enabled,
        sync_every_blocks=sync_every,
        block_repeats=block_repeats,
    )


#: Pilot duration on each side of the modem signal on live-tx paths. Only
#: two hard requirements: (a) wake the sink from IDLE, which takes ~50 ms;
#: (b) give the coarse-offset FFT some real 4-FSK tone energy so it locks
#: to offset 0 even before the payload preamble arrives.
_LIVE_TX_PILOT_MIN_SECONDS: float = 0.2

#: Floor on total live-tx duration. A 1200-baud single-char payload is
#: ~250 ms of modem signal -- even with 200 ms pilots on each side the
#: whole transmission fits inside RX's first buffer poll, and the leading
#: preamble ends up straddling the pilot/signal boundary. Padding to at
#: least ~1 s of total audio gives RX two clean poll windows to lock onto
#: the preamble pair. For 45/9 baud the natural signal already exceeds
#: this floor so no extra pilot is added.
_LIVE_TX_MIN_SECONDS: float = 1.0


def _pilot_signal(config: ModemConfig, duration_seconds: float) -> "np.ndarray":  # noqa: F821
    """Produce ``duration_seconds`` of 4-FSK pilot -- modem-tone audio that
    carries no data. Pseudo-random symbol sequence so all four tones are
    exercised uniformly and the coarse-offset FFT sees real tone energy at
    every expected position."""
    import numpy as np

    from weaklink.modem.waveform import NUM_TONES, modulate

    symbols_needed = max(1, int(round(duration_seconds * config.waveform.baud)))
    rng = np.random.default_rng(0xC0DE)
    symbols = rng.integers(0, NUM_TONES, size=symbols_needed, dtype=np.int64)
    return modulate(symbols, config.waveform)


def _run_tx(args: argparse.Namespace) -> int:
    import numpy as np

    config = _make_config(args)
    payload = sys.stdin.buffer.read()
    samples = encode(payload, config)
    if args.modem_wav is not None:
        from weaklink.modem.audio import write_wav

        write_wav(args.modem_wav, samples, config.waveform.sample_rate)
    else:
        from weaklink.modem.audio import play

        sample_rate = config.waveform.sample_rate
        signal_seconds = len(samples) / sample_rate
        # Pilot each side: at least ``_LIVE_TX_PILOT_MIN_SECONDS`` for
        # sink-warmup / FFT lock, more if the signal is short enough that
        # total tx would fall below ``_LIVE_TX_MIN_SECONDS``.
        pilot_each_side = max(
            _LIVE_TX_PILOT_MIN_SECONDS,
            (_LIVE_TX_MIN_SECONDS - signal_seconds) / 2.0,
        )
        pilot = _pilot_signal(config, pilot_each_side).astype(np.float32)
        padded = np.concatenate([pilot, samples, pilot])
        play(padded, sample_rate, device=args.modem_audio_output)
    return 0


def _run_rx(args: argparse.Namespace) -> int:
    import numpy as np

    config = _make_config(args)
    if args.modem_wav is not None:
        # File mode: one-shot decode of the whole WAV.
        from weaklink.modem.audio import read_wav

        samples, _ = read_wav(args.modem_wav, expected_sample_rate=config.waveform.sample_rate)
        decoded = decode(np.asarray(samples), config)
        output = decoded.rstrip(b"\x00")
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()
        return 0

    # Live mode: streaming decode. As samples come in from the audio device we
    # re-decode the growing buffer once per second and print any newly-decoded
    # bytes to stdout immediately. Ctrl-C stops recording.
    return _live_stream_decode(config, audio_input=args.modem_audio_input)


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

    # Rolling audio buffer with a sample-cursor model. samples_before_buffer is
    # how many samples we've already emitted-through and dropped from the head
    # of the list. cursor is the absolute stream-position of the earliest
    # sample we still need to consider for future decodes.
    chunks: list[np.ndarray] = []
    samples_before_buffer = 0
    cursor = 0

    # Buffer cap. Must hold the full worst-case group (preamble + all data
    # blocks in one sync group at ``block_repeats`` copies) plus enough head
    # room to cover buffer trimming latency. Scales with baud: at 9 baud a
    # single-block payload's group is ~50 s, so a fixed 60 s cap would drop
    # the leading preamble before the trailing one arrives, silently making
    # 9 baud impossible to decode live. Minimum floor of 60 s so short-
    # payload / fast-baud paths still have some slack.
    from weaklink.modem.codec import _block_symbol_length  # noqa: WPS433

    _max_group_symbols = (
        config.sync_every_blocks * _block_symbol_length(config) * config.block_repeats
        + 32  # preamble length
    )
    _max_group_seconds = _max_group_symbols / config.waveform.baud
    MAX_WINDOW_SAMPLES = int(max(60.0, 3.0 * _max_group_seconds) * sample_rate)
    _log.debug(
        "live rx buffer cap: %.1f s (%.1f s per group)",
        MAX_WINDOW_SAMPLES / sample_rate, _max_group_seconds,
    )

    def _callback(indata_1d: np.ndarray) -> None:
        chunks.append(indata_1d)

    _log.debug("live rx: polling every 100 ms, source %s", target.describe())

    def _total_buffered() -> int:
        return samples_before_buffer + sum(chunk.size for chunk in chunks)

    def _try_emit_from_buffer() -> None:
        nonlocal samples_before_buffer, cursor
        if not chunks:
            return
        total_samples_seen = _total_buffered()
        # Enough audio to fit a leading preamble + one data block + trailing
        # preamble -- the smallest window that could plausibly contain a
        # decodable group. At 1200 baud that's ~240 ms; at 9 baud ~32 s.
        # Retries every poll as buffer grows, so over-eager attempts are
        # cheap on the fast bauds where they're most likely to happen.
        preamble_length_symbols = 32
        block_symbols = _block_symbol_length(config)
        min_group_symbols = 2 * preamble_length_symbols + block_symbols
        min_wait_samples = int(min_group_symbols / config.waveform.baud * sample_rate)
        if total_samples_seen - cursor < min_wait_samples:
            return
        # Concatenate current chunks. Slice to start at the cursor.
        buffer = np.concatenate(chunks).reshape(-1)
        buffer_start_stream_pos = samples_before_buffer
        cursor_in_buffer = max(0, cursor - buffer_start_stream_pos)
        window = buffer[cursor_in_buffer:]
        if window.size < sample_rate:
            return
        decoded, safe_cursor_offset = decode(window, config, streaming=True)
        if decoded:
            sys.stdout.buffer.write(decoded)
            sys.stdout.buffer.flush()
        # Advance the cursor by however much the decoder was confident about.
        cursor = buffer_start_stream_pos + cursor_in_buffer + safe_cursor_offset

        # Trim the retained audio: drop chunks that are entirely before the cursor.
        while chunks:
            first_chunk_end_stream_pos = samples_before_buffer + chunks[0].size
            if first_chunk_end_stream_pos <= cursor:
                samples_before_buffer += chunks[0].size
                chunks.pop(0)
            else:
                break
        # Emergency: if buffer still exceeds the cap (nothing decoded for a
        # long time), drop the oldest samples anyway to keep memory bounded.
        overflow = _total_buffered() - samples_before_buffer - MAX_WINDOW_SAMPLES
        while overflow > 0 and chunks:
            drop = min(overflow, chunks[0].size)
            if drop >= chunks[0].size:
                samples_before_buffer += chunks[0].size
                chunks.pop(0)
            else:
                chunks[0] = chunks[0][drop:]
                samples_before_buffer += drop
            overflow = _total_buffered() - samples_before_buffer - MAX_WINDOW_SAMPLES
            if cursor < samples_before_buffer:
                cursor = samples_before_buffer  # can't go back in time

    def _log_audio_snapshot() -> None:
        """One-second audio-level snapshot: peak + RMS."""
        if not chunks:
            return
        recent_needed = sample_rate  # 1 second
        recent: list[np.ndarray] = []
        recent_len = 0
        for chunk in reversed(chunks):
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
                _try_emit_from_buffer()
                if poll_counter % snapshot_every_polls == 0:
                    _log_audio_snapshot()
    except KeyboardInterrupt:
        # No final decode. Every byte that was ever going to be decodable
        # was already emitted during the 10 Hz poll loop; running one last
        # decode on the full retained buffer just adds a noticeable Ctrl-C
        # hang, especially at slow bauds with 150+ s of audio retained.
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
