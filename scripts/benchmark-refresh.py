#!/usr/bin/env python3
"""Measure refresh orchestration using synthetic collectors and a stub publisher."""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def measure(refresh, directory: Path, scenario: str, reload_delay: float) -> dict:
    package = directory / "runtime/bars_collector"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    source = """
import json, os, pathlib, sys, time
path = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])
def write(completed):
    temp = path.with_suffix('.new')
    temp.write_text(json.dumps({'completed': completed}))
    os.replace(temp, path)
"""
    if scenario == "staged":
        source += "for completed in range(4):\n    time.sleep(0.3)\n    write(completed)\n"
    elif scenario == "deadline":
        source += "time.sleep(60)\n"
    else:
        source += "write(1)\n"
    (package / "__main__.py").write_text(source)
    publisher = directory / "StubPublisher"
    publisher.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        f"if '--no-reload' not in sys.argv: time.sleep({reload_delay!r})\n"
    )
    publisher.chmod(0o700)
    config = {"python": sys.executable, "collector_path": str(package.parent),
              "app_executable": str(publisher)}
    if scenario == "disabled_noop":
        # All four providers are disabled before either invocation. No credential
        # lookup, provider worker, network call or native process can run here.
        config["collector_path"] = str(ROOT / "collector")
        config["disabled_providers"] = list(refresh.PROVIDERS)
        env = refresh.child_environment()
        env["PYTHONPATH"] = config["collector_path"]
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        args = [sys.executable, "-m", "bars_collector", "--output", str(directory / "snapshot.json")]
        for provider in refresh.PROVIDERS:
            args += ["--disable", provider]
        subprocess.run(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True, timeout=10)

    counts = {"subprocesses": 0, "revision_checks": 0, "poll_calls": 0, "wait_calls": 0,
              "sleep_calls": 0, "publications": 0, "reloads": 0}
    real_popen = subprocess.Popen
    real_revision = refresh.revision
    real_sleep = time.sleep
    real_publish = refresh.publish

    def popen(*args, **kwargs):
        counts["subprocesses"] += 1
        process = real_popen(*args, **kwargs)
        real_poll = process.poll
        real_wait = process.wait

        def poll(*args, **kwargs):
            counts["poll_calls"] += 1
            return real_poll(*args, **kwargs)

        def wait(*args, **kwargs):
            counts["wait_calls"] += 1
            return real_wait(*args, **kwargs)

        process.poll = poll
        process.wait = wait
        return process

    def revision(*args):
        counts["revision_checks"] += 1
        return real_revision(*args)

    def sleep(seconds):
        counts["sleep_calls"] += 1
        return real_sleep(seconds)

    def publish(*args, **kwargs):
        counts["publications"] += 1
        counts["reloads"] += int(kwargs["reload"])
        return real_publish(*args, **kwargs)

    logger = logging.getLogger("bars.benchmark")
    logger.addHandler(logging.NullHandler())
    usage_before = [resource.getrusage(who) for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)]
    started = time.perf_counter()
    with patch.object(refresh.subprocess, "Popen", popen), \
            patch.object(refresh, "revision", revision), \
            patch.object(refresh.time, "sleep", sleep), \
            patch.object(refresh, "publish", publish), \
            patch.object(refresh, "MAX_RUN_SECONDS", 0.1 if scenario == "deadline" else refresh.MAX_RUN_SECONDS):
        result = refresh.collect_and_publish(config, directory, None, logger)
    elapsed = time.perf_counter() - started
    usage_after = [resource.getrusage(who) for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)]
    expected = 1 if scenario == "deadline" else 0
    if result != expected:
        raise RuntimeError("Synthetic refresh returned an unexpected status.")
    cpu = sum(after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime
              for before, after in zip(usage_before, usage_after))
    return {"elapsed_seconds": elapsed, "cpu_seconds": cpu, "exit_status": result, **counts}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-script", type=Path, default=ROOT / "integration/refresh.py")
    parser.add_argument("--output", type=Path, default=ROOT / ".build/performance/refresh/timings.json")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--reload-delay", type=float, default=0,
                        help="Seconds spent in the stub publisher's final reload request.")
    args = parser.parse_args()
    if args.samples < 1 or not 0 <= args.reload_delay <= 5:
        parser.error("Samples must be positive and reload delay must be between 0 and 5 seconds.")
    artifact_directory = ROOT / ".build/performance/refresh"
    artifact_directory.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("refresh_benchmark", args.refresh_script)
    refresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(refresh)
    scenarios = {}
    for scenario in ("fast", "disabled_noop", "staged", "deadline"):
        samples = []
        for _ in range(args.samples):
            with tempfile.TemporaryDirectory(prefix="synthetic-", dir=artifact_directory) as temporary:
                samples.append(measure(refresh, Path(temporary), scenario, args.reload_delay))
        summary = {key: statistics.median(sample[key] for sample in samples)
                   for key in samples[0] if key != "exit_status"}
        scenarios[scenario] = {"median": summary, "samples": samples}
    report = {"python": sys.version.split()[0], "refresh_script": str(args.refresh_script),
              "reload_delay_seconds": args.reload_delay, "scenarios": scenarios}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({name: data["median"] for name, data in scenarios.items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
