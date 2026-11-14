"""Recompute every multi-container RAM / CPU window from the raw docker-stats CSVs with the fixed `window()`.

Bug found 14 Nov: `docker stats` streams each frame twice, so sample.py's CSVs hold every (time, container) row
twice, and r5_driver.window() summed both, doubling the RAM and CPU totals of Rounds 5-8. Per-container figures
(Rounds 1-4, analyze_r1.py, r2_driver.py, r4_driver.py) take a container's own average / peak, which duplicates do
not change, so they are unaffected.

Each Sampler CSV covers exactly the measured window (started just before the work, stopped after it), so the
whole file is the window. The old, doubled values are kept beside the new ones as `load_before_dedupe_fix`.

  python recompute_windows.py   -> rewrites results/r5_*.json, r6_*.json, r7_*.json, r8_*.json in place
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from r5_driver import window  # noqa: E402  (the fixed version)

RES = Path(__file__).resolve().parents[1] / "results"

# result file -> list of (key path inside the JSON, stats CSV, container-name parts to leave out)
JOBS: dict[str, list[tuple[tuple[str, ...], str, tuple[str, ...]]]] = {
    "r5_phoenix.json": [(("load",), "r5_phoenix_stats.csv", ())],
    "r5_langfuse.json": [(("load",), "r5_langfuse_stats.csv", ())],
    "r5_opik.json": [(("load",), "r5_opik_stats.csv", ())],
    "r6_haraka.json": [(("load",), "r6_haraka_stats.csv", ())],
    "r6_stalwart.json": [(("load",), "r6_stalwart_stats.csv", ())],
    "r6_postal.json": [(("load",), "r6_postal_stats.csv", ("haraka",))],  # Haraka is only the relay sink
    "r7_asterisk.json": [(("load",), "r7_asterisk_stats.csv", ("sipp",))],  # SIPp is the caller
    "r7_freeswitch.json": [(("load",), "r7_freeswitch_stats.csv", ("sipp",))],
    "r8_crowdsec.json": [(("load",), "r8_crowdsec_stats.csv", ())],
    "r8_bunkerweb.json": [(("direct", "bunkerweb_load"), "r8_bw_direct_stats.csv", ()),
                          (("via_bunkerweb", "bunkerweb_load"), "r8_bw_via_bunkerweb_stats.csv", ())],
    # r8_safeline_proxy.json is not listed: its final run (14 Nov, after the fix) already used the fixed window().
    # Its first run (r8_safeline_proxy_run1_nostaff.json) was recomputed once before being set aside.
}


def names_in(path: Path, leave_out: tuple[str, ...]) -> list[str]:
    names = {r["container"] for r in csv.DictReader(path.open())}
    return sorted(n for n in names if n != "container" and not any(x in n for x in leave_out))


def main() -> None:
    report: dict[str, Any] = {}
    for fname, jobs in JOBS.items():
        f = RES / fname
        if not f.exists():
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        for keys, csv_name, leave_out in jobs:
            src = RES / csv_name
            if not src.exists():
                report[f"{fname}:{'/'.join(keys)}"] = "csv missing"
                continue
            parent = data
            for k in keys[:-1]:
                parent = parent.setdefault(k, {})
            old = parent.get(keys[-1])
            if isinstance(old, dict) and "load_before_dedupe_fix" in parent:
                old = parent["load_before_dedupe_fix"]  # already fixed once: keep the original doubled value
            new = window(src, names_in(src, leave_out))
            parent["load_before_dedupe_fix"] = old
            parent[keys[-1]] = new
            report[f"{fname}:{'/'.join(keys)}"] = {"old": old, "new": new, "containers": names_in(src, leave_out)}
        f.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    out = RES / "recompute_windows_report.json"
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    for k, v in report.items():
        if isinstance(v, dict):
            o, n = v["old"] or {}, v["new"] or {}
            print(f"{k:45s} RAM peak {o.get('ram_total_peak_mib')} -> {n.get('ram_total_peak_mib')} MiB, "
                  f"avg {o.get('ram_total_avg_mib')} -> {n.get('ram_total_avg_mib')}; CPU avg "
                  f"{o.get('cpu_total_avg_cores')} -> {n.get('cpu_total_avg_cores')}, peak "
                  f"{o.get('cpu_total_peak_cores')} -> {n.get('cpu_total_peak_cores')}")
        else:
            print(k, v)


if __name__ == "__main__":
    main()
