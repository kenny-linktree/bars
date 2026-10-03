#!/usr/bin/env python3
"""Measure synthetic collector startup, persistence and sampled process-tree memory.

No adapter is invoked: workers return fixed synthetic usage without reading logins
or contacting providers. Memory sampling is a separate run, so its overhead does
not affect latency measurements. Run from any directory with Python 3.9 or later.
"""

import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

from bars_collector.model import PROVIDERS, now  # noqa: E402
from bars_collector.parsers import devin  # noqa: E402
from bars_collector.runner import collect_results, run  # noqa: E402


def synthetic_result():
    return devin({"overage_balance": 25, "daily_percentage": 30})


def held_result():
    # Hold workers long enough to sample them; excluded from latency measurements.
    time.sleep(0.3)
    return synthetic_result()


def measure(iterations, operation):
    samples = []
    for _ in range(iterations):
        started = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - started) * 1000)
    return {"median_ms": round(statistics.median(samples), 3),
            "max_ms": round(max(samples), 3), "samples": iterations}


def sample_tree(stop, peak):
    while not stop.is_set():
        rows = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,rss="],
                              capture_output=True, text=True, check=True, timeout=5)
        processes = {}
        for row in rows.stdout.splitlines():
            pid, parent, rss = map(int, row.split())
            processes[pid] = (parent, rss)
        descendants = {os.getpid()}
        while True:
            extended = descendants | {pid for pid, (parent, _) in processes.items()
                                      if parent in descendants}
            if extended == descendants:
                break
            descendants = extended
        # Includes the sampler's short-lived ps process; RSS double-counts shared pages.
        total = sum(processes[pid][1] for pid in descendants if pid in processes) * 1024
        peak.append(total)
        stop.wait(0.05)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 100:
        parser.error("--iterations must be between 1 and 100")
    directory = ROOT / ".build/benchmarks/collector"
    directory.mkdir(parents=True, exist_ok=True)
    providers = list(PROVIDERS)
    collectors = dict.fromkeys(providers, synthetic_result)
    output = {"python": sys.version.split()[0], "synthetic_only": True}
    output["four_spawned_workers"] = measure(
        args.iterations, lambda: list(collect_results(providers, collectors=collectors)))
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        path = Path(temporary) / "snapshot.json"

        def persist():
            stamp = now()
            values = ((pid, dict(status="ok", message=None, fetched_at=stamp,
                                 last_attempt_at=stamp, **synthetic_result())) for pid in providers)
            run(path, providers, results=values)

        output["four_atomic_snapshot_writes"] = measure(args.iterations, persist)
        output["workers_and_persistence"] = measure(
            args.iterations, lambda: run(path, providers, results=collect_results(providers, collectors=collectors)))
    stop = threading.Event()
    peak = []
    sampler = threading.Thread(target=sample_tree, args=(stop, peak))
    sampler.start()
    try:
        list(collect_results(providers, collectors=dict.fromkeys(providers, held_result)))
    finally:
        stop.set()
        sampler.join()
    output["sampled_process_tree"] = {
        "peak_rss_bytes": max(peak) if peak else None,
        "samples": len(peak),
        "note": "Separate run; includes ps, double-counts shared pages, and may miss short peaks.",
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
