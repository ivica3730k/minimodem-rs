"""Regression tests: each committed below-noise-floor WAV must decode.

If someone changes the correlator, FEC pipeline, or preset defaults in a
way that pushes the decode cliff up, these tests catch it: the WAVs were
generated at exactly 3 dB above the cliff each preset had at commit time,
so any regression that eats more than 3 dB of margin breaks a test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from weaklink.modem.audio import read_wav
from weaklink.modem.codec import ModemConfig, decode
from weaklink.modem.waveform import WaveformConfig


EXPECTED_PAYLOAD = b"weaklink below-noise-floor test payload"

# (baud, filename, default-preset kwargs matching weaklink.modem.cli.BAUD_PRESETS)
BELOW_NOISE_CASES = [
    (9,    "below_noise_9baud_snr-27dB.wav",    dict(rs_data_bytes=16, rs_parity_bytes=8, block_repeats=2, sync_every_blocks=4)),
    (45,   "below_noise_45baud_snr-21dB.wav",   dict(rs_data_bytes=32, rs_parity_bytes=8, block_repeats=2, sync_every_blocks=4)),
    (300,  "below_noise_300baud_snr-10dB.wav",  dict(rs_data_bytes=16, rs_parity_bytes=8, block_repeats=1, sync_every_blocks=4)),
    (1200, "below_noise_1200baud_snr-4dB.wav",  dict(rs_data_bytes=16, rs_parity_bytes=8, block_repeats=1, sync_every_blocks=4)),
]

WAV_DIR = Path(__file__).resolve().parents[1] / "test_signals"


@pytest.mark.parametrize("baud, filename, preset", BELOW_NOISE_CASES, ids=[f"{c[0]}baud" for c in BELOW_NOISE_CASES])
def test_below_noise_floor_wav_decodes(baud: int, filename: str, preset: dict) -> None:
    wav_path = WAV_DIR / filename
    assert wav_path.exists(), f"missing test signal: {wav_path}"

    config = ModemConfig(
        waveform=WaveformConfig(baud=float(baud), tone_spacing_hz=float(baud)),
        **preset,
    )
    samples, _ = read_wav(wav_path, expected_sample_rate=config.waveform.sample_rate)
    decoded = decode(samples, config) or b""
    assert EXPECTED_PAYLOAD in decoded, (
        f"below-NF WAV for {baud} baud failed to decode -- "
        f"decode cliff has moved above the 3 dB margin baked into this file. "
        f"Got: {decoded[:80]!r}"
    )
