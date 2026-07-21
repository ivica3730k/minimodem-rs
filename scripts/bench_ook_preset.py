"""One OOK cliff-search per supported baud using that baud's preset
parameters. Prints a markdown table to stdout and appends it to
``results.md`` between OOK-specific markers.

Payload is 16 bytes -- 100 would run for hours at 45 baud + 1 bit/symbol.
"""

from __future__ import annotations

import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from weaklink.modem.benchmark import (
    Config,
    Result,
    _find_cliff,
    _random_payload,
    format_table,
)
from weaklink.modem.constants import BAUD_PRESETS

OOK_PAYLOAD_BYTES = 16
TRIALS = 5

OOK_START_MARKER = "<!-- OOK RESULTS START -->"
OOK_END_MARKER = "<!-- OOK RESULTS END -->"


def _preset_ook_configs() -> list[Config]:
    """One OOK config per supported baud, using that baud's preset."""
    configs: list[Config] = []
    for baud_float, preset in BAUD_PRESETS.items():
        configs.append(
            Config(
                baud=int(baud_float),
                rs_data=int(preset["rs_data_bytes"]),
                rs_parity=int(preset["rs_parity_bytes"]),
                block_repeats=int(preset["block_repeats"]),
                num_tones=1,
                sync_every=int(preset["sync_every_blocks"]),
                payload_bytes=OOK_PAYLOAD_BYTES,
            )
        )
    return configs


def _run_one(bundle: tuple[Config, int]) -> Result:
    config, trials = bundle
    payload = _random_payload(config.payload_bytes)
    return _find_cliff(config, trials=trials, payload=payload)


def _upsert_section(readme_path: Path, section_body: str) -> None:
    text = readme_path.read_text()
    section = f"\n\n{OOK_START_MARKER}\n\n{section_body}\n\n{OOK_END_MARKER}\n"
    start = text.find(OOK_START_MARKER)
    end = text.find(OOK_END_MARKER)
    if start == -1 or end == -1:
        readme_path.write_text(text.rstrip() + section)
        return
    before = text[:start].rstrip()
    after = text[end + len(OOK_END_MARKER) :].lstrip()
    readme_path.write_text(before + section + ("\n" + after if after else ""))


def main() -> None:
    configs = _preset_ook_configs()
    print(f"OOK preset sweep: {len(configs)} configs, {OOK_PAYLOAD_BYTES}-byte payload, {TRIALS} trials/point.")
    started = time.perf_counter()
    with ProcessPoolExecutor(max_workers=len(configs)) as pool:
        results = list(pool.map(_run_one, [(c, TRIALS) for c in configs]))
    elapsed = time.perf_counter() - started
    print(f"Done in {elapsed:.1f} s.\n")

    table = format_table(results)
    print(table)

    section = (
        f"## OOK / 1-tone (preset per baud, {OOK_PAYLOAD_BYTES}-byte payload)\n\n"
        f"{table}\n"
    )
    readme = Path(__file__).resolve().parent.parent / "results.md"
    _upsert_section(readme, section)
    print(f"\nPatched {readme}")


if __name__ == "__main__":
    main()
