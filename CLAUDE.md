# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Streaming MFSK modem: bytes on stdin → audio → bytes on stdout. Reed-Solomon + convolutional K=7 r=1/2 + soft Viterbi + per-block interleaver + soft-LLR combining across block repeats. Modes: OOK and 2/4/8/16-FSK at 45 / 300 / 1200 baud. Published to PyPI as `weaklink-modem`; the Python module is `weaklink.modem` (src/ layout).

## Common commands

```bash
poetry install                                          # dev setup
poetry run pytest -q                                    # full suite (~2 min)
poetry run pytest tests/test_modem_end_to_end.py -q     # single file
poetry run pytest -k "cliff and monotonic" -q           # by expression
poetry run pytest -n auto -q                            # parallel via pytest-xdist

poetry run weaklink-modem tx --modem-wav /tmp/out.wav   # CLI (entry point)
poetry run weaklink-modem rx --modem-wav /tmp/out.wav
poetry run weaklink-modem-benchmark                     # SNR-cliff sweep → results.md

python scripts/build_pypi_readme.py <tag>               # regenerate readme-pypi.md
```

Pre-commit hooks (installed by `poetry install` via `pre-commit`): ruff (F, I001), black (line-length 120), mypy (strict), and `conventional-pre-commit` on commit messages. Don't skip them (`--no-verify` is off-limits without an explicit ask).

## Release flow

Direct pushes to `main` are blocked by the sandbox classifier. Route every change through a branch → PR → `gh pr merge --merge --admin`. Only cut releases when explicitly asked.

Creating `gh release create v<X.Y.Z> --target main` on a tag triggers two workflows: `release.yaml` (wheel + sdist + x86_64/arm64 PyInstaller binaries + amd64/arm64 .deb, uploaded to the GitHub release) and `publish-pypi.yaml` (PyPI upload). Both run `poetry version "${GITHUB_REF_NAME#v}"` from the tag — `pyproject.toml` stays at `0.0.0` in git.

## Architecture

Signal chain top-down (TX; RX is the mirror):

```
stdin → codec.encode_stream → rs.encode_block → fec.conv_encode
      → interleaver.permute → waveform.modulate → audio.write
```

- **`codec.py`** — orchestrator. Slot layout `[preamble][RS-block]`, session layout `[pilot][slot]…[slot][pilot]`. Each RS block wraps `[length(1B)][block_index(2B)][payload][pad][CRC-32(4B)][RS parity]`. Message boundaries are inferred at RX from non-block-length spans between preambles, so one rx pipe can watch many independent tx sessions in a row.
- **`waveform.py`** — MFSK CPFSK modulator + non-coherent I/Q demod. Continuous phase across symbol boundaries; Gray-coded symbol → tone mapping; max-log-MAP soft output. **Single tone at a time regardless of `num_tones`** — `num_tones` is the alphabet size (log₂ M bits per symbol), not the count of simultaneous carriers. Envelope is constant (PAPR = 3 dB) for all M.
- **`rs.py`** — `reedsolo`-backed outer code; CRC-32 inside.
- **`fec.py`** — K=7 r=1/2 NASA/CCSDS generators (171, 133 octal); soft Viterbi driven by per-bit LLRs.
- **`interleaver.py`** — per-block bit permutation cycling every 32 blocks (breaks up burst errors before Viterbi + avoids periodic-noise alignment).
- **`streaming.py`** — live-rx poll loop, pilot burst generation, streaming decoder. Message boundaries derived here.
- **`audio.py`** — WAV via soundfile; live via sounddevice or `paplay`/`parec` subprocess for Pulse endpoints PortAudio can't see (e.g. `virt.monitor`).
- **`api.py`** — public Python API. **Mirrors the CLI 1:1**: every `--modem-*` flag is a kwarg, every runtime mode (WAV, live in/out, PTT, tune, batch samples) is available end-to-end. The API takes device kwargs and streams — it does not shuffle audio arrays around.
- **`cli.py`** — argparse entry point, `weaklink-modem tx|rx`. Baud presets from `constants.BAUD_PRESETS`.
- **`benchmark.py`** — sweeps baud × num_tones × RS × repeats; writes markdown-fenced regions in `results.md`. Every result is persisted to `.benchmark-cache/*.jsonl` as it lands (crash-safe; gitignored).
- **`constants.py`** — `BAUD_PRESETS` (45 / 300 / 1200), `REFERENCE_BANDWIDTH_HZ = 3000` (SNR normalization convention), preamble PN sequence.
- **`ptt.py`** — CM108-style PTT via HID (optional).
- **`exceptions.py`** — typed exceptions (`ConfigError`, `TonesOutOfRangeError`, etc.).

Tests: `test_*.py` pairs — most batch-decode tests have an `_streaming` companion that drives audio through the same `_StreamingRxPump` the CLI uses.

## Constraints and conventions

- **Legal**: do not mention transmission over regulated bands or licensed frequencies. The modem is an audio-domain byte pipe; keep docs framed that way.
- **Scope discipline**: mirror the source, answer questions literally, don't add features / aliases / tests unless asked.
- **No lazy imports**. All imports at module top.
- **No `**kwargs` passthrough**. Every option is an explicit named parameter (mirrors the CLI shape).
- **Comments are for non-obvious *why*** (hidden constraint, workaround, surprising behavior). Don't restate what the code does or reference tickets/PRs.
- Specific mode names like `4-FSK`, `16-FSK` are conventional shorthand for a fixed M and are kept as-is. `MFSK` is the family name. `N-FSK` is not used anywhere.
