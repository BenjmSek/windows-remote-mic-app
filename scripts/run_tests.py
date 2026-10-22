#!/usr/bin/env python3
"""Parallel test runner for the Windows remote-mic source package.

The suite is ~3.7k unittest cases in 131 modules and took about five minutes
under one ``unittest discover`` process. Most of that is idle waiting
(subprocess probes, QML offscreen start-up, bounded thread joins), so tests
are spread over several processes instead.

Design notes:

* stdlib only - no pytest/xdist dependency, nothing to install.
* **One process per target.** Bundling modules into a single worker looked
  faster until a native crash (0xC0000005 in a Qt test) took the whole bundle
  down and silently dropped every module after it. Per-target isolation turns
  a crash into one reported line.
* Heavy modules are split into class groups sized to roughly 12s of work, so
  the tail is not one 70s process while still not paying a Python + Qt
  interpreter start-up per class (448 tiny processes were slower than 60).
* A target that crashes is retried alone, then split per class, then per test.
  That separates "unstable under load" from "this module cannot share a
  process with itself".
* A handful of tests assert layout or timing and go red merely because the
  machine is busy, so every failure is re-run on its own before it is
  reported. Ones that pass alone are listed separately and do not fail the
  run.
* Target costs are cached in ``scripts/.test-timings.json`` and reused to
  start the expensive ones first.

Usage:
    python run_tests.py                # parallel, default jobs = min(cpu, 8)
    python run_tests.py -j 4           # fewer processes
    python run_tests.py --serial       # single process, old behaviour
    python run_tests.py test_config    # only modules matching a substring
    python run_tests.py -v             # verbose per-test output
    python run_tests.py -f             # stop scheduling after a failure
    python run_tests.py --no-confirm   # trust the parallel result as-is
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"
SRC_DIR = ROOT / "src"
TIMINGS_PATH = Path(__file__).resolve().parent / ".test-timings.json"
# Failures already confirmed alone on an idle machine. They are reported every
# run but not re-confirmed, so iterating on a known-red suite stays fast.
BASELINE_PATH = Path(__file__).resolve().parent / ".test-known-failures.json"

RAN_RE = re.compile(r"^Ran (\d+) tests? in ([\d.]+)s", re.M)
HEADER_RE = re.compile(r"^(FAIL|ERROR): (\S+) \(([^)]+)\)", re.M)
SUMMARY_RE = re.compile(r"^(OK|FAILED)\b", re.M)

# Rough seconds of work one process should own after a module is split. Small
# enough to stay parallel, large enough that interpreter start-up stays noise.
CHUNK_SECONDS = 12.0
MAX_CHUNKS = 8

LIST_SNIPPET = """
import json, sys, unittest
names = sys.argv[1:]
ids = []
for name in names:
    suite = unittest.defaultTestLoader.loadTestsFromName(name)
    def walk(node):
        for item in node:
            if isinstance(item, unittest.TestSuite):
                yield from walk(item)
            else:
                yield item
    ids.extend(case.id() for case in walk(suite))
print(json.dumps(ids))
"""


@dataclass
class Target:
    """One process worth of tests: 1..n importable unittest names."""

    names: tuple[str, ...]
    module: str
    weight: float
    depth: int = 0  # 0 = whole module, 1 = class group, 2 = single test

    @property
    def label(self) -> str:
        # The first name keeps chunk labels unique (and therefore usable as
        # timing-cache keys) instead of "module [2 classes]" twice over.
        if len(self.names) == 1:
            return self.names[0]
        return f"{self.names[0]} +{len(self.names) - 1} more"


@dataclass
class Result:
    target: str
    module: str
    returncode: int = 0
    ran: int = 0
    duration: float = 0.0
    failures: list[tuple[str, str, str]] = field(default_factory=list)
    output: str = ""
    timed_out: bool = False
    note: str = ""

    @property
    def crashed(self) -> bool:
        return self.timed_out or self.returncode not in (0, 1)

    @property
    def import_failed(self) -> bool:
        """A target that ran nothing, yet still exited non-zero.

        ``unittest`` maps a failing *assertion* to returncode 1, so 1 alone
        cannot mean "broken". But an import that raises anything other than
        ``ImportError`` - a missing VERSION file, a bad constant - escapes
        unittest entirely: the process dies with a bare traceback, prints no
        "Ran N tests" line and no FAIL/ERROR block, and still exits 1. Such a
        target was silently counted as a clean pass with zero tests run.
        """
        return self.ran == 0 and self.returncode != 0

    @property
    def status(self) -> str:
        if self.timed_out:
            return "TIMEOUT"
        if self.returncode < 0:
            return f"CRASH({self.returncode})"
        if self.crashed:
            return f"CRASH({self.returncode:#x})"
        if self.import_failed:
            return "IMPORT-FAILED"
        return "FAIL" if self.failures else "ok"


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    parts = [str(ROOT), str(SRC_DIR)]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def discover_modules(filters: list[str]) -> list[tuple[str, int]]:
    """Every ``tests/**/test_*.py``, mirroring ``unittest discover -s tests``."""

    found: list[tuple[str, int]] = []
    for path in sorted(TESTS_DIR.rglob("test_*.py")):
        module = "tests." + ".".join(path.relative_to(TESTS_DIR).with_suffix("").parts)
        if filters and not any(f in module for f in filters):
            continue
        found.append((module, path.stat().st_size))
    return found


def load_timings() -> dict[str, float]:
    try:
        data = json.loads(TIMINGS_PATH.read_text(encoding="utf-8"))
        return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}
    except (OSError, ValueError):
        return {}


def load_known_failures() -> set[str]:
    try:
        data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        return {str(item) for item in data.get("confirmed", [])}
    except (OSError, ValueError, AttributeError):
        return set()


def save_known_failures(confirmed: list[str]) -> None:
    try:
        BASELINE_PATH.write_text(
            json.dumps({"confirmed": sorted(confirmed)}, indent=1) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def save_timings(timings: dict[str, float]) -> None:
    try:
        TIMINGS_PATH.write_text(
            json.dumps(timings, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError:
        pass  # A read-only checkout must not fail the run.


def _import_error_line(output: str) -> str:
    """The last meaningful line of an import traceback, for the summary."""

    for line in reversed((output or "").splitlines()):
        stripped = line.strip()
        if stripped and not stripped.startswith("File \"") and stripped != "^" * len(stripped):
            return stripped[:160]
    return "no tests ran; import failed"


def split_failures(output: str) -> list[tuple[str, str, str]]:
    """Pull (kind, test_id, traceback) blocks out of a unittest run."""

    blocks = []
    matches = list(HEADER_RE.finditer(output))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(output)
        block = output[match.start():end]
        summary = SUMMARY_RE.search(block)
        if summary:  # drop the trailing OK/FAILED summary from the last block
            block = block[: summary.start()]
        blocks.append((match.group(1), match.group(3), block.rstrip() + "\n"))
    return blocks


def run_target(target: Target, *, verbose: bool, timeout: float) -> Result:
    cmd = [sys.executable, "-m", "unittest", *target.names]
    if verbose:
        cmd.append("-v")
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(ROOT),
            env=child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        output, returncode, timed_out = completed.stdout or "", completed.returncode, False
    except subprocess.TimeoutExpired as exc:
        output = exc.output if isinstance(exc.output, str) else ""
        returncode, timed_out = -1, True

    ran = RAN_RE.search(output)
    return Result(
        target=target.label,
        module=target.module,
        returncode=returncode,
        ran=int(ran.group(1)) if ran else 0,
        duration=time.perf_counter() - started,
        failures=split_failures(output),
        output=output,
        timed_out=timed_out,
    )


def list_test_ids(names: tuple[str, ...], timeout: float) -> list[str]:
    """Ask a child process for the test ids under ``names`` (empty on failure)."""

    try:
        completed = subprocess.run(
            [sys.executable, "-c", LIST_SNIPPET, *names],
            cwd=str(ROOT),
            env=child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if completed.returncode != 0:
            return []
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (subprocess.SubprocessError, ValueError, IndexError):
        return []


def expand(target: Target, timeout: float, *, chunk: bool) -> list[Target] | None:
    """Split module -> class groups, class group -> single tests.

    ``chunk`` groups classes into a few processes (start-up cost matters when
    splitting proactively); without it every class gets its own process, which
    is what we want when hunting a crash.
    """

    if target.depth >= 2:
        return None
    ids = list_test_ids(target.names, timeout)
    if not ids:
        return None
    if target.depth == 0:
        units = sorted({test_id.rsplit(".", 1)[0] for test_id in ids})
    else:
        units = ids
    if len(units) < 2:
        only = units[0] if units else None
        if only is None or only == target.names[0]:
            return None
        return expand(Target((only,), target.module, target.weight, target.depth + 1),
                      timeout, chunk=chunk)

    # Measured: splitting further (per test method) is a big loss - each
    # method re-pays the Qt import plus QQmlApplicationEngine start-up, so 829
    # processes took 196s against 85s for the class-level split. Class groups
    # are where the parallelisation pays off.
    if target.depth == 0 and chunk:
        count = max(2, min(MAX_CHUNKS, math.ceil(target.weight / CHUNK_SECONDS)))
        if count < len(units):
            buckets = [units[index::count] for index in range(count)]
            share = target.weight / count
            return [Target(tuple(bucket), target.module, share, 1)
                    for bucket in buckets if bucket]

    share = target.weight / len(units)
    return [Target((unit,), target.module, share, target.depth + 1) for unit in units]


def execute(target: Target, args: argparse.Namespace) -> list[Result]:
    """Run one target, recovering from crashes by retrying then splitting."""

    result = run_target(target, verbose=args.verbose, timeout=args.timeout)
    if not result.crashed:
        return [result]
    if args.jobs > 1 and args.retries:
        for _ in range(args.retries):
            retry = run_target(target, verbose=args.verbose, timeout=args.timeout)
            if not retry.crashed:
                retry.note = f"passed alone after {result.status}"
                return [retry]
            result = retry
    children = expand(target, args.timeout, chunk=False)
    if children is None:
        return [result]
    print(f"  {result.status:<12} {target.label}  -> split into {len(children)}", flush=True)
    results: list[Result] = []
    for child in children:
        results.extend(execute(child, args))
    return results


def module_of(test_id: str, known: set[str]) -> str:
    """Longest known module that prefixes ``test_id``."""

    parts = test_id.split(".")
    for length in range(len(parts), 0, -1):
        candidate = ".".join(parts[:length])
        if candidate in known:
            return candidate
    return test_id.rsplit(".", 2)[0]


def confirm_failures(failing: list[str], known: set[str], args: argparse.Namespace):
    """Re-run every failure alone: layout/timing tests go red on a busy box."""

    print(f"confirming {len(failing)} failure(s) one at a time ...", flush=True)
    confirmed: list[str] = []
    flaky: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.confirm_jobs)) as pool:
        futures = {
            pool.submit(
                run_target,
                Target((test_id,), module_of(test_id, known), 0.0, 2),
                verbose=False,
                timeout=args.timeout,
            ): test_id
            for test_id in failing
        }
        for future in concurrent.futures.as_completed(futures):
            test_id = futures[future]
            result = future.result()
            (confirmed if (result.failures or result.crashed) else flaky).append(test_id)
    return sorted(confirmed), sorted(flaky)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("filters", nargs="*", help="only run modules containing these substrings")
    parser.add_argument("-j", "--jobs", type=int, default=min(os.cpu_count() or 1, 8),
                        help="parallel processes (default: min(cpu, 8))")
    parser.add_argument("--serial", action="store_true",
                        help="one process, no splitting (old unittest discover behaviour)")
    parser.add_argument("-v", "--verbose", action="store_true", help="per-test output")
    parser.add_argument("-f", "--failfast", action="store_true", help="stop scheduling after a failure")
    parser.add_argument("--timeout", type=float, default=600.0, help="per-target timeout seconds")
    parser.add_argument("--retries", type=int, default=1,
                        help="re-run a crashed target alone up to N times (default 1)")
    parser.add_argument("--split-threshold", type=float, default=20.0,
                        help="split modules slower than this into class groups (default 20s, 0 disables)")
    parser.add_argument("--no-cache", action="store_true", help="ignore and do not write timings")
    parser.add_argument("--confirm", dest="confirm", action="store_true", default=None,
                        help="re-run each failure alone to separate real failures from "
                             "load-sensitive ones (default: on whenever jobs > 1)")
    parser.add_argument("--no-confirm", dest="confirm", action="store_false",
                        help="trust the parallel result as-is")
    parser.add_argument("--confirm-jobs", type=int, default=4,
                        help="parallelism while confirming failures (default 4)")
    parser.add_argument("--reconfirm", action="store_true",
                        help="re-confirm failures that were already confirmed on an earlier run")
    args = parser.parse_args()

    if not (SRC_DIR / "ovb").is_dir():
        print(f"cannot find {SRC_DIR / 'ovb'}", file=sys.stderr)
        return 2

    modules = discover_modules(args.filters)
    if not modules:
        print("no test modules matched", file=sys.stderr)
        return 2

    timings = {} if args.no_cache else load_timings()

    targets: list[Target] = []
    heavy = 0
    for module, size in modules:
        weight = timings.get(module) or max(size / 4000.0, 0.05)
        candidate = Target((module,), module, weight)
        if not args.serial and args.split_threshold and weight > args.split_threshold:
            children = expand(candidate, args.timeout, chunk=True)
            if children:
                heavy += 1
                targets.extend(children)
                continue
        targets.append(candidate)
    return _run_targets(targets, modules, timings, args, heavy=heavy)



    note = f", {heavy} heavy module(s) split" if heavy else ""
    print(
        f"running {len(modules)} modules as {len(targets)} target(s) "
        f"with {jobs} process(es) python={sys.version.split()[0]}{note}",
        flush=True,
    )
    started = time.perf_counter()
    collected: list[Result] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(execute, target, args): target for target in targets}
        for future in concurrent.futures.as_completed(futures):
            batch = future.result()
            collected.extend(batch)
            for result in batch:
                if result.status == "FAIL":
                    print(f"  {'FAIL':<12} {result.target}  ({result.duration:.1f}s)", flush=True)
                elif result.crashed:
                    print(f"  {result.status:<12} {result.target}", flush=True)
                elif result.import_failed:
                    # No test ids to name, so surface the reason directly -
                    # otherwise the run looks clean while a whole module
                    # never executed.
                    print(f"  {'IMPORT-FAILED':<12} {result.target}  "
                          f"({_import_error_line(result.output)})", flush=True)
                elif result.note:
                    print(f"  {'unstable':<12} {result.target}  ({result.note})", flush=True)
            if args.failfast and any(r.failures for r in batch):
                for pending in futures:
                    pending.cancel()

    parallel_elapsed = time.perf_counter() - started

    if args.verbose:
        for result in sorted(collected, key=lambda r: r.target):
            print(f"\n===== {result.target} =====")
            print(result.output, end="")

    all_failures = sorted(
        {(kind, test_id, trace) for result in collected for kind, test_id, trace in result.failures},
        key=lambda item: item[1],
    )
    for _kind, _test_id, trace in all_failures:
        print(trace)

    failing = sorted({test_id for _kind, test_id, _trace in all_failures})
    flaky: list[str] = []
    should_confirm = bool(failing) and (
        args.confirm is True or (args.confirm is None and jobs > 1)
    )
    if should_confirm:
        previously = set() if args.reconfirm else load_known_failures()
        carried = [test_id for test_id in failing if test_id in previously]
        fresh = [test_id for test_id in failing if test_id not in previously]
        known_modules = {name for name, _size in modules}
        if fresh:
            confirmed, flaky = confirm_failures(fresh, known_modules, args)
        else:
            confirmed = []
            print(f"\n{len(carried)} failure(s) already confirmed, re-checking none", flush=True)
        failing = sorted(carried + confirmed)
        if not args.no_cache:
            save_known_failures(failing)

    if failing:
        print(f"--- failing tests ({len(failing)}) ---")
        for test_id in failing:
            print(f"  {test_id}")
        print()
    if flaky:
        print(f"--- passed when re-run alone ({len(flaky)}): load-sensitive, not counted ---")
        for test_id in flaky:
            print(f"  {test_id}")
        print()

    crashed = [result for result in collected if result.crashed or result.import_failed]
    if crashed:
        print(f"--- crashed / import-failed targets ({len(crashed)}) ---")
        for result in crashed:
            print(f"  {result.status} {result.target}")
        print()

    print("--- slowest targets ---")
    for result in sorted(collected, key=lambda r: -r.duration)[:8]:
        shared = " (shared cpu)" if jobs > 1 else ""
        print(f"  {result.duration:7.2f}s{shared}  {result.target}")

    if not args.no_cache:
        merged = dict(timings)
        merged.update({r.target: round(r.duration, 3) for r in collected if r.ran})
        save_timings(merged)

    ran = sum(result.ran for result in collected)
    total_elapsed = time.perf_counter() - started
    confirm_note = f" (+{total_elapsed - parallel_elapsed:.0f}s confirming)" if flaky or failing else ""
    print(
        f"\nRan {ran} tests in {parallel_elapsed:.1f}s{confirm_note} ({jobs} process(es))  "
        f"failing={len(failing)} flaky={len(flaky)} crashed={len(crashed)}"
    )
    return 1 if (failing or crashed) else 0


def _run_targets(targets: list[Target], modules: list[tuple[str, int]],
                 timings: dict[str, float], args: argparse.Namespace, heavy: int = 0) -> int:
    """Run the given targets and return the process exit code."""

    targets = sorted(targets, key=lambda item: -item.weight)
    jobs = 1 if args.serial else max(1, min(args.jobs, len(targets)))
    heavy_note = f", {heavy} heavy module(s) split" if heavy else ""

    print(
        f"running {len(modules)} modules as {len(targets)} target(s) "
        f"with {jobs} process(es) python={sys.version.split()[0]}{heavy_note}",
        flush=True,
    )
    started = time.perf_counter()
    collected: list[Result] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(execute, target, args): target for target in targets}
        for future in concurrent.futures.as_completed(futures):
            batch = future.result()
            collected.extend(batch)
            for result in batch:
                if result.status == "FAIL":
                    print(f"  {'FAIL':<12} {result.target}  ({result.duration:.1f}s)", flush=True)
                elif result.crashed:
                    print(f"  {result.status:<12} {result.target}", flush=True)
                elif result.import_failed:
                    # No test ids to name, so surface the reason directly -
                    # otherwise the run looks clean while a whole module
                    # never executed.
                    print(f"  {'IMPORT-FAILED':<12} {result.target}  "
                          f"({_import_error_line(result.output)})", flush=True)
                elif result.note:
                    print(f"  {'unstable':<12} {result.target}  ({result.note})", flush=True)
            if args.failfast and any(r.failures for r in batch):
                for pending in futures:
                    pending.cancel()

    parallel_elapsed = time.perf_counter() - started

    if args.verbose:
        for result in sorted(collected, key=lambda r: r.target):
            print(f"\n===== {result.target} =====")
            print(result.output, end="")

    all_failures = sorted(
        {(kind, test_id, trace) for result in collected for kind, test_id, trace in result.failures},
        key=lambda item: item[1],
    )
    for _kind, _test_id, trace in all_failures:
        print(trace)

    failing = sorted({test_id for _kind, test_id, _trace in all_failures})
    flaky: list[str] = []
    should_confirm = bool(failing) and (
        args.confirm is True or (args.confirm is None and jobs > 1)
    )
    if should_confirm:
        previously = set() if args.reconfirm else load_known_failures()
        carried = [test_id for test_id in failing if test_id in previously]
        fresh = [test_id for test_id in failing if test_id not in previously]
        known_modules = {name for name, _size in modules}
        if fresh:
            confirmed, flaky = confirm_failures(fresh, known_modules, args)
        else:
            confirmed = []
            print(f"\n{len(carried)} failure(s) already confirmed, re-checking none", flush=True)
        failing = sorted(carried + confirmed)
        if not args.no_cache:
            save_known_failures(failing)

    if failing:
        print(f"--- failing tests ({len(failing)}) ---")
        for test_id in failing:
            print(f"  {test_id}")
        print()
    if flaky:
        print(f"--- passed when re-run alone ({len(flaky)}): load-sensitive, not counted ---")
        for test_id in flaky:
            print(f"  {test_id}")
        print()

    crashed = [result for result in collected if result.crashed or result.import_failed]
    if crashed:
        print(f"--- crashed / import-failed targets ({len(crashed)}) ---")
        for result in crashed:
            print(f"  {result.status} {result.target}")
        print()

    print("--- slowest targets ---")
    for result in sorted(collected, key=lambda r: -r.duration)[:8]:
        shared = " (shared cpu)" if jobs > 1 else ""
        print(f"  {result.duration:7.2f}s{shared}  {result.target}")

    if not args.no_cache:
        merged = dict(timings)
        merged.update({r.target: round(r.duration, 3) for r in collected if r.ran})
        save_timings(merged)

    ran = sum(result.ran for result in collected)
    total_elapsed = time.perf_counter() - started
    confirm_note = f" (+{total_elapsed - parallel_elapsed:.0f}s confirming)" if flaky or failing else ""
    print(
        f"\nRan {ran} tests in {parallel_elapsed:.1f}s{confirm_note} ({jobs} process(es))  "
        f"failing={len(failing)} flaky={len(flaky)} crashed={len(crashed)}"
    )
    return 1 if (failing or crashed) else 0




if __name__ == "__main__":
    sys.exit(main())
