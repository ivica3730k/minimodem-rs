"""Benchmark suite: sweep every documented modem mode, find its SNR cliff,
compute the Shannon limit at the same info rate, and rewrite the results
table in ``README.md`` between the ``<!-- BENCHMARK ... -->`` markers.

Run with::

    poetry run weaklink-benchmark               # full sweep, updates README
    poetry run weaklink-benchmark --trials 3    # faster smoke run
    poetry run weaklink-benchmark --dry-run     # print table, don't touch README

Cliff-finding is a coarse sweep in 1 dB steps: for each mode we start above
the expected floor, walk down, and record the lowest SNR at which every trial
succeeds. That's a conservative "reliable" number — the true 50% cliff is
usually 1-2 dB below.
"""

from __future__ import annotations

import argparse
import math
import random
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from weaklink.modem.codec import ModemConfig, decode, encode
from weaklink.modem.waveform import WaveformConfig

REFERENCE_BANDWIDTH_HZ: float = 3_000.0
README_START_MARKER = "<!-- BENCHMARK RESULTS START -->"
README_END_MARKER = "<!-- BENCHMARK RESULTS END -->"


@dataclass
class ModeSpec:
    name: str
    settings: str
    build_config: Callable[[], ModemConfig]
    payload_size_bytes: int
    seed: int = 0
    snr_search_high_db: float = 15.0
    snr_search_low_db: float = -25.0

    def make_payload(self) -> bytes:
        alphabet = (string.ascii_letters + string.digits + " ").encode("ascii")
        return bytes(random.Random(self.seed).choices(alphabet, k=self.payload_size_bytes))


def _weak_signal_config() -> ModemConfig:
    return ModemConfig(
        waveform=WaveformConfig(baud=30.0, tone_spacing_hz=30.0),
        preamble_length=64,
        payload_repeats=3,
        rs_data_bytes=16,
        rs_parity_bytes=8,
        rs_crc_enabled=True,
    )


def _paragraph_config(repeats: int) -> ModemConfig:
    return ModemConfig(
        waveform=WaveformConfig(baud=100.0, tone_spacing_hz=100.0),
        preamble_length=64,
        payload_repeats=repeats,
        rs_data_bytes=150,
        rs_parity_bytes=32,
        rs_crc_enabled=True,
    )


def _file_pipe_config() -> ModemConfig:
    return ModemConfig(
        waveform=WaveformConfig(baud=100.0, tone_spacing_hz=100.0),
        preamble_length=64,
        payload_repeats=1,
        rs_data_bytes=32,
        rs_parity_bytes=8,
        rs_crc_enabled=True,
    )


def _baseline_config() -> ModemConfig:
    return ModemConfig(
        waveform=WaveformConfig(baud=300.0, tone_spacing_hz=300.0),
        preamble_length=32,
        payload_repeats=1,
    )


MODES: list[ModeSpec] = [
    ModeSpec(
        name="Baseline modem",
        settings="300 baud 4-FSK, r=1/2 K=7 Viterbi, no RS, no repeat",
        build_config=_baseline_config,
        payload_size_bytes=21,
        snr_search_high_db=10.0,
        snr_search_low_db=-15.0,
    ),
    ModeSpec(
        name="Weak-signal preset",
        settings="30 baud, RS(24,16), 3&times; repeat",
        build_config=_weak_signal_config,
        payload_size_bytes=15,
        snr_search_high_db=-5.0,
        snr_search_low_db=-22.0,
    ),
    ModeSpec(
        name="Paragraph, 1&times;",
        settings="100 baud, RS(174,142), 1&times; repeat",
        build_config=lambda: _paragraph_config(repeats=1),
        payload_size_bytes=142,
        snr_search_high_db=5.0,
        snr_search_low_db=-15.0,
    ),
    ModeSpec(
        name="Paragraph, 2&times;",
        settings="100 baud, RS(174,142), 2&times; repeat",
        build_config=lambda: _paragraph_config(repeats=2),
        payload_size_bytes=142,
        snr_search_high_db=0.0,
        snr_search_low_db=-18.0,
    ),
    ModeSpec(
        name="File pipe",
        settings="100 baud, RS(40,32), 1&times; repeat (16 blocks)",
        build_config=_file_pipe_config,
        payload_size_bytes=500,
        snr_search_high_db=5.0,
        snr_search_low_db=-15.0,
    ),
]


@dataclass
class Result:
    mode: ModeSpec
    info_rate_bit_per_s: float
    duration_seconds: float
    cliff_snr_db: float | None  # lowest SNR at which all trials passed; None if never
    shannon_snr_db: float


def shannon_snr_db(info_rate_bit_per_s: float, bandwidth_hz: float = REFERENCE_BANDWIDTH_HZ) -> float:
    """Shannon-limit SNR (linear -> dB) for a given info rate in bandwidth."""
    if info_rate_bit_per_s <= 0:
        return -math.inf
    linear = 2 ** (info_rate_bit_per_s / bandwidth_hz) - 1
    return 10.0 * math.log10(linear)


def _add_awgn(samples: np.ndarray, snr_db: float, sample_rate: float, *, seed: int) -> np.ndarray:
    signal_power = float(np.mean(np.asarray(samples, dtype=np.float64) ** 2))
    noise_variance = signal_power * sample_rate / (2.0 * REFERENCE_BANDWIDTH_HZ) / (10 ** (snr_db / 10.0))
    rng = np.random.default_rng(seed)
    return samples + rng.normal(0.0, np.sqrt(noise_variance), size=samples.shape).astype(np.float32)


def find_cliff(mode: ModeSpec, *, trials: int) -> Result:
    config = mode.build_config()
    payload = mode.make_payload()
    samples = encode(payload, config)
    duration = len(samples) / config.waveform.sample_rate
    info_rate = mode.payload_size_bytes * 8.0 / duration
    shannon = shannon_snr_db(info_rate)

    # Walk from high SNR down in 1 dB steps until we lose all trials.
    cliff: float | None = None
    for snr_db in _snr_range(mode.snr_search_high_db, mode.snr_search_low_db):
        successes = 0
        for trial in range(trials):
            noisy = _add_awgn(
                samples,
                snr_db=snr_db,
                sample_rate=config.waveform.sample_rate,
                seed=(mode.seed * 1000 + trial * 31 + int(snr_db * 10) + 5000) & 0x7FFFFFFF,
            )
            if decode(noisy, config, payload_length_bytes=mode.payload_size_bytes) == payload:
                successes += 1
        if successes == trials:
            cliff = float(snr_db)
        else:
            break
    return Result(
        mode=mode,
        info_rate_bit_per_s=info_rate,
        duration_seconds=duration,
        cliff_snr_db=cliff,
        shannon_snr_db=shannon,
    )


def _snr_range(high_db: float, low_db: float, step_db: float = 1.0) -> list[float]:
    values = []
    current = high_db
    while current >= low_db:
        values.append(current)
        current -= step_db
    return values


def format_table(results: list[Result]) -> str:
    lines = [
        "| Mode | Settings | Throughput | Info rate | Our cliff (SNR in 3 kHz) | Shannon @ same rate | Gap |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for r in results:
        cliff_text = f"**{r.cliff_snr_db:+.0f} dB**" if r.cliff_snr_db is not None else "not reached"
        shannon_text = f"{r.shannon_snr_db:+.1f} dB"
        gap_text = (
            f"{r.cliff_snr_db - r.shannon_snr_db:.1f} dB"
            if r.cliff_snr_db is not None
            else "n/a"
        )
        info_rate_text = f"{r.info_rate_bit_per_s:.1f} bit/s"
        throughput_text = f"{r.mode.payload_size_bytes} chars in {r.duration_seconds:.1f} s"
        lines.append(
            f"| {r.mode.name} | {r.mode.settings} | {throughput_text} | {info_rate_text} | "
            f"{cliff_text} | {shannon_text} | {gap_text} |"
        )
    return "\n".join(lines)


def update_readme(table_md: str, readme_path: Path) -> None:
    text = readme_path.read_text()
    start = text.find(README_START_MARKER)
    end = text.find(README_END_MARKER)
    if start == -1 or end == -1 or end < start:
        raise RuntimeError(
            f"Could not find markers {README_START_MARKER!r} and {README_END_MARKER!r} in README"
        )
    before = text[: start + len(README_START_MARKER)]
    after = text[end:]
    new_section = f"\n\n{table_md}\n\n"
    readme_path.write_text(before + new_section + after)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="weaklink-benchmark")
    parser.add_argument("--trials", type=int, default=5, help="Trials per SNR point (default 5).")
    parser.add_argument("--dry-run", action="store_true", help="Print table but don't touch README.")
    parser.add_argument(
        "--readme",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "README.md",
        help="Path to README.md to patch.",
    )
    args = parser.parse_args(argv)

    results: list[Result] = []
    started = time.perf_counter()
    for mode in MODES:
        t0 = time.perf_counter()
        result = find_cliff(mode, trials=args.trials)
        elapsed = time.perf_counter() - t0
        cliff = f"{result.cliff_snr_db:+.0f} dB" if result.cliff_snr_db is not None else "no decode"
        print(
            f"[{elapsed:5.1f}s] {mode.name:22s}  info={result.info_rate_bit_per_s:6.1f} bit/s  "
            f"cliff={cliff:>10s}  shannon={result.shannon_snr_db:+.1f} dB"
        )
        results.append(result)
    total_elapsed = time.perf_counter() - started
    print(f"\nTotal sweep: {total_elapsed:.1f}s\n")

    table = format_table(results)
    if args.dry_run:
        print(table)
    else:
        update_readme(table, args.readme)
        print(f"Patched {args.readme}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
