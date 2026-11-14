"""Round 1 driver: idle -> catalogue publish -> 10 users (10 worker children) -> 10 users (3 children).

Runs with the project venv from the repo root; writes results/r1_phases.json plus snapshots and load reports.
`sample.py stream` must already be recording docker stats.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "resource-usage-bench"
RES = BENCH / "results"
PY = sys.executable
COMPOSE = ["docker", "compose", "-p", "kxbench", "-f", str(BENCH / "compose" / "r1-app.yml")]
phases: dict[str, list[float]] = {}


def mark(name: str, start: float) -> None:
    phases[name] = [start, time.time()]
    (RES / "r1_phases.json").write_text(json.dumps(phases, indent=1))
    print(f"{name}: {phases[name][1] - start:.0f} s", flush=True)


def snap(tag: str) -> None:
    subprocess.run([PY, str(BENCH / "scripts" / "sample.py"), "snapshot", "--out", str(RES / f"r1_snap_{tag}.json")],
                   check=False)


def kb_chunks() -> int:
    out = subprocess.run(["docker", "exec", "kxbench-postgres-1", "psql", "-U", "postgres", "-d", "agents", "-At", "-c",
                          "select count(*) from agents.kb_chunks"], capture_output=True, text=True, check=False).stdout
    try:
        return int(out.strip() or 0)
    except ValueError:
        return -1


def pending_index_jobs() -> int:
    out = subprocess.run(["docker", "exec", "kxbench-postgres-1", "psql", "-U", "postgres", "-d", "automation", "-At",
                          "-c", "select count(*) from automation.scheduled_jobs where task='agents.handle_event' "
                          "and status in ('todo','running')"], capture_output=True, text=True, check=False).stdout
    try:
        return int(out.strip() or 0)
    except ValueError:
        return -1


def first_index_job_ts(after: float) -> float:
    out = subprocess.run(["docker", "exec", "kxbench-postgres-1", "psql", "-U", "postgres", "-d", "automation", "-At",
                          "-c", "select extract(epoch from min(created_at)) from automation.scheduled_jobs "
                          f"where task='agents.handle_event' and created_at > to_timestamp({after})"], capture_output=True, text=True, check=False).stdout
    return float(out.strip() or time.time())


def load(minutes: float, tag: str) -> None:
    subprocess.run([PY, str(BENCH / "scripts" / "load10.py"), "run", "--minutes", str(minutes),
                    "--out", str(RES / f"r1_load_{tag}.json")], check=False, stdout=subprocess.DEVNULL)


def main() -> None:
    resume = "--resume-b" in sys.argv or "--from-c" in sys.argv
    if resume:
        phases.update(json.loads((RES / "r1_phases.json").read_text()))
    else:
        snap("0_start")
    if "--from-c" in sys.argv:  # rerun only the two load phases (A and B already measured)
        time.sleep(120)  # let the recreated api/worker settle
        snap("2_after_cms")
        return load_phases()
    if not resume:
        t = time.time()
        time.sleep(180)
        mark("A_idle", t)
        snap("1_after_idle")
        subprocess.run([PY, str(BENCH / "scripts" / "load10.py"), "cms", "--products", "100"], check=False,
                       stdout=subprocess.DEVNULL)
    t = first_index_job_ts(phases["A_idle"][1])
    idle_polls = 0
    while idle_polls < 2 and time.time() - t < 7200:  # until no re-index job is queued or running (twice)
        time.sleep(15)
        idle_polls = idle_polls + 1 if pending_index_jobs() == 0 else 0
    mark("B_cms_index", t)
    phases["B_kb_chunks"] = [float(kb_chunks()), 0.0]
    snap("2_after_cms")
    load_phases()


def load_phases() -> None:
    t = time.time()
    load(15, "c10")
    mark("C_load_worker10", t)
    snap("3_after_load_c10")

    t = time.time()
    env = dict(os.environ, WORKER_CONCURRENCY="3")
    subprocess.run([*COMPOSE, "up", "-d", "--force-recreate", "--no-deps", "worker"], env=env, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(90)
    mark("D0_idle_worker3", t)
    t = time.time()
    load(10, "c3")
    mark("D_load_worker3", t)
    snap("4_after_load_c3")
    print("done", flush=True)


if __name__ == "__main__":
    main()
