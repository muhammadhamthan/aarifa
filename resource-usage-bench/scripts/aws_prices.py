"""Read AWS's official public EC2 price list for the UAE region (me-central-1) and print on-demand Linux prices for
GPU instances (g*, p* families) and the general-purpose sizes a self-hosted free stack would run on.

Source file: https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/20261114012926/me-central-1/index.csv
(AWS Price List bulk API, publication 2026-11-14T01:29:26Z), saved to F:/kx-bench/pricing/.
  python aws_prices.py F:/kx-bench/pricing/ec2_me-central-1_20261114.csv  -> results/aws_ec2_me-central-1_prices.json
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

RES = Path(__file__).resolve().parents[1] / "results"
WANT_GENERAL = {"m7g.xlarge", "m7g.2xlarge", "m7g.4xlarge", "m6g.2xlarge", "m6g.4xlarge", "c7g.2xlarge",
                "t4g.xlarge", "t4g.2xlarge", "m7i.2xlarge", "m6i.2xlarge", "r7g.xlarge", "r7g.2xlarge"}


def main() -> None:
    path = Path(sys.argv[1])
    with path.open(encoding="utf-8", newline="") as f:
        for _ in range(5):  # metadata lines before the header
            next(f)
        rows = csv.DictReader(f)
        out: dict[str, dict[str, str]] = {}
        for r in rows:
            it = r.get("Instance Type", "")
            if not it:
                continue
            if not (it.split(".")[0].startswith(("g", "p")) or it in WANT_GENERAL):
                continue
            if (r.get("TermType") != "OnDemand" or r.get("Operating System") != "Linux"
                    or r.get("Tenancy") != "Shared" or r.get("Pre Installed S/W") != "NA"
                    or r.get("CapacityStatus") != "Used" or r.get("Unit") != "Hrs"):
                continue
            out[it] = {"usd_per_hour": r["PricePerUnit"], "vcpu": r.get("vCPU", ""), "memory": r.get("Memory", ""),
                       "gpu": r.get("GPU", ""), "gpu_memory": r.get("GPU Memory", ""),
                       "usd_per_month_730h": f"{float(r['PricePerUnit']) * 730:.2f}"}
    res = {"source": "AWS Price List bulk API, AmazonEC2 me-central-1, publication 2026-11-14T01:29:26Z",
           "filter": "OnDemand, Linux, Shared tenancy, no pre-installed software, CapacityStatus Used",
           "gpu_instances_found": sorted(k for k in out if k.split(".")[0].startswith(("g", "p"))),
           "prices": dict(sorted(out.items()))}
    (RES / "aws_ec2_me-central-1_prices.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
