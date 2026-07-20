"""Encode bytes and stream them to a live audio output.

Adjust the constants below to match your setup, then run:
    python examples/tx_live.py
"""

from __future__ import annotations

from weaklink.modem import ModemOptions, tx

PAYLOAD = b"hello over the air"
AUDIO_OUTPUT = ""            # "" = OS default; "USB" / "pulse:47" / "5" / etc.
HAMLIB_PTT = None            # None = no PTT; "localhost:4532" = rigctld default.
TX_VOLUME = 100              # 0-100.

tx(
    PAYLOAD,
    ModemOptions(baud=300, num_tones=4),
    tx_volume=TX_VOLUME,
    audio_output=AUDIO_OUTPUT,
    ptt=HAMLIB_PTT,
)
