"""Rate-1/2 K=7 convolutional code with soft-decision Viterbi.

Uses the industry-standard NASA/CCSDS generators::

    g0 = 171 octal = 0b1111001  -> taps at [6, 5, 4, 3, 0]
    g1 = 133 octal = 0b1011011  -> taps at [6, 4, 3, 1, 0]

Constraint length K=7 means 64 shift-register states. This code gives roughly
5 dB of coding gain at BER=1e-5 vs. uncoded BPSK with a soft Viterbi decoder;
plenty for a first cut and small enough that the trellis fits in NumPy without
heroics.

Terminated code: we flush ``K-1`` zero tail bits so the trellis ends in state 0
and the decoder gets a hard boundary condition. That's an efficiency cost we
pay for simplicity; can be swapped for tail-biting later if needed.

Input to the decoder is soft LLR-shaped values (positive = bit likely 0). The
Viterbi metric is the sum-of-LLRs along a path, maximised.
"""

from __future__ import annotations

import numpy as np

CONSTRAINT_LENGTH = 7
NUM_STATES = 1 << (CONSTRAINT_LENGTH - 1)  # 64
GENERATORS = (0o171, 0o133)


def _output_bit(state_and_input: int, generator: int) -> int:
    """One output-bit tap for a given (7-bit) combined state+input."""
    masked = state_and_input & generator
    parity = 0
    while masked:
        parity ^= masked & 1
        masked >>= 1
    return parity


def _precompute_trellis() -> tuple[np.ndarray, np.ndarray]:
    """Build the next-state and output-symbol tables for every (state, input).

    Returns:
        next_state: shape (NUM_STATES, 2)
        outputs: shape (NUM_STATES, 2, 2) — two output bits per transition.
    """
    next_state = np.zeros((NUM_STATES, 2), dtype=np.int32)
    outputs = np.zeros((NUM_STATES, 2, 2), dtype=np.int8)
    for state in range(NUM_STATES):
        for input_bit in (0, 1):
            combined = (input_bit << (CONSTRAINT_LENGTH - 1)) | state
            bit0 = _output_bit(combined, GENERATORS[0])
            bit1 = _output_bit(combined, GENERATORS[1])
            outputs[state, input_bit, 0] = bit0
            outputs[state, input_bit, 1] = bit1
            next_state[state, input_bit] = combined >> 1
    return next_state, outputs


_NEXT_STATE, _OUTPUTS = _precompute_trellis()


def encode(bits: bytes) -> bytes:
    """Convolutionally encode ``bits`` (bytes of 0/1). Adds K-1 flush bits.

    Output length is ``2 * (len(bits) + K - 1)`` bits.
    """
    padded = np.concatenate([np.frombuffer(bits, dtype=np.int8), np.zeros(CONSTRAINT_LENGTH - 1, dtype=np.int8)])
    state = 0
    out = np.empty(2 * len(padded), dtype=np.int8)
    for index, input_bit in enumerate(padded):
        pair = _OUTPUTS[state, int(input_bit)]
        out[2 * index] = pair[0]
        out[2 * index + 1] = pair[1]
        state = int(_NEXT_STATE[state, int(input_bit)])
    return bytes(out.tolist())


def decode(soft_bits: np.ndarray, num_output_bits: int) -> bytes:
    """Soft-decision Viterbi.

    ``soft_bits`` are LLR-shaped values from the demodulator, shape (2N,).
    Positive → bit likely 0; negative → likely 1. Returns ``num_output_bits``
    decoded information bits, i.e. before the ``K-1`` tail flush.

    Precondition: ``len(soft_bits) == 2 * (num_output_bits + K - 1)``.
    """
    expected = 2 * (num_output_bits + CONSTRAINT_LENGTH - 1)
    if len(soft_bits) != expected:
        raise ValueError(f"soft_bits length {len(soft_bits)} != expected {expected}")

    num_steps = num_output_bits + CONSTRAINT_LENGTH - 1
    metric = np.full(NUM_STATES, -np.inf, dtype=np.float64)
    metric[0] = 0.0

    # Note: for this K=7 shift-register code, both predecessors of a given
    # destination state use the *same* input bit (the bit that becomes the top
    # bit of the new state). So we can't disambiguate the predecessors by input
    # bit alone — we store which predecessor won, and recover the input bit
    # from the destination state during traceback.
    predecessors = np.asarray(
        [[prev for prev, _ in _build_predecessor_table()[s]] for s in range(NUM_STATES)],
        dtype=np.int32,
    )  # shape (NUM_STATES, 2)
    back_pred = np.zeros((num_steps, NUM_STATES), dtype=np.int8)

    # Loop-invariants hoisted from the per-step body.
    signs = 1.0 - 2.0 * _OUTPUTS.astype(np.float64)  # (NUM_STATES, 2, 2)
    # Predecessor input bit for each destination: top bit of dest state,
    # since dest = (u << K-1 | prev) >> 1.
    dest_input_bit = ((np.arange(NUM_STATES) >> (CONSTRAINT_LENGTH - 2)) & 1).astype(np.int64)
    prev0 = predecessors[:, 0]
    prev1 = predecessors[:, 1]

    for step in range(num_steps):
        pair0 = float(soft_bits[2 * step])
        pair1 = float(soft_bits[2 * step + 1])
        branch_metrics = signs[:, :, 0] * pair0 + signs[:, :, 1] * pair1
        candidate_metrics = metric[:, None] + branch_metrics

        metric_a = candidate_metrics[prev0, dest_input_bit]
        metric_b = candidate_metrics[prev1, dest_input_bit]
        pick_second = metric_b > metric_a
        metric = np.where(pick_second, metric_b, metric_a)
        back_pred[step] = pick_second.astype(np.int8)

    # Traceback from the terminating state 0.
    state = 0
    output_bits = np.zeros(num_steps, dtype=np.int8)
    for step in range(num_steps - 1, -1, -1):
        input_bit = (state >> (CONSTRAINT_LENGTH - 2)) & 1
        output_bits[step] = input_bit
        pick = int(back_pred[step, state])
        state = int(predecessors[state, pick])

    return bytes(output_bits[:num_output_bits].tolist())


_PredecessorTable = tuple[tuple[tuple[int, int], ...], ...]
_PREDECESSOR_TABLE: _PredecessorTable | None = None


def _build_predecessor_table() -> _PredecessorTable:
    """For each destination state, list the two (prev_state, input_bit) predecessors."""
    global _PREDECESSOR_TABLE
    if _PREDECESSOR_TABLE is not None:
        return _PREDECESSOR_TABLE
    predecessors: list[list[tuple[int, int]]] = [[] for _ in range(NUM_STATES)]
    for prev_state in range(NUM_STATES):
        for input_bit in (0, 1):
            dest = int(_NEXT_STATE[prev_state, input_bit])
            predecessors[dest].append((prev_state, input_bit))
    _PREDECESSOR_TABLE = tuple(tuple(entries) for entries in predecessors)
    return _PREDECESSOR_TABLE
