"""Encode bytes and stream them out through a live audio device.

Run:
    python examples/tx_live.py                    # OS default output
    python examples/tx_live.py -o USB             # substring match
    python examples/tx_live.py -o pulse:47        # Pulse sink by id
    python examples/tx_live.py -o pulse:my_sink   # Pulse sink by name
    python examples/tx_live.py -o 5 --ptt         # sounddevice index, hamlib PTT

Point another instance of ``rx_live.py`` at whatever audio is on the
other end.
"""

from __future__ import annotations

import argparse

from weaklink.modem import tx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", help="Audio output device (any syntax the CLI accepts)")
    parser.add_argument("--ptt", nargs="?", const="localhost:4532",
                        help="rigctld PTT endpoint (default: localhost:4532)")
    parser.add_argument("--baud", type=float, default=300.0)
    parser.add_argument("--num-tones", type=int, default=4)
    parser.add_argument("--volume", type=int, default=100)
    args = parser.parse_args()

    tx(
        b"hello over the air",
        baud=args.baud,
        num_tones=args.num_tones,
        tx_volume=args.volume,
        # Empty string = OS default output; string = specific device.
        audio_output=args.output if args.output is not None else "",
        hamlib_ptt=args.ptt,        # None -> no PTT.
    )


if __name__ == "__main__":
    main()
