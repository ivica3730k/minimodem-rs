"""Benchmark suite: sweep the full grid of {baud} x {repeats} x {RS config}
with a fixed 100-byte payload, find each combination's SNR cliff, compute
Shannon at the same info rate, and rewrite the results table in README.md
between the BENCHMARK RESULTS markers.

Run with::

    poetry run weaklink-benchmark               # default 5 trials/point
    poetry run weaklink-benchmark --trials 3    # faster
    poetry run weaklink-benchmark --dry-run     # print table, no README edit

Cliff-finding: walk down from a high SNR in 1 dB steps, record the lowest SNR
at which every trial still decodes byte-for-byte. Conservative — the true 50%
cliff is typically 1-2 dB below the reported value.
"""

from __future__ import annotations

import argparse
import math
import random
import string
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from weaklink.modem.codec import ModemConfig, decode, encode
from weaklink.modem.waveform import WaveformConfig

REFERENCE_BANDWIDTH_HZ: float = 3_000.0
README_START_MARKER = "<!-- BENCHMARK RESULTS START -->"
README_END_MARKER = "<!-- BENCHMARK RESULTS END -->"

PAYLOAD_BYTES: int = 100
PAYLOAD_SEED: int = 0
BAUDS: tuple[int, ...] = (45, 100, 300, 1200)
REPEATS: tuple[int, ...] = (1, 2, 4)
# RS configs as (data_bytes, parity_bytes). Rough overhead ratios:
#   (16, 8)  = 33% parity, small block (7 blocks per 100 bytes)
#   (32, 8)  = 20% parity, mid block  (4 blocks per 100 bytes)
#   (128, 32) = 20% parity, big block (1 padded block per 100 bytes)
RS_CONFIGS: tuple[tuple[int, int], ...] = ((16, 8), (32, 8), (128, 32))


@dataclass
class Config:
    baud: int
    repeats: int
    rs_data: int
    rs_parity: int
    payload_bytes: int = PAYLOAD_BYTES
    note: str = ""  # optional label shown in the throughput column

    def build(self) -> ModemConfig:
        return ModemConfig(
            waveform=WaveformConfig(baud=float(self.baud), tone_spacing_hz=float(self.baud)),
            preamble_length=64,
            payload_repeats=self.repeats,
            rs_data_bytes=self.rs_data,
            rs_parity_bytes=self.rs_parity,
            rs_crc_enabled=True,
        )

    def rs_label(self) -> str:
        block_size = self.rs_data + self.rs_parity + 4  # +CRC-32
        return f"RS({block_size},{self.rs_data})"

    def snr_search_high_db(self) -> float:
        return 10.0

    def snr_search_low_db(self) -> float:
        return -28.0


@dataclass
class Result:
    config: Config
    duration_seconds: float
    info_rate_bit_per_s: float
    cliff_snr_db: float | None
    shannon_snr_db: float


def _random_payload(size_bytes: int) -> bytes:
    alphabet = (string.ascii_letters + string.digits + " ").encode("ascii")
    return bytes(random.Random(PAYLOAD_SEED).choices(alphabet, k=size_bytes))


def shannon_snr_db(info_rate_bit_per_s: float, bandwidth_hz: float = REFERENCE_BANDWIDTH_HZ) -> float:
    if info_rate_bit_per_s <= 0:
        return -math.inf
    return 10.0 * math.log10(2 ** (info_rate_bit_per_s / bandwidth_hz) - 1)


def _add_awgn(samples: np.ndarray, snr_db: float, sample_rate: float, *, seed: int) -> np.ndarray:
    signal_power = float(np.mean(np.asarray(samples, dtype=np.float64) ** 2))
    noise_variance = signal_power * sample_rate / (2.0 * REFERENCE_BANDWIDTH_HZ) / (10 ** (snr_db / 10.0))
    rng = np.random.default_rng(seed)
    return samples + rng.normal(0.0, np.sqrt(noise_variance), size=samples.shape).astype(np.float32)


def _find_cliff(config: Config, *, trials: int, payload: bytes) -> Result:
    modem_config = config.build()
    samples = encode(payload, modem_config)
    duration = len(samples) / modem_config.waveform.sample_rate
    info_rate = len(payload) * 8.0 / duration
    shannon = shannon_snr_db(info_rate)

    cliff: float | None = None
    snr_high = config.snr_search_high_db()
    snr_low = config.snr_search_low_db()
    snr_db = snr_high
    while snr_db >= snr_low:
        successes = 0
        for trial in range(trials):
            seed_input = (config.baud * 1_000_003) + (config.repeats * 977) + (config.rs_data * 71) + (trial * 31) + int(snr_db * 10)
            noisy = _add_awgn(
                samples,
                snr_db=snr_db,
                sample_rate=modem_config.waveform.sample_rate,
                seed=abs(seed_input) & 0x7FFFFFFF,
            )
            if decode(noisy, modem_config, payload_length_bytes=len(payload)) == payload:
                successes += 1
        if successes == trials:
            cliff = float(snr_db)
            snr_db -= 1.0
        else:
            break
    return Result(
        config=config,
        duration_seconds=duration,
        info_rate_bit_per_s=info_rate,
        cliff_snr_db=cliff,
        shannon_snr_db=shannon,
    )


def _enumerate_configs() -> list[Config]:
    configs = []
    for baud in BAUDS:
        for repeats in REPEATS:
            for rs_data, rs_parity in RS_CONFIGS:
                configs.append(Config(baud=baud, repeats=repeats, rs_data=rs_data, rs_parity=rs_parity))
    # Notable single-config rows below the main grid.
    configs.extend(
        [
            Config(
                baud=45,
                repeats=6,
                rs_data=16,
                rs_parity=8,
                payload_bytes=15,
                note="fixed 15-byte payload, 6x repeat — SNR floor push",
            ),
            Config(
                baud=45,
                repeats=4,
                rs_data=16,
                rs_parity=8,
                payload_bytes=20,
                note="20 B = 10 sensor reports via protocol codec (~99 B as ASCII)",
            ),
        ]
    )
    return configs


def format_table(results: list[Result]) -> str:
    header = [
        f"Payload: {PAYLOAD_BYTES} random-ASCII bytes unless noted. Reference bandwidth: 3 kHz.",
        "",
        "| Baud | RS | Repeats | Throughput | Info rate | Our cliff | Shannon | Gap |",
        "|---:|---|---:|---|---:|---:|---:|---:|",
    ]
    rows = []
    for r in results:
        cliff_text = f"**{r.cliff_snr_db:+.0f} dB**" if r.cliff_snr_db is not None else "not reached"
        gap_text = (
            f"{r.cliff_snr_db - r.shannon_snr_db:.1f} dB"
            if r.cliff_snr_db is not None
            else "n/a"
        )
        throughput = f"{r.config.payload_bytes} chars in {r.duration_seconds:.1f} s"
        if r.config.note:
            throughput = f"{throughput}<br/><sub>{r.config.note}</sub>"
        rows.append(
            f"| {r.config.baud} | {r.config.rs_label()} | {r.config.repeats}&times; | "
            f"{throughput} | "
            f"{r.info_rate_bit_per_s:.1f} bit/s | {cliff_text} | "
            f"{r.shannon_snr_db:+.1f} dB | {gap_text} |"
        )
    return "\n".join(header + rows)


def update_readme(table_md: str, readme_path: Path) -> None:
    text = readme_path.read_text()
    start = text.find(README_START_MARKER)
    end = text.find(README_END_MARKER)
    if start == -1 or end == -1 or end < start:
        raise RuntimeError(
            f"could not find markers {README_START_MARKER!r} and {README_END_MARKER!r} in README"
        )
    before = text[: start + len(README_START_MARKER)]
    after = text[end:]
    readme_path.write_text(before + f"\n\n{table_md}\n\n" + after)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="weaklink-benchmark")
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--readme",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "README.md",
    )
    args = parser.parse_args(argv)

    configs = _enumerate_configs()
    print(f"Sweeping {len(configs)} configs with {args.trials} trials/point.\n")
    results: list[Result] = []
    started = time.perf_counter()
    for config in configs:
        row_start = time.perf_counter()
        payload = _random_payload(config.payload_bytes)
        result = _find_cliff(config, trials=args.trials, payload=payload)
        elapsed = time.perf_counter() - row_start
        cliff = f"{result.cliff_snr_db:+.0f} dB" if result.cliff_snr_db is not None else "no decode"
        print(
            f"[{elapsed:5.1f}s] baud={config.baud:>4} {config.rs_label():>13} "
            f"repeats={config.repeats}x  duration={result.duration_seconds:6.1f}s  "
            f"info={result.info_rate_bit_per_s:7.1f} bit/s  cliff={cliff:>9s}  "
            f"shannon={result.shannon_snr_db:+.1f} dB"
        )
        results.append(result)
    total = time.perf_counter() - started
    print(f"\nTotal: {total:.1f}s\n")

    table = format_table(results)
    if args.dry_run:
        print(table)
    else:
        update_readme(table, args.readme)
        print(f"Patched {args.readme}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
