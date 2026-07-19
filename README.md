# weaklink

Streaming digital modem: bytes on stdin → audio → bytes on stdout. Works
with `tail -f`; no memory buffering, no wait-for-EOF.

![alt text](image.png)

Distribution: `weaklink-9a3ice`.

---

## Install

Portable Linux binary:

```bash
sudo apt install libportaudio2 libsndfile1
curl -L -O https://github.com/ivica3730k/weaklink-9a3ice/releases/latest/download/weaklink-9a3ice-linux-x86_64-latest
chmod +x weaklink-9a3ice-linux-x86_64-latest
```

Debian / Ubuntu `.deb`:

```bash
curl -L -O https://github.com/ivica3730k/weaklink-9a3ice/releases/latest/download/weaklink-9a3ice_amd64-latest.deb
sudo dpkg -i weaklink-9a3ice_amd64-latest.deb
```

From source:

```bash
poetry install
poetry run weaklink-9a3ice --version
```

---

## Quickstart

```bash
# WAV roundtrip
echo -n "hello weaklink" | weaklink-9a3ice tx --modem-wav /tmp/hello.wav
weaklink-9a3ice rx --modem-wav /tmp/hello.wav

# Live speaker → mic
weaklink-9a3ice rx > out.txt &
echo -n "over the room" | weaklink-9a3ice tx

# Long-lived stream
tail -f /var/log/syslog | weaklink-9a3ice tx --modem-baud 300
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

`weaklink-9a3ice rx --modem-debug > out.txt` writes diagnostics to
`log.txt` (stdout stays clean for piping). Watch for:

- `audio: peak +X dBFS` below −40 dBFS → wrong mic or gain too low.
- `RS corrected` — outer code saved a block.
- `N slot(s) failed CRC/RS` — unrecoverable, data lost.
- macOS mic AGC / voice-isolation destroys tones; disable in System Settings.

---

## SNR benchmarks

Auto-generated. Re-run `poetry run weaklink-benchmark` to refresh the
tables between the markers.

<!-- BENCHMARK RESULTS START -->

Streaming modem. Payload: 100 random-ASCII bytes. Sync every 4 data blocks. Reference bandwidth: 3 kHz.

| Baud | CLI (both tx / rx) | Throughput | Info rate | Measured best SNR |
|---:|---|---|---:|---:|
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 1` | 100 chars in 51.9 s | 15.4 bit/s | **-12 dB** |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 2` | 100 chars in 103.1 s | 7.8 bit/s | **-13 dB** |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 4` | 100 chars in 205.5 s | 3.9 bit/s | **-13 dB** |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 8` | 100 chars in 410.3 s | 1.9 bit/s | **-12 dB** |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 1` | 100 chars in 37.7 s | 21.2 bit/s | **-12 dB** |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 2` | 100 chars in 74.7 s | 10.7 bit/s | **-13 dB** |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 4` | 100 chars in 148.6 s | 5.4 bit/s | **-13 dB** |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 8` | 100 chars in 296.5 s | 2.7 bit/s | **-13 dB** |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 1` | 100 chars in 32.7 s | 24.5 bit/s | **-13 dB** |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 2` | 100 chars in 64.7 s | 12.4 bit/s | **-14 dB** |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 4` | 100 chars in 128.7 s | 6.2 bit/s | **-14 dB** |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 8` | 100 chars in 256.7 s | 3.1 bit/s | **-14 dB** |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 1` | 100 chars in 7.8 s | 102.7 bit/s | **-4 dB** |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 2` | 100 chars in 15.5 s | 51.7 bit/s | **-4 dB** |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 4` | 100 chars in 30.8 s | 26.0 bit/s | **-4 dB** |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 8` | 100 chars in 61.5 s | 13.0 bit/s | **-5 dB** |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 1` | 100 chars in 5.7 s | 141.5 bit/s | **-4 dB** |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 2` | 100 chars in 11.2 s | 71.4 bit/s | **-4 dB** |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 4` | 100 chars in 22.3 s | 35.9 bit/s | **-4 dB** |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 8` | 100 chars in 44.5 s | 18.0 bit/s | **-5 dB** |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 1` | 100 chars in 4.9 s | 163.0 bit/s | **-4 dB** |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 2` | 100 chars in 9.7 s | 82.4 bit/s | **-6 dB** |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 4` | 100 chars in 19.3 s | 41.4 bit/s | **-6 dB** |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 8` | 100 chars in 38.5 s | 20.8 bit/s | **-5 dB** |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 1` | 100 chars in 1.9 s | 411.0 bit/s | **+2 dB** |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 2` | 100 chars in 3.9 s | 206.9 bit/s | **+2 dB** |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 4` | 100 chars in 7.7 s | 103.8 bit/s | **+1 dB** |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 8` | 100 chars in 15.4 s | 52.0 bit/s | **+2 dB** |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 1` | 100 chars in 1.4 s | 566.0 bit/s | **+2 dB** |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 2` | 100 chars in 2.8 s | 285.7 bit/s | **+1 dB** |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 4` | 100 chars in 5.6 s | 143.5 bit/s | **+2 dB** |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 8` | 100 chars in 11.1 s | 71.9 bit/s | **+1 dB** |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 1` | 100 chars in 1.2 s | 652.2 bit/s | **+2 dB** |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 2` | 100 chars in 2.4 s | 329.7 bit/s | **+0 dB** |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 4` | 100 chars in 4.8 s | 165.7 bit/s | **+1 dB** |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 8` | 100 chars in 9.6 s | 83.1 bit/s | **+0 dB** |

### Shannon limit vs measured best SNR

How far above the theoretical lower bound each config sits.

| Baud | CLI (both tx / rx) | Shannon | Measured best SNR | Gap |
|---:|---|---:|---:|---:|
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 1` | -24.5 dB | **-12 dB** | 12.5 dB |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 2` | -27.5 dB | **-13 dB** | 14.5 dB |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 4` | -30.5 dB | **-13 dB** | 17.5 dB |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 8` | -33.5 dB | **-12 dB** | 21.5 dB |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 1` | -23.1 dB | **-12 dB** | 11.1 dB |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 2` | -26.1 dB | **-13 dB** | 13.1 dB |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 4` | -29.1 dB | **-13 dB** | 16.1 dB |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 8` | -32.1 dB | **-13 dB** | 19.1 dB |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 1` | -22.5 dB | **-13 dB** | 9.5 dB |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 2` | -25.4 dB | **-14 dB** | 11.4 dB |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 4` | -28.4 dB | **-14 dB** | 14.4 dB |
| 45 | `--modem-baud 45`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 8` | -31.4 dB | **-14 dB** | 17.4 dB |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 1` | -16.2 dB | **-4 dB** | 12.2 dB |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 2` | -19.2 dB | **-4 dB** | 15.2 dB |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 4` | -22.2 dB | **-4 dB** | 18.2 dB |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 8` | -25.2 dB | **-5 dB** | 20.2 dB |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 1` | -14.8 dB | **-4 dB** | 10.8 dB |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 2` | -17.8 dB | **-4 dB** | 13.8 dB |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 4` | -20.8 dB | **-4 dB** | 16.8 dB |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 8` | -23.8 dB | **-5 dB** | 18.8 dB |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 1` | -14.2 dB | **-4 dB** | 10.2 dB |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 2` | -17.2 dB | **-6 dB** | 11.2 dB |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 4` | -20.2 dB | **-6 dB** | 14.2 dB |
| 300 | `--modem-baud 300`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 8` | -23.2 dB | **-5 dB** | 18.2 dB |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 1` | -10.0 dB | **+2 dB** | 12.0 dB |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 2` | -13.1 dB | **+2 dB** | 15.1 dB |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 4` | -16.1 dB | **+1 dB** | 17.1 dB |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 16`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 8` | -19.2 dB | **+2 dB** | 21.2 dB |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 1` | -8.5 dB | **+2 dB** | 10.5 dB |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 2` | -11.7 dB | **+1 dB** | 12.7 dB |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 4` | -14.7 dB | **+2 dB** | 16.7 dB |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 32`<br/>`--modem-rs-parity-bytes 8`<br/>`--modem-block-repeats 8` | -17.8 dB | **+1 dB** | 18.8 dB |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 1` | -7.9 dB | **+2 dB** | 9.9 dB |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 2` | -11.0 dB | **+0 dB** | 11.0 dB |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 4` | -14.1 dB | **+1 dB** | 15.1 dB |
| 1200 | `--modem-baud 1200`<br/>`--modem-rs-data-bytes 128`<br/>`--modem-rs-parity-bytes 32`<br/>`--modem-block-repeats 8` | -17.1 dB | **+0 dB** | 17.1 dB |

<!-- BENCHMARK RESULTS END -->

---

## CLI reference

| Flag | Default | Description |
|------|---------|-------------|
| `--modem-baud N` | `300` | Symbol rate. Only `45`, `300`, `1200` supported. |
| `--modem-num-tones M` | `4` | M-FSK order: 2 / 4 / 8 / 16 / 32. Higher packs more bits per symbol at wider bandwidth and worse cliff. 2 halves throughput but fits narrow audio paths (e.g. FM voice via SignaLink). TX and RX must match. |
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

- **4-FSK / CPFSK** — Four continuous-phase tones, 2 bits per tone.
- **Preamble** — Fixed 32-symbol PN sequence bracketing every slot; RX locks timing / frequency / amplitude from it.
- **Slot** — Preamble + one RS-encoded block.
- **Block** — RS-encoded chunk carrying header + payload.
- **RS(N,K)** — Reed-Solomon outer code. K data + parity → N wire bytes; corrects (N-K)/2 byte errors.
- **CRC-32** — Catches errors past RS correction.
- **Convolutional code (K=7, r=1/2) + soft Viterbi** — Inner FEC and its decoder, driven by per-bit LLRs.
- **Interleaver** — Bit shuffle so bursts become isolated errors. Ours changes every block (32-permutation cycle).
- **Non-coherent demod** — Tone detection by energy; ~3 dB behind coherent.
- **LO offset** — Radio frequency error; we correct up to ±500 Hz.
- **Pilot** — Short random 4-FSK burst before / after every live TX.

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
