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
import sys
from pathlib import Path
from typing import Sequence

from weaklink.modem.codec import ModemConfig, decode, encode
from weaklink.modem.waveform import WaveformConfig


# Per-baud preset: RS parameters, block repetition, sync marker density
# picked from the measured AWGN benchmark cliff-optimum for each tested baud.
# Off-preset baud rates fall back to the 300-baud preset with a warning.
BAUD_PRESETS: dict[float, dict[str, int]] = {
    9.0:    dict(rs_data_bytes=16, rs_parity_bytes=8,  block_repeats=2, sync_every_blocks=4),
    45.0:   dict(rs_data_bytes=32, rs_parity_bytes=8,  block_repeats=2, sync_every_blocks=4),
    300.0:  dict(rs_data_bytes=16, rs_parity_bytes=8,  block_repeats=1, sync_every_blocks=4),
    1200.0: dict(rs_data_bytes=16, rs_parity_bytes=8,  block_repeats=1, sync_every_blocks=4),
}
FALLBACK_PRESET_BAUD: float = 300.0


def _add_modem_args(sub: argparse.ArgumentParser) -> None:
    """All modem-side knobs. ``--modem-*``.

    Presetable knobs default to ``None`` at the CLI layer so we can detect
    "user didn't set this" and fill from ``BAUD_PRESETS`` instead. Explicit
    ``--modem-*`` values still win.
    """
    modem = sub.add_argument_group("modem", "modem-layer configuration + sample-side I/O")
    modem.add_argument("--modem-baud", type=float, default=300.0, dest="modem_baud")
    modem.add_argument("--modem-sample-rate", type=float, default=48_000.0, dest="modem_sample_rate")
    modem.add_argument(
        "--modem-tone-spacing",
        type=float,
        default=None,
        dest="modem_tone_spacing",
        help="Base tone-spacing unit in Hz. Defaults to --modem-baud.",
    )
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
        "--modem-debug",
        dest="modem_debug",
        action="store_true",
        default=False,
        help="Print RX diagnostics to stderr: sample stats, preamble peaks, per-group decode results.",
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


def _pick_preset(baud: float) -> dict[str, int]:
    """Look up the preset for ``baud``; fall back to the 300-baud preset with a warning."""
    if baud in BAUD_PRESETS:
        return BAUD_PRESETS[baud]
    print(
        f"weaklink-9a3ice: warning: baud {baud} is not in the tested preset set "
        f"{sorted(BAUD_PRESETS.keys())!s}; falling back to the {FALLBACK_PRESET_BAUD:g}-baud "
        f"preset. Override any modem knob explicitly to silence this.",
        file=sys.stderr,
    )
    return BAUD_PRESETS[FALLBACK_PRESET_BAUD]


def _make_config(args: argparse.Namespace) -> ModemConfig:
    preset = _pick_preset(args.modem_baud)
    tone_spacing = args.modem_tone_spacing if args.modem_tone_spacing is not None else args.modem_baud
    rs_data_bytes = args.modem_rs_data_bytes if args.modem_rs_data_bytes is not None else preset["rs_data_bytes"]
    rs_parity_bytes = args.modem_rs_parity_bytes if args.modem_rs_parity_bytes is not None else preset["rs_parity_bytes"]
    sync_every = args.modem_sync_every_blocks if args.modem_sync_every_blocks is not None else preset["sync_every_blocks"]
    block_repeats = args.modem_block_repeats if args.modem_block_repeats is not None else preset["block_repeats"]
    return ModemConfig(
        waveform=WaveformConfig(
            baud=args.modem_baud,
            sample_rate=args.modem_sample_rate,
            tone_spacing_hz=tone_spacing,
        ),
        rs_data_bytes=rs_data_bytes,
        rs_parity_bytes=rs_parity_bytes,
        rs_crc_enabled=args.modem_rs_crc_enabled,
        sync_every_blocks=sync_every,
        block_repeats=block_repeats,
    )


def _run_tx(args: argparse.Namespace) -> int:
    config = _make_config(args)
    payload = sys.stdin.buffer.read()
    samples = encode(payload, config)
    if args.modem_wav is not None:
        from weaklink.modem.audio import write_wav

        write_wav(args.modem_wav, samples, config.waveform.sample_rate)
    else:
        from weaklink.modem.audio import play

        play(samples, config.waveform.sample_rate)
    return 0


def _run_rx(args: argparse.Namespace) -> int:
    import numpy as np

    config = _make_config(args)
    if args.modem_wav is not None:
        from weaklink.modem.audio import read_wav

        samples, _ = read_wav(args.modem_wav, expected_sample_rate=config.waveform.sample_rate)
    else:
        from weaklink.modem.audio import record_until_interrupted

        samples = record_until_interrupted(config.waveform.sample_rate)

    decoded = decode(np.asarray(samples), config, debug=args.modem_debug)
    output = decoded.rstrip(b"\x00")  # strip trailing NUL padding TX added at the RS-block boundary
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
