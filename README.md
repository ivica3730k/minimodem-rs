# weaklink

A streaming digital modem: takes arbitrary bytes and turns them into audio;
takes audio and turns it back into bytes. Simple byte pipe — no user-visible
framing, no length field, no message vocabulary. You add whatever
protocol on top (or don't).

Both sides stream: tx encodes stdin block-by-block and pipes audio to the
soundcard as it goes (no memory buffering, works with `tail -f`), rx
drips decoded bytes to stdout as blocks are recovered.

![alt text](image.png)

Distribution: `weaklink-9a3ice`.

## Glossary

Skip this if you already know the vocabulary. Terms below appear
throughout the rest of the README.

- **Baud** — Symbols per second. Higher baud = more bits/s but needs more SNR. Presets: 9, 45, 300, 1200.
- **4-FSK** — Modulation using four distinct tones; each tone carries 2 bits.
- **CPFSK** — Continuous-Phase FSK. Phase doesn't jump between tones — cleaner spectrum than plain FSK.
- **Preamble** — Known 32-symbol pattern that brackets every slot. RX uses it to lock onto timing, frequency, and amplitude.
- **PN sequence** — Pseudo-random pattern that looks like noise but is deterministic. Our preamble is a 32-symbol PN sequence.
- **Slot** — One wire unit: a preamble followed by one RS-encoded block. Every slot decodes standalone.
- **Block** — Fixed-size RS-encoded chunk inside a slot. Holds a slice of user bytes plus header, CRC, and parity.
- **Reed-Solomon (RS)** — Outer error-correcting code. RS(N,K) sends K data bytes plus parity as N wire bytes; corrects up to (N-K)/2 byte errors per block.
- **CRC-32** — 32-bit checksum inside every block. Catches errors that slipped past RS, so we know when to drop a bad block.
- **Convolutional code (K=7, rate-1/2)** — Inner code that doubles the coded-bit rate but sharply improves error resilience.
- **Viterbi decoder** — Standard decoder for the convolutional code. "Soft" Viterbi uses per-bit confidence instead of hard 0/1.
- **Soft bits / LLR** — Log-Likelihood Ratio. Per-bit confidence value fed to the decoder. Massively better than hard decisions.
- **Interleaver** — Deterministically shuffles bits before TX and unshuffles at RX. Turns bursty noise into isolated errors that FEC handles well.
- **Per-block interleaver** — Bit shuffle changes every block (cycling through 32 permutations). Breaks periodic-noise alignment that a fixed shuffle can't.
- **Non-coherent demod** — Detects tones by energy alone, without tracking the transmitter's carrier phase. Simple, robust, ~3 dB behind coherent.
- **LO offset** — Local-oscillator frequency error. Radios rarely tune exactly right; we estimate and correct up to ±500 Hz.
- **SNR (dB)** — Signal-to-noise ratio. Negative dB = noise louder than signal. Ours are measured in a 3 kHz reference bandwidth.
- **Shannon limit** — Theoretical lowest SNR at which a given data rate is *possible*. Our "Gap" column shows how far above it we sit.
- **Pilot** — Short random 4-FSK burst before and after every live TX. Wakes idle audio sinks and gives the RX FFT real tone energy.

## Features

**Error correction & data integrity**
- Reed-Solomon RS(N,K) outer code — corrects up to K/2 byte errors per block.
- CRC-32 inside every RS block — catches errors past the RS correction limit.
- Rate-1/2 K=7 convolutional inner code — buys ~5 dB of coding gain.
- Soft-decision Viterbi decoder — uses tone-magnitude LLRs, not hard bits.
- Per-block pseudorandom interleaver — 32-cycle shuffle beats bursts *and* periodic noise (SMPS, mains hum).
- 1-byte length header per block — strips zero-padding, no trailing NUL leakage.
- 2-byte block-index header — dedupes retry copies and pins block position.
- `--modem-block-repeats N` — N copies per block, each permuted differently; RX soft-combines LLRs for real diversity.

**Synchronisation & channel**
- 32-symbol PN preamble — deterministic pseudo-random pattern bracketing every slot.
- Amplitude-normalised correlator — fade-invariant, still locks below −20 dB SNR.
- MAD-based signal-presence gate — 6σ above noise; ignores pure-noise buffers.
- Coarse FFT frequency-offset search — locks the LO within ±500 Hz.
- Per-preamble fine frequency tracking — follows drift slot-by-slot at ~1 Hz.
- Non-coherent 4-FSK CPFSK demod — no carrier-phase recovery required.
- Spurious mid-stream peak rejection — drops false detections via stride sanity.
- Virtual head/tail preamble projection — decodes slots whose edge preambles were chopped.

**Streaming**
- TX streams stdin — encodes block-by-block, no buffering; works with `tail -f`.
- RX drips stdout — bytes emit as blocks decode, no wait-for-EOF.
- Back-to-back tx sessions decoded in order — message boundaries auto-detected.
- Missing-block gap tolerance — one lost slot doesn't strand the tail.
- 65 535 slots per session — 2-byte index, cheap headroom for long streams.

**Live audio**
- PulseAudio / PipeWire via `paplay` / `parec` subprocess.
- `sounddevice` / PortAudio for anything else.
- Explicit `--modem-audio-input` / `--modem-audio-output` (index, substring, or Pulse sink).
- WAV mode for offline encode / decode.
- Pilot padding each side — wakes idle sinks, gives the FFT real tone energy.
- Snappy Ctrl-C — `parec` killed cleanly, no lingering audio processes.

## Install

**Grab the portable Linux binary** (recommended — no Python, no venv):

```bash
sudo apt install libportaudio2 libsndfile1        # runtime shared libs
curl -L -O https://github.com/ivica3730k/weaklink-9a3ice/releases/latest/download/weaklink-9a3ice-linux-x86_64-latest
chmod +x weaklink-9a3ice-linux-x86_64-latest
./weaklink-9a3ice-linux-x86_64-latest --version
```

The `-latest` suffix is a stable filename that CI overwrites on every
release, so the URL above never rots. If you want to pin a specific
version, grab `weaklink-9a3ice-linux-x86_64-X.Y.Z` from that release.

**Or install the `.deb`** on Debian / Ubuntu (puts `weaklink-9a3ice` on `PATH`):

```bash
curl -L -O https://github.com/ivica3730k/weaklink-9a3ice/releases/latest/download/weaklink-9a3ice_amd64-latest.deb
sudo dpkg -i weaklink-9a3ice_amd64-latest.deb
weaklink-9a3ice --version
```

## 30-second quickstart

```bash
# encode a message to a WAV file, then decode it back
echo -n "hello weaklink" | ./weaklink-9a3ice-linux-x86_64-latest tx --modem-wav /tmp/hello.wav
./weaklink-9a3ice-linux-x86_64-latest rx --modem-wav /tmp/hello.wav
# → hello weaklink

# live: play through speakers, record on the mic
./weaklink-9a3ice-linux-x86_64-latest rx > out.txt &      # start listening
echo -n "over the room" | ./weaklink-9a3ice-linux-x86_64-latest tx
# Ctrl-C the rx after the tones stop
```

## Supported presets

Four hard-coded baud presets. Any other `--modem-baud` value raises
`NotImplementedError` — the tone stacks are tuned per preset and there's no
point pretending arbitrary bauds work. Both sides launch with matching
flags (there is no handshake, so config has to agree).

| Baud | 4-FSK tones (Hz) | Total spread | Fits 2.7 kHz SSB? | Default repeats | Approx cliff (SNR in 3 kHz) |
|---:|---|---:|---|---:|---:|
| 9 | 1350 / 1450 / 1550 / 1650 | 300 Hz | ✓ | 8× | ≈ −20 dB |
| 45 | 1200 / 1400 / 1600 / 1800 | 600 Hz | ✓ | 4× | ≈ −14 dB |
| 300 | 1050 / 1350 / 1650 / 1950 | 900 Hz | ✓ | 2× | ≈ −5 dB |
| 1200 | 500 / 1700 / 2900 / 4100 | 3600 Hz | ✗ (wideband only) | 2× | ≈ +2 dB |

**Fast, clean channels** (default, 300 baud, ~1 kbps):
```bash
./weaklink-9a3ice-linux-x86_64-latest tx | ./weaklink-9a3ice-linux-x86_64-latest rx
```

**Moderate noise via SSB** (45 baud, ~20 bit/s):
```bash
./weaklink-9a3ice-linux-x86_64-latest tx --modem-baud 45 < msg.txt
./weaklink-9a3ice-linux-x86_64-latest rx --modem-baud 45 > received.txt
```

**Weak-signal / short messages** (9 baud, ~3 bit/s, cliff around −20 dB):
```bash
./weaklink-9a3ice-linux-x86_64-latest tx --modem-baud 9 < short_msg.txt
```

**Wideband high-throughput** (1200 baud, ~500 bit/s, needs 5+ kHz channel or wired/virt):
```bash
./weaklink-9a3ice-linux-x86_64-latest tx --modem-baud 1200 < msg.txt
```

Override `--modem-block-repeats N` on both sides for more (or fewer) copies:
each doubling buys ~2–3 dB of AWGN margin via soft-LLR combining across the
per-copy permutations. Cost is proportionally longer transmission — see the
benchmark table below.

## Debugging a live-audio setup

Local WAV roundtrip works but mic-and-speaker doesn't decode? Add
`--modem-debug` to the RX side:

```bash
./weaklink-9a3ice-linux-x86_64-latest rx --modem-debug > out.txt
```

Diagnostics go to `log.txt` (not stdout — so piping stays clean). Look for:

- `audio: peak +X dBFS, rms +Y dBFS` — one per second while live rx runs.
  Peak below −40 dBFS means mic is muted, wrong device, or gain too low.
- `RS corrected ... byte-symbol(s)` — outer code saved a block.
- `RS failed on ... block(s)` — a block was unrecoverable (data lost).
- With `--modem-debug`: coarse and per-preamble frequency offsets,
  preamble positions, block-decode counts per group.

Common local-audio gotchas that this catches:

- **RX device isn't the mic you think** — peak level shows −∞ dBFS or
  no preambles found. Change the OS default input.
- **macOS built-in mic runs through AGC / noise suppression / voice
  isolation** which butchers modem tones. Disable in System Settings →
  Sound → Input (turn off "Noise Cancellation" / "Voice Isolation"), or
  select a different input device.
- **Sample-rate mismatch** — 4-FSK tones drift, preamble correlator
  reports peaks at odd positions or misses them. Force the OS input
  device to 48 kHz.
- **Volume too low** — peak level below −40 dBFS. Turn up mic gain or
  TX output volume.

## Signal chain

```
stdin ──▶ chunk into (rs_data − 3)-byte payloads ──▶ frame per block ──▶
     RS(N,K) + CRC-32 ──▶ conv encode (K=7, r=1/2) ──▶ 8×32 interleave
     ──▶ 4-FSK CPFSK ──▶ [pre][slot 0][pre][slot 1]...[pre] ──▶ audio
                                                                    │
                                                                    ▼
stdout ◀── emit in block_index order ◀── strip zero-pad via length header
       ◀── RS + CRC per slot ◀── soft Viterbi ◀── deinterleave
       ◀── preamble correlator (per-slot sync) ◀── non-coherent demod ◀──
```

Every slot is bracketed by a preamble, so any single slot decodes
standalone. The correlator finds each preamble independently; adjacent
mid-stream peaks that look wrong get dropped as spurious. Message
boundaries (between separate tx sessions) are inferred from
non-block-length spans between preambles — one rx pipe can watch many
tx sessions in a row.

### Wire format

```
One tx session (live audio):

  ┌────────┬─────┬───────┬─────┬───────┬─────┬─────┬───────┬─────┬────────┐
  │ pilot  │ pre │slot 0 │ pre │slot 1 │ pre │ ... │slot N-1│ pre │ pilot  │
  └────────┴─────┴───────┴─────┴───────┴─────┴─────┴───────┴─────┴────────┘
    ~0.2 s   32     ~L      32     ~L                                ~0.2 s
     4-FSK  syms  syms    syms   syms                                4-FSK
     tones                                                           tones

  L (block_length) depends on the preset: RS wire bytes → conv → interleave.
  With block_repeats > 1, each slot is emitted back-to-back R times.

One RS block, data area (before conv + interleave + FSK):

  ┌── 1B ──┬── 2B ────┬──── rs_data − 3 B ────┬── 4B CRC ──┬── rs_parity B ──┐
  │ length │block_idx │ payload (zero-padded) │  CRC-32    │  RS parity      │
  └────────┴──────────┴───────────────────────┴────────────┴─────────────────┘
    strips    dedupes    user bytes; last          integrity   corrects up to
    trailing  copies /   block in the stream       check       parity/2 byte
    NUL pad   picks      is short — length            ↑         errors per
              output     header records how                     block
              slot       many bytes are real
```

Cap: `block_idx` is 2 bytes, so a single tx session is bounded at 65 535
slots. That's roughly 1.9 MB at the 45-baud preset (~35 hours of tx
time), so in practice the ceiling is air time, not the header.

## SNR performance

Auto-generated benchmark: `poetry run weaklink-benchmark` re-measures every
config and rewrites the table between the markers below.

<!-- BENCHMARK RESULTS START -->

Streaming modem. Payload: 100 random-ASCII bytes. Sync every 4 data blocks. Reference bandwidth: 3 kHz.

| Baud | Block layout (wire / data / parity, +4 B CRC) | Block repeats | Throughput | Info rate | Our cliff | Shannon | Gap |
|---:|---|---:|---|---:|---:|---:|---:|
| 45 | 28B block / 16B data / 8B parity | 1&times; | 100 chars in 42.0 s | 19.1 bit/s | **-9 dB** | -23.6 dB | 14.6 dB |
| 45 | 28B block / 16B data / 8B parity | 2&times; | 100 chars in 81.8 s | 9.8 bit/s | **-13 dB** | -26.5 dB | 13.5 dB |
| 45 | 28B block / 16B data / 8B parity | 4&times; | 100 chars in 161.5 s | 5.0 bit/s | **-7 dB** | -29.4 dB | 22.4 dB |
| 45 | 28B block / 16B data / 8B parity | 8&times; | 100 chars in 320.8 s | 2.5 bit/s | **-13 dB** | -32.4 dB | 19.4 dB |
| 45 | 44B block / 32B data / 8B parity | 1&times; | 100 chars in 35.6 s | 22.5 bit/s | **-11 dB** | -22.8 dB | 11.8 dB |
| 45 | 44B block / 32B data / 8B parity | 2&times; | 100 chars in 69.7 s | 11.5 bit/s | **-14 dB** | -25.8 dB | 11.8 dB |
| 45 | 44B block / 32B data / 8B parity | 4&times; | 100 chars in 138.0 s | 5.8 bit/s | **-13 dB** | -28.7 dB | 15.7 dB |
| 45 | 44B block / 32B data / 8B parity | 8&times; | 100 chars in 274.6 s | 2.9 bit/s | **-14 dB** | -31.7 dB | 17.7 dB |
| 45 | 164B block / 128B data / 32B parity | 1&times; | 100 chars in 32.7 s | 24.4 bit/s | **-12 dB** | -22.5 dB | 10.5 dB |
| 45 | 164B block / 128B data / 32B parity | 2&times; | 100 chars in 64.0 s | 12.5 bit/s | **-12 dB** | -25.4 dB | 13.4 dB |
| 45 | 164B block / 128B data / 32B parity | 4&times; | 100 chars in 126.6 s | 6.3 bit/s | **-12 dB** | -28.4 dB | 16.4 dB |
| 45 | 164B block / 128B data / 32B parity | 8&times; | 100 chars in 251.8 s | 3.2 bit/s | **-13 dB** | -31.3 dB | 18.3 dB |
| 100 | 28B block / 16B data / 8B parity | 1&times; | 100 chars in 18.9 s | 42.4 bit/s | **-8 dB** | -20.1 dB | 12.1 dB |
| 100 | 28B block / 16B data / 8B parity | 2&times; | 100 chars in 36.8 s | 21.7 bit/s | **-8 dB** | -23.0 dB | 15.0 dB |
| 100 | 28B block / 16B data / 8B parity | 4&times; | 100 chars in 72.6 s | 11.0 bit/s | **-9 dB** | -25.9 dB | 16.9 dB |
| 100 | 28B block / 16B data / 8B parity | 8&times; | 100 chars in 144.3 s | 5.5 bit/s | **-4 dB** | -28.9 dB | 24.9 dB |
| 100 | 44B block / 32B data / 8B parity | 1&times; | 100 chars in 16.0 s | 50.0 bit/s | **-8 dB** | -19.3 dB | 11.3 dB |
| 100 | 44B block / 32B data / 8B parity | 2&times; | 100 chars in 31.4 s | 25.5 bit/s | **-9 dB** | -22.3 dB | 13.3 dB |
| 100 | 44B block / 32B data / 8B parity | 4&times; | 100 chars in 62.1 s | 12.9 bit/s | **-9 dB** | -25.3 dB | 16.3 dB |
| 100 | 44B block / 32B data / 8B parity | 8&times; | 100 chars in 123.5 s | 6.5 bit/s | **-10 dB** | -28.2 dB | 18.2 dB |
| 100 | 164B block / 128B data / 32B parity | 1&times; | 100 chars in 14.7 s | 54.3 bit/s | **-8 dB** | -19.0 dB | 11.0 dB |
| 100 | 164B block / 128B data / 32B parity | 2&times; | 100 chars in 28.8 s | 27.8 bit/s | **-11 dB** | -21.9 dB | 10.9 dB |
| 100 | 164B block / 128B data / 32B parity | 4&times; | 100 chars in 57.0 s | 14.0 bit/s | **-11 dB** | -24.9 dB | 13.9 dB |
| 100 | 164B block / 128B data / 32B parity | 8&times; | 100 chars in 113.3 s | 7.1 bit/s | **-10 dB** | -27.9 dB | 17.9 dB |
| 300 | 28B block / 16B data / 8B parity | 1&times; | 100 chars in 6.3 s | 127.1 bit/s | **-3 dB** | -15.3 dB | 12.3 dB |
| 300 | 28B block / 16B data / 8B parity | 2&times; | 100 chars in 12.3 s | 65.2 bit/s | **-5 dB** | -18.2 dB | 13.2 dB |
| 300 | 28B block / 16B data / 8B parity | 4&times; | 100 chars in 24.2 s | 33.0 bit/s | **-1 dB** | -21.2 dB | 20.2 dB |
| 300 | 28B block / 16B data / 8B parity | 8&times; | 100 chars in 48.1 s | 16.6 bit/s | **-5 dB** | -24.1 dB | 19.1 dB |
| 300 | 44B block / 32B data / 8B parity | 1&times; | 100 chars in 5.3 s | 150.0 bit/s | **-4 dB** | -14.5 dB | 10.5 dB |
| 300 | 44B block / 32B data / 8B parity | 2&times; | 100 chars in 10.5 s | 76.5 bit/s | **-5 dB** | -17.5 dB | 12.5 dB |
| 300 | 44B block / 32B data / 8B parity | 4&times; | 100 chars in 20.7 s | 38.7 bit/s | **-5 dB** | -20.5 dB | 15.5 dB |
| 300 | 44B block / 32B data / 8B parity | 8&times; | 100 chars in 41.2 s | 19.4 bit/s | **-5 dB** | -23.5 dB | 18.5 dB |
| 300 | 164B block / 128B data / 32B parity | 1&times; | 100 chars in 4.9 s | 163.0 bit/s | **-4 dB** | -14.2 dB | 10.2 dB |
| 300 | 164B block / 128B data / 32B parity | 2&times; | 100 chars in 9.6 s | 83.3 bit/s | **-5 dB** | -17.1 dB | 12.1 dB |
| 300 | 164B block / 128B data / 32B parity | 4&times; | 100 chars in 19.0 s | 42.1 bit/s | **-5 dB** | -20.1 dB | 15.1 dB |
| 300 | 164B block / 128B data / 32B parity | 8&times; | 100 chars in 37.8 s | 21.2 bit/s | **-6 dB** | -23.1 dB | 17.1 dB |
| 1200 | 28B block / 16B data / 8B parity | 1&times; | 100 chars in 1.6 s | 508.5 bit/s | **+7 dB** | -9.0 dB | 16.0 dB |
| 1200 | 28B block / 16B data / 8B parity | 2&times; | 100 chars in 3.1 s | 260.9 bit/s | **+8 dB** | -12.1 dB | 20.1 dB |
| 1200 | 28B block / 16B data / 8B parity | 4&times; | 100 chars in 6.1 s | 132.2 bit/s | **+2 dB** | -15.1 dB | 17.1 dB |
| 1200 | 28B block / 16B data / 8B parity | 8&times; | 100 chars in 12.0 s | 66.5 bit/s | **+2 dB** | -18.1 dB | 20.1 dB |
| 1200 | 44B block / 32B data / 8B parity | 1&times; | 100 chars in 1.3 s | 600.0 bit/s | **+3 dB** | -8.3 dB | 11.3 dB |
| 1200 | 44B block / 32B data / 8B parity | 2&times; | 100 chars in 2.6 s | 306.1 bit/s | **+2 dB** | -11.3 dB | 13.3 dB |
| 1200 | 44B block / 32B data / 8B parity | 4&times; | 100 chars in 5.2 s | 154.6 bit/s | **+2 dB** | -14.4 dB | 16.4 dB |
| 1200 | 44B block / 32B data / 8B parity | 8&times; | 100 chars in 10.3 s | 77.7 bit/s | **+2 dB** | -17.4 dB | 19.4 dB |
| 1200 | 164B block / 128B data / 32B parity | 1&times; | 100 chars in 1.2 s | 652.2 bit/s | **+2 dB** | -7.9 dB | 9.9 dB |
| 1200 | 164B block / 128B data / 32B parity | 2&times; | 100 chars in 2.4 s | 333.3 bit/s | **+2 dB** | -11.0 dB | 13.0 dB |
| 1200 | 164B block / 128B data / 32B parity | 4&times; | 100 chars in 4.7 s | 168.5 bit/s | **+1 dB** | -14.0 dB | 15.0 dB |
| 1200 | 164B block / 128B data / 32B parity | 8&times; | 100 chars in 9.4 s | 84.7 bit/s | **+1 dB** | -17.0 dB | 18.0 dB |
| 9 | 28B block / 16B data / 8B parity | 1&times; | 20 chars in 64.0 s<br/><sub>9 baud floor, 20-byte payload, 1x repeat</sub> | 2.5 bit/s | **+2 dB** | -32.4 dB | 34.4 dB |
| 9 | 28B block / 16B data / 8B parity | 2&times; | 20 chars in 120.9 s<br/><sub>9 baud floor, 20-byte payload, 2x repeat</sub> | 1.3 bit/s | **+1 dB** | -35.1 dB | 36.1 dB |
| 9 | 28B block / 16B data / 8B parity | 4&times; | 20 chars in 234.7 s<br/><sub>9 baud floor, 20-byte payload, 4x repeat</sub> | 0.7 bit/s | **+0 dB** | -38.0 dB | 38.0 dB |
| 9 | 28B block / 16B data / 8B parity | 8&times; | 20 chars in 462.2 s<br/><sub>9 baud floor, 20-byte payload, 8x repeat</sub> | 0.3 bit/s | **+1 dB** | -41.0 dB | 42.0 dB |

<!-- BENCHMARK RESULTS END -->

Shannon-limit context: the "Gap" column is how many dB above the theoretical
lower bound each config lands at. We're roughly 10–15 dB above Shannon
everywhere — that's the K=7 Viterbi + non-coherent detection budget. Closing
more of the gap would need LDPC or coherent detection.

## From source (power users / macOS / hacking)

```bash
poetry install
poetry run weaklink-9a3ice --version
```

Replace `./weaklink-9a3ice-linux-x86_64-latest` with `poetry run weaklink-9a3ice`
in any command above. On Debian / Ubuntu also install the system libs first:
`sudo apt install libportaudio2 libsndfile1`.

## CLI reference

Two subcommands, `tx` and `rx`. Byte data goes over stdin/stdout — use
shell redirection for files or pipes. Everything about the modem itself is
prefixed `--modem-*`.

| Flag | Default | Description |
|------|---------|-------------|
| `--modem-baud N` | `300` | Symbol rate. Only `9`, `45`, `300`, `1200` supported — others raise `NotImplementedError`. Every 4× slower ≈ 6 dB more margin. |
| `--modem-sample-rate HZ` | `48000` | Audio sample rate. Match your soundcard. |
| `--modem-rs-data-bytes N` | preset | Reed-Solomon data bytes per block. |
| `--modem-rs-parity-bytes N` | preset | RS parity bytes. Corrects up to N/2 byte errors per block. |
| `--modem-no-rs-crc` | CRC on | Skip the payload CRC-32 inside each RS block. |
| `--modem-sync-every-blocks N` | `4` | Legacy knob (kept for buffer-cap sizing on the rx side). Doesn't affect the wire format — a preamble is emitted between every slot regardless. |
| `--modem-block-repeats N` | preset | Each block sent N times, each copy with a different pseudorandom bit permutation. RX soft-combines LLRs across copies for real diversity gain — ~2–3 dB per doubling in AWGN, more against burst / periodic interference. |
| `--modem-wav PATH` | live audio | WAV file mode instead of the live audio device. |
| `--modem-audio-output NAME` | OS default | tx audio target: sounddevice index, substring of a device name (e.g. `USB`), or a Pulse sink name (e.g. `virt`). |
| `--modem-audio-input NAME` | OS default | rx audio source: same syntax as output; Pulse sources like `virt.monitor` supported via `parec` subprocess. |
| `--modem-debug` | off | Full DEBUG chatter to log file. Default INFO shows just audio levels + RS corrections + failures. |
| `--modem-log-file PATH` | `./log.txt` | Diagnostics land here. stdout/stderr stay clean for byte piping. |

## Test suite

```bash
poetry run pytest -q            # unit + integration, ~1 s
poetry run pytest -m slow -v -s # SNR-sweep benchmarks, ~2 min
```

CI runs the full suite (including the slow SNR sweeps) on every push.

## Roadmap / known limits

- **Non-coherent detection only.** Coherent Costas-loop demod would
  buy another ~3 dB across the board. Big DSP lift.
- **No LDPC.** Would close ~2–4 dB of the Shannon gap. Was drafted
  then removed as experimental; needs a proper girth-optimising
  construction.

## License

MIT. See LICENSE. Contributions welcome; open an issue first if it's a
non-trivial change so we can agree on shape before code lands.

## Acknowledgments

Reed-Solomon via [`reedsolo`](https://github.com/tomerfiliba-org/reedsolomon).
Convolutional code uses the standard NASA/CCSDS (171, 133) generator
polynomials. Audio via [`sounddevice`](https://github.com/spatialaudio/python-sounddevice)
and [`soundfile`](https://github.com/bastibe/python-soundfile).
