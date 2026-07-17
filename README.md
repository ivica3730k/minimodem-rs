# weaklink

A streaming digital modem for ham radio HF SSB (and wider) channels. It takes
whatever bytes you feed it and pushes them out as audio, and turns received
audio back into bytes. Simple as that — nothing about the shape of your
messages, just a byte pipe. **You add your own protocol on top** (or don't).

Distribution: `weaklink-9a3ice` · Repo: `weaklink-9a3ice` · Status: **v0.0.5,
lab-tested in AWGN simulation only. Not yet on-air validated. Use with
appropriate care and follow your local ham regulations.**

## 30-second quickstart

```bash
poetry install

# encode a message to a WAV file, then decode it back
echo -n "hello weaklink" | poetry run weaklink-modem tx --modem-wav /tmp/hello.wav
poetry run weaklink-modem rx --modem-wav /tmp/hello.wav
# → hello weaklink

# live: play into the room and record on a mic
poetry run weaklink-modem rx > out.txt &     # start listening
echo -n "over the air" | poetry run weaklink-modem tx
# Ctrl-C the rx after the transmission stops
```

## Is this for me?

Compared to what most hams have used:

| Mode | What it sends | Sensitivity |
|---|---|---:|
| **RTTY** | ASCII, no FEC | needs +5 to +10 dB SNR |
| **PSK31** | ASCII, weak FEC | ~ 0 dB SNR |
| **JS8Call** | Text with structure | ~ −16 dB |
| **FT8 / WSPR** | Fixed-form messages (callsign + grid + report) | −21 / −28 dB |
| **weaklink** | **Arbitrary bytes**, streaming | **−3 to −20 dB** depending on baud + FEC config |

**When it fits**: you want to move arbitrary data (JSON, structs, whatever)
over an SSB link at ranges where casual modes fall over. Sensor telemetry
returns, custom message protocols, file transfers over noisy paths.

**When it doesn't fit**: you want to chat with random hams — nobody else has
this modem yet. You want FT8-tier sensitivity for standard messages — use
FT8. You want a well-established mode with logging integration — use JS8Call
or VARA.

## Recommended presets

Pick one and go. Both sides launch with matching flags — there's no
handshake, so config must agree.

**Quick chat / regional VHF or clean HF** (default, 300 baud):
```bash
weaklink-modem tx | weaklink-modem rx      # ~1 kbps of throughput, cliff ≈ −3 dB
```

**Weak-signal HF, ~100-byte messages** (100 baud + block repetition):
```bash
FLAGS="--modem-baud 100 --modem-block-repeats 2"
weaklink-modem tx $FLAGS < msg.txt
weaklink-modem rx $FLAGS > received.txt
# ~30 s per 100 chars, cliff ≈ −10 dB
```

**Absolute weak-signal / WSPR-adjacent** (9 baud, short messages only):
```bash
FLAGS="--modem-baud 9 --modem-tone-spacing 30 --modem-block-repeats 2"
weaklink-modem tx $FLAGS < short_msg.txt
# ~2 minutes for 20 chars, cliff ≈ −20 dB in 3 kHz
```

## On-air setup

You'll need:

1. **HF/VHF SSB rig** — any modern radio with SSB and an audio interface
2. **Rig audio interface** — SignaLink, Digirig, dedicated soundcard cable,
   or the rig's built-in USB codec if it has one
3. **PTT keying** — VOX works if enabled; CAT/serial keying is more reliable
4. **Antenna** for the band
5. Something to run `weaklink-modem tx` and `rx`

The modem auto-handles ±500 Hz of dial drift (always-on FFT-based coarse
offset search) and tracks slow LO drift or residual satellite Doppler at
every sync marker. You don't need to be on-frequency to within 1 Hz — just
close enough that the tones land in the SSB passband.

**Digital sub-bands** (region-dependent — check your band plan):
- 40 m: ~7.070 MHz
- 20 m: ~14.070 MHz
- 2 m: 144.140 – 144.170 MHz USB (weak-signal segment)

**Bandwidth**: the 4-FSK stack occupies roughly `5 × baud` Hz. At 300 baud
that's ~1.5 kHz, comfortably inside a 2.4 kHz SSB filter. Any baud from 45
up to ~500 fits in narrow SSB; go wider for higher rates.

## Signal chain

```
stdin ──▶ RS(N,K)+CRC blocks ──▶ conv encode (K=7, r=1/2, per-block) ──▶
         interleave ──▶ 4-FSK CPFSK ──▶ [preamble][data]×sync_every[preamble]... ──▶ audio
                                                                                       │
                                                                                       ▼
stdout ◀── strip NUL pad ◀── RS decode per block ◀── soft Viterbi ◀── deinterleave ◀──
       ◀── preamble correlator (finds every sync boundary) ◀── non-coherent demod ◀──
```

At RX: sliding preamble correlator finds every sync marker, per-preamble
fine offset tracking, then Viterbi + RS on each data block. Undecodable
blocks silently dropped, missing bytes replaced with nothing on the output.

## SNR performance

Auto-generated benchmark: `poetry run weaklink-benchmark` re-measures every
config and rewrites the table between the markers below.

<!-- BENCHMARK RESULTS START -->

Streaming modem. Payload: 100 random-ASCII bytes. Sync every 4 data blocks. Reference bandwidth: 3 kHz.

| Baud | RS | Block repeats | Throughput | Info rate | Our cliff | Shannon | Gap |
|---:|---|---:|---|---:|---:|---:|---:|
| 45 | RS(28,16) | 1&times; | 100 chars in 42.0 s | 19.1 bit/s | **-12 dB** | -23.6 dB | 11.6 dB |
| 45 | RS(28,16) | 2&times; | 100 chars in 81.8 s | 9.8 bit/s | **-12 dB** | -26.5 dB | 14.5 dB |
| 45 | RS(28,16) | 4&times; | 100 chars in 161.5 s | 5.0 bit/s | **-12 dB** | -29.4 dB | 17.4 dB |
| 45 | RS(44,32) | 1&times; | 100 chars in 35.6 s | 22.5 bit/s | **-12 dB** | -22.8 dB | 10.8 dB |
| 45 | RS(44,32) | 2&times; | 100 chars in 69.7 s | 11.5 bit/s | **-14 dB** | -25.8 dB | 11.8 dB |
| 45 | RS(44,32) | 4&times; | 100 chars in 138.0 s | 5.8 bit/s | **-12 dB** | -28.7 dB | 16.7 dB |
| 45 | RS(164,128) | 1&times; | 100 chars in 32.7 s | 24.4 bit/s | **-12 dB** | -22.5 dB | 10.5 dB |
| 45 | RS(164,128) | 2&times; | 100 chars in 64.0 s | 12.5 bit/s | **-11 dB** | -25.4 dB | 14.4 dB |
| 45 | RS(164,128) | 4&times; | 100 chars in 126.6 s | 6.3 bit/s | **-12 dB** | -28.4 dB | 16.4 dB |
| 100 | RS(28,16) | 1&times; | 100 chars in 18.9 s | 42.4 bit/s | **-9 dB** | -20.1 dB | 11.1 dB |
| 100 | RS(28,16) | 2&times; | 100 chars in 36.8 s | 21.7 bit/s | **-8 dB** | -23.0 dB | 15.0 dB |
| 100 | RS(28,16) | 4&times; | 100 chars in 72.6 s | 11.0 bit/s | **-8 dB** | -25.9 dB | 17.9 dB |
| 100 | RS(44,32) | 1&times; | 100 chars in 16.0 s | 50.0 bit/s | **-6 dB** | -19.3 dB | 13.3 dB |
| 100 | RS(44,32) | 2&times; | 100 chars in 31.4 s | 25.5 bit/s | **-9 dB** | -22.3 dB | 13.3 dB |
| 100 | RS(44,32) | 4&times; | 100 chars in 62.1 s | 12.9 bit/s | **-9 dB** | -25.3 dB | 16.3 dB |
| 100 | RS(164,128) | 1&times; | 100 chars in 14.7 s | 54.3 bit/s | **-9 dB** | -19.0 dB | 10.0 dB |
| 100 | RS(164,128) | 2&times; | 100 chars in 28.8 s | 27.8 bit/s | **-9 dB** | -21.9 dB | 12.9 dB |
| 100 | RS(164,128) | 4&times; | 100 chars in 57.0 s | 14.0 bit/s | **-8 dB** | -24.9 dB | 16.9 dB |
| 300 | RS(28,16) | 1&times; | 100 chars in 6.3 s | 127.1 bit/s | **-3 dB** | -15.3 dB | 12.3 dB |
| 300 | RS(28,16) | 2&times; | 100 chars in 12.3 s | 65.2 bit/s | **-2 dB** | -18.2 dB | 16.2 dB |
| 300 | RS(28,16) | 4&times; | 100 chars in 24.2 s | 33.0 bit/s | **-3 dB** | -21.2 dB | 18.2 dB |
| 300 | RS(44,32) | 1&times; | 100 chars in 5.3 s | 150.0 bit/s | **-2 dB** | -14.5 dB | 12.5 dB |
| 300 | RS(44,32) | 2&times; | 100 chars in 10.5 s | 76.5 bit/s | **-4 dB** | -17.5 dB | 13.5 dB |
| 300 | RS(44,32) | 4&times; | 100 chars in 20.7 s | 38.7 bit/s | **-4 dB** | -20.5 dB | 16.5 dB |
| 300 | RS(164,128) | 1&times; | 100 chars in 4.9 s | 163.0 bit/s | **-4 dB** | -14.2 dB | 10.2 dB |
| 300 | RS(164,128) | 2&times; | 100 chars in 9.6 s | 83.3 bit/s | **-5 dB** | -17.1 dB | 12.1 dB |
| 300 | RS(164,128) | 4&times; | 100 chars in 19.0 s | 42.1 bit/s | **-5 dB** | -20.1 dB | 15.1 dB |
| 1200 | RS(28,16) | 1&times; | 100 chars in 1.6 s | 508.5 bit/s | **+7 dB** | -9.0 dB | 16.0 dB |
| 1200 | RS(28,16) | 2&times; | 100 chars in 3.1 s | 260.9 bit/s | **+5 dB** | -12.1 dB | 17.1 dB |
| 1200 | RS(28,16) | 4&times; | 100 chars in 6.1 s | 132.2 bit/s | **+3 dB** | -15.1 dB | 18.1 dB |
| 1200 | RS(44,32) | 1&times; | 100 chars in 1.3 s | 600.0 bit/s | **+3 dB** | -8.3 dB | 11.3 dB |
| 1200 | RS(44,32) | 2&times; | 100 chars in 2.6 s | 306.1 bit/s | **+3 dB** | -11.3 dB | 14.3 dB |
| 1200 | RS(44,32) | 4&times; | 100 chars in 5.2 s | 154.6 bit/s | **+3 dB** | -14.4 dB | 17.4 dB |
| 1200 | RS(164,128) | 1&times; | 100 chars in 1.2 s | 652.2 bit/s | **+3 dB** | -7.9 dB | 10.9 dB |
| 1200 | RS(164,128) | 2&times; | 100 chars in 2.4 s | 333.3 bit/s | **+5 dB** | -11.0 dB | 16.0 dB |
| 1200 | RS(164,128) | 4&times; | 100 chars in 4.7 s | 168.5 bit/s | **+3 dB** | -14.0 dB | 17.0 dB |
| 9 | RS(28,16) | 1&times; | 20 chars in 64.0 s<br/><sub>9 baud floor, 20-byte payload, 1x repeat</sub> | 2.5 bit/s | **-19 dB** | -32.4 dB | 13.4 dB |
| 9 | RS(28,16) | 2&times; | 20 chars in 120.9 s<br/><sub>9 baud floor, 20-byte payload, 2x repeat</sub> | 1.3 bit/s | **-20 dB** | -35.1 dB | 15.1 dB |
| 9 | RS(28,16) | 4&times; | 20 chars in 234.7 s<br/><sub>9 baud floor, 20-byte payload, 4x repeat</sub> | 0.7 bit/s | **-19 dB** | -38.0 dB | 19.0 dB |

<!-- BENCHMARK RESULTS END -->

Shannon-limit context: the "Gap" column is how many dB over the theoretical
lower bound each config lands at. We're roughly 10–15 dB above Shannon
everywhere — that's the K=7 Viterbi + non-coherent detection budget. Closing
more of the gap needs LDPC or coherent detection (planned).

## Install

```bash
poetry install
```

Adds `numpy`, `soundfile` (WAV I/O), and `sounddevice` (live audio via
PortAudio → PulseAudio/CoreAudio) on top of the existing deps.

On Debian/Ubuntu:

```bash
sudo apt install libportaudio2 libsndfile1
```

## CLI reference

Two subcommands, `tx` and `rx`. Byte data goes over stdin/stdout — use
shell redirection for files or pipes. Everything about the modem itself is
prefixed `--modem-*`.

### Common configuration

| Flag | Default | Description |
|------|---------|-------------|
| `--modem-baud N` | `300` | Symbol rate. Every 10× slower ≈ 10 dB more SNR margin. |
| `--modem-tone-spacing HZ` | `--modem-baud` | 4-FSK tone spacing. Match baud for orthogonality; widen if you have wide-SSB or FM bandwidth to spare. |
| `--modem-sample-rate HZ` | `48000` | Audio sample rate. Match your soundcard. |
| `--modem-rs-data-bytes N` | `16` | Reed-Solomon data bytes per block. |
| `--modem-rs-parity-bytes N` | `8` | RS parity bytes. Corrects up to N/2 byte errors per block. |
| `--modem-no-rs-crc` | CRC on | Skip the payload CRC-32 inside each RS block. |
| `--modem-sync-every-blocks N` | `4` | Preamble inserted every N data blocks. Smaller = better resync at low SNR, higher overhead. |
| `--modem-block-repeats N` | `1` | Each block sent N times, round-robin. RX sums soft LLRs — ~2 dB per doubling in AWGN + fade diversity. |
| `--modem-wav PATH` | live audio | WAV file mode. Omit on RX = block recording until Ctrl-C, then decode. |

## Test suite

```bash
poetry run pytest -q            # unit + integration, ~1 s
poetry run pytest -m slow -v -s # SNR-sweep benchmarks, ~2 min
```

CI runs the full suite (including the slow SNR sweeps) on every push.

## Roadmap / known limits

- **Not on-air tested yet** — bench numbers only.
- **No LDPC**. Would close ~2–4 dB of the Shannon gap. Was drafted then
  removed as experimental; needs a proper girth-optimising construction.
- **Not FT8/JS8/VARA compatible** — this is a custom wire format.
- **No rig control** built in. Bring your own `hamlib` / `rigctld` setup.
- **Non-coherent detection only**. Coherent Costas-loop demod would buy
  another ~3 dB, big DSP lift.

## License

MIT. See LICENSE. Contributions welcome; open an issue first if it's a
non-trivial change so we can agree on shape before code lands.

## Acknowledgments

Reed-Solomon via [`reedsolo`](https://github.com/tomerfiliba-org/reedsolomon).
Convolutional code uses the standard NASA/CCSDS (171, 133) generator
polynomials. Audio via [`sounddevice`](https://github.com/spatialaudio/python-sounddevice)
and [`soundfile`](https://github.com/bastibe/python-soundfile).
