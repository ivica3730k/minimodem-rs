"""Block interleaver: break up burst errors before Viterbi.

Viterbi is optimal for random errors; it struggles with bursts (e.g. a deep
fade wipes out a run of consecutive coded bits, and the trellis can't recover).
A row-column interleaver spreads a burst over multiple decode windows so the
errors look random again.

Given a coded stream of length N = rows * cols, we write in row-major order
and read in column-major order. Padding: if the input isn't a perfect rectangle
we round up to the next full rectangle with zeros, and strip on deinterleave.
The rectangle geometry is a fixed config parameter — no header on the wire.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


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


def interleave(bits: bytes, config: InterleaverConfig) -> bytes:
    padded_length = _round_up_multiple(len(bits), config.block_size)
    padded = np.zeros(padded_length, dtype=np.int8)
    padded[: len(bits)] = np.frombuffer(bits, dtype=np.int8)
    matrix = padded.reshape(-1, config.rows, config.cols)
    permuted = matrix.transpose(0, 2, 1).reshape(-1)
    return bytes(permuted.tolist())


def deinterleave_soft(soft: np.ndarray, config: InterleaverConfig, output_length: int) -> np.ndarray:
    padded_length = _round_up_multiple(output_length, config.block_size)
    if len(soft) < padded_length:
        raise ValueError(f"soft stream length {len(soft)} shorter than padded target {padded_length}")
    trimmed = soft[:padded_length]
    matrix = trimmed.reshape(-1, config.cols, config.rows)
    restored = matrix.transpose(0, 2, 1).reshape(-1)
    return restored[:output_length]


def _round_up_multiple(value: int, multiple: int) -> int:
    remainder = value % multiple
    if remainder == 0:
        return value
    return value + (multiple - remainder)
