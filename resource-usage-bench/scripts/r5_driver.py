"""Round 5: Phoenix vs Langfuse vs Opik on the same AI-trace stream (trace_replay.py: spans shaped like
kx/agents/tracing.py). Each platform runs ALONE from its official setup:
  phoenix  - arizephoenix/phoenix on the bench Postgres (as the platform runs it, r1-app.yml)
  langfuse - official docker-compose.yml (F:/kx-bench/repos/langfuse), headless project via LANGFUSE_INIT_*
  opik     - official docker-compose.yaml --profile opik (F:/kx-bench/repos/opik/deployment/docker-compose)
Per platform: start -> ready -> 60 s idle (RAM) -> 1,000 spans at 10/s while docker stats are recorded -> 45 s to
flush -> count what was STORED (the platform's own API / database) -> disk growth of its Docker volumes.

  python r5_driver.py [phoenix langfuse opik]   -> results/r5_<name>.json, r5_<name>_stats.csv, r5_summary.json
"""

from __future__ import annotations

import base64
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

BENCH = Path(__file__).resolve().parents[1]
RES = BENCH / "results"
PY = sys.executable
SPANS, RATE = 1000, 10.0
LF_PK, LF_SK = "pk-lf-kxbench", "sk-lf-kxbench"
LF_AUTH = "Basic " + base64.b64encode(f"{LF_PK}:{LF_SK}".encode()).decode()

STACKS: dict[str, dict[str, Any]] = {
    "phoenix": {
        "project": "kxbench",
        "compose": ["-f", str(BENCH / "compose" / "r1-app.yml")],
        "services": ["postgres", "phoenix"],
        "only": ["phoenix", "postgres"],  # containers of this project that belong to the tracing stack
        "ready": "http://localhost:26006/",
        "endpoint": "http://localhost:26006/v1/traces",
        "headers": [],
        "env": {},
    },
    "langfuse": {
        "project": "kxlangfuse",
        "compose": ["-f", r"F:\kx-bench\repos\langfuse\docker-compose.yml",
                    "-f", str(BENCH / "compose" / "r5-langfuse-override.yml")],  # frees host ports 5432 / 6379
        "services": [],
        "only": None,
        "ready": "http://localhost:3000/api/public/health",
        "endpoint": "http://localhost:3000/api/public/otel/v1/traces",
        "headers": [f"Authorization={LF_AUTH}"],
        "env": {"LANGFUSE_INIT_ORG_ID": "kxbench", "LANGFUSE_INIT_ORG_NAME": "kxbench",
                "LANGFUSE_INIT_PROJECT_ID": "pack", "LANGFUSE_INIT_PROJECT_NAME": "pack",
                "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": LF_PK, "LANGFUSE_INIT_PROJECT_SECRET_KEY": LF_SK,
                "LANGFUSE_INIT_USER_EMAIL": "bench@bench.test", "LANGFUSE_INIT_USER_NAME": "bench",
                "LANGFUSE_INIT_USER_PASSWORD": "bench-password-123"},
    },
    "opik": {
        "project": "kxopik",
        "compose": ["-f", r"F:\kx-bench\repos\opik\deployment\docker-compose\docker-compose.yaml", "--profile", "opik"],
        "services": [],
        "only": None,
        "ready": "http://localhost:5173/api/v1/private/projects",
        "endpoint": "http://localhost:5173/api/v1/private/otel/v1/traces",
        "headers": ["projectName=pack"],
        "env": {},
    },
}


def compose(st: dict[str, Any], *args: str, env: dict[str, str] | None = None, check: bool = False) -> None:
    subprocess.run(["docker", "compose", "-p", st["project"], *st["compose"], *args], env=env, check=check,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def containers(st: dict[str, Any]) -> list[str]:
    out = subprocess.run(["docker", "ps", "--filter", f"label=com.docker.compose.project={st['project']}",
                          "--format", "{{.Names}}"], capture_output=True, text=True, check=False).stdout.split()
    return [c for c in out if st["only"] is None or any(c.endswith(f"-{o}-1") for o in st["only"])]


def ram_now(names: list[str]) -> dict[str, float]:
    out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}", *names],
                         capture_output=True, text=True, check=False).stdout
    res = {}
    for line in out.splitlines():
        name, usage = line.split(" ", 1)
        v = usage.split("/")[0].strip()
        num = float(v.rstrip("GiBMKk") or 0)
        res[name] = round(num * 1024 if "GiB" in v else num / 1024 if "KiB" in v else num, 1)
    return res


def volume_bytes(st: dict[str, Any]) -> dict[str, int]:
    vols = subprocess.run(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={st['project']}"],
                          capture_output=True, text=True, check=False).stdout.split()
    res = {}
    for v in vols:
        if st["only"] is not None and "pgdata" not in v and "phoenix" not in v:
            continue
        out = subprocess.run(["docker", "run", "--rm", "-v", f"{v}:/v:ro", "alpine:3.21", "du", "-sb", "/v"],
                             capture_output=True, text=True, check=False).stdout.split()
        res[v] = int(out[0]) if out else 0
    return res


def stored(name: str) -> Any:
    """How many spans/traces the platform actually kept (not just accepted)."""
    try:
        if name == "phoenix":
            out = subprocess.run(["docker", "exec", "kxbench-postgres-1", "psql", "-U", "postgres", "-d", "phoenix",
                                  "-Atc", "select count(*), pg_database_size('phoenix') from spans"],
                                 capture_output=True, text=True, check=False).stdout.strip()
            spans, db = out.split("|")
            return {"spans": int(spans), "phoenix_db_bytes": int(db)}
        if name == "langfuse":  # count rows in Langfuse's own ClickHouse tables (the public API path moved in v4)
            sql = ("select name, total_rows from system.tables where database = 'default' and "
                   "(name like '%observation%' or name like '%trace%' or name like '%event%' or name like '%span%') "
                   "and engine not like '%View%' format TSV")
            out = subprocess.run(["docker", "exec", "kxlangfuse-clickhouse-1", "clickhouse-client", "--user",
                                  "clickhouse", "--password", "clickhouse", "-q", sql],
                                 capture_output=True, text=True, check=False)
            rows = dict(line.split("\t") for line in out.stdout.strip().splitlines() if "\t" in line)
            return {"clickhouse_rows": {k: int(v) if v.isdigit() else v for k, v in rows.items()},
                    "error": out.stderr.strip()[-200:] or None}
        r = httpx.get("http://localhost:5173/api/v1/private/spans", params={"project_name": "pack", "size": 1}, timeout=30)
        return {"spans": r.json().get("total"), "http": r.status_code}
    except Exception as exc:  # noqa: BLE001 - recorded, not fatal
        return {"error": repr(exc)}


def wait_ready(url: str, limit: float = 900) -> float:
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            if httpx.get(url, timeout=10).status_code < 500:
                return round(time.time() - t0, 1)
        except httpx.HTTPError:
            pass
        time.sleep(3)
    raise TimeoutError(url)


def window(path: Path, names: list[str]) -> dict[str, Any]:
    rows = [r for r in csv.DictReader(path.open()) if r["cpu_pct"].replace(".", "", 1).isdigit()]
    by_ts: dict[str, dict[str, float]] = {}
    seen: set[tuple[str, str]] = set()
    for r in rows:
        # `docker stats` streams every frame twice, so each (time, container) appears twice in sample.py's CSV;
        # summing both doubled every total until 14 Nov. Count each container once per timestamp.
        if (r["ts"], r["container"]) in seen:
            continue
        seen.add((r["ts"], r["container"]))
        if any(r["container"] in n for n in names):
            by_ts.setdefault(r["ts"], {"mem": 0.0, "cpu": 0.0})
            by_ts[r["ts"]]["mem"] += float(r["mem_bytes"])
            by_ts[r["ts"]]["cpu"] += float(r["cpu_pct"])
    if not by_ts:
        return {}
    mem = [v["mem"] for v in by_ts.values()]
    cpu = [v["cpu"] for v in by_ts.values()]
    return {"ram_total_peak_mib": round(max(mem) / 2**20, 1), "ram_total_avg_mib": round(sum(mem) / len(mem) / 2**20, 1),
            "cpu_total_peak_cores": round(max(cpu) / 100, 2), "cpu_total_avg_cores": round(sum(cpu) / len(cpu) / 100, 2)}


def run(name: str) -> dict[str, Any]:
    st = STACKS[name]
    env = dict(os.environ, **st["env"])
    t0 = time.time()
    compose(st, "up", "-d", *st["services"], env=env)
    ready_s = wait_ready(st["ready"])
    time.sleep(60)
    names = containers(st)
    idle = ram_now(names)
    vol_c0 = volume_bytes(st)
    # control: the same length of time with NO spans (ClickHouse / MySQL / ZooKeeper write logs and metrics anyway)
    time.sleep(SPANS / RATE + 45)
    vol0 = volume_bytes(st)
    control_growth = sum(vol0.values()) - sum(vol_c0.values())
    before = stored(name)
    csv_path = RES / f"r5_{name}_stats.csv"
    sampler = subprocess.Popen([PY, str(BENCH / "scripts" / "sample.py"), "stream", "--project", st["project"],
                                "--out", str(csv_path), "--seconds", "3600"])
    replay = subprocess.run([PY, str(BENCH / "scripts" / "trace_replay.py"), "--endpoint", st["endpoint"],
                             *[x for h in st["headers"] for x in ("--header", h)], "--spans", str(SPANS),
                             "--rate", str(RATE)], capture_output=True, text=True, check=False)
    time.sleep(45)  # let the platform write what it accepted
    sampler.terminate()
    sampler.wait(timeout=30)
    after = stored(name)
    vol1 = volume_bytes(st)
    images = subprocess.run(["docker", "compose", "-p", st["project"], *st["compose"], "images", "--format", "json"],
                            capture_output=True, text=True, check=False).stdout
    res = {
        "platform": name, "start_to_ready_s": round(ready_s + 0.0, 1), "wall_from_up_s": round(time.time() - t0, 1),
        "containers": names, "ram_idle_mib": idle, "ram_idle_total_mib": round(sum(idle.values()), 1),
        "load": window(csv_path, names), "replay": replay.stdout.strip()[-400:], "replay_errors": replay.stderr[-800:],
        "stored_before": before, "stored_after": after,
        "volume_bytes_before": vol0, "volume_bytes_after": vol1,
        "disk_growth_bytes": sum(vol1.values()) - sum(vol0.values()),
        "control_growth_bytes_same_duration_no_spans": control_growth,
        "disk_growth_per_1000_spans_mb": round((sum(vol1.values()) - sum(vol0.values()) - control_growth)
                                               / 1e6 * 1000 / SPANS, 2),  # net of the background writes
        "images_json": images[:4000],
    }
    (RES / f"r5_{name}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


def main() -> None:
    names = sys.argv[1:] or list(STACKS)
    summary_path = RES / "r5_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    for name in names:
        try:
            res = run(name)
            summary[name] = {k: res[k] for k in ("start_to_ready_s", "ram_idle_total_mib", "load", "stored_after",
                                                  "disk_growth_per_1000_spans_mb")}
        except Exception as exc:  # noqa: BLE001
            summary[name] = {"error": repr(exc)}
        finally:
            st = STACKS[name]
            if name == "phoenix":
                compose(st, "stop", "phoenix", "postgres")
            else:
                compose(st, "down", "-v")  # bench-only stack: remove its volumes too
        summary_path.write_text(json.dumps(summary, indent=1), encoding="utf-8")
        print(name, json.dumps(summary[name]), flush=True)


if __name__ == "__main__":
    main()
