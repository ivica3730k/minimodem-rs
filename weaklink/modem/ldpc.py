"""Regular (3, 6) rate-1/2 LDPC code with soft-decision min-sum decoding.

EXPERIMENTAL — NOT WIRED INTO THE MODEM
=======================================
This is a first-cut implementation using Gallager's random construction and
min-sum decoding. In practice it cliffs at ~+5 dB Eb/N0 rather than the
~1.5 dB a good LDPC hits, because random Gallager codes have short cycles in
the Tanner graph that trap the min-sum decoder. That means this LDPC is
*worse* than the K=7 Viterbi we already use.

To make this competitive we'd need one of:
  * PEG (Progressive Edge Growth) or similar girth-optimising construction
  * A standard code (WiFi 802.11n, DVB-S2) with a documented base matrix
  * True sum-product (tanh-based) decoding with numerical stabilisation
  * Larger block length (girth grows with n)

Kept in the tree as a starting point for someone doing that follow-up.



Trade-offs
==========
- **Construction**: Gallager's method, seeded for reproducibility. Not the
  girth-optimal PEG construction — a modern LDPC library would win perhaps
  0.5 dB on top of what we get here. For a first cut this is well within the
  Viterbi-beats-Shannon envelope.
- **Rate**: fixed 1/2 for now (d_v = 3, d_c = 6). Other rates would need
  different d_v, d_c and a re-derivation.
- **Decoder**: log-domain min-sum with 30 iterations. Slightly worse than true
  sum-product (~0.5 dB) but ~5× faster and numerically stable. Early exit on
  syndrome check.
- **Encoding**: systematic — we run Gaussian elimination over GF(2) once at
  construction time to find a systematic generator G. The column permutation
  used is baked into the codec so encode/decode stay bit-compatible.

The whole thing is deterministic given ``(n, seed)``. TX and RX pin the same
seed and are guaranteed to be talking about the same code.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


D_V: int = 3
D_C: int = 6


def _gallager_h(n: int, seed: int) -> np.ndarray:
    """Regular (D_V, D_C) parity-check matrix via Gallager's construction.

    m = n * D_V / D_C. The first D_V-th of rows lays out consecutive D_C ones
    per row; subsequent D_V-ths are column permutations of the first, so every
    column ends up with exactly D_V ones.
    """
    if n % D_C != 0:
        raise ValueError(f"n={n} must be a multiple of D_C={D_C}")
    m = (n * D_V) // D_C
    rows_per_strip = m // D_V
    rng = np.random.default_rng(seed)

    first_strip = np.zeros((rows_per_strip, n), dtype=np.int8)
    for row in range(rows_per_strip):
        first_strip[row, row * D_C : (row + 1) * D_C] = 1

    strips = [first_strip]
    for _ in range(D_V - 1):
        permutation = rng.permutation(n)
        strips.append(first_strip[:, permutation])
    return np.vstack(strips)


def _systematic_form(h_input: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Reduce H to ``[P | I_r]`` via GF(2) row reduction + column reordering.

    Algorithm:
      1. Row-reduce H (with free column swaps) so we get r linearly independent
         rows, each with a unit column somewhere. Track which columns became
         pivot columns.
      2. Move pivot columns to the *rightmost* r positions via a final column
         permutation so the layout is exactly [P | I_r].
      3. Drop the ``m - r`` all-zero rows at the bottom.

    Gallager's construction is rank-deficient by 1-2 rows for our parameters;
    the resulting (n, k) code has k = n - r, slightly above rate 1/2.
    """
    m, n = h_input.shape
    h = h_input.copy()
    column_permutation = np.arange(n)
    pivot_columns: list[int] = []

    for pivot_row in range(m):
        # Find any column that has a 1 in this row (and hasn't been used yet).
        remaining = h[pivot_row].copy()
        for used in pivot_columns:
            remaining[used] = 0
        candidates = np.where(remaining == 1)[0]
        if candidates.size == 0:
            # Try to fix by swapping a row from below that has any 1 in an unused column.
            found = False
            for source_row in range(pivot_row + 1, m):
                row_available = h[source_row].copy()
                for used in pivot_columns:
                    row_available[used] = 0
                if row_available.any():
                    h[[pivot_row, source_row]] = h[[source_row, pivot_row]]
                    candidates = np.where(row_available == 1)[0]
                    found = True
                    break
            if not found:
                continue  # this row is genuinely dependent; skip it
        chosen_column = int(candidates[0])
        pivot_columns.append(chosen_column)
        # Clear the chosen column in every other row.
        for other_row in range(m):
            if other_row != pivot_row and h[other_row, chosen_column] == 1:
                h[other_row] ^= h[pivot_row]

    r = len(pivot_columns)
    if r == 0:
        raise RuntimeError("H has rank 0; check the Gallager construction")

    # Move pivot columns to the last r positions in a stable order (preserving
    # the order in which they were discovered so the identity block is I_r).
    non_pivot_columns = [c for c in range(n) if c not in set(pivot_columns)]
    final_column_order = non_pivot_columns + pivot_columns
    h = h[:, final_column_order]
    column_permutation = column_permutation[final_column_order]

    # Drop the zero rows. Because we placed pivots in order, the top r rows
    # after column reordering have unit columns at positions k, k+1, ..., k+r-1
    # respectively — but that's only true if the pivot rows are the top r rows.
    # We didn't reorder rows, so first sort so pivot rows come first.
    row_pivot_index = np.full(m, -1, dtype=np.int64)
    for pivot_index, col in enumerate(pivot_columns):
        # After column reordering, that column is at position n - r + pivot_index.
        new_col = n - r + pivot_index
        rows_with_one = np.where(h[:, new_col] == 1)[0]
        # There is exactly one (we cleared the rest).
        row_pivot_index[int(rows_with_one[0])] = pivot_index
    keep_rows = np.where(row_pivot_index >= 0)[0]
    keep_rows = keep_rows[np.argsort(row_pivot_index[keep_rows])]
    h = h[keep_rows]

    k = n - r
    parity = h[:, :k]
    g = np.zeros((k, n), dtype=np.int8)
    g[:, :k] = np.eye(k, dtype=np.int8)
    g[:, k:] = parity.T
    return h, g, column_permutation


@dataclass(frozen=True)
class LDPCCodec:
    """Rate-1/2 LDPC over an ``n``-bit codeword.

    Precomputes H, G, and the wire-order column permutation. The permutation
    ensures the transmitted bits are ordered so the H at RX (the untouched
    Gallager matrix) still applies.
    """

    n: int = 384
    seed: int = 0xA5C3
    max_iterations: int = 30

    h: np.ndarray = field(init=False)
    g: np.ndarray = field(init=False)
    column_permutation: np.ndarray = field(init=False)
    inverse_permutation: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        h_original = _gallager_h(self.n, self.seed)
        _, generator, permutation = _systematic_form(h_original)
        # Permute the *original* H's columns to match the wire order. This keeps
        # every parity row (including the linearly dependent ones — those still
        # help the min-sum decoder converge) while agreeing with G's column layout.
        h_permuted = h_original[:, permutation]
        object.__setattr__(self, "h", h_permuted)
        object.__setattr__(self, "g", generator)
        object.__setattr__(self, "column_permutation", permutation)

    @property
    def k(self) -> int:
        return self.g.shape[0]

    def encode(self, info_bits: bytes) -> bytes:
        if len(info_bits) != self.k:
            raise ValueError(f"info_bits must be {self.k} bits, got {len(info_bits)}")
        info_array = np.frombuffer(info_bits, dtype=np.int8)
        codeword = (info_array @ self.g) % 2
        return bytes(codeword.astype(np.int8).tolist())

    def decode(self, soft_llrs: np.ndarray) -> bytes:
        """Min-sum belief propagation. ``soft_llrs`` has the same length as ``n``.

        Sign convention: positive → bit is more likely 0. Returns ``k`` info bits.
        """
        if soft_llrs.shape[0] != self.n:
            raise ValueError(f"soft_llrs must be {self.n} long, got {soft_llrs.shape[0]}")

        h = self.h.astype(np.int8)
        m = h.shape[0]
        # For each check row, list the connected variable indices (fixed d_c).
        check_neighbours = [np.where(h[row] == 1)[0] for row in range(m)]
        # For each variable column, list the connected check indices (fixed d_v).
        variable_neighbours = [np.where(h[:, col] == 1)[0] for col in range(self.n)]

        # Edge messages, keyed by (check, variable). Initialise variable->check
        # messages to the channel LLR.
        variable_to_check = {}
        check_to_variable = {}
        for check_index, variables in enumerate(check_neighbours):
            for variable_index in variables:
                variable_to_check[(check_index, variable_index)] = float(soft_llrs[variable_index])
                check_to_variable[(check_index, variable_index)] = 0.0

        hard_bits = np.zeros(self.n, dtype=np.int8)
        for _ in range(self.max_iterations):
            # Check-to-variable: min-sum over the "other" variables.
            for check_index, variables in enumerate(check_neighbours):
                incoming = np.array([variable_to_check[(check_index, v)] for v in variables])
                signs = np.sign(incoming)
                signs[signs == 0] = 1.0
                abs_values = np.abs(incoming)
                for local_position, variable_index in enumerate(variables):
                    other_mask = np.ones(len(variables), dtype=bool)
                    other_mask[local_position] = False
                    outgoing_sign = float(np.prod(signs[other_mask]))
                    outgoing_min = float(np.min(abs_values[other_mask]))
                    check_to_variable[(check_index, variable_index)] = outgoing_sign * outgoing_min

            # Variable posterior + variable-to-check update.
            posterior = np.array(soft_llrs, dtype=np.float64, copy=True)
            for variable_index, checks in enumerate(variable_neighbours):
                for check_index in checks:
                    posterior[variable_index] += check_to_variable[(check_index, variable_index)]
            for variable_index, checks in enumerate(variable_neighbours):
                for check_index in checks:
                    variable_to_check[(check_index, variable_index)] = (
                        posterior[variable_index] - check_to_variable[(check_index, variable_index)]
                    )

            hard_bits = (posterior < 0).astype(np.int8)
            # Early exit on satisfied parity check.
            syndrome = (h @ hard_bits) % 2
            if not syndrome.any():
                break

        # The codeword is stored in "wire = permuted" order, and G was systematic
        # in the same order — info bits are the first k.
        return bytes(hard_bits[: self.k].tolist())
