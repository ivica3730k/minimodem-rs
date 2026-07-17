"""4-FSK waveform: continuous-phase FSK modulator + non-coherent soft demodulator.

Design choices
==============
- **4 tones, 2 bits per symbol.** Two bits per symbol at 300 baud gives 600 raw
  channel bits/second (300 net after rate-1/2 FEC), fitting comfortably in an
  SSB voice passband.
- **Continuous-phase FSK.** We keep phase continuous across symbol boundaries
  so the transmitted spectrum stays narrow — a big deal for weak-signal work
  since spectral splatter is wasted power.
- **Non-coherent demodulation.** We correlate each symbol against I and Q of
  each tone and take the magnitude sqrt(I^2+Q^2). This avoids carrier phase
  recovery (which is fragile at low SNR) at a ~3 dB cost vs. coherent detection.
- **Soft output.** For each of the two bits inside a symbol, the demodulator
  emits an LLR-shaped soft value computed by max-log-MAP: for each bit
  position, LLR ≈ max_{symbols with bit=0}(|corr|^2) − max_{symbols with bit=1}(|corr|^2).
  This isn't a true LLR but preserves the ordering the Viterbi decoder needs
  and is scale-tolerant.

Bit-to-symbol mapping is Gray-coded (00→tone0, 01→tone1, 11→tone2, 10→tone3) so
that adjacent-tone confusions cost only one bit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

BITS_PER_SYMBOL = 2
NUM_TONES = 4  # 2 ** BITS_PER_SYMBOL


@dataclass(frozen=True)
class WaveformConfig:
    baud: float = 300.0
    sample_rate: float = 48_000.0
    center_hz: float = 1_500.0
    """Centre of the 4-tone stack in the SSB passband."""
    tone_spacing_hz: float = 300.0
    """Spacing between adjacent tones. For 300 baud the orthogonality floor is
    150 Hz coherent / 300 Hz non-coherent; we sit at the non-coherent floor."""
    amplitude: float = 0.25
    """Peak amplitude, well under 1.0 to leave headroom in WAV / audio devices."""

    tones_hz: tuple[float, ...] = field(init=False)

    def __post_init__(self) -> None:
        first = self.center_hz - (NUM_TONES - 1) * self.tone_spacing_hz / 2.0
        tones = tuple(first + i * self.tone_spacing_hz for i in range(NUM_TONES))
        object.__setattr__(self, "tones_hz", tones)
        if self.samples_per_symbol < 8:
            raise ValueError("sample_rate / baud must be >= 8 samples per symbol")

    @property
    def samples_per_symbol(self) -> int:
        return int(round(self.sample_rate / self.baud))


# Gray code: symbol index i emits bits GRAY_TO_BITS[i]; a pair of bits
# (b1, b0) selects tone BITS_TO_GRAY[(b1 << 1) | b0].
GRAY_TO_BITS: tuple[tuple[int, int], ...] = ((0, 0), (0, 1), (1, 1), (1, 0))
BITS_TO_GRAY: tuple[int, ...] = (0, 1, 3, 2)


def bits_to_symbols(bits: bytes) -> np.ndarray:
    """Pack an even-length 0/1 bit stream into 4-FSK symbol indices."""
    if len(bits) % BITS_PER_SYMBOL != 0:
        raise ValueError(f"bit count {len(bits)} not a multiple of {BITS_PER_SYMBOL}")
    packed = np.frombuffer(bits, dtype=np.int8).reshape(-1, BITS_PER_SYMBOL)
    keys = (packed[:, 0] << 1) | packed[:, 1]
    lookup = np.asarray(BITS_TO_GRAY, dtype=np.int8)
    return lookup[keys]


def symbols_to_bits(symbols: np.ndarray) -> bytes:
    lookup = np.asarray(GRAY_TO_BITS, dtype=np.int8)
    bit_pairs = lookup[symbols]
    return bytes(bit_pairs.reshape(-1).tolist())


def modulate(symbols: np.ndarray, config: WaveformConfig) -> np.ndarray:
    """Continuous-phase FSK modulator. Returns float32 samples in [-A, A]."""
    samples_per_symbol = config.samples_per_symbol
    total_samples = len(symbols) * samples_per_symbol
    signal = np.zeros(total_samples, dtype=np.float64)

    dt = 1.0 / config.sample_rate
    phase = 0.0
    for index, symbol in enumerate(symbols):
        freq = config.tones_hz[int(symbol)]
        omega = 2.0 * np.pi * freq * dt
        start = index * samples_per_symbol
        phases = phase + omega * np.arange(1, samples_per_symbol + 1)
        signal[start : start + samples_per_symbol] = np.sin(phases)
        phase = phases[-1]
    return (config.amplitude * signal).astype(np.float32)


def demodulate_soft(samples: np.ndarray, config: WaveformConfig) -> np.ndarray:
    """Non-coherent demod. Returns an ``(N, 4)`` array of squared magnitudes,
    one row per symbol, one column per tone."""
    samples_per_symbol = config.samples_per_symbol
    num_symbols = len(samples) // samples_per_symbol
    if num_symbols == 0:
        return np.zeros((0, NUM_TONES), dtype=np.float64)
    trimmed = np.asarray(samples[: num_symbols * samples_per_symbol], dtype=np.float64)
    windowed = trimmed.reshape(num_symbols, samples_per_symbol)

    time_axis = np.arange(samples_per_symbol) / config.sample_rate
    magnitudes_sq = np.empty((num_symbols, NUM_TONES), dtype=np.float64)
    for tone_index, freq in enumerate(config.tones_hz):
        cos_wave = np.cos(2.0 * np.pi * freq * time_axis)
        sin_wave = np.sin(2.0 * np.pi * freq * time_axis)
        in_phase = windowed @ cos_wave
        quadrature = windowed @ sin_wave
        magnitudes_sq[:, tone_index] = in_phase ** 2 + quadrature ** 2
    return magnitudes_sq


def soft_bits_from_magnitudes(magnitudes_sq: np.ndarray) -> np.ndarray:
    """Turn per-symbol tone magnitudes into per-bit soft LLR-shaped values.

    Returns a 1-D array of length ``2 * num_symbols``, ordered ``[b1_0, b0_0, b1_1, b0_1, ...]``.

    Positive value → bit is more likely 0; negative → more likely 1. Magnitudes
    are unbounded; the Viterbi decoder scales them internally.
    """
    num_symbols = magnitudes_sq.shape[0]
    if num_symbols == 0:
        return np.zeros(0, dtype=np.float64)

    # For each bit position, find the max magnitude among symbols whose bit is 0
    # vs. symbols whose bit is 1. This is max-log-MAP: the LLR of a bit is
    # approximated by the difference of the log-likelihoods of its most-likely
    # 0-symbol and 1-symbol.
    bit_masks = np.asarray(GRAY_TO_BITS, dtype=np.int8)  # shape (4, 2)
    llrs = np.empty((num_symbols, BITS_PER_SYMBOL), dtype=np.float64)
    for bit_position in range(BITS_PER_SYMBOL):
        zero_symbols = np.where(bit_masks[:, bit_position] == 0)[0]
        one_symbols = np.where(bit_masks[:, bit_position] == 1)[0]
        max_zero = magnitudes_sq[:, zero_symbols].max(axis=1)
        max_one = magnitudes_sq[:, one_symbols].max(axis=1)
        llrs[:, bit_position] = max_zero - max_one
    return llrs.reshape(-1)
