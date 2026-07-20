"""Decode a WAV back to bytes using the public Python API.

Run:
    python examples/tx.py    # produce /tmp/hello.wav first
    python examples/rx.py

Also shows how to inject a logger to stream signal-level events.
"""

from __future__ import annotations

import logging
import sys

import soundfile as sf

from weaklink.modem import rx


def main() -> None:
    audio, sample_rate = sf.read("/tmp/hello.wav", dtype="float32")
    if sample_rate != 18_000:
        raise SystemExit(f"expected 18 kHz WAV, got {sample_rate}")

    # Optional: subscribe to weaklink's internal diagnostics.
    logger = logging.getLogger("example.rx")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(name)s %(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    payload = rx(audio, baud=300, num_tones=4, logger=logger)
    print(f"decoded {len(payload)} bytes: {payload!r}")


if __name__ == "__main__":
    main()
