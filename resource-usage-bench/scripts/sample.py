"""Measurement helper for the resource-usage bench (read-only towards the app).

  python sample.py stream   --project kxbench --out r1_stats.csv --seconds 1200
  python sample.py snapshot --project kxbench --out r1_before.json [--pg-port 25432]
  python sample.py summary  --csv r1_stats.csv --idle-until <epoch> --busy-from <epoch> --busy-until <epoch>

`stream` records `docker stats` (CPU, RAM, network, disk I/O, processes) for every container of the project
about once a second. `snapshot` records table sizes and row counts in every database, live Postgres
connections, Redis memory, image and volume sizes, and the fake vendors' byte counters.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

UNITS = {"b": 1, "kb": 1e3, "kib": 1024, "mb": 1e6, "mib": 1024**2, "gb": 1e9, "gib": 1024**3, "tb": 1e12}


def to_bytes(text: str) -> float:
    m = re.match(r"\s*([\d.]+)\s*([a-zA-Z]*)", text)
    if not m:
        return 0.0
    return float(m.group(1)) * UNITS.get(m.group(2).lower() or "b", 1)


def pair(text: str) -> tuple[float, float]:
    a, _, b = text.partition("/")
    return to_bytes(a), to_bytes(b)


def stream(project: str, out: Path, seconds: float) -> None:
    proc = subprocess.Popen(  # noqa: S603
        ["docker", "stats", "--format", "{{json .}}"],  # noqa: S607
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    end = time.time() + seconds
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "container", "cpu_pct", "mem_bytes", "net_rx", "net_tx", "blk_read", "blk_write", "pids"])
        assert proc.stdout is not None
        for line in proc.stdout:
            line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line[line.index("{") :])
            except ValueError:
                continue
            name = row.get("Name", "")
            if not name.startswith(project + "-"):
                continue
            mem, _ = pair(row.get("MemUsage", "0B / 0B"))
            rx, tx = pair(row.get("NetIO", "0B / 0B"))
            br, bw = pair(row.get("BlockIO", "0B / 0B"))
            w.writerow(
                [f"{time.time():.1f}", name.removeprefix(project + "-").removesuffix("-1"),
                 row.get("CPUPerc", "0%").rstrip("%"), int(mem), int(rx), int(tx), int(br), int(bw), row.get("PIDs", "")]
            )
            f.flush()
            if time.time() > end:
                break
    proc.terminate()


def psql(container: str, db: str, sql: str) -> list[list[str]]:
    out = subprocess.run(  # noqa: S603
        ["docker", "exec", container, "psql", "-U", "postgres", "-d", db, "-At", "-F", "\t", "-c", sql],  # noqa: S607
        capture_output=True, text=True, encoding="utf-8", check=False,
    ).stdout
    return [line.split("\t") for line in out.splitlines() if line.strip()]


def snapshot(project: str, out: Path) -> None:
    pg = f"{project}-postgres-1"
    snap: dict[str, Any] = {"ts": time.time(), "databases": {}, "tables": {}}
    for name, size in psql(pg, "postgres", "select datname, pg_database_size(datname) from pg_database "
                           "where not datistemplate"):
        snap["databases"][name] = int(size)
    for db in snap["databases"]:
        if db == "postgres":
            continue
        psql(pg, db, "analyze")
        rows = psql(pg, db, "select schemaname||'.'||relname, n_live_tup, pg_total_relation_size(relid), "
                    "pg_relation_size(relid) from pg_stat_user_tables")
        snap["tables"][db] = {r[0]: {"rows": int(r[1]), "total_bytes": int(r[2]), "heap_bytes": int(r[3])} for r in rows}
    act = psql(pg, "postgres", "select coalesce(usename,'(bg)'), count(*) from pg_stat_activity group by 1")
    snap["pg_connections"] = {r[0]: int(r[1]) for r in act}
    snap["pg_settings"] = dict(psql(pg, "postgres", "select name, setting from pg_settings where name in "
                                    "('shared_buffers','max_connections','work_mem')"))
    info = subprocess.run(["docker", "exec", f"{project}-redis-1", "redis-cli", "info", "memory"],  # noqa: S607
                          capture_output=True, text=True, check=False).stdout
    snap["redis"] = {k: v for k, _, v in (line.partition(":") for line in info.splitlines()) if k in
                     ("used_memory", "used_memory_rss", "used_memory_peak")}
    df = subprocess.run(["docker", "system", "df", "-v", "--format", "{{json .}}"],  # noqa: S607
                        capture_output=True, text=True, check=False).stdout
    try:
        dfj = json.loads(df)
        snap["images"] = {i["Repository"] + ":" + i["Tag"]: i["Size"] for i in dfj.get("Images", [])}
        snap["volumes"] = {v["Name"]: v["Size"] for v in dfj.get("Volumes", []) if v["Name"].startswith(project)}
    except (ValueError, KeyError):
        snap["images_raw"] = df[:2000]
    try:
        with urllib.request.urlopen("http://localhost:29000/_stats", timeout=5) as r:  # noqa: S310
            snap["fakes"] = json.loads(r.read())
    except OSError as exc:
        snap["fakes"] = {"error": str(exc)}
    out.write_text(json.dumps(snap, indent=1))
    print(f"snapshot -> {out}")


def summary(csv_path: Path, idle: tuple[float, float], busy: tuple[float, float]) -> dict[str, Any]:
    rows: dict[str, list[dict[str, float]]] = {}
    with csv_path.open() as f:
        for r in csv.DictReader(f):
            rows.setdefault(r["container"], []).append({k: float(r[k] or 0) for k in r if k != "container"})
    out: dict[str, Any] = {}
    for name, rs in sorted(rows.items()):
        def window(lo: float, hi: float, rs: list[dict[str, float]] = rs) -> list[dict[str, float]]:
            return [r for r in rs if lo <= r["ts"] <= hi]
        i, b = window(*idle), window(*busy)
        mib = 1024 * 1024
        out[name] = {
            "idle_mem_mib": round(sum(r["mem_bytes"] for r in i) / len(i) / mib, 1) if i else None,
            "busy_mem_avg_mib": round(sum(r["mem_bytes"] for r in b) / len(b) / mib, 1) if b else None,
            "busy_mem_peak_mib": round(max(r["mem_bytes"] for r in b) / mib, 1) if b else None,
            "idle_cpu_avg_pct": round(sum(r["cpu_pct"] for r in i) / len(i), 2) if i else None,
            "busy_cpu_avg_pct": round(sum(r["cpu_pct"] for r in b) / len(b), 2) if b else None,
            "busy_cpu_peak_pct": round(max(r["cpu_pct"] for r in b), 1) if b else None,
            "busy_net_rx_mb": round((b[-1]["net_rx"] - b[0]["net_rx"]) / 1e6, 2) if len(b) > 1 else None,
            "busy_net_tx_mb": round((b[-1]["net_tx"] - b[0]["net_tx"]) / 1e6, 2) if len(b) > 1 else None,
            "busy_disk_write_mb": round((b[-1]["blk_write"] - b[0]["blk_write"]) / 1e6, 2) if len(b) > 1 else None,
            "pids_peak": int(max(r["pids"] for r in b)) if b else None,
        }
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stream")
    s.add_argument("--project", default="kxbench")
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--seconds", type=float, default=1200)
    n = sub.add_parser("snapshot")
    n.add_argument("--project", default="kxbench")
    n.add_argument("--out", type=Path, required=True)
    m = sub.add_parser("summary")
    m.add_argument("--csv", type=Path, required=True)
    m.add_argument("--idle", type=float, nargs=2, required=True)
    m.add_argument("--busy", type=float, nargs=2, required=True)
    m.add_argument("--out", type=Path)
    a = p.parse_args()
    if a.cmd == "stream":
        stream(a.project, a.out, a.seconds)
    elif a.cmd == "snapshot":
        snapshot(a.project, a.out)
    else:
        result = summary(a.csv, tuple(a.idle), tuple(a.busy))
        text = json.dumps(result, indent=1)
        if a.out:
            a.out.write_text(text)
        sys.stdout.write(text + "\n")


if __name__ == "__main__":
    main()
