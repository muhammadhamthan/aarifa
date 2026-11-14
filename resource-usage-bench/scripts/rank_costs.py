"""Monthly cost of each free platform on AWS in the UAE, computed only from measured footprints and AWS's official
price list, next to the paid service it would replace (prices from the repo's Recommendation.md / Options by field).

Sizing rule (stated in RANK.md): the smallest valid Fargate task whose memory >= the measured peak RAM and whose vCPU
>= the measured average CPU under the test load. GPU work: the smallest UAE GPU instance. 730 hours a month.
Prices: results/aws_fargate_me-central-1_prices.json (x86, because the measured images were amd64) and
results/aws_ec2_me-central-1_prices.json.
  python rank_costs.py -> results/rank_costs.json
"""

from __future__ import annotations

import json
from pathlib import Path

RES = Path(__file__).resolve().parents[1] / "results"
FG = json.loads((RES / "aws_fargate_me-central-1_prices.json").read_text())["on_demand_usd"]
EC2 = json.loads((RES / "aws_ec2_me-central-1_prices.json").read_text(encoding="utf-8"))["prices"]
H = 730

# Fargate's valid (vCPU, memory GB) combinations (AWS ECS task-size table)
SIZES = ([(0.25, m) for m in (0.5, 1, 2)] + [(0.5, m) for m in (1, 2, 3, 4)] + [(1, m) for m in range(2, 9)]
         + [(2, m) for m in range(4, 17)] + [(4, m) for m in range(8, 31)] + [(8, m) for m in range(16, 61, 4)])


def fargate(peak_mib: float, avg_cores: float) -> dict[str, float]:
    cpu, mem = next((c, m) for c, m in SIZES if m * 1024 >= peak_mib and c >= avg_cores)
    usd = (cpu * FG["x86_vcpu_hour"] + mem * FG["x86_gb_hour"]) * H
    return {"vcpu": cpu, "memory_gb": mem, "usd_month": round(usd, 2)}


def ec2(name: str) -> dict[str, object]:
    p = EC2[name]
    return {"instance": name, "vcpu": p["vcpu"], "memory": p["memory"], "gpu_memory": p["gpu_memory"],
            "usd_hour": float(p["usd_per_hour"]), "usd_month": float(p["usd_per_month_730h"])}


# measured: peak RAM MiB, average cores under the test load (results/*.json after the dedupe fix)
MEASURED = {
    "haraka": (127.0, 0.18), "stalwart": (228.1, 0.22), "postal": (652.0, 0.71),
    "asterisk": (52.3, 0.05), "freeswitch": (50.3, 0.03),
    "crowdsec": (158.7, 0.91), "bunkerweb": (472.1, 0.05), "safeline": (1639.4, 0.03),
    "phoenix": (510.1, 0.05), "langfuse": (2549.0, 0.22), "opik": (3322.7, 1.43),
    # embeddings: peak RAM while indexing (R4); CPU while indexing used 6-16 threads, the plan's container has 1 vCPU
    "qwen3_embedding": (3174.4, 1.0), "bge_m3": (4945.9, 1.0), "e5_large": (4874.2, 1.0),
}
# Langfuse: idle reading 2.49 GB was higher than its run peak (2.19 GB), so its larger idle value is used

# paid lines, US$ a month at Light / Normal / Heavy (Recommendation.md + Options by field.md, prices of 7-11 Nov)
PAID = {
    "stt_deepgram": [14, 69, 230], "tts_elevenlabs": [27, 135, 450], "voice_livekit_cloud": [50, 75, 250],
    "calls_telnyx": [10, 46, 152], "email_ses_postmark": [17, 19, 22], "waf_aws_turnstile": [10, 11, 14],
    "tracing_langfuse_cloud_core": [29, 47, 105], "tracing_phoenix_plan": [19, 19, 19],
    "embeddings_qwen3_plan": [44, 44, 44], "embeddings_gemini_api": [0.28, 1.30, 4.20],
}
CALL_MINUTES = [36, 180, 600]  # Recommendation.md usage table (staff alert / call-back minutes)
TELNYX_VOICE_API_PER_MIN = 0.002  # Options by field.md: $0.25/min UAE mobile + $0.002 Voice API


def main() -> None:
    out: dict[str, object] = {"rule": __doc__.split("Sizing rule (stated in RANK.md): ")[1].split("\n730")[0]}
    out["fargate"] = {k: {"peak_mib": v[0], "avg_cores": v[1], **fargate(*v)} for k, v in MEASURED.items()}
    out["fargate"]["langfuse"]["note"] = "sized on its idle reading 2.49 GB (larger than the run peak)"
    out["fargate"]["embeddings_arm_plan_size"] = {
        "qwen3_1vcpu_4gb_arm": round((1 * FG["arm_vcpu_hour"] + 4 * FG["arm_gb_hour"]) * H, 2),
        "bge_m3_or_e5_1vcpu_8gb_arm": round((1 * FG["arm_vcpu_hour"] + 8 * FG["arm_gb_hour"]) * H, 2)}
    out["gpu"] = {k: ec2(k) for k in ("g6.xlarge", "g6.2xlarge", "g5.xlarge")}
    voice_paid = [a + b + c for a, b, c in zip(PAID["stt_deepgram"], PAID["tts_elevenlabs"],
                                                PAID["voice_livekit_cloud"])]
    out["voice"] = {
        "paid_stt_tts_transport": voice_paid,
        "free_on_g6.xlarge_saving": [round(p - out["gpu"]["g6.xlarge"]["usd_month"], 2) for p in voice_paid],  # type: ignore[index]
        "free_on_g6.2xlarge_saving": [round(p - out["gpu"]["g6.2xlarge"]["usd_month"], 2) for p in voice_paid],  # type: ignore[index]
    }
    # Piper on CPU: 0.43 core per voice speaking at the same moment (R3: 67.21 cpu-s for 157.2 s of speech)
    piper_core_per_stream = round(67.21 / 157.2, 3)
    # RAM measured at two points only: 4 voices loaded, single jobs (397.8 MiB) and 10 parallel jobs (2,067 MiB)
    out["piper"] = {"core_per_simultaneous_stream": piper_core_per_stream,
                    "1_stream": fargate(397.8, piper_core_per_stream * 1),
                    "10_streams": fargate(2067.0, piper_core_per_stream * 10)}
    out["calls"] = {"telnyx_voice_api_part": [round(m * TELNYX_VOICE_API_PER_MIN, 2) for m in CALL_MINUTES],
                    "telnyx_total": PAID["calls_telnyx"]}
    out["paid"] = PAID
    f = out["fargate"]  # type: ignore[assignment]
    g6 = out["gpu"]["g6.xlarge"]["usd_month"]  # type: ignore[index]

    def saving(paid: list[float], free: float | list[float]) -> list[float]:
        fl = free if isinstance(free, list) else [free] * 3
        return [round(p - x, 2) for p, x in zip(paid, fl)]

    rows = {
        "voice chain (STT+TTS+transport) on g6.xlarge": saving(voice_paid, g6),
        # Asterisk replaces only Telnyx's $0.002/min Voice API; phone-line minutes stay paid (SIP trunk)
        "calls: Asterisk instead of Telnyx Voice API": saving(out["calls"]["telnyx_voice_api_part"],  # type: ignore[index]
                                                              f["asterisk"]["usd_month"]),
        "email: Postal (send) + Stalwart (replies) instead of SES + Postmark": saving(
            PAID["email_ses_postmark"], f["postal"]["usd_month"] + f["stalwart"]["usd_month"]),
        "WAF: SafeLine instead of AWS WAF + Turnstile": saving(PAID["waf_aws_turnstile"], f["safeline"]["usd_month"]),
        "WAF: BunkerWeb instead of AWS WAF + Turnstile": saving(PAID["waf_aws_turnstile"], f["bunkerweb"]["usd_month"]),
        "tracing: Phoenix (plan) vs Langfuse Cloud Core": saving(PAID["tracing_langfuse_cloud_core"],
                                                                PAID["tracing_phoenix_plan"]),
        "embeddings: BGE-M3 (1 vCPU + 8 GB ARM) vs Qwen3 plan container": saving(
            PAID["embeddings_qwen3_plan"], out["fargate"]["embeddings_arm_plan_size"]["bge_m3_or_e5_1vcpu_8gb_arm"]),  # type: ignore[index]
    }
    total_free_everything = [round(sum(rows[k][i] for k in (
        "voice chain (STT+TTS+transport) on g6.xlarge", "calls: Asterisk instead of Telnyx Voice API",
        "email: Postal (send) + Stalwart (replies) instead of SES + Postmark",
        "WAF: SafeLine instead of AWS WAF + Turnstile")), 2) for i in range(3)]
    out["saving_per_month_light_normal_heavy"] = rows
    out["saving_if_all_four_replaced"] = total_free_everything
    (RES / "rank_costs.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
