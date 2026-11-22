"""Bounded, localhost-only load and recovery checks against a disposable index.

Run with the optional requirements-qa.txt dependencies installed.
"""

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter, sleep
from urllib.request import Request, urlopen

import psutil

from rag.documents import Passage
from rag.search import Index, build_index

ROOT = Path(__file__).resolve().parents[1]
QUESTION = "How long do webhook retries continue?"


def distribution(samples: list[float]) -> dict:
    ordered = sorted(samples)
    return {f"p{percent}_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * percent / 100))], 2)
            for percent in (50, 95, 99)}


def run(soak_seconds: float) -> dict:
    report = {"platform": platform.platform(), "python": platform.python_version(), "load": []}
    with TemporaryDirectory(prefix="relay-qa-") as temporary:
        database = Path(temporary) / "index.sqlite3"
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        environment = {**os.environ, "KB_DATABASE": str(database), "KB_DOCUMENTS": str(ROOT / "knowledge")}
        environment.pop("KB_OLLAMA_MODEL", None)

        def get(path):
            with urlopen(origin + path, timeout=5) as response:
                return json.load(response)

        def start():
            clock = perf_counter()
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "rag.app:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
                cwd=ROOT, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                while perf_counter() - clock < 20:
                    if process.poll() is not None:
                        raise RuntimeError(f"Server exited with {process.returncode}")
                    try:
                        if get("/api/health")["status"] == "ready":
                            return process, (perf_counter() - clock) * 1000
                    except OSError:
                        sleep(0.05)
                raise TimeoutError("Server startup timed out")
            except BaseException:
                stop(process)
                raise

        def stop(process):
            # A Windows venv executable is a launcher; its child owns the socket.
            try:
                parent = psutil.Process(process.pid)
                owned = [*parent.children(recursive=True), parent]
            except psutil.NoSuchProcess:
                return
            for child in reversed(owned):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(owned, timeout=5)
            if alive:
                raise RuntimeError("Test server process tree did not stop")
            process.wait(timeout=5)

        def ask(_):
            clock = perf_counter()
            request = Request(origin + "/api/ask", data=json.dumps({"question": QUESTION}).encode(),
                              headers={"Content-Type": "application/json"})
            try:
                with urlopen(request, timeout=15) as response:
                    result = json.load(response)
                good = result["status"] == "answered" and "24 hours" in result["answer"]
                good = good and result["citations"][0]["id"] == "webhooks/delivery-retries/1"
                return (perf_counter() - clock) * 1000, good
            except (OSError, ValueError, KeyError, IndexError):
                return (perf_counter() - clock) * 1000, False

        process, startup = start()
        report["startup_ms"] = round(startup, 2)
        try:
            launcher = psutil.Process(process.pid)
            candidates = [launcher, *launcher.children(recursive=True)]
            monitor = max(candidates, key=lambda child: child.memory_info().rss)
            report["monitored_worker"] = {"pid": monitor.pid, "command": monitor.cmdline()[-8:]}
            for workers, count in [(1, 100), (8, 400), (32, 1000)]:
                clock = perf_counter()
                cpu = monitor.cpu_times()
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    results = list(pool.map(ask, range(count)))
                elapsed = perf_counter() - clock
                after_cpu = monitor.cpu_times()
                report["load"].append({
                    "concurrency": workers, "requests": count, "errors": sum(not good for _, good in results),
                    "elapsed_seconds": round(elapsed, 2), "requests_per_second": round(count / elapsed, 2),
                    "cpu_seconds": round(after_cpu.user + after_cpu.system - cpu.user - cpu.system, 2),
                    **distribution([duration for duration, _ in results]),
                })
            samples, latencies, errors = [], [], 0
            clock = perf_counter()
            cpu = monitor.cpu_times()
            with ThreadPoolExecutor(max_workers=8) as pool:
                while perf_counter() - clock < soak_seconds:
                    results = list(pool.map(ask, range(8)))
                    latencies.extend(duration for duration, _ in results)
                    errors += sum(not good for _, good in results)
                    samples.append({"second": round(perf_counter() - clock, 2),
                                    "rss_mb": round(monitor.memory_info().rss / 1024**2, 2),
                                    "handles": monitor.num_handles() if os.name == "nt" else monitor.num_fds()})
                    sleep(0.1)
            after_cpu = monitor.cpu_times()
            report["soak"] = {
                "seconds": round(perf_counter() - clock, 2), "requests": len(latencies), "errors": errors,
                "cpu_seconds": round(after_cpu.user + after_cpu.system - cpu.user - cpu.system, 2),
                "rss_start_mb": samples[0]["rss_mb"], "rss_end_mb": samples[-1]["rss_mb"],
                "rss_peak_mb": max(sample["rss_mb"] for sample in samples),
                "handles_start": samples[0]["handles"], "handles_end": samples[-1]["handles"],
                "samples": samples, **distribution(latencies),
            }
            report["source_lookup"] = get("/api/sources/webhooks/delivery-retries/1")["id"]
            previous_worker = monitor.pid
            stop(process)  # Only this run's process tree and disposable database.
            database.write_bytes(b"simulated interrupted/corrupt derived index")
            process, recovery = start()
            report["recovery"] = {"startup_ms": round(recovery, 2), "answered": ask(0)[1],
                                  "previous_worker_stopped": not psutil.pid_exists(previous_worker)}
        finally:
            if process.poll() is None:
                stop(process)

        scaling = []
        for count in (1000, 10000):
            passages = [Passage(f"synthetic-{number}/retries/1", f"Synthetic {number}", "Delivery retries",
                                f"Webhook retries stop after 24 hours. Synthetic record {number}.") for number in range(count)]
            clock = perf_counter()
            build_index(database, passages)
            build_ms = (perf_counter() - clock) * 1000
            index, timings = Index(database), []
            for _ in range(40):
                clock = perf_counter()
                assert index.find(QUESTION) is not None
                timings.append((perf_counter() - clock) * 1000)
            clock = perf_counter()
            assert index.source(passages[-1].id) == passages[-1]
            scaling.append({"passages": count, "build_ms": round(build_ms, 2), "find_p50_ms": round(median(timings), 2),
                            "source_last_ms": round((perf_counter() - clock) * 1000, 2), "db_bytes": database.stat().st_size})
        report["synthetic_scaling"] = scaling
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--soak-seconds", type=float, default=120)
    parser.add_argument("--output", type=Path, default=Path(".data/qa/performance.json"))
    args = parser.parse_args()
    if not 1 <= args.soak_seconds <= 3600:
        parser.error("soak-seconds must be between 1 and 3600")
    report = run(args.soak_seconds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**report, "soak": {key: value for key, value in report["soak"].items() if key != "samples"}}, indent=2))
    raise SystemExit(int(any(stage["errors"] for stage in report["load"]) or report["soak"]["errors"] > 0 or not report["recovery"]["answered"]))


if __name__ == "__main__":
    main()
