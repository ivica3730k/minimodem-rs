"""Streaming-modem benchmark: sweep baud x RS x sync-every, measure the SNR
cliff, compute Shannon at the same info rate, rewrite the README table.

Run with::

    poetry run weaklink-benchmark               # default 5 trials/point
    poetry run weaklink-benchmark --trials 3    # faster
    poetry run weaklink-benchmark --dry-run     # print table, no README edit

Cliff finding: 1 dB steps, record the lowest SNR at which every trial still
decodes the whole payload byte-for-byte. Conservative — the 50% cliff is
usually 1–2 dB below.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import string
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from weaklink.modem.codec import ModemConfig, decode, encode
from weaklink.modem.exceptions import ConfigError
from weaklink.modem.waveform import WaveformConfig

REFERENCE_BANDWIDTH_HZ: float = 3_000.0
README_START_MARKER = "<!-- BENCHMARK RESULTS START -->"
README_END_MARKER = "<!-- BENCHMARK RESULTS END -->"

PAYLOAD_BYTES: int = 100
#: OOK (1 tone) carries 1 bit per symbol -- 100 bytes takes ~10x the air
#: time of 4-FSK at the same baud. Cap it lower so the sweep finishes.
OOK_PAYLOAD_BYTES: int = 16
PAYLOAD_SEED: int = 0
BAUDS: tuple[int, ...] = (45, 300, 1200)
RS_CONFIGS: tuple[tuple[int, int], ...] = ((16, 8), (32, 8), (128, 32))
BLOCK_REPEATS: tuple[int, ...] = (1, 2, 4, 8)
NUM_TONES: tuple[int, ...] = (1, 2, 4, 8, 16)
SYNC_EVERY_FIXED: int = 4


@dataclass
class Config:
    baud: int
    rs_data: int
    rs_parity: int
    block_repeats: int
    num_tones: int = 4
    sync_every: int = SYNC_EVERY_FIXED
    payload_bytes: int = PAYLOAD_BYTES
    note: str = ""

    def build(self) -> ModemConfig:
        return ModemConfig(
            waveform=WaveformConfig(
                baud=float(self.baud),
                tone_spacing_hz=float(self.baud),
                num_tones=self.num_tones,
            ),
            rs_data_bytes=self.rs_data,
            rs_parity_bytes=self.rs_parity,
            rs_crc_enabled=True,
            sync_every_blocks=self.sync_every,
            block_repeats=self.block_repeats,
        )

    def rs_label(self) -> str:
        """Block layout label: ``<wire>B block / <data>B data / <parity>B parity``.

        Wire = data + 4 (CRC-32) + parity. Textbook RS notation would be
        ``RS(wire, data+4)``; we spell it out so the user knows the numbers
        match ``--modem-rs-data-bytes`` and ``--modem-rs-parity-bytes``.
        """
        wire = self.rs_data + self.rs_parity + 4
        return f"{wire}B block / {self.rs_data}B data / {self.rs_parity}B parity"


@dataclass
class Result:
    config: Config
    duration_seconds: float
    info_rate_bit_per_s: float
    cliff_snr_db: float | None
    shannon_snr_db: float


def _random_payload(size: int) -> bytes:
    alphabet = (string.ascii_letters + string.digits + " ").encode("ascii")
    return bytes(random.Random(PAYLOAD_SEED).choices(alphabet, k=size))


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
    snr_db = 10.0
    while snr_db >= -28.0:
        successes = 0
        for trial in range(trials):
            seed_input = (
                config.baud * 1_000_003
                + config.rs_data * 71
                + config.sync_every * 13
                + config.block_repeats * 97
                + trial * 31
                + int(snr_db * 10)
            )
            noisy = _add_awgn(
                samples,
                snr_db=snr_db,
                sample_rate=modem_config.waveform.sample_rate,
                seed=abs(seed_input) & 0x7FFFFFFF,
            )
            decoded = decode(noisy, modem_config).rstrip(b"\x00")
            if decoded == payload.rstrip(b"\x00"):
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


#: OOK is 1 bit/symbol -- one 45-baud config with block_repeats=8 would
#: run for hours. Restrict the OOK sweep to bauds and repeat counts
#: that finish in a reasonable time.
OOK_BAUDS: tuple[int, ...] = (300, 1200)
OOK_BLOCK_REPEATS: tuple[int, ...] = (1, 2)


def _enumerate_configs() -> list[Config]:
    configs: list[Config] = []
    for baud in BAUDS:
        for rs_data, rs_parity in RS_CONFIGS:
            for block_repeats in BLOCK_REPEATS:
                for num_tones in NUM_TONES:
                    if num_tones == 1:
                        if baud not in OOK_BAUDS or block_repeats not in OOK_BLOCK_REPEATS:
                            continue
                    cfg = Config(
                        baud=baud,
                        rs_data=rs_data,
                        rs_parity=rs_parity,
                        block_repeats=block_repeats,
                        num_tones=num_tones,
                        payload_bytes=OOK_PAYLOAD_BYTES if num_tones == 1 else PAYLOAD_BYTES,
                    )
                    # Skip Nyquist-infeasible combos (e.g. 300 baud x 32 tones
                    # needs 9.3 kHz of tone stack, our 18 kHz internal rate
                    # can't).
                    try:
                        cfg.build()
                    except ConfigError:
                        continue
                    configs.append(cfg)
    return configs


def _cli_snippet(cfg: Config) -> str:
    """One `--modem-*` per line so the table cell stays narrow."""
    parts = [
        f"`--modem-baud {cfg.baud}`",
        f"`--modem-num-tones {cfg.num_tones}`",
        f"`--modem-rs-data-bytes {cfg.rs_data}`",
        f"`--modem-rs-parity-bytes {cfg.rs_parity}`",
        f"`--modem-block-repeats {cfg.block_repeats}`",
    ]
    return "<br/>".join(parts)


def format_table(results: list[Result]) -> str:
    """Combined table -- throughput, info rate, measured cliff, Shannon
    limit, and gap all in one row per config."""
    header = [
        f"Streaming modem. Payload: {PAYLOAD_BYTES} random-ASCII bytes. Sync every "
        f"{SYNC_EVERY_FIXED} data blocks. Reference bandwidth: 3 kHz.",
        "",
        "| Baud | Tones | CLI (both tx / rx) | Throughput | Info rate | Best SNR | Shannon | Gap |",
        "|---:|---:|---|---|---:|---:|---:|---:|",
    ]
    rows = []
    for r in results:
        cliff_text = (
            f"**{r.cliff_snr_db:+.0f} dB**" if r.cliff_snr_db is not None else "not reached"
        )
        gap_text = (
            f"{r.cliff_snr_db - r.shannon_snr_db:.1f} dB"
            if r.cliff_snr_db is not None else "n/a"
        )
        throughput = f"{r.config.payload_bytes} chars in {r.duration_seconds:.1f} s"
        if r.config.note:
            throughput = f"{throughput}<br/><sub>{r.config.note}</sub>"
        rows.append(
            f"| {r.config.baud} | {r.config.num_tones} | {_cli_snippet(r.config)} | {throughput} | "
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


def _print_row(config: Config, result: Result, elapsed_seconds: float | None) -> None:
    cliff = (
        f"{result.cliff_snr_db:+.0f} dB"
        if result.cliff_snr_db is not None else "no decode"
    )
    when = f"[{elapsed_seconds:5.1f}s] " if elapsed_seconds is not None else ""
    print(
        f"{when}baud={config.baud:>4} {config.rs_label():>13} "
        f"repeats={config.block_repeats}x  duration={result.duration_seconds:6.1f}s  "
        f"info={result.info_rate_bit_per_s:7.1f} bit/s  cliff={cliff:>9s}  "
        f"shannon={result.shannon_snr_db:+.1f} dB",
        flush=True,
    )


def _run_one(bundle: tuple[Config, int]) -> Result:
    """Pool worker: run cliff-search for one config. Kept at module top
    level so multiprocessing can pickle it."""
    config, trials = bundle
    payload = _random_payload(config.payload_bytes)
    return _find_cliff(config, trials=trials, payload=payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="weaklink-benchmark")
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--bauds",
        type=str,
        default=",".join(str(b) for b in BAUDS),
        help="Comma-separated list of baud rates to sweep. Default: all "
        "supported. Example: --bauds 300,1200 skips the slow 45-baud rows.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=max(1, (os.cpu_count() or 4) - 2),
        help="Parallel worker processes. Configs are independent so this "
        "scales roughly linearly with core count (minus BLAS overhead). "
        "Default: (num_cores - 2), one workload per core with headroom.",
    )
    parser.add_argument(
        "--readme",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "results.md",
        help="Markdown file to update between the BENCHMARK RESULTS markers. "
        "Defaults to ``results.md`` at repo root.",
    )
    args = parser.parse_args(argv)

    selected_bauds = {int(b) for b in args.bauds.split(",") if b.strip()}
    configs = [c for c in _enumerate_configs() if c.baud in selected_bauds]
    if not configs:
        print(f"no configs match --bauds {args.bauds!r}")
        return 1

    print(
        f"Sweeping {len(configs)} configs with {args.trials} trials/point "
        f"across {args.workers} worker(s).\n"
    )
    started = time.perf_counter()
    results: list[Result] = [None] * len(configs)  # type: ignore[list-item]
    bundles = [(c, args.trials) for c in configs]
    if args.workers <= 1:
        # Sequential path for debugging / single-core boxes.
        for i, bundle in enumerate(bundles):
            row_start = time.perf_counter()
            results[i] = _run_one(bundle)
            _print_row(bundle[0], results[i], time.perf_counter() - row_start)
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for i, result in enumerate(pool.map(_run_one, bundles)):
                results[i] = result
                _print_row(configs[i], result, None)
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
