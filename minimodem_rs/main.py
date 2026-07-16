#!/usr/bin/env python3
"""CLI entrypoint for minimodem-rs: tx/rx subcommands with minimodem passthrough."""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import BinaryIO, Sequence

from minimodem_rs.helpers import (
    FramingConfig,
    ReedSolomonFramer,
    build_minimodem_argv,
    run_rx,
    run_tx,
    split_minimodem_passthrough,
)

MINIMODEM_BINARY = "minimodem"

DEFAULT_DATA_BYTES = 16
DEFAULT_PARITY_BYTES = 8
DEFAULT_SYNC_PAYLOAD = "ABCDEFGH"
DEFAULT_SYNC_EVERY = 1


def _add_framing_arguments(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--data-bytes", type=int, default=DEFAULT_DATA_BYTES, dest="data_bytes")
    subparser.add_argument("--parity-bytes", type=int, default=DEFAULT_PARITY_BYTES, dest="parity_bytes")
    subparser.add_argument("--sync-payload", type=str, default=DEFAULT_SYNC_PAYLOAD, dest="sync_payload")
    fec_group = subparser.add_mutually_exclusive_group()
    fec_group.add_argument("--fec", dest="fec_enabled", action="store_true", default=True)
    fec_group.add_argument("--no-fec", dest="fec_enabled", action="store_false")
    subparser.add_argument("--rs", dest="fec_enabled", action="store_true", help="alias for --fec")
    subparser.add_argument("--no-rs", dest="fec_enabled", action="store_false", help="alias for --no-fec")
    crc_group = subparser.add_mutually_exclusive_group()
    crc_group.add_argument("--crc", dest="crc_enabled", action="store_true", default=False)
    crc_group.add_argument("--no-crc", dest="crc_enabled", action="store_false")
    subparser.add_argument("baud_mode", metavar="BAUD_MODE", help="baud rate or mode passed to minimodem (e.g. 1200, 300, rtty)")


def _build_argument_parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        prog="minimodem-rs",
        description=(
            "Reed-Solomon framing wrapper around minimodem. "
            "Pass minimodem options through with --mm-<option> [value]."
        ),
    )
    subparsers = argument_parser.add_subparsers(dest="direction", required=True)

    tx_parser = subparsers.add_parser("tx", help="Encode stdin (or --input) and transmit via minimodem")
    _add_framing_arguments(tx_parser)
    tx_parser.add_argument("--sync-every", type=int, default=DEFAULT_SYNC_EVERY, dest="sync_every_blocks")
    tx_parser.add_argument("--input", dest="input_file", default=None, help="input file (default: stdin)")

    rx_parser = subparsers.add_parser("rx", help="Receive from minimodem, decode frames, write payload to stdout (or --output)")
    _add_framing_arguments(rx_parser)
    rx_parser.add_argument("--output", dest="output_file", default=None, help="output file (default: stdout)")

    return argument_parser


def _build_framer(parsed_arguments: argparse.Namespace) -> ReedSolomonFramer:
    return ReedSolomonFramer(
        FramingConfig(
            data_bytes=parsed_arguments.data_bytes,
            parity_bytes=parsed_arguments.parity_bytes,
            sync_payload=parsed_arguments.sync_payload.encode("utf-8"),
            fec_enabled=parsed_arguments.fec_enabled,
            crc_enabled=parsed_arguments.crc_enabled,
        )
    )


def _open_input_stream(path: str | None) -> tuple[BinaryIO, bool]:
    if path is None:
        return sys.stdin.buffer, False
    return open(path, "rb"), True


def _open_output_stream(path: str | None) -> tuple[BinaryIO, bool]:
    if path is None:
        return sys.stdout.buffer, False
    return open(path, "wb"), True


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    filtered_argv, minimodem_passthrough = split_minimodem_passthrough(raw_argv)

    if minimodem_passthrough and (not filtered_argv or filtered_argv[0] not in ("tx", "rx")):
        return subprocess.call([MINIMODEM_BINARY, *minimodem_passthrough, *filtered_argv])

    parsed_arguments = _build_argument_parser().parse_args(filtered_argv)

    framer = _build_framer(parsed_arguments)
    direction_flag = "--tx" if parsed_arguments.direction == "tx" else "--rx"
    if parsed_arguments.direction == "rx" and not any(a == "--quiet" or a.startswith("--quiet=") for a in minimodem_passthrough):
        minimodem_passthrough = ["--quiet", *minimodem_passthrough]
    minimodem_argv = build_minimodem_argv(
        direction=direction_flag,
        passthrough_args=minimodem_passthrough,
        baud_mode=parsed_arguments.baud_mode,
    )

    if parsed_arguments.direction == "tx":
        input_stream, opened_by_us = _open_input_stream(parsed_arguments.input_file)
        try:
            return run_tx(
                minimodem_argv=minimodem_argv,
                framer=framer,
                sync_every_blocks=parsed_arguments.sync_every_blocks,
                input_stream=input_stream,
            )
        finally:
            if opened_by_us:
                input_stream.close()

    output_stream, opened_by_us = _open_output_stream(parsed_arguments.output_file)
    try:
        return run_rx(
            minimodem_argv=minimodem_argv,
            framer=framer,
            output_stream=output_stream,
        )
    finally:
        if opened_by_us:
            output_stream.close()


if __name__ == "__main__":
    sys.exit(main())
