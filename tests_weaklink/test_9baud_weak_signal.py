"""9 baud must decode reliably at -20 dB SNR (in a 3 kHz reference
bandwidth) with the preset defaults. That's the whole point of the
preset -- if it doesn't hold, we should delete 9 baud.

Slow: ~8.5 minutes of audio for a 24-byte payload. Marked ``slow``
so CI can gate it behind a flag if needed. Uses R=8 default from
BAUD_PRESETS and lets per-copy permutation + LLR combining close
the SNR gap.
"""

from __future__ import annotations

import numpy as np
import pytest

from weaklink.modem.cli import BAUD_PRESETS
from weaklink.modem.codec import ModemConfig, decode, encode
from weaklink.modem.waveform import WaveformConfig


def _cfg() -> ModemConfig:
    preset = BAUD_PRESETS[9.0]
    return ModemConfig(
        waveform=WaveformConfig(baud=9.0, tone_spacing_hz=preset["tone_spacing_hz"]),
        rs_data_bytes=int(preset["rs_data_bytes"]),
        rs_parity_bytes=int(preset["rs_parity_bytes"]),
        block_repeats=int(preset["block_repeats"]),
    )


def _add_awgn(samples: np.ndarray, snr_db: float, sample_rate: float, *, seed: int) -> np.ndarray:
    """AWGN normalised to a 3 kHz reference bandwidth (matches the
    ``weaklink-benchmark`` sweep numbers in the README)."""
    sig_p = float(np.mean(samples.astype(np.float64) ** 2))
    noise_var = sig_p * sample_rate / (2.0 * 3000.0) / (10 ** (snr_db / 10.0))
    rng = np.random.default_rng(seed)
    return (samples + rng.normal(0.0, np.sqrt(noise_var), size=samples.shape).astype(np.float32)).astype(np.float32)


@pytest.mark.slow
def test_9baud_reliably_decodes_at_minus_20db_snr() -> None:
    """Regression: if this ever regresses, delete the 9 baud preset."""
    config = _cfg()
    payload = b"hello weaklink at 9 baud"
    samples = encode(payload, config)
    successes = 0
    trials = 5
    for trial in range(trials):
        noisy = _add_awgn(samples, snr_db=-20.0, sample_rate=48_000.0, seed=trial * 17 + 3)
        decoded = decode(noisy, config) or b""
        if payload in decoded:
            successes += 1
    assert successes == trials, (
        f"9 baud @ -20 dB: only {successes}/{trials} decodes -- the preset's promise is broken; "
        f"either fix the modem or remove 9 baud from BAUD_PRESETS."
    )
