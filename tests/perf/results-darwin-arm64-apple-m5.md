# blocksync benchmark — darwin-arm64-apple-m5

* Date: 2026-10-08 15:48 UTC
* Platform: Darwin 27.0.0 (arm64), 10 CPUs, Python 3.13.5
* Device: 1.00 GiB regular file, random data with a 64 MiB hole every 256 MiB; changes are random 1 MiB extents
* Sender and receiver on the same host through pipes; files in the page cache (CPU ceiling of read + BLAKE2b + apply, not disk or network throughput)
* Load average (1/5/15 min) before: 3.3 / 2.7 / 2.6, after: 2.8 / 2.7 / 2.6 (other work on the host makes single runs vary)
* Command: `python3 tests/perf/bench_blocksync.py --size-gib 1`

| Chunk | Workers | Pass | Changed | Scan MiB/s | Chunks changed | Transferred | Wall s |
|---:|---:|---|---:|---:|---:|---:|---:|
| 1 MiB | 1 | full (--assume-zero) | — | 1047 | 768 / 1024 | 768.0 MiB | 0.98 |
| 1 MiB | 1 | delta | 1 % (10.0 MiB) | 1388 | 10 / 1024 | 10.0 MiB | 0.74 |
| 1 MiB | 1 | delta | 5 % (51.0 MiB) | 1422 | 51 / 1024 | 51.0 MiB | 0.72 |
| 1 MiB | 1 | delta | 20 % (204.0 MiB) | 1275 | 204 / 1024 | 204.0 MiB | 0.80 |
| 1 MiB | 4 | full (--assume-zero) | — | 1030 | 768 / 1024 | 768.0 MiB | 0.99 |
| 1 MiB | 4 | delta | 1 % (10.0 MiB) | 2749 | 10 / 1024 | 10.0 MiB | 0.37 |
| 1 MiB | 4 | delta | 5 % (51.0 MiB) | 2676 | 51 / 1024 | 51.0 MiB | 0.38 |
| 1 MiB | 4 | delta | 20 % (204.0 MiB) | 2064 | 204 / 1024 | 204.0 MiB | 0.50 |
| 4 MiB | 1 | full (--assume-zero) | — | 956 | 192 / 256 | 768.0 MiB | 1.07 |
| 4 MiB | 1 | delta | 1 % (10.0 MiB) | 1381 | 10 / 256 | 40.0 MiB | 0.74 |
| 4 MiB | 1 | delta | 5 % (51.0 MiB) | 1239 | 47 / 256 | 188.0 MiB | 0.83 |
| 4 MiB | 1 | delta | 20 % (204.0 MiB) | 636 | 152 / 256 | 608.0 MiB | 1.61 |
| 4 MiB | 4 | full (--assume-zero) | — | 1142 | 192 / 256 | 768.0 MiB | 0.90 |
| 4 MiB | 4 | delta | 1 % (10.0 MiB) | 2878 | 10 / 256 | 40.0 MiB | 0.36 |
| 4 MiB | 4 | delta | 5 % (51.0 MiB) | 2127 | 49 / 256 | 196.0 MiB | 0.48 |
| 4 MiB | 4 | delta | 20 % (204.0 MiB) | 1244 | 143 / 256 | 572.0 MiB | 0.82 |
| 16 MiB | 1 | full (--assume-zero) | — | 1103 | 48 / 64 | 768.0 MiB | 0.93 |
| 16 MiB | 1 | delta | 1 % (10.0 MiB) | 1282 | 9 / 64 | 144.0 MiB | 0.80 |
| 16 MiB | 1 | delta | 5 % (51.0 MiB) | 1065 | 34 / 64 | 544.0 MiB | 0.96 |
| 16 MiB | 1 | delta | 20 % (204.0 MiB) | 593 | 63 / 64 | 1008.0 MiB | 1.73 |
| 16 MiB | 4 | full (--assume-zero) | — | 1178 | 48 / 64 | 768.0 MiB | 0.87 |
| 16 MiB | 4 | delta | 1 % (10.0 MiB) | 2625 | 10 / 64 | 160.0 MiB | 0.39 |
| 16 MiB | 4 | delta | 5 % (51.0 MiB) | 1238 | 40 / 64 | 640.0 MiB | 0.83 |
| 16 MiB | 4 | delta | 20 % (204.0 MiB) | 935 | 62 / 64 | 992.0 MiB | 1.09 |
