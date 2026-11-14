"""Turn Round 1 raw data into the measured numbers for the report.

  python analyze_r1.py   (reads results/r1_*.json + r1_stats*.csv, writes results/r1_summary.json)

Per container and phase: RAM (avg / peak), CPU (avg / peak, 100% = one core), network and disk writes.
Per business unit: database bytes per AI turn and per WhatsApp message, Phoenix bytes per model call, quote
PDF bytes, outside-vendor bytes per AI turn (from the fakes' counters).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

RES = Path(__file__).resolve().parents[1] / "results"


def rows() -> list[dict[str, Any]]:
    out = []
    for name in ("r1_stats.csv", "r1_stats_2.csv"):
        p = RES / name
        if p.exists():
            with p.open() as f:
                out += [{**r, **{k: float(r[k] or 0) for k in r if k not in ("container", "pids")}} for r in csv.DictReader(f)]
    return out


def window(data: list[dict[str, Any]], lo: float, hi: float) -> dict[str, Any]:
    by: dict[str, list[dict[str, Any]]] = {}
    for r in data:
        if lo <= r["ts"] <= hi:
            by.setdefault(r["container"], []).append(r)
    mib = 1024 * 1024
    out = {}
    for name, rs in sorted(by.items()):
        rs.sort(key=lambda r: r["ts"])
        out[name] = {
            "ram_avg_mib": round(sum(r["mem_bytes"] for r in rs) / len(rs) / mib, 1),
            "ram_peak_mib": round(max(r["mem_bytes"] for r in rs) / mib, 1),
            "cpu_avg_pct": round(sum(r["cpu_pct"] for r in rs) / len(rs), 1),
            "cpu_peak_pct": round(max(r["cpu_pct"] for r in rs), 1),
            "net_rx_mb": round((rs[-1]["net_rx"] - rs[0]["net_rx"]) / 1e6, 2),
            "net_tx_mb": round((rs[-1]["net_tx"] - rs[0]["net_tx"]) / 1e6, 2),
            "disk_write_mb": round((rs[-1]["blk_write"] - rs[0]["blk_write"]) / 1e6, 1),
            "samples": len(rs),
        }
    return out


def table_delta(a: dict[str, Any], b: dict[str, Any]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for db, tables in b["tables"].items():
        for t, v in tables.items():
            old = a["tables"].get(db, {}).get(t, {"rows": 0, "total_bytes": 0})
            d_rows, d_bytes = v["rows"] - old["rows"], v["total_bytes"] - old["total_bytes"]
            if d_rows or d_bytes:
                out[f"{db}:{t}"] = {"rows": d_rows, "bytes": d_bytes}
    return out


def load_phase(tag: str, a: str, b: str) -> dict[str, Any]:
    rep = json.loads((RES / f"r1_load_{tag}.json").read_text(encoding="utf-8"))
    sa, sb = (json.loads((RES / f"r1_snap_{x}.json").read_text()) for x in (a, b))
    deltas = table_delta(sa, sb)
    acts = rep["actions"]
    turns = acts.get("web.message", {}).get("count", 0) + acts.get("wa.webhook", {}).get("count", 0)
    llm = rep["fakes"]["llm"]
    vendors = rep["fakes"]["vendors"]
    db_bytes = {db: sb["databases"][db] - sa["databases"].get(db, 0) for db in sb["databases"]}
    app_dbs = sum(v for k, v in db_bytes.items() if k in ("crm", "automation", "agents"))
    return {
        "minutes": round((rep["end"] - rep["start"]) / 60, 1),
        "actions": {k: {kk: v[kk] for kk in ("count", "codes", "p50_s", "p95_s", "max_s") if kk in v}
                    for k, v in acts.items() if not k.startswith("_")},
        "buyer_turns": turns,
        "llm_calls": llm.get("calls_haiku", 0) + llm.get("calls_sonnet", 0) + llm.get("calls_other", 0),
        "llm_tokens": {k: llm.get(k) for k in ("input_tokens", "output_tokens", "cache_read_tokens")},
        "vendor_bytes": vendors,
        "s3_bytes_total": rep["fakes"].get("s3_bytes"), "s3_objects": rep["fakes"].get("s3_objects"),
        "db_growth_bytes": db_bytes,
        "app_db_bytes_per_buyer_turn": round(app_dbs / turns) if turns else None,
        "phoenix_bytes_per_llm_call": round(db_bytes.get("phoenix", 0) / max(1, llm.get("calls_haiku", 0) + llm.get("calls_sonnet", 0))),
        "claude_bytes_per_buyer_turn": round((vendors.get("claude", {}).get("bytes_in", 0) + vendors.get("claude", {}).get("bytes_out", 0)) / turns) if turns else None,
        "top_table_growth": dict(sorted(deltas.items(), key=lambda kv: -kv[1]["bytes"])[:15]),
        "pg_connections_after": sb.get("pg_connections"),
        "redis_after": sb.get("redis"),
    }


def main() -> None:
    data = rows()
    phases = json.loads((RES / "r1_phases.json").read_text())
    summary: dict[str, Any] = {"phases": phases, "containers": {}}
    for name, span in phases.items():
        if isinstance(span, list) and len(span) == 2 and span[1] > span[0] > 1e9:
            summary["containers"][name] = window(data, *span)
    if (RES / "r1_load_c10.json").exists():
        summary["load_worker10"] = load_phase("c10", "2_after_cms", "3_after_load_c10")
    if (RES / "r1_load_c3.json").exists():
        summary["load_worker3"] = load_phase("c3", "3_after_load_c10", "4_after_load_c3")
    (RES / "r1_summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    print(json.dumps(summary, indent=1, ensure_ascii=False)[:12000])


if __name__ == "__main__":
    main()
