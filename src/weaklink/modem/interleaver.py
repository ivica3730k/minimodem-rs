"""Per-block pseudorandom bit interleaver: break up burst errors before Viterbi
and avoid periodic-noise alignment across blocks.

Viterbi is optimal for random errors and struggles with bursts (a deep fade
wipes a run of coded bits and the trellis can't recover). Interleaving spreads
a burst over many decode positions so errors look random again.

We use a per-block pseudorandom permutation of ``rows * cols`` bit positions,
seeded by the block index. TX and RX derive the same permutation from the
same seed; RX brute-forces a small window of candidate seeds per slot until
the CRC clears, so an occasional missed slot doesn't desync the whole stream.

Per-block variation matters against periodic man-made noise (SMPS, mains
harmonics, car chargers): with a fixed rectangular interleaver, a repeating
disturbance hits the same bit positions in every block and RS sees a
persistent error pattern. Randomising the permutation per block turns that
back into "random errors spread across the code."
"""

from __future__ import annotations

import functools
from dataclasses import dataclass

import numpy as np

#: Base seed the per-block permutation is XOR'd with. Any fixed non-zero
#: value works; shared between TX and RX so both derive the same shuffle.
_INTERLEAVER_SEED_BASE: int = 0xC0DEC0DE

#: Number of distinct permutations that get cycled across a stream. Each
#: block indexes into this pool via ``block_index % _CYCLE_SIZE``. Bigger
#: pool = longer non-repeating pattern but more permutations to precompute
#: and more RX candidates to try in the worst case. 32 keeps the memory
#: footprint tiny (~32 × 256 × 8 B ≈ 64 KB cached) while still giving 32
#: unique bit orderings before any repeats.
_CYCLE_SIZE: int = 32


@dataclass(frozen=True)
class InterleaverConfig:
    rows: int
    cols: int

    def __post_init__(self) -> None:
        if self.rows < 1 or self.cols < 1:
            raise ValueError("rows and cols must be >= 1")

    @property
    def block_size(self) -> int:
        return self.rows * self.cols


@functools.lru_cache(maxsize=None)
def _bit_permutation_by_slot(seed_slot: int, size: int) -> tuple[np.ndarray, np.ndarray]:
    """Compute and cache one permutation. Called at most ``_CYCLE_SIZE``
    times per ``size`` -- the whole pool fits in a few tens of KB and is
    warm after the first pass through the stream."""
    seed = _INTERLEAVER_SEED_BASE ^ ((seed_slot * 2654435761) & 0xFFFFFFFF)
    rng = np.random.default_rng(seed)
    perm = rng.permutation(size)
    inverse = np.argsort(perm)
    return perm, inverse


def _bit_permutation(block_index: int, size: int) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(perm, inverse_perm)`` for block ``block_index``. Cycles
    through ``_CYCLE_SIZE`` distinct permutations: block N uses the same
    permutation as block N + _CYCLE_SIZE, block N + 2*_CYCLE_SIZE, etc.
    Adjacent blocks always use different permutations."""
    return _bit_permutation_by_slot(block_index % _CYCLE_SIZE, size)


def cycle_size() -> int:
    """Exposed so RX can cap its brute-force seed search at exactly the
    pool size (any further tries would just repeat permutations)."""
    return _CYCLE_SIZE


def interleave(bits: bytes, config: InterleaverConfig, block_index: int = 0) -> bytes:
    padded_length = _round_up_multiple(len(bits), config.block_size)
    padded = np.zeros(padded_length, dtype=np.int8)
    padded[: len(bits)] = np.frombuffer(bits, dtype=np.int8)
    perm, _ = _bit_permutation(block_index, padded_length)
    return bytes(padded[perm].tolist())


def deinterleave_soft(
    soft: np.ndarray,
    config: InterleaverConfig,
    output_length: int,
    block_index: int = 0,
) -> np.ndarray:
    padded_length = _round_up_multiple(output_length, config.block_size)
    if len(soft) < padded_length:
        raise ValueError(
            f"soft stream length {len(soft)} shorter than padded target {padded_length}"
        )
    trimmed = soft[:padded_length]
    _, inverse = _bit_permutation(block_index, padded_length)
    return trimmed[inverse][:output_length]


def _round_up_multiple(value: int, multiple: int) -> int:
    remainder = value % multiple
    if remainder == 0:
        return value
    return value + (multiple - remainder)
