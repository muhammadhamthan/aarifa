"""CloudWatch metrics as Embedded Metric Format (EMF) log lines: no SDK, no extra API calls.

ECS ships stdout to CloudWatch Logs, which turns every EMF line into a metric in the `Kairox` namespace.
`name` is the bare metric name (the namespace is added here). The alarms in infra (observability module)
watch, with the dimension set exactly `[Company]`:
* `QueueLagSeconds` — age of the oldest due job (the schedule table, D25);
* `VendorSwitch` — a field fell back to its next vendor (Fallback plan: "every switch writes a metric and
  pages the on-call engineer");
* `SpendCap` — a daily spend cap crossed 80 % or 100 % (D34; the value is the percentage).
The collector also ships these log lines to Better Stack. They carry no buyer data.
"""

from __future__ import annotations

import sys
import time
from typing import Final, Literal

import orjson

NAMESPACE: Final = "Kairox"
Unit = Literal["Count", "Seconds", "Milliseconds", "None", "Percent"]


def emf_line(name: str, value: float, *, company: str, unit: Unit = "Count", **properties: str) -> bytes:
    """One EMF document. Extra `properties` (e.g. field, vendor) are searchable but not dimensions."""
    doc = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": [["Company"]],
                    "Metrics": [{"Name": name, "Unit": unit}],
                }
            ],
        },
        "Company": company,
        name: value,
        **properties,
    }
    return orjson.dumps(doc)


def emit_metric(name: str, value: float, *, company: str, unit: Unit = "Count", **properties: str) -> None:
    sys.stdout.buffer.write(emf_line(name, value, company=company, unit=unit, **properties) + b"\n")
    sys.stdout.flush()
