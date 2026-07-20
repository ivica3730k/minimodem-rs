"""Decode success must not go up as SNR drops. Guards against
correlator threshold regressions like the 2-FSK sidelobe bug."""

from __future__ import annotations

import numpy as np
import pytest

from weaklink.modem.codec import ModemConfig, decode, encode
from weaklink.modem.exceptions import ConfigError
from weaklink.modem.waveform import WaveformConfig


def _awgn(samples: np.ndarray, snr_db: float, seed: int) -> np.ndarray:
    sig_p = float(np.mean(samples.astype(np.float64) ** 2))
    noise_variance = sig_p * 18_000.0 / (2.0 * 3_000.0) / (10 ** (snr_db / 10.0))
    rng = np.random.default_rng(seed)
    return (samples + rng.standard_normal(samples.size).astype(np.float32) * np.sqrt(noise_variance)).astype(np.float32)


@pytest.mark.parametrize("num_tones", [2, 4, 8, 16])
@pytest.mark.parametrize("baud", [45.0, 300.0, 1200.0])
def test_decode_success_monotonic_in_snr(baud: float, num_tones: int) -> None:
    try:
        waveform = WaveformConfig(baud=baud, tone_spacing_hz=baud, num_tones=num_tones)
    except (ValueError, ConfigError) as e:
        pytest.skip(str(e))
    config = ModemConfig(
        waveform=waveform,
        rs_data_bytes=16, rs_parity_bytes=8, block_repeats=2,
    )
    payload = b"HI!!"
    samples = encode(payload, config)

    snr_sweep = (10.0, 5.0, 0.0, -5.0, -10.0)
    successes: list[int] = []
    for snr in snr_sweep:
        ok = sum(
            1 for trial in range(3)
            if payload in (decode(_awgn(samples, snr, seed=trial * 17 + int(num_tones)), config) or b"")
        )
        successes.append(ok)

    for i in range(len(successes) - 1):
        assert successes[i] >= successes[i + 1], (
            f"baud={baud} num_tones={num_tones}: non-monotonic across "
            f"SNR {snr_sweep} -> {successes}"
        )
