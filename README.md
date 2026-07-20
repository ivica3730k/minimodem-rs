# weaklink

Streaming digital modem: bytes on stdin → audio → bytes on stdout. Works
with `tail -f`; no memory buffering, no wait-for-EOF.

![alt text](image.png)

Distribution: `weaklink-modem` (PyPI + release binaries).

---

## Install

Portable Linux binary:

```bash
sudo apt install libportaudio2 libsndfile1
curl -L -O https://github.com/ivica3730k/weaklink-9a3ice/releases/latest/download/weaklink-modem-linux-x86_64-latest
chmod +x weaklink-modem-linux-x86_64-latest
```

Debian / Ubuntu `.deb`:

```bash
curl -L -O https://github.com/ivica3730k/weaklink-9a3ice/releases/latest/download/weaklink-modem_amd64-latest.deb
sudo dpkg -i weaklink-modem_amd64-latest.deb
```

From source:

```bash
poetry install
poetry run weaklink-modem --version
```

PyPI:

```bash
pip install weaklink-modem
```

---

## Quickstart

```bash
# WAV roundtrip
echo -n "hello weaklink" | weaklink-modem tx --modem-wav /tmp/hello.wav
weaklink-modem rx --modem-wav /tmp/hello.wav

# Live speaker → mic
weaklink-modem rx > out.txt &
echo -n "over the room" | weaklink-modem tx

# Long-lived stream
tail -f /var/log/syslog | weaklink-modem tx --modem-baud 300
```

Both sides must use the same `--modem-baud` (no handshake).

---

## Presets

Three baud rates. Every preset carries 13 B of payload per RS block
(RS(16,8) + CRC-32), so message sizes map identically across bauds.

| Baud | CLI (both tx / rx) | 4-FSK tones (Hz) | Bandwidth | Default repeats | Measured best SNR | Min live tx (13 B payload) |
|---:|---|---|---:|---:|---:|---:|
| 45 | `--modem-baud 45` | 1200 / 1400 / 1600 / 1800 | 600 Hz | 4× | ≈ −14 dB | 28 s |
| 300 | `--modem-baud 300` | 1050 / 1350 / 1650 / 1950 | 900 Hz | 2× | ≈ −5 dB | 2.4 s |
| 1200 | `--modem-baud 1200` | 500 / 1700 / 2900 / 4100 | 3600 Hz | 2× | ≈ +2 dB | 1.0 s |

SNR is measured with AWGN normalised to a 3 kHz reference band — a
cross-baud comparison convention, not a physical channel filter.

Override `--modem-block-repeats N` on both sides for more copies. Each
doubling buys ~2–3 dB via soft-LLR combining, at proportional air time.

---

## Debugging live audio

`weaklink-modem rx --modem-debug > out.txt` writes diagnostics to
`log.txt` (stdout stays clean for piping). Watch for:

- `audio: peak +X dBFS` below −40 dBFS → wrong mic or gain too low.
- `RS corrected` — outer code saved a block.
- `N slot(s) failed CRC/RS` — unrecoverable, data lost.
- macOS mic AGC / voice-isolation destroys tones; disable in System Settings.

---

## Full SNR sweep

Every baud × num_tones × RS × repeats combo is measured in
[`results.md`](results.md). Re-run `poetry run weaklink-modem-benchmark` to
refresh.

---

## Supported audio backends

| Backend | Linux | macOS | Windows | Notes |
|---|:---:|:---:|:---:|---|
| sounddevice / PortAudio | ✓ | ✓ | ✓* | Default. Index (`5`) or name substring (`USB`). WASAPI / CoreAudio / ALSA under the hood. |
| Pulse / PipeWire subprocess | ✓ | — | — | `paplay` / `parec`. Fires on `pulse:<id>`, `pulse:<name>`, a bare Pulse sink id resolvable by `pactl`, or a name PortAudio doesn't know (e.g. `virt.monitor`). |
| WAV via soundfile | ✓ | ✓ | ✓ | File I/O only, via `--modem-wav`. |

`*` Windows is untested — PortAudio supports it so it should work, but no CI on that platform yet.

---

## CLI reference

| Flag | Default | Description |
|------|---------|-------------|
| `--modem-baud N` | `300` | Symbol rate. Only `45`, `300`, `1200` supported. |
| `--modem-num-tones N` | `4` | N-FSK order: 2 / 4 / 8 / 16. Higher packs more bits per symbol at wider bandwidth and worse cliff. 2 halves throughput but fits narrow audio paths (e.g. FM voice via SignaLink). TX and RX must match. |
| `--modem-rs-data-bytes N` | preset | Reed-Solomon data bytes per block. |
| `--modem-rs-parity-bytes N` | preset | RS parity bytes. Corrects up to N/2 byte errors per block. |
| `--modem-no-rs-crc` | CRC on | Skip the CRC-32 inside each RS block. |
| `--modem-block-repeats N` | preset | N copies per block, each permuted differently; RX soft-combines LLRs. |
| `--modem-wav PATH` | live | WAV file instead of live audio. |
| `--modem-audio-output NAME` | OS default | tx audio target: sounddevice index, name substring, or Pulse sink. |
| `--modem-audio-input NAME` | OS default | rx audio source: same syntax; Pulse sources like `virt.monitor` supported. |
| `--modem-debug` | off | Verbose DEBUG chatter in the log file. |
| `--modem-log-file PATH` | `./log.txt` | Diagnostics land here. |

---

## How it works

### TX signal chain

```mermaid
flowchart LR
    A[stdin bytes] --> B[Chunk<br/>rs_data − 3 B<br/>per block]
    B --> C[Frame<br/>len · idx · payload · pad]
    C --> D[RS + CRC-32]
    D --> E[Conv encode<br/>K=7, r=1/2]
    E --> F[Per-block<br/>interleaver<br/>32-cycle PN]
    F --> G[Bits → 4-FSK<br/>symbols]
    G --> H[CPFSK<br/>modulator]
    H --> I[+ preamble<br/>+ pilot]
    I --> J[audio out]
```

### RX signal chain

```mermaid
flowchart LR
    A[audio in] --> B[Non-coherent<br/>4-FSK demod]
    B --> C[Coarse FFT<br/>LO offset]
    C --> D[Preamble<br/>correlator]
    D --> E[Peak detect<br/>+ non-max<br/>suppression]
    E --> F[Per-preamble<br/>fine offset]
    F --> G[Slot soft LLRs]
    G --> H[Deinterleave<br/>seed brute-force]
    H --> I[Soft Viterbi]
    I --> J{RS + CRC<br/>ok?}
    J -- yes --> K[Assemble<br/>by block_index]
    J -- no --> L[Buffer LLRs;<br/>combine across<br/>R copies]
    L --> I
    K --> M[Strip zero-pad<br/>via length]
    M --> N[stdout bytes]
```

Every slot is bracketed by a preamble, so any single slot decodes
standalone. Spurious mid-stream peaks get dropped. Message boundaries
between separate tx sessions are inferred from non-block-length spans
between preambles — one rx pipe can watch many tx sessions in a row.

### Wire format

```
One tx session (live audio):

  ┌────────┬─────┬────────┬─────┬────────┬─────┬─────┬────────┬─────┬────────┐
  │ pilot  │ pre │ slot 0 │ pre │ slot 1 │ pre │ ... │slot N-1│ pre │ pilot  │
  └────────┴─────┴────────┴─────┴────────┴─────┴─────┴────────┴─────┴────────┘

One RS block, data area (before conv + interleave + FSK):

  ┌── 1B ──┬── 2B ────┬──── rs_data − 3 B ────┬── 4B CRC ──┬── rs_parity B ──┐
  │ length │block_idx │ payload (zero-padded) │  CRC-32    │  RS parity      │
  └────────┴──────────┴───────────────────────┴────────────┴─────────────────┘
```

`block_idx` is 2 bytes → one tx session is bounded at 65 535 slots.

---

## Testing

```bash
poetry run pytest -q            # ~2 min, full suite
```

Every batch-decode test has an e2e-streaming companion that drives audio
through the same `_StreamingRxPump` the CLI uses.

---

## Glossary

- **N-FSK / CPFSK** — N continuous-phase tones, log₂(N) bits per symbol. Default N=4.
- **Preamble** — Fixed 32-symbol PN sequence bracketing every slot; RX locks timing / frequency / amplitude from it.
- **Slot** — Preamble + one RS-encoded block.
- **Block** — RS-encoded chunk carrying header + payload.
- **RS(n,k)** — Reed-Solomon outer code. k data + parity → n wire bytes; corrects (n-k)/2 byte errors.
- **CRC-32** — Catches errors past RS correction.
- **Convolutional code (K=7, r=1/2) + soft Viterbi** — Inner FEC and its decoder, driven by per-bit LLRs.
- **LLR** — Log-likelihood ratio; a soft (real-valued) confidence per bit instead of a hard 0/1.
- **Interleaver** — Bit shuffle so bursts become isolated errors. Ours changes every block (32-permutation cycle).
- **Non-coherent demod** — Tone detection by energy; ~3 dB behind coherent.
- **LO offset** — Radio frequency error; we correct up to ±500 Hz.
- **Pilot** — Short random N-FSK burst before / after every live TX.
- **SNR (dB)** — Signal-to-noise ratio. Negative = noise louder than signal.
- **AWGN** — Additive white Gaussian noise; the standard clean-channel noise model used by the benchmark.
- **Shannon limit** — Theoretical lowest SNR at which a given data rate can be decoded error-free. Every FEC decoder sits some dB above it — that gap is what "Gap" columns report.
- **Best SNR / cliff** — Lowest SNR at which decode still works for a given config. Below it, everything breaks.
- **Nyquist theorem** — A signal is only recoverable if sampled above twice its highest frequency. In practice: tones must sit below sample_rate/2, and N-FSK tone spacing must be at least `1/T_symbol` for non-coherent orthogonality.

---

## Roadmap

- **Coherent detection** — Costas-loop demod, ~3 dB gain. Big DSP lift.
- **LDPC** — Closes ~2–4 dB of the Shannon gap. Needs a proper construction.

---

## License

MIT. See `LICENSE`.

Reed-Solomon via [`reedsolo`](https://github.com/tomerfiliba-org/reedsolomon).
Convolutional code uses the standard NASA/CCSDS (171, 133) generator
polynomials. Audio via [`sounddevice`](https://github.com/spatialaudio/python-sounddevice)
and [`soundfile`](https://github.com/bastibe/python-soundfile).
