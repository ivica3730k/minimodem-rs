"""Streaming modem CLI.

TX reads stdin (or --input) and streams the whole thing through the modem;
there is no length field on the wire. RX writes stdout (or --output) with
every successfully-decoded block payload concatenated. Callers add whatever
framing / message structure they want on top.

    echo -n "hello over air" | poetry run weaklink-modem tx --wav out.wav
    poetry run weaklink-modem rx --wav out.wav  > received.bin
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from weaklink.modem.codec import ModemConfig, decode, encode
from weaklink.modem.waveform import WaveformConfig


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="weaklink-modem", description="Streaming 4-FSK modem.")
    subparsers = parser.add_subparsers(dest="direction", required=True)

    for name in ("tx", "rx"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--baud", type=float, default=300.0)
        sub.add_argument("--sample-rate", type=float, default=48_000.0)
        sub.add_argument("--tone-spacing", type=float, default=None, help="Tone spacing in Hz. Defaults to baud.")
        sub.add_argument("--rs-data-bytes", type=int, default=16)
        sub.add_argument("--rs-parity-bytes", type=int, default=8)
        sub.add_argument("--no-rs-crc", dest="rs_crc_enabled", action="store_false", default=True)
        sub.add_argument("--sync-every", type=int, default=4, dest="sync_every_blocks",
                         help="Preamble inserted every N data blocks (default 4).")
        sub.add_argument("--block-repeat", type=int, default=1, dest="block_repeats",
                         help="Each block transmitted N times, round-robin (default 1). "
                              "3 dB per doubling in AWGN, plus burst-fade diversity.")
        sub.add_argument("--wav", type=Path, help="Read from / write to a WAV file instead of the audio device.")

    tx_parser = subparsers.choices["tx"]
    tx_parser.add_argument("--input", type=Path, help="Input file (default: stdin).")

    rx_parser = subparsers.choices["rx"]
    rx_parser.add_argument("--output", type=Path, help="Output file (default: stdout).")
    rx_parser.add_argument("--record-seconds", type=float, default=None, help="Live record duration when --wav is not set.")
    rx_parser.add_argument("--coarse-freq-search-hz", type=float, default=0.0, help="Enable FFT-based coarse LO-offset search up to +/-N Hz.")
    return parser


def _make_config(args: argparse.Namespace) -> ModemConfig:
    tone_spacing = args.tone_spacing if args.tone_spacing is not None else args.baud
    coarse = getattr(args, "coarse_freq_search_hz", 0.0)
    return ModemConfig(
        waveform=WaveformConfig(baud=args.baud, sample_rate=args.sample_rate, tone_spacing_hz=tone_spacing),
        rs_data_bytes=args.rs_data_bytes,
        rs_parity_bytes=args.rs_parity_bytes,
        rs_crc_enabled=args.rs_crc_enabled,
        sync_every_blocks=args.sync_every_blocks,
        block_repeats=args.block_repeats,
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
