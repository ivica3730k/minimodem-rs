"""Streaming modem CLI. Bytes on stdin/stdout, samples via WAV or live audio.
Baud presets in ``BAUD_PRESETS`` (45/300/1200); ``--modem-*`` flags override.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Iterable, Sequence

from weaklink.modem.api import ModemOptions, rx, tx
from weaklink.modem.constants import BAUD_PRESETS, DEFAULT_LOG_PATH
from weaklink.modem.exceptions import WeaklinkError

_log = logging.getLogger("weaklink.cli")


def _add_modem_args(sub: argparse.ArgumentParser) -> None:
    """All modem-side parameters. ``--modem-*``.

    Presetable parameters default to ``None`` at the CLI layer so we can detect
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
        choices=(2, 4, 8, 16),
        help="Number of FSK tones (power of 2). 4 (default) is standard; "
        "8/16 pack more bits per symbol at wider bandwidth and worse "
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
        help="Audio output device for tx. Accepts a Pulse sink id (e.g. '42' -- "
        "from `pactl list short sinks`), a sounddevice index (same syntax, used "
        "when pactl doesn't know the id), a substring of a device name "
        "(e.g. 'USB'), or a Pulse sink name (e.g. 'virt'). Prefix with 'pulse:' "
        "to force the Pulse path (e.g. 'pulse:42'). Default: OS default output.",
    )
    modem.add_argument(
        "--modem-audio-input",
        type=str,
        default=None,
        dest="modem_audio_input",
        help="Audio input device for rx. Same syntax as --modem-audio-output but "
        "matches against input devices / Pulse source names (e.g. 'virt.monitor') "
        "or ids from `pactl list short sources`. Default: OS default input.",
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


def _build_parser() -> argparse.ArgumentParser:
    try:
        version = _pkg_version("weaklink-modem")
    except PackageNotFoundError:
        version = "unknown"

    parser = argparse.ArgumentParser(prog="weaklink-modem", description="Streaming N-FSK modem.")
    parser.add_argument("--version", action="version", version=f"weaklink-modem {version}")
    subparsers = parser.add_subparsers(dest="direction", required=True)
    tx_parser = subparsers.add_parser("tx", help="Encode stdin bytes and transmit (or write to WAV).")
    _add_modem_args(tx_parser)
    tx_parser.add_argument(
        "--modem-tune",
        action="store_true",
        default=False,
        dest="modem_tune",
        help="Emit every tone of the selected mode in round-robin (one symbol "
        "each, cycling). No framing, no preamble, no stdin -- just clean tones "
        "for radio tuneup / audio path verification. Runs until Ctrl-C. "
        "Honours --modem-tx-volume and --hamlib-ptt.",
    )
    tx_parser.add_argument(
        "--modem-tx-volume",
        type=int,
        default=100,
        dest="modem_tx_volume",
        metavar="0-100",
        help="TX peak amplitude, 0-100 (default: 100 = full scale). Bump if "
        "downstream audio path is faint; drop to leave headroom for a hot chain.",
    )
    tx_parser.add_argument(
        "--hamlib-ptt",
        nargs="?",
        const="localhost:4532",
        default=None,
        dest="hamlib_ptt",
        metavar="HOST:PORT",
        help="Keyed PTT via rigctld before audio starts, released after. "
        "Bare --hamlib-ptt defaults to localhost:4532; pass HOST:PORT to override. "
        "Only applied when playing to a live audio device.",
    )
    rx_parser = subparsers.add_parser("rx", help="Receive (or read WAV) and decode to stdout bytes.")
    _add_modem_args(rx_parser)
    return parser


def _options_from_args(args: argparse.Namespace) -> ModemOptions:
    return ModemOptions(
        baud=args.modem_baud,
        num_tones=args.modem_num_tones if args.modem_num_tones is not None else 4,
        rs_data_bytes=args.modem_rs_data_bytes,
        rs_parity_bytes=args.modem_rs_parity_bytes,
        rs_crc_enabled=args.modem_rs_crc_enabled,
        block_repeats=args.modem_block_repeats,
        sync_every_blocks=args.modem_sync_every_blocks,
    )


def _stdin_chunks() -> Iterable[bytes]:
    """Modest-sized reads so the encoder can start emitting audio before
    the whole input arrives (matters for pipes like ``tail -f | tx``)."""
    while True:
        block = sys.stdin.buffer.read(4096)
        if not block:
            return
        yield block


def _run_tx(args: argparse.Namespace) -> int:
    opts = _options_from_args(args)
    volume = args.modem_tx_volume
    audio_output = args.modem_audio_output or ""
    if args.modem_tune:
        tx(None, opts, tx_volume=volume, audio_output=audio_output, ptt=args.hamlib_ptt, tune=True)
    elif args.modem_wav is not None:
        tx(_stdin_chunks(), opts, tx_volume=volume, wav=args.modem_wav)
    else:
        tx(_stdin_chunks(), opts, tx_volume=volume, audio_output=audio_output, ptt=args.hamlib_ptt)
    return 0


def _run_rx(args: argparse.Namespace) -> int:
    opts = _options_from_args(args)
    if args.modem_wav is not None:
        # Wrap so on_bytes matches the ``Callable[[bytes], None]`` signature
        # (BufferedWriter.write returns int).
        def _to_stdout(data: bytes) -> None:
            sys.stdout.buffer.write(data)

        rx(options=opts, wav=args.modem_wav, on_bytes=_to_stdout)
        sys.stdout.buffer.flush()
    else:
        # PULSE_SOURCE env-var fallback matches the previous CLI default.
        hint = args.modem_audio_input if args.modem_audio_input else os.environ.get("PULSE_SOURCE", "")
        rx(options=opts, audio_input=hint)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    handler = logging.FileHandler(args.modem_log_file, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("weaklink")
    root.setLevel(logging.DEBUG if args.modem_debug else logging.INFO)
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.propagate = False

    _log.debug("weaklink-modem %s starting", args.direction)
    try:
        try:
            return _run_tx(args) if args.direction == "tx" else _run_rx(args)
        except WeaklinkError as e:
            # Typed library errors get a clean shell line, not a traceback.
            print(f"error: {e}", file=sys.stderr)
            return 2
    finally:
        logging.shutdown()


if __name__ == "__main__":
    sys.exit(main())
