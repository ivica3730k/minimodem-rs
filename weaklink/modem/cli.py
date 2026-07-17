"""Streaming modem CLI.

TX reads stdin (or --input) and streams the whole thing through the modem;
there is no length field on the wire. RX writes stdout (or --output) with
every successfully-decoded block payload concatenated. Callers add whatever
framing / message structure they want on top.

Argument convention: everything that configures the modem itself is prefixed
``--modem-*``. Everything else (I/O, files, audio device) uses plain names.

    echo -n "hello over air" | poetry run weaklink-modem tx --wav out.wav
    poetry run weaklink-modem rx --wav out.wav > received.bin
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from weaklink.modem.codec import ModemConfig, decode, encode
from weaklink.modem.waveform import WaveformConfig


def _add_modem_args(sub: argparse.ArgumentParser) -> None:
    """All modem-layer knobs (into ModemConfig). Prefixed --modem-*."""
    modem = sub.add_argument_group("modem", "modem-layer configuration (into ModemConfig)")
    modem.add_argument("--modem-baud", type=float, default=300.0, dest="modem_baud")
    modem.add_argument("--modem-sample-rate", type=float, default=48_000.0, dest="modem_sample_rate")
    modem.add_argument(
        "--modem-tone-spacing",
        type=float,
        default=None,
        dest="modem_tone_spacing",
        help="Tone spacing in Hz. Defaults to --modem-baud (Nyquist optimum for non-coherent 4-FSK).",
    )
    modem.add_argument("--modem-rs-data-bytes", type=int, default=16, dest="modem_rs_data_bytes")
    modem.add_argument("--modem-rs-parity-bytes", type=int, default=8, dest="modem_rs_parity_bytes")
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
        default=4,
        dest="modem_sync_every_blocks",
        help="Preamble inserted every N data blocks (default 4).",
    )
    modem.add_argument(
        "--modem-block-repeats",
        type=int,
        default=1,
        dest="modem_block_repeats",
        help="Each block transmitted N times, round-robin (default 1). "
        "~3 dB per doubling in AWGN plus burst-fade diversity.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="weaklink-modem", description="Streaming 4-FSK modem.")
    subparsers = parser.add_subparsers(dest="direction", required=True)

    tx_parser = subparsers.add_parser("tx")
    _add_modem_args(tx_parser)
    io_tx = tx_parser.add_argument_group("io", "input / output")
    io_tx.add_argument("--input", type=Path, help="Input file (default: stdin).")
    io_tx.add_argument("--wav", type=Path, help="Write to a WAV file instead of playing to the audio device.")

    rx_parser = subparsers.add_parser("rx")
    _add_modem_args(rx_parser)
    rx_modem_extra = rx_parser.add_argument_group("modem (rx-only)")
    rx_modem_extra.add_argument(
        "--modem-coarse-freq-search-hz",
        type=float,
        default=0.0,
        dest="modem_coarse_freq_search_hz",
        help="FFT-based coarse LO-offset search up to +/-N Hz. 0 disables.",
    )
    io_rx = rx_parser.add_argument_group("io", "input / output")
    io_rx.add_argument("--output", type=Path, help="Output file (default: stdout).")
    io_rx.add_argument("--wav", type=Path, help="Read from a WAV file instead of recording from the audio device.")
    io_rx.add_argument(
        "--record-seconds",
        type=float,
        default=None,
        help="Live record duration when --wav is not set.",
    )
    return parser


def _make_config(args: argparse.Namespace) -> ModemConfig:
    tone_spacing = args.modem_tone_spacing if args.modem_tone_spacing is not None else args.modem_baud
    coarse = getattr(args, "modem_coarse_freq_search_hz", 0.0)
    return ModemConfig(
        waveform=WaveformConfig(
            baud=args.modem_baud,
            sample_rate=args.modem_sample_rate,
            tone_spacing_hz=tone_spacing,
        ),
        rs_data_bytes=args.modem_rs_data_bytes,
        rs_parity_bytes=args.modem_rs_parity_bytes,
        rs_crc_enabled=args.modem_rs_crc_enabled,
        sync_every_blocks=args.modem_sync_every_blocks,
        block_repeats=args.modem_block_repeats,
        coarse_frequency_search_hz=coarse,
    )


def _run_tx(args: argparse.Namespace) -> int:
    config = _make_config(args)
    payload = args.input.read_bytes() if args.input is not None else sys.stdin.buffer.read()
    samples = encode(payload, config)
    if args.wav is not None:
        from weaklink.modem.audio import write_wav

        write_wav(args.wav, samples, config.waveform.sample_rate)
    else:
        from weaklink.modem.audio import play

        play(samples, config.waveform.sample_rate)
    return 0


def _run_rx(args: argparse.Namespace) -> int:
    import numpy as np

    config = _make_config(args)
    if args.wav is not None:
        from weaklink.modem.audio import read_wav

        samples, _ = read_wav(args.wav, expected_sample_rate=config.waveform.sample_rate)
    else:
        if args.record_seconds is None:
            print("error: --record-seconds is required for live rx", file=sys.stderr)
            return 2
        from weaklink.modem.audio import record

        samples = record(args.record_seconds, config.waveform.sample_rate)

    decoded = decode(np.asarray(samples), config)
    output = decoded.rstrip(b"\x00")  # strip trailing NUL padding TX added at the RS-block boundary
    if args.output is not None:
        args.output.write_bytes(output)
    else:
        sys.stdout.buffer.write(output)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.direction == "tx":
        return _run_tx(args)
    return _run_rx(args)


if __name__ == "__main__":
    sys.exit(main())
