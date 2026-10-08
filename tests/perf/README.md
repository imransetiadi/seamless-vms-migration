# Performance tests

## blocksync benchmark

`bench_blocksync.py` measures the chunk delta-sync engine of the warm
migration path (`plugins/module_utils/blocksync.py`) through its real CLI:
`blocksync.py receive … -- blocksync.py send …` on two local files.

For every chunk size (1, 4, 16 MiB) and worker count (1, 4) it:

1. restores the same source (random data with a 64 MiB hole every 256 MiB,
   so zero frames are exercised) and creates a fresh sparse destination;
2. runs a **full** pass with `--assume-zero` (pass 1 of a warm migration into
   a new volume);
3. overwrites 1 %, 5 % and 20 % of the source with random 1 MiB extents and runs
   a **delta** pass after each (pre-copy and final passes);
4. checks that the destination is byte-identical to the source.

It prints a Markdown table and writes it to `tests/perf/results-<host>.md`.
`<host>` is a hardware/OS label (for example `darwin-arm64-apple-m5`), not the
network host name; use `--host-label` or `--output` to choose another name.

```bash
python3 tests/perf/bench_blocksync.py --size-gib 1
python3 tests/perf/bench_blocksync.py --size-gib 8 --chunk-mib 4 --workers 4 --tmpdir /var/tmp
python3 tests/perf/bench_blocksync.py --help
```

The script only needs Python 3.6+ and about twice `--size-gib` of free space
plus one more copy of the source in `--tmpdir` (default: the system temporary
directory). The files are removed at the end.

### Reading the results

| Column | Meaning |
|---|---|
| Scan MiB/s | source bytes compared per second of wall time (both sides hash in parallel) |
| Chunks changed | chunks sent as data or zero frames / chunks in the device |
| Transferred | payload bytes of data frames (zero frames carry none) |
| Wall s | wall time of the receive, including starting both processes |

* Sender and receiver run on one host and talk through pipes, and the files
  are in the page cache: the numbers are the **CPU ceiling** of reading,
  hashing (BLAKE2b-128) and applying frames. On conversion hosts the volume
  read rate and, for the transferred bytes, the network link usually dominate.
* **Chunk size** trades transfer volume against per-chunk overhead: with changes
  scattered in 1 MiB extents, 1 % of a 1 GiB device costs about 10 MiB with
  1 MiB chunks, 40 MiB with 4 MiB chunks and 160 MiB with 16 MiB chunks, because
  any change resends its whole chunk. 4 MiB is the default
  (`os_migrate_warm_chunk_size`).
* **Workers** parallelise the read and hash of each side (`hashlib` releases the
  GIL); delta passes scan two to three times faster with 4 workers than with 1.
* Results of a single run vary with whatever else the host is doing; the
  results file records the load average before and after.

Committed results: [results-darwin-arm64-apple-m5.md](results-darwin-arm64-apple-m5.md).
