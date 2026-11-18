#!/usr/bin/env python3
"""Measure how long pause and resume take at different VM memory sizes (Track C, for Track B's grace period).

Calls node_agent directly, because the API doesn't pass the timings back. Run on the host, from sbx/:

    sudo .venv/bin/python scripts/bench_pause.py                    # 512/1024/2048 MiB, 3 runs each
    sudo .venv/bin/python scripts/bench_pause.py --cold             # drop the host's file cache before each resume
    sudo .venv/bin/python scripts/bench_pause.py --sizes 2048 --runs 5 --fill 0

Each run: create a sandbox at that size, fill part of its RAM (so the snapshot holds real data, not empty
pages), pause, resume, check the filler process survived, destroy. Prints a Markdown table of medians and
writes it, plus every raw run, to --out.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # so `import node_agent` works

import node_agent


FILL_MARKER = "/tmp/bench_filled"   # the filler writes this once its memory is allocated
FILL_WAIT_TIMEOUT = 60.0
PAUSE_PARTS = ("sync_s", "freeze_s", "snapshot_s", "kill_s")
RESUME_PARTS = ("start_s", "load_s", "health_s")
COLUMNS = PAUSE_PARTS + ("pause_total_s",) + RESUME_PARTS + ("resume_total_s", "mem_file_mib")


def filler_command(mib: int) -> str:
    """Return a guest shell command that holds `mib` MiB of non-zero memory until killed."""
    script = (
        f"b = bytearray(b'x') * ({mib} * 1024 * 1024); "   # every page written, so none stay empty
        f"open('{FILL_MARKER}', 'w').close(); "
        "import time; time.sleep(10**9)"
    )
    return f'python3 -c "{script}"'


def fill_memory(sandbox_id: str, mib: int) -> int:
    """Start the filler in the guest, wait until its memory is allocated, and return its PID."""
    pid = node_agent.exec(sandbox_id, filler_command(mib), background=True)["pid"]
    node_agent.wait_until(
        lambda: node_agent.exec(sandbox_id, f"test -f {FILL_MARKER}", timeout=10)["exit_code"] == 0,
        timeout=FILL_WAIT_TIMEOUT, interval=0.5,
        waiting_for=f"the filler to allocate {mib} MiB in {sandbox_id}",
    )
    return pid


def process_alive(sandbox_id: str, pid: int) -> bool:
    """Return True if the guest process is still running."""
    return node_agent.exec(sandbox_id, f"kill -0 {pid}", timeout=10)["exit_code"] == 0


def drop_host_cache() -> None:
    """Empty the host's file cache, so resume reads the memory file from disk like after a long pause."""
    os.sync()
    with open("/proc/sys/vm/drop_caches", "w") as f:
        f.write("3")


def timed_call(step):
    """Run one step and return (its result, how many seconds it took)."""
    started = time.monotonic()
    result = step()
    return result, time.monotonic() - started


def measure_one(size_mib: int, fill_fraction: float, cold: bool) -> dict:
    """Create a sandbox at size_mib, pause and resume it once, destroy it, and return every timing."""
    node_agent.MEMORY_MIB = size_mib   # read at boot and by the snapshot size check
    sandbox = node_agent.create_sandbox()
    try:
        filler_pid = fill_memory(sandbox.id, int(size_mib * fill_fraction)) if fill_fraction > 0 else None
        pause, pause_total = timed_call(lambda: node_agent.pause_sandbox(sandbox.id))
        mem_file_mib = os.path.getsize(node_agent.mem_path(sandbox.id)) / node_agent.BYTES_PER_MIB
        if cold:
            drop_host_cache()
        resume, resume_total = timed_call(lambda: node_agent.resume_sandbox(sandbox.id))
        survived = filler_pid is None or process_alive(sandbox.id, filler_pid)
    finally:
        node_agent.destroy_sandbox(sandbox.id)
    return {
        "size_mib": size_mib, **{k: pause[k] for k in PAUSE_PARTS}, "pause_total_s": pause_total,
        **{k: resume[k] for k in RESUME_PARTS}, "resume_total_s": resume_total,
        "mem_file_mib": mem_file_mib, "boot_s": sandbox.boot_seconds, "filler_survived": survived,
    }


def medians(runs: list[dict]) -> dict:
    """Return the median of every timing column across runs of one size."""
    return {col: statistics.median(run[col] for run in runs) for col in COLUMNS}


def format_size(size_mib: int) -> str:
    """Return a size as people say it: 512 MiB, 1 GiB, 2 GiB."""
    return f"{size_mib // 1024} GiB" if size_mib % 1024 == 0 else f"{size_mib} MiB"


def markdown_table(results: dict[int, list[dict]]) -> str:
    """Return a Markdown table with one row of medians per memory size."""
    header = "| Memory | " + " | ".join(COLUMNS) + " | runs |"
    rule = "|---" * (len(COLUMNS) + 2) + "|"
    rows = []
    for size_mib, runs in results.items():
        m = medians(runs)
        cells = [f"{m[col]:.0f}" if col == "mem_file_mib" else f"{m[col]:.3f}" for col in COLUMNS]
        rows.append(f"| {format_size(size_mib)} | " + " | ".join(cells) + f" | {len(runs)} |")
    return "\n".join([header, rule, *rows])


def report(results: dict[int, list[dict]], args: argparse.Namespace) -> str:
    """Return the full Markdown report: settings, median table, and any runs where the filler died."""
    lost = [r for runs in results.values() for r in runs if not r["filler_survived"]]
    lines = [
        "# Pause/resume timings", "",
        f"Medians of {args.runs} runs per size, in seconds. vCPUs: {node_agent.VCPU_COUNT}. "
        f"RAM filled before pause: {args.fill:.0%}. Host file cache before resume: "
        f"{'dropped (cold)' if args.cold else 'kept (warm)'}.", "",
        markdown_table(results), "",
        "Fill in: host machine type, disk type (pd-balanced / pd-ssd), date.", "",
    ]
    if lost:
        lines.append(f"WARNING: the filler process was gone after resume in {len(lost)} run(s).")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    """Read the command-line options."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sizes", type=int, nargs="+", default=[512, 1024, 2048], help="memory sizes in MiB")
    parser.add_argument("--runs", type=int, default=3, help="runs per size (median is reported)")
    parser.add_argument("--fill", type=float, default=0.5, help="fraction of RAM to fill before pausing (0 = idle)")
    parser.add_argument("--cold", action="store_true", help="drop the host file cache before each resume")
    parser.add_argument("--out", default="pause_timings.md", help="where to write the report (raw runs go next to it)")
    return parser.parse_args()


def main() -> None:
    """Run every size the asked number of times, then print and save the report."""
    args = parse_args()
    if os.geteuid() != 0:
        sys.exit("run with sudo: it creates VMs, network namespaces and cgroups")
    results: dict[int, list[dict]] = {}
    for size_mib in args.sizes:
        for i in range(args.runs):
            run = measure_one(size_mib, args.fill, args.cold)
            print(f"{format_size(size_mib)} run {i + 1}: pause {run['pause_total_s']:.2f}s, "
                  f"resume {run['resume_total_s']:.2f}s", flush=True)
            results.setdefault(size_mib, []).append(run)
    text = report(results, args)
    print("\n" + text)
    with open(args.out, "w") as f:
        f.write(text)
    with open(os.path.splitext(args.out)[0] + ".jsonl", "w") as f:
        f.writelines(json.dumps(run) + "\n" for runs in results.values() for run in runs)


if __name__ == "__main__":
    main()
