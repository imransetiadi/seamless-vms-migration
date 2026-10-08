"""Benchmark of the blocksync delta-sync engine.

Runs the real CLI (``blocksync.py receive -- blocksync.py send``) on two
local files: a full copy into a fresh sparse destination (``--assume-zero``)
followed by delta passes after 1 %, 5 % and 20 % of the source changed, for
every combination of chunk size and worker count. Prints a Markdown table
and writes it to ``tests/perf/results-<host>.md``.

Sender and receiver talk through local pipes and the files stay in the page
cache, so the numbers are the CPU ceiling of scanning (read + BLAKE2b) and
applying frames, not disk or network throughput.

    python3 tests/perf/bench_blocksync.py --size-gib 1
"""

from __future__ import absolute_import, division, print_function

import argparse
import datetime
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir))
BLOCKSYNC = os.path.join(REPO, "plugins", "module_utils", "blocksync.py")
MIB = 1024 * 1024
GIB = 1024 * MIB
BLOCK = 64 * MIB  # source generation granularity: every 4th block is a hole
EXTENT = MIB  # changes are random 1 MiB extents


def host_label():
    """A hardware/OS description (the network host name may be personal)."""
    cpu = platform.processor() or ""
    if sys.platform == "darwin":
        try:
            cpu = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                stdout=subprocess.PIPE, universal_newlines=True, check=False,
            ).stdout.strip() or cpu
        except OSError:
            pass
    elif os.path.exists("/proc/cpuinfo"):
        with open("/proc/cpuinfo") as f:
            match = re.search(r"model name\s*:\s*(.+)", f.read())
        cpu = match.group(1) if match else cpu
    label = "-".join(part for part in (platform.system(), platform.machine(), cpu) if part)
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "unknown"


def make_source(path, size):
    """Random data with a 64 MiB hole in every 256 MiB (sparse, zero frames)."""
    with open(path, "wb") as f:
        f.truncate(size)
        offset = 0
        index = 0
        while offset < size:
            length = min(BLOCK, size - offset)
            if index % 4 != 3:
                os.pwrite(f.fileno(), os.urandom(length), offset)
            offset += length
            index += 1


def make_sparse(path, size):
    with open(path, "wb") as f:
        f.truncate(size)


def change(path, size, percent, rng):
    """Overwrite ``percent`` % of the file with random 1 MiB extents."""
    extents = size // EXTENT
    count = max(1, extents * percent // 100)
    with open(path, "r+b") as f:
        for index in rng.sample(range(extents), count):
            os.pwrite(f.fileno(), os.urandom(EXTENT), index * EXTENT)
    return count * EXTENT


def run_pass(python, src, dst, chunk, workers, assume_zero):
    cmd = [python, BLOCKSYNC, "receive", "--device", dst, "--chunk-size", str(chunk),
           "--workers", str(workers), "--progress-interval", "3600"]
    if assume_zero:
        cmd.append("--assume-zero")
    cmd += ["--", python, BLOCKSYNC, "send", "--device", src,
            "--chunk-size", str(chunk), "--workers", str(workers)]
    started = time.monotonic()
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, check=False)
    wall = time.monotonic() - started
    lines = proc.stdout.strip().splitlines()
    summary = json.loads(lines[-1]) if lines else {}
    if proc.returncode != 0 or not summary.get("ok"):
        raise SystemExit("blocksync failed (%s): %s\n%s" % (proc.returncode, summary, proc.stderr))
    return summary, wall


def same(a, b, size):
    with open(a, "rb") as fa, open(b, "rb") as fb:
        offset = 0
        while offset < size:
            if fa.read(BLOCK) != fb.read(BLOCK):
                return False
            offset += BLOCK
    return True


def fmt_bytes(value):
    if value >= GIB:
        return "%.2f GiB" % (value / GIB)
    return "%.1f MiB" % (value / MIB)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--size-gib", type=float, default=2.0, help="device size (default 2)")
    parser.add_argument("--chunk-mib", default="1,4,16", help="chunk sizes in MiB")
    parser.add_argument("--workers", default="1,4", help="worker counts")
    parser.add_argument("--changes", default="1,5,20", help="changed percentages")
    parser.add_argument("--tmpdir", default=None, help="where the two files are created")
    parser.add_argument("--python", default=sys.executable, help="interpreter for blocksync")
    parser.add_argument("--host-label", default=None, help="label of the results file")
    parser.add_argument("--output", default=None, help="results file (default tests/perf/results-<host>.md)")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    size = int(args.size_gib * GIB) // EXTENT * EXTENT
    chunks = [int(c) * MIB for c in args.chunk_mib.split(",")]
    workers = [int(w) for w in args.workers.split(",")]
    changes = [int(p) for p in args.changes.split(",")]
    label = args.host_label or host_label()
    output = args.output or os.path.join(HERE, "results-%s.md" % label)
    rng = random.Random(args.seed)

    workdir = tempfile.mkdtemp(prefix="bench-blocksync-", dir=args.tmpdir)
    pristine = os.path.join(workdir, "pristine")
    src = os.path.join(workdir, "src")
    dst = os.path.join(workdir, "dst")
    rows = []
    load_before = os.getloadavg() if hasattr(os, "getloadavg") else None
    try:
        print("Generating a %s source in %s ..." % (fmt_bytes(size), workdir), file=sys.stderr)
        make_source(pristine, size)
        for chunk in chunks:
            for count in workers:
                # Every configuration starts from the same source.
                shutil.copyfile(pristine, src)
                make_sparse(dst, size)
                summary, wall = run_pass(args.python, src, dst, chunk, count, assume_zero=True)
                rows.append((chunk, count, "full (--assume-zero)", None, summary, wall))
                for percent in changes:
                    changed = change(src, size, percent, rng)
                    summary, wall = run_pass(args.python, src, dst, chunk, count, assume_zero=False)
                    rows.append((chunk, count, "delta", (percent, changed), summary, wall))
                if not same(src, dst, size):
                    raise SystemExit("destination differs from the source after the passes")
                print("chunk %d MiB, %d worker(s): done" % (chunk // MIB, count), file=sys.stderr)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    load_after = os.getloadavg() if hasattr(os, "getloadavg") else None

    header = [
        "# blocksync benchmark — %s" % label,
        "",
        "* Date: %s" % datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "* Platform: %s %s (%s), %d CPUs, Python %s" % (
            platform.system(), platform.release(), platform.machine(), os.cpu_count() or 0,
            platform.python_version()),
        "* Device: %s regular file, random data with a 64 MiB hole every 256 MiB; "
        "changes are random 1 MiB extents" % fmt_bytes(size),
        "* Sender and receiver on the same host through pipes; files in the page cache "
        "(CPU ceiling of read + BLAKE2b + apply, not disk or network throughput)",
        "* Load average (1/5/15 min) before: %s, after: %s (other work on the host makes "
        "single runs vary)" % (
            " / ".join("%.1f" % v for v in load_before) if load_before else "n/a",
            " / ".join("%.1f" % v for v in load_after) if load_after else "n/a"),
        "* Command: `python3 tests/perf/bench_blocksync.py %s`" % " ".join(
            sys.argv[1:] if argv is None else argv),
        "",
        "| Chunk | Workers | Pass | Changed | Scan MiB/s | Chunks changed | Transferred | Wall s |",
        "|---:|---:|---|---:|---:|---:|---:|---:|",
    ]
    lines = []
    for chunk, count, kind, changed, summary, wall in rows:
        scan_rate = summary["bytes_scanned"] / MIB / wall if wall else 0.0
        lines.append("| %d MiB | %d | %s | %s | %.0f | %d / %d | %s | %.2f |" % (
            chunk // MIB, count, kind,
            "—" if changed is None else "%d %% (%s)" % (changed[0], fmt_bytes(changed[1])),
            scan_rate, summary["chunks_changed"], summary["chunks"],
            fmt_bytes(summary["bytes_transferred"]), wall,
        ))
    table = "\n".join(header + lines) + "\n"
    print(table)
    with open(output, "w") as f:
        f.write(table)
    print("Wrote %s" % os.path.relpath(output, REPO), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
