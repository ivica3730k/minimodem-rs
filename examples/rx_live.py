"""Stream from a live audio input, decode continuously, print bytes
as they arrive. Ctrl-C stops it. Mirrors ``weaklink-modem rx`` exactly.

Run:
    python examples/rx_live.py                    # OS default input
    python examples/rx_live.py -i virt.monitor    # Pulse source name
    python examples/rx_live.py -i pulse:47        # Pulse source by id
    python examples/rx_live.py -i 5               # sounddevice index
"""

from __future__ import annotations

import argparse
import logging
import sys

from weaklink.modem import rx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-i", "--input", help="Audio input device (any syntax the CLI accepts)")
    parser.add_argument("--baud", type=float, default=300.0)
    parser.add_argument("--num-tones", type=int, default=4)
    parser.add_argument("--verbose", action="store_true",
                        help="Print weaklink diagnostics (peak/rms, decode outcomes) to stderr.")
    args = parser.parse_args()

    logger: logging.Logger | None = None
    if args.verbose:
        logger = logging.getLogger("example.rx")
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("[%(name)s %(levelname)s] %(message)s"))
        logger.addHandler(h)
        logger.setLevel(logging.INFO)

    def _on_bytes(data: bytes) -> None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    rx(
        baud=args.baud,
        num_tones=args.num_tones,
        # Empty string = OS default input; string = specific device.
        audio_input=args.input if args.input is not None else "",
        on_bytes=_on_bytes,
        logger=logger,
    )


if __name__ == "__main__":
    main()
