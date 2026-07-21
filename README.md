# weaklink

Streaming MFSK modem: bytes on stdin → audio → bytes on stdout.
Works with `tail -f`; no memory buffering, no wait-for-EOF. Reed-Solomon
+ convolutional K=7 r=1/2 + soft Viterbi, per-block interleaver,
soft-LLR combining across block repeats. Modes: **OOK** and
**2/4/8/16-FSK** at **45 / 300 / 1200 baud**.

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

The three baud rates below are **presets** — starting points, not
fixed configurations. Every preset carries 13 B of payload per RS
block (RS(16,8) + CRC-32) by default; the table shows what you get
if you don't override anything. Every parameter is overridable on
both sides via CLI flags (`--modem-num-tones`, `--modem-rs-data-bytes`,
`--modem-rs-parity-bytes`, `--modem-block-repeats`, ...) or via
`ModemOptions` in the Python API.

Preset defaults use M=4 (4-FSK); the `--modem-num-tones` flag selects
any M ∈ {1, 2, 4, 8, 16}. M=1 is OOK; the sole carrier sits at the
center of the tone range shown below.

| Baud | CLI (both tx / rx) | Default tones (M=4, Hz) | Bandwidth | Default repeats | Measured best SNR | Min live tx (13 B payload) |
|---:|---|---|---:|---:|---:|---:|
| 45 | `--modem-baud 45` | 1200 / 1400 / 1600 / 1800 | 600 Hz | 4× | ≈ −14 dB | 28 s |
| 300 | `--modem-baud 300` | 1050 / 1350 / 1650 / 1950 | 900 Hz | 2× | ≈ −5 dB | 2.4 s |
| 1200 | `--modem-baud 1200` | 500 / 1700 / 2900 / 4100 | 3600 Hz | 2× | ≈ +2 dB | 1.0 s |

SNR is measured with AWGN normalised to a 3 kHz reference band — a
cross-baud comparison convention, not a physical channel filter.

Doubling `--modem-block-repeats` buys ~2–3 dB via soft-LLR combining
at proportional air time. Full sweep of every combo we test is in
[`results.md`](results.md).

### OOK (M=1)

`--modem-num-tones 1` selects on-off keying: single carrier at
`center_hz`, symbol 0 = silence, symbol 1 = tone. Narrowest possible
bandwidth of any mode — just the carrier and its modulation sidelobes,
no tone stack. Same 1 bit/symbol as 2-FSK but a few dB worse in AWGN
(silence half the time = less average energy). Cliff at 45 baud
with `block_repeats=4`: ≈ −14 dB, matching 4-FSK at the same
settings. See [`results.md`](results.md) for the per-baud numbers.

### Constant envelope for any M

`--modem-num-tones M` means "M possible frequencies to pick from per
symbol", **not** "M frequencies playing simultaneously". The transmitter
emits exactly one sinusoid at any instant, hopping between frequencies
at the symbol clock. Constant envelope (PAPR = 3 dB, the peak-to-RMS
of a pure sine) regardless of M.

Consequences:

- **All transmit power in one tone at a time** — maximum per-symbol SNR, no `1/M` power split.
- **Higher M buys log₂(M) bits/symbol** with no PAPR cost. 16-FSK carries 4× the bits of 2-FSK at the same baud, same peak power.

On an SDR waterfall you'll see all M frequency slots "lit up" during a long transmission — that's the display integrating over time. Shrink the FFT window below one symbol duration (~3.3 ms at 300 baud) and you'll see the transmitter chasing one tone across the slots instead.

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
| `--modem-num-tones M` | `4` | MFSK order: `1` (OOK) / `2` / `4` / `8` / `16`. Higher M packs more bits per symbol (log₂ M) at wider bandwidth. 2 halves throughput vs 4 but fits narrow audio paths (e.g. FM voice via SignaLink). 1 (OOK) is narrowest and switching-amp friendly. TX and RX must match. |
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
    F --> G[Bits → MFSK<br/>symbols]
    G --> H[CPFSK<br/>modulator]
    H --> I[+ preamble<br/>+ pilot]
    I --> J[audio out]
```

### RX signal chain

```mermaid
flowchart LR
    A[audio in] --> B[Non-coherent<br/>MFSK demod]
    B --> C[Coarse FFT<br/>frequency offset]
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

One RS block, data area (before conv + interleave + MFSK):

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

- **MFSK** — M-ary Frequency Shift Keying. M possible tone frequencies; each symbol picks one → log₂(M) bits/symbol. Exactly one tone on the air at any instant (never a stack). Default M=4.
- **CPFSK** — Continuous-Phase FSK. Frequency changes between symbols with no phase discontinuity, so the envelope stays clean at symbol boundaries. This modem's MFSK is CPFSK.
- **OOK** — On-off keying. `--modem-num-tones 1`: single carrier, symbol 0 = silence, symbol 1 = tone. 1 bit/symbol like 2-FSK, narrower bandwidth, a few dB worse in AWGN.
- **PAPR** — Peak-to-average power ratio. 3 dB for any single-tone signal (this modem); ~10·log₁₀(M) dB for M summed tones (OFDM-style, not this modem).
- **Preamble** — Fixed 32-symbol PN sequence bracketing every slot; RX locks timing / frequency / amplitude from it.
- **Slot** — One preamble + one RS-encoded block on the wire.
- **Block** — RS-encoded chunk carrying header + payload.
- **RS(n,k)** — Reed-Solomon outer code: k data → n wire bytes; corrects up to (n−k)/2 byte errors.
- **Convolutional (K=7, r=1/2) + soft Viterbi** — Inner FEC and its decoder, driven by per-bit LLRs.
- **LLR** — Log-likelihood ratio; a soft (real-valued) confidence per bit instead of a hard 0/1.
- **Interleaver** — Bit shuffle so bursts become isolated errors. Changes every block (32-permutation cycle).
- **Pilot** — Short random MFSK burst before / after every live TX (audio-level marker).
- **SNR (dB)** — Signal-to-noise ratio in the 3 kHz reference band. Negative = noise louder than signal.
- **AWGN** — Additive white Gaussian noise; the clean-channel noise model the benchmark uses.
- **Best SNR / cliff** — Lowest SNR at which decode still works for a given config. Below it, everything breaks.
- **Shannon limit** — Theoretical lowest SNR at which a given data rate can be decoded error-free. Reported gap-to-Shannon in `results.md`.

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
