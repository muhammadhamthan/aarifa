"""Monthly usage and cost at 50 / 100 / 150 users a day, for three ways of running the platform.

ONE PLANNING RULE (the only assumption, stated in the guide and easy to change below):
  1 user a day = 1 buyer conversation of TURNS_PER_USER AI turns. Voice minutes, WhatsApp messages, emails and
  staff-call minutes scale with AI turns by the ratios of the repo's own "Light" usage level
  (Recommendation.md: 14,000 turns, 1,500 voice min, 3,900 WhatsApp, 2,000 / 500 emails, 36 call min a month).

Unit prices come from the repo's price docs (Recommendation.md, Options by field.md, vendor pages checked 7-11 Nov
2026); Namecheap list prices as indexed from namecheap.com configure pages on 14 Nov 2026 (Namecheap blocks
automated reading of its pages: verify live before buying); Coolify from coolify.io/pricing (14 Nov 2026).
  python users_per_day_costs.py -> results/users_per_day_costs.json
"""

from __future__ import annotations

import json
from pathlib import Path

RES = Path(__file__).resolve().parents[1] / "results"
DAYS = 30
TURNS_PER_USER = 10  # <- the planning rule; change it and rerun

LIGHT = {"turns": 14_000, "voice_min": 1_500, "whatsapp": 3_900, "email_out": 2_000, "email_in": 500,
         "call_min": 36}  # Recommendation.md "Light"
RATIO = {k: v / LIGHT["turns"] for k, v in LIGHT.items() if k != "turns"}

# --- vendor unit prices (repo price docs) -----------------------------------------------------------------
AI_PER_TURN = 49 / 14_000  # Sonnet on 40 % money turns + Haiku on the rest = $49 at 14k turns (linear: 228 / 65k)
DEEPGRAM_PER_MIN = 0.0092
ELEVENLABS_PER_MIN = 27 / 1_500  # $0.04 per 1,000 characters; the repo's line is $27 for 1,500 voice minutes
LIVEKIT_BASE, LIVEKIT_INCL_MIN, LIVEKIT_PER_MIN = 50.0, 5_000, 0.01
TELNYX_FIXED, TELNYX_PER_MIN = 1.0, 0.25  # fits the repo's lines exactly: 10 @ 36 min, 46 @ 180, 152 @ 600
TELNYX_VOICE_API_PER_MIN = 0.002  # the only part a self-hosted Asterisk replaces
WA_LIGHT, WA_NORMAL = (3_900, 26.0), (19_500, 213.0)  # Meta fees; 1,000 free service messages per number
SES_PER_EMAIL, POSTMARK_PRO = 0.16 / 1000, 16.50
VERCEL_PRO = 20.0
AWS_WAF_LIGHT = 10.0

# --- AWS-hosted plan (Recommendation.md lines, Light sizes and Normal sizes) ------------------------------
AWS_HOSTING_LIGHT = {"servers (Fargate)": 150, "database (RDS Multi-AZ)": 139, "cache (ElastiCache)": 29,
                     "website + CRM (Vercel Pro)": 20, "knowledge search container": 44, "AI tracing (Phoenix)": 19,
                     "monitoring (Better Stack)": 64, "DB monitoring": 18, "secrets": 8,
                     "AWS network, logs, storage": 148, "DNS": 2, "3D/video storage (R2)": 1, "quote PDFs": 7,
                     "AWS Business Support+": 52, "firewall (AWS WAF + Turnstile)": 10}
AWS_HOSTING_NORMAL = dict(AWS_HOSTING_LIGHT, **{"database (RDS Multi-AZ)": 312, "cache (ElastiCache)": 58,
                                                "website + CRM (Vercel Pro)": 40, "AWS network, logs, storage": 182,
                                                "3D/video storage (R2)": 2, "AWS Business Support+": 73,
                                                "firewall (AWS WAF + Turnstile)": 11})

# --- Namecheap dedicated servers (Phoenix, Arizona, USA; list monthly prices as indexed 14 Nov 2026) --------
NAMECHEAP = {
    "Xeon E-2236 (6 cores / 12 threads), 32 GB, 2 x 480 GB SSD": 79.88,
    "Xeon E-2236, 64 GB, 2 x 960 GB SSD (SATA)": 83.88,
    "Xeon E-2236, 64 GB, 2 x 960 GB SSD (NVMe)": 94.88,
    "Dual Xeon Silver 4208, 64 GB, 2 x 1 TB NVMe": 153.88,
    "Dual Xeon Gold 5218, 128 GB, 4 x 1 TB NVMe": 229.88,
}
NAMECHEAP_PICK = "Xeon E-2236, 64 GB, 2 x 960 GB SSD (SATA)"
COOLIFY_SELF_HOSTED, COOLIFY_CLOUD = 0.0, 5.0  # coolify.io/pricing: self-hosted free; Cloud $5 incl. 2 servers

# measured footprint of everything that would run on the one server (GiB, peak; results/*)
FOOTPRINT_GIB = {
    "app: api + worker (concurrency 3) + beat + Postgres + Redis + Qwen3 embeddings (R1)": 4.5,
    "voice worker, platform code, 10 calls (R2b)": 4.21,
    "Phoenix traces (R5)": 0.51,
    "Postal + MariaDB, sending (R6)": 0.65,
    "Stalwart mailbox (R6)": 0.23,
    "Asterisk, 10 calls (R7)": 0.05,
    "SafeLine firewall (R8)": 1.60,
    "CrowdSec (R8)": 0.16,
    "Coolify (its docs: minimum 2 GB)": 2.0,
}
FOOTPRINT_EST_GIB = {"CMS + 2 Next.js sites (not measured, estimate)": 1.0, "OS (not measured, estimate)": 0.8}

GPU_G6_XLARGE = 721.42  # results/aws_ec2_me-central-1_prices.json: $0.98824/h x 730 h

# per-stack server footprint (GiB at peak, measured unless marked E); R1 peaks: api 0.80, worker(c=3) 0.90,
# beat 0.08, Postgres 0.42, Redis 0.01; R4 BGE-M3 4.83; R5 Phoenix 0.51; R2b voice worker 4.21; R2 LiveKit 0.09;
# R3 Piper 10 jobs 2.07; R8 SafeLine 1.60 (1,639 MiB peak), CrowdSec 0.16; R6 Postal 0.65, Stalwart 0.23; R7 Asterisk 0.05
STACK_RAM_GIB = {
    "hybrid": {"app (api + worker c=3 + beat) + PostgreSQL + Redis": 2.21, "BGE-M3 embeddings": 4.83, "Phoenix": 0.51,
               "voice worker (10 calls)": 4.21, "LiveKit server": 0.09, "Piper (10 voices at once)": 2.07,
               "SafeLine": 1.60, "CrowdSec": 0.16, "Coolify (docs minimum)": 2.0, "CMS + websites (E)": 1.0, "OS (E)": 0.8},
}
STACK_RAM_GIB["free"] = dict(STACK_RAM_GIB["hybrid"], **{"Postal + MariaDB": 0.65, "Stalwart": 0.23, "Asterisk": 0.05})
# disk the images and models need on the server (GB, measured image sizes)
STACK_DISK_GB = {
    "hybrid": {"app image (with Chromium)": 2.68, "pgvector": 0.63, "redis": 0.20, "TEI": 0.94, "BGE-M3 ONNX model": 2.3,
               "Phoenix": 1.44, "LiveKit server": 0.13, "Piper 4 voices": 0.25, "SafeLine (7 images)": 2.34,
               "CrowdSec": 0.48},
}
STACK_DISK_GB["free"] = dict(STACK_DISK_GB["hybrid"], **{"Postal + MariaDB": 2.37, "Stalwart": 0.47, "Asterisk": 0.49})


def wa_cost(msgs: float) -> float:
    (n1, c1), (n2, c2) = WA_LIGHT, WA_NORMAL
    return round(c1 + (msgs - n1) * (c2 - c1) / (n2 - n1), 2)


def usage(users: int) -> dict[str, float]:
    turns = users * TURNS_PER_USER * DAYS
    return {"turns": turns, **{k: round(turns * r, 1) for k, r in RATIO.items()}}


def vendors(u: dict[str, float]) -> dict[str, float]:
    return {
        "AI replies (Claude Sonnet + Haiku)": round(u["turns"] * AI_PER_TURN, 2),
        "WhatsApp (Meta)": wa_cost(u["whatsapp"]),
        "speech-to-text (Deepgram)": round(u["voice_min"] * DEEPGRAM_PER_MIN, 2),
        "text-to-speech (ElevenLabs)": round(u["voice_min"] * ELEVENLABS_PER_MIN, 2),
        "voice transport (LiveKit Cloud Ship)": round(LIVEKIT_BASE + max(0, u["voice_min"] - LIVEKIT_INCL_MIN)
                                                      * LIVEKIT_PER_MIN, 2),
        "staff alert calls (Telnyx)": round(TELNYX_FIXED + u["call_min"] * TELNYX_PER_MIN, 2),
        "email (SES + Postmark Pro)": round(POSTMARK_PRO + u["email_out"] * SES_PER_EMAIL, 2),
    }


def main() -> None:
    out: dict[str, object] = {"rule": f"1 user a day = 1 conversation of {TURNS_PER_USER} AI turns; other channels "
                                      "scale by Recommendation.md Light ratios", "ratios_per_turn": RATIO,
                              "namecheap_plans": NAMECHEAP, "namecheap_pick": NAMECHEAP_PICK,
                              "footprint_measured_gib": FOOTPRINT_GIB, "footprint_estimated_gib": FOOTPRINT_EST_GIB,
                              "footprint_total_gib": round(sum(FOOTPRINT_GIB.values()) + sum(FOOTPRINT_EST_GIB.values()), 2)}
    rows = {}
    for users in (50, 100, 150):
        u = usage(users)
        v = vendors(u)
        aws_light = sum(AWS_HOSTING_LIGHT.values())
        aws_normal = sum(AWS_HOSTING_NORMAL.values())
        # A: the 11 Nov plan (AWS + all paid vendors)
        a_low, a_high = round(aws_light + sum(v.values()), 2), round(aws_normal + sum(v.values()), 2)
        # B: Namecheap dedicated server + Coolify; self-host app, DB, cache, embeddings, Phoenix, SafeLine, CrowdSec,
        #    Asterisk; keep paid: Claude, WhatsApp, voice vendors (Namecheap has no GPU), phone minutes, SES + Postmark,
        #    Vercel Pro for the two websites
        b_items = {"Namecheap dedicated server": NAMECHEAP[NAMECHEAP_PICK], "Coolify (self-hosted)": COOLIFY_SELF_HOSTED,
                   **{k: x for k, x in v.items() if "Telnyx" not in k},
                   "phone minutes (SIP trunk at Telnyx rates; Asterisk replaces only the Voice API part)":
                       round(v["staff alert calls (Telnyx)"] - u["call_min"] * TELNYX_VOICE_API_PER_MIN, 2),
                   "website + CRM (Vercel Pro)": VERCEL_PRO}
        # C: like B but email also self-hosted (Postal + Stalwart on the same server: no SES / Postmark bill)
        c_items = {k: x for k, x in b_items.items() if not k.startswith("email")}
        # --- the three stacks compared in the guide's last chapter -------------------------------------------
        # HYBRID (recommended on measured performance): one Namecheap server + Coolify runs the app, PostgreSQL,
        # Redis, BGE-M3, Phoenix, SafeLine + CrowdSec, the LiveKit server and Piper; paid: Claude, WhatsApp,
        # Deepgram (no GPU at Namecheap), Telnyx, SES + Postmark, Vercel.
        hybrid = {"Namecheap dedicated server (64 GB)": NAMECHEAP[NAMECHEAP_PICK], "Coolify (self-hosted)": 0.0,
                  "AI replies (Claude)": v["AI replies (Claude Sonnet + Haiku)"], "WhatsApp (Meta)": v["WhatsApp (Meta)"],
                  "speech-to-text (Deepgram)": v["speech-to-text (Deepgram)"],
                  "text-to-speech (Piper, self-hosted)": 0.0, "voice transport (LiveKit server, self-hosted)": 0.0,
                  "staff alert calls (Telnyx)": v["staff alert calls (Telnyx)"],
                  "email (SES + Postmark)": v["email (SES + Postmark Pro)"], "website + CRM (Vercel Pro)": VERCEL_PRO}
        # FREE (as much as possible): HYBRID + Postal/Stalwart for email, Asterisk instead of Telnyx's Voice API, and
        # the free speech-to-text on a GPU server (Namecheap has none; cheapest UAE GPU with an official price: AWS)
        free = {"Namecheap dedicated server (64 GB)": NAMECHEAP[NAMECHEAP_PICK], "Coolify (self-hosted)": 0.0,
                "GPU server for faster-whisper (AWS g6.xlarge, UAE)": GPU_G6_XLARGE,
                "AI replies (Claude)": v["AI replies (Claude Sonnet + Haiku)"], "WhatsApp (Meta)": v["WhatsApp (Meta)"],
                "speech-to-text (faster-whisper)": 0.0, "text-to-speech (Piper)": 0.0, "voice transport (LiveKit)": 0.0,
                "phone minutes (SIP trunk; Asterisk does the rest)":
                    round(v["staff alert calls (Telnyx)"] - u["call_min"] * TELNYX_VOICE_API_PER_MIN, 2),
                "email (Postal + Stalwart)": 0.0, "website + CRM (Vercel Pro)": VERCEL_PRO}
        rows[str(users)] = {
            "usage_per_month": u, "vendor_lines": v,
            "stack_paid": {"total_low": a_low, "total_high": a_high},
            "stack_hybrid": {"items": hybrid, "total": round(sum(hybrid.values()), 2),
                             "elevenlabs_per_min_for_ta_si_calls": round(ELEVENLABS_PER_MIN, 4)},
            "stack_free": {"items": free, "total": round(sum(free.values()), 2)},
            "A_aws_plan_all_paid": {"low_light_sizes": a_low, "high_normal_sizes": a_high,
                                    "hosting_light": aws_light, "hosting_normal": aws_normal},
            "B_namecheap_coolify_paid_voice": {"items": b_items, "total": round(sum(b_items.values()), 2)},
            "C_namecheap_coolify_paid_voice_selfhosted_email": {"items": c_items, "total": round(sum(c_items.values()), 2)},
            "disk_growth_gb_per_month": round(u["turns"] * 17 / 1e6, 2),  # R1: ~17 KB per turn (DB + Phoenix)
        }
    out["by_users_per_day"] = rows
    out["stack_ram_gib"] = {k: {"items": v, "total": round(sum(v.values()), 2)} for k, v in STACK_RAM_GIB.items()}
    out["stack_disk_gb"] = {k: {"items": v, "total": round(sum(v.values()), 2)} for k, v in STACK_DISK_GB.items()}
    (RES / "users_per_day_costs.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    for k, r in rows.items():
        print(k, "users/day:", r["usage_per_month"]["turns"], "turns |",
              "A AWS plan", r["A_aws_plan_all_paid"]["low_light_sizes"], "-", r["A_aws_plan_all_paid"]["high_normal_sizes"],
              "| B Namecheap", r["B_namecheap_coolify_paid_voice"]["total"],
              "| C Namecheap+own email", r["C_namecheap_coolify_paid_voice_selfhosted_email"]["total"])
    print("footprint", out["footprint_total_gib"], "GiB")


if __name__ == "__main__":
    main()
