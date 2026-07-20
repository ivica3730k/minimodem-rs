"""Stream from a live audio input, decode continuously, print bytes
as they arrive. Ctrl-C stops it. Mirrors ``weaklink-modem rx`` exactly.

Adjust the constants below to match your setup, then run:
    python examples/rx_live.py
"""

from __future__ import annotations

import sys

from weaklink.modem import rx

AUDIO_INPUT = ""             # "" = OS default; "virt.monitor" / "pulse:47" / "5" / etc.
BAUD = 300.0
NUM_TONES = 4


def on_bytes(data: bytes) -> None:
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


rx(
    baud=BAUD,
    num_tones=NUM_TONES,
    audio_input=AUDIO_INPUT,
    on_bytes=on_bytes,
)
