# blocksync benchmark — darwin-arm64-apple-m5

* Date: 2026-10-08 06:23 UTC
* Platform: Darwin 27.0.0 (arm64), 10 CPUs, Python 3.13.5
* Device: 1.00 GiB regular file, random data with a 64 MiB hole every 256 MiB; changes are random 1 MiB extents
* Sender and receiver on the same host through pipes; files in the page cache (CPU ceiling of read + BLAKE2b + apply, not disk or network throughput)
* Load average (1/5/15 min) before: 5.4 / 8.0 / 10.1, after: 7.4 / 8.3 / 10.1 (other work on the host makes single runs vary)
* Command: `python3 tests/perf/bench_blocksync.py --size-gib 1`

| Chunk | Workers | Pass | Changed | Scan MiB/s | Chunks changed | Transferred | Wall s |
|---:|---:|---|---:|---:|---:|---:|---:|
| 1 MiB | 1 | full (--assume-zero) | — | 228 | 768 / 1024 | 768.0 MiB | 4.49 |
| 1 MiB | 1 | delta | 1 % (10.0 MiB) | 622 | 10 / 1024 | 10.0 MiB | 1.65 |
| 1 MiB | 1 | delta | 5 % (51.0 MiB) | 438 | 51 / 1024 | 51.0 MiB | 2.34 |
| 1 MiB | 1 | delta | 20 % (204.0 MiB) | 630 | 204 / 1024 | 204.0 MiB | 1.63 |
| 1 MiB | 4 | full (--assume-zero) | — | 511 | 768 / 1024 | 768.0 MiB | 2.00 |
| 1 MiB | 4 | delta | 1 % (10.0 MiB) | 1653 | 10 / 1024 | 10.0 MiB | 0.62 |
| 1 MiB | 4 | delta | 5 % (51.0 MiB) | 1555 | 51 / 1024 | 51.0 MiB | 0.66 |
| 1 MiB | 4 | delta | 20 % (204.0 MiB) | 1214 | 204 / 1024 | 204.0 MiB | 0.84 |
| 4 MiB | 1 | full (--assume-zero) | — | 294 | 192 / 256 | 768.0 MiB | 3.49 |
| 4 MiB | 1 | delta | 1 % (10.0 MiB) | 400 | 10 / 256 | 40.0 MiB | 2.56 |
| 4 MiB | 1 | delta | 5 % (51.0 MiB) | 405 | 47 / 256 | 188.0 MiB | 2.53 |
| 4 MiB | 1 | delta | 20 % (204.0 MiB) | 425 | 152 / 256 | 608.0 MiB | 2.41 |
| 4 MiB | 4 | full (--assume-zero) | — | 515 | 192 / 256 | 768.0 MiB | 1.99 |
| 4 MiB | 4 | delta | 1 % (10.0 MiB) | 1118 | 10 / 256 | 40.0 MiB | 0.92 |
| 4 MiB | 4 | delta | 5 % (51.0 MiB) | 728 | 49 / 256 | 196.0 MiB | 1.41 |
| 4 MiB | 4 | delta | 20 % (204.0 MiB) | 557 | 143 / 256 | 572.0 MiB | 1.84 |
| 16 MiB | 1 | full (--assume-zero) | — | 441 | 48 / 64 | 768.0 MiB | 2.32 |
| 16 MiB | 1 | delta | 1 % (10.0 MiB) | 517 | 9 / 64 | 144.0 MiB | 1.98 |
| 16 MiB | 1 | delta | 5 % (51.0 MiB) | 343 | 34 / 64 | 544.0 MiB | 2.98 |
| 16 MiB | 1 | delta | 20 % (204.0 MiB) | 345 | 63 / 64 | 1008.0 MiB | 2.97 |
| 16 MiB | 4 | full (--assume-zero) | — | 494 | 48 / 64 | 768.0 MiB | 2.07 |
| 16 MiB | 4 | delta | 1 % (10.0 MiB) | 472 | 10 / 64 | 160.0 MiB | 2.17 |
| 16 MiB | 4 | delta | 5 % (51.0 MiB) | 472 | 40 / 64 | 640.0 MiB | 2.17 |
| 16 MiB | 4 | delta | 20 % (204.0 MiB) | 407 | 62 / 64 | 992.0 MiB | 2.52 |
