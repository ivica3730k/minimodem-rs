"""Encode bytes to a WAV using the public Python API.

Run:
    python examples/tx.py

Produces /tmp/hello.wav containing the modulated audio.
"""

from __future__ import annotations

import soundfile as sf

from weaklink.modem import tx


def main() -> None:
    payload = b"hello from weaklink-modem"
    audio = tx(
        payload,
        baud=300,
        num_tones=4,
        # Preset knobs -- override only if you know what you want:
        # block_repeats=4,
        # rs_data_bytes=32, rs_parity_bytes=8,
        tx_volume=100,   # 0-100, 100 = full scale
    )
    sf.write("/tmp/hello.wav", audio, 18_000)
    print(f"wrote {audio.size} samples ({audio.size / 18_000:.2f} s) to /tmp/hello.wav")


if __name__ == "__main__":
    main()
