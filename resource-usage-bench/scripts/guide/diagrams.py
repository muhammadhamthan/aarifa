"""Draw every workflow diagram and chart used by the Word guide (docs/Kairox_Free_Stack_Guide.docx).

All numbers on the charts are read from results/*.json (measured) or results/users_per_day_costs.json /
rank_costs.json (computed). Run with the doc-building venv (python-docx + matplotlib, outside the project):
  F:/kx-bench/venv/docbuild/Scripts/python.exe scripts/guide/diagrams.py
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

BENCH = Path(__file__).resolve().parents[2]
RES = BENCH / "results"
OUT = BENCH / "docs" / "guide_images"
OUT.mkdir(parents=True, exist_ok=True)

C = {"buyer": "#FFE8B3", "edge": "#FFC7C7", "app": "#CDE7FF", "data": "#D8F5D0", "ai": "#E9D9FF", "out": "#FFD9EC",
     "voice": "#C9F1EE", "grey": "#EEEEEE", "paid": "#FFE0CC"}
INK = "#24324A"


def box(ax, x, y, w, h, text, color, size=10, bold=False):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08", fc=color, ec=INK, lw=1.3))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=size, color=INK,
            fontweight="bold" if bold else "normal", wrap=True)


def arrow(ax, x1, y1, x2, y2, label="", color=INK, style="-|>", size=8.5, curve=0.0):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style, mutation_scale=14, lw=1.4, color=color,
                                 connectionstyle=f"arc3,rad={curve}"))
    if label:
        ax.text((x1 + x2) / 2, (y1 + y2) / 2 + 0.12, label, ha="center", va="bottom", fontsize=size, color="#5A5A5A",
                style="italic")


def canvas(w, h, title):
    fig, ax = plt.subplots(figsize=(w, h), dpi=160)
    ax.set_xlim(0, w)
    ax.set_ylim(0, h)
    ax.axis("off")
    ax.text(w / 2, h - 0.35, title, ha="center", va="center", fontsize=14, fontweight="bold", color=INK)
    return fig, ax


def save(fig, name):
    fig.savefig(OUT / name, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def chain(name, title, steps, notes=None, colors=None, w=13.0, h=3.4):
    """Left-to-right flow of rounded boxes with optional labels under the arrows."""
    fig, ax = canvas(w, h, title)
    n = len(steps)
    gap = 0.35
    bw = (w - 0.4 - gap * (n - 1)) / n
    y, bh = 0.9, 1.6
    size = 9 if bw >= 1.9 else 8
    for i, s in enumerate(steps):
        x = 0.2 + i * (bw + gap)
        box(ax, x, y, bw, bh, s, (colors or [C["app"]] * n)[i], size=size)
        if i < n - 1:
            arrow(ax, x + bw, y + bh / 2, x + bw + gap, y + bh / 2)
    if notes:
        ax.text(w / 2, 0.35, notes, ha="center", va="center", fontsize=9, color="#5A5A5A")
    save(fig, name)


def big_picture():
    w, h = 15, 8.6
    fig, ax = canvas(w, h, "Arifa Pack platform: the five zones (orange = paid outside service, the rest = free software)")
    cols = [
        ("1. BUYERS", C["buyer"], [("Website chat", None), ("Website voice\n(browser mic)", None), ("WhatsApp", None),
                                  ("Email", None)]),
        ("2. FRONT DOOR", C["edge"], [("SafeLine\nweb firewall", None), ("CrowdSec\nlog watcher", None),
                                      ("LiveKit server\nvoice exchange", None), ("Stalwart / Haraka\nmail in", None)]),
        ("3. THE BRAIN", C["app"], [("API (FastAPI)\nCRM, chat, webhooks", None), ("Celery worker\nreplies, PDFs, jobs", None),
                                    ("Celery beat\ntimers (exactly one)", None),
                                    ("Voice worker\nWhisper ears + Piper voice", None)]),
        ("4. HELPERS", C["data"], [("PostgreSQL + pgvector\nall data", None), ("Redis\njob queue, limits", None),
                                   ("TEI embeddings\nknowledge search", None), ("Phoenix\nAI flight recorder", None)]),
        ("5. OUT TO THE WORLD", C["out"], [("Claude\nwrites replies", C["paid"]), ("Meta WhatsApp API", C["paid"]),
                                          ("Postal (or SES)\nmail out", None), ("Asterisk + SIP trunk\nstaff calls", C["paid"])]),
    ]
    cw, gap = 2.5, 0.45
    for ci, (head, color, items) in enumerate(cols):
        x = 0.25 + ci * (cw + gap)
        ax.add_patch(FancyBboxPatch((x - 0.1, 0.75), cw + 0.2, h - 1.9, boxstyle="round,pad=0.02,rounding_size=0.15",
                                    fc="#FAFAFA", ec="#C8C8C8", lw=1))
        ax.text(x + cw / 2, h - 1.35, head, ha="center", fontsize=10.5, fontweight="bold", color=INK)
        for ri, (it, override) in enumerate(items):
            y = h - 2.75 - ri * 1.5
            box(ax, x + 0.05, y, cw - 0.1, 1.1, it, override or color, size=8.8)
        if ci < len(cols) - 1:
            arrow(ax, x + cw + 0.1, (h - 0.5) / 2, x + cw + gap - 0.1, (h - 0.5) / 2, style="simple", size=8)
    ax.text(w / 2, 0.35, "Read it left to right: a buyer's message passes the front door, the brain decides, the helpers "
            "remember and look things up, and the answer goes back out. Coolify (not drawn) deploys and watches every box.",
            ha="center", fontsize=8.8, color="#5A5A5A")
    save(fig, "00_big_picture.png")


def voice_flow():
    chain("02_voice_flow.png", "Website voice call: what happens between \"hello\" and the AI's answer",
          ["Buyer speaks\n(browser mic)", "LiveKit server\ncarries audio\n16-90 MiB", "Silero VAD\n\"is someone\ntalking?\"",
           "faster-whisper\nspeech -> text\nGPU: 0.24 s/clip", "Turn detector\n\"finished the\nsentence?\"",
           "Kairox graphs\nknowledge search\n+ Claude", "Price guard\nchecks every\nnumber", "Piper\ntext -> speech\nfirst audio 0.1-0.3 s",
           "Buyer hears\nthe answer"],
          notes="Measured with 10 callers on the bench GPU: reply p50 4.7 s (bench agent), 10 s (the platform's own worker). "
                "The ~1-2 s fake-model delay is included.",
          colors=[C["buyer"], C["voice"], C["voice"], C["voice"], C["voice"], C["ai"], C["edge"], C["voice"], C["buyer"]],
          w=18.5, h=3.6)


def chat_flow():
    chain("01_chat_flow.png", "A buyer message (website chat or WhatsApp): the journey of one AI turn",
          ["Buyer types\na question", "SafeLine\nchecks it\n(+0-1 ms)", "API receives\nWhatsApp ack:\n14 ms p50",
           "Celery worker\npicks the job\n(Redis queue)", "Knowledge search\nTEI + pgvector\n0.16 s / query",
           "Claude writes\nthe reply\n(2.2 calls/turn)", "Price guard +\nopt-out check", "Reply sent\nWhatsApp / chat",
           "Phoenix records\nthe AI calls\n4.3 KB each"],
          notes="Every turn also writes 6.5-8 KB to the app databases (measured, Round 1).",
          colors=[C["buyer"], C["edge"], C["app"], C["app"], C["data"], C["paid"], C["edge"], C["out"], C["data"]],
          w=18.5, h=3.6)


def email_flow():
    fig, ax = canvas(13.5, 5.2, "Email: quote out, reply back")
    top = [("Salesperson\napproves quote", C["buyer"]), ("OutboundService\nsingle-use grant", C["app"]),
           ("Postal\nSMTP auth, DKIM sign,\nstores raw mail", C["out"]), ("Relay / internet\n(bench: Haraka sink)", C["grey"]),
           ("Buyer's inbox", C["buyer"])]
    bot = [("Buyer replies", C["buyer"]), ("Stalwart\nreceives + stores\n(JMAP / IMAP)", C["out"]),
           ("App inbox\nreply joins the deal", C["app"])]
    bw, gap = 2.3, 0.4
    for i, (t, c) in enumerate(top):
        x = 0.25 + i * (bw + gap)
        box(ax, x, 2.8, bw, 1.3, t, c, size=9)
        if i < len(top) - 1:
            arrow(ax, x + bw, 3.45, x + bw + gap, 3.45)
    for i, (t, c) in enumerate(bot):
        x = 0.25 + (len(top) - 1 - i) * (bw + gap)
        box(ax, x, 0.7, bw, 1.3, t, c, size=9)
        if i < len(bot) - 1:
            arrow(ax, x, 1.35, x - gap, 1.35)
    arrow(ax, 0.25 + 4 * (bw + gap) + bw / 2, 2.8, 0.25 + 4 * (bw + gap) + bw / 2, 2.0)
    ax.text(6.75, 0.25, "Measured: Postal 1,000 sends at 44/s, all delivered in 39 s; Stalwart 1,000 stored in 11 s.",
            ha="center", fontsize=9, color="#5A5A5A")
    save(fig, "03_email_flow.png")


def call_flow():
    chain("04_call_flow.png", "Staff alert call (e.g. a quote waiting 1 hour for approval)",
          ["Celery beat\ntimer fires", "Worker decides\n\"call Sara\"", "Asterisk\nplaces the call\n~0.3 MB per call",
           "SIP trunk (paid)\nreaches the\nphone network", "Staff phone\nrings", "Spoken message\n+ logged on\nthe deal"],
          notes="Bench test: SIPp made 10 simultaneous calls with two-way audio; Asterisk and FreeSWITCH both answered 10 of 10.",
          colors=[C["app"], C["app"], C["out"], C["paid"], C["buyer"], C["data"]], w=13.5)


def waf_flow():
    fig, ax = canvas(13.5, 5.0, "Front door: firewall and log watcher")
    box(ax, 0.3, 2.1, 2.2, 1.3, "Any web request\n(buyer, Meta webhook,\nattacker)", C["buyer"], 9)
    box(ax, 3.3, 2.1, 2.4, 1.3, "SafeLine\nlooks at the request\n(rules + AI detector)", C["edge"], 9, True)
    box(ax, 6.6, 3.5, 2.6, 1.0, "403: blocked\nSQL injection, XSS,\npath traversal", "#FFB3B3", 9)
    box(ax, 6.6, 1.0, 2.6, 1.0, "200: passed\nchat, CRM, webhooks", "#BDF2C0", 9)
    box(ax, 10.0, 1.0, 2.6, 1.0, "Platform API", C["app"], 9)
    box(ax, 10.0, 3.5, 2.6, 1.0, "CrowdSec reads logs\nbans repeat offenders", C["edge"], 9)
    arrow(ax, 2.5, 2.75, 3.3, 2.75)
    arrow(ax, 5.7, 3.0, 6.6, 4.0)
    arrow(ax, 5.7, 2.5, 6.6, 1.5)
    arrow(ax, 9.2, 1.5, 10.0, 1.5)
    arrow(ax, 9.2, 4.0, 10.0, 4.0, label="logs")
    ax.text(6.75, 0.3, "Measured: SafeLine blocked 3/3 test attacks and 0 of ~330 real requests; BunkerWeb blocked 3/3 "
            "attacks but also 89/89 WhatsApp webhooks.", ha="center", fontsize=9, color="#5A5A5A")
    save(fig, "05_waf_flow.png")


def embed_flow():
    fig, ax = canvas(13.5, 5.0, "Knowledge search: how the AI finds the right fact")
    top = [("CMS publish\n(products, policies)", C["buyer"]), ("Cut into chunks", C["app"]),
           ("TEI embedder\ntext -> 1,024 numbers", C["ai"]), ("pgvector\nstores the vectors", C["data"])]
    bot = [("Buyer question\n(any language)", C["buyer"]), ("TEI embedder\nsame model", C["ai"]),
           ("pgvector\nnearest facts", C["data"]), ("Claude answers\nusing those facts", C["paid"])]
    bw, gap = 2.6, 0.6
    for row, items, y in ((0, top, 2.9), (1, bot, 0.9)):
        for i, (t, c) in enumerate(items):
            x = 0.3 + i * (bw + gap)
            box(ax, x, y, bw, 1.2, t, c, size=9)
            if i < len(items) - 1:
                arrow(ax, x + bw, y + 0.6, x + bw + gap, y + 0.6)
    ax.text(6.75, 0.35, "Measured: BGE-M3 re-indexes 2,500 chunks in ~31 min, Qwen3 in ~3 h 15 min; both find the right "
            "fact for 35-36 of 38 hard questions.", ha="center", fontsize=9, color="#5A5A5A")
    save(fig, "06_embed_flow.png")


def trace_flow():
    chain("07_trace_flow.png", "AI tracing: the platform's flight recorder",
          ["Every Claude call\nin LangGraph", "OpenTelemetry span\nprompt, reply,\ntools, time",
           "Phoenix\ncollector", "Stored in the\nplatform's Postgres\n1.9 MB / 1,000 spans",
           "Staff open Phoenix\n\"why did the AI\nsay that?\""],
          colors=[C["paid"], C["app"], C["data"], C["data"], C["buyer"]], w=12.5)


def coolify_flow():
    chain("08_coolify_flow.png", "Coolify: the building manager on your own server",
          ["You push code\nto GitHub", "Coolify sees it\n(webhook)", "Builds the Docker\nimage on the server",
           "Starts / restarts\ncontainers\n(api, worker, beat ...)", "Adds HTTPS\ncertificates,\ndomains",
           "Shows logs,\nhealth, backups\nin one dashboard"],
          notes="Not load-tested by the bench. Facts from coolify.io: self-hosted is free; needs 2 CPU cores, 2 GB RAM, 10 GB disk; "
                "Linux, amd64 or arm64; Cloud $5/month for 2 servers.",
          colors=[C["buyer"], C["edge"], C["app"], C["app"], C["edge"], C["data"]], w=13.5)


def ram_chart():
    j = lambda f: json.loads((RES / f).read_text(encoding="utf-8"))  # noqa: E731
    rows = [
        ("Asterisk (10 calls)", 49, j("r7_asterisk.json")["load"]["ram_total_peak_mib"]),
        ("FreeSWITCH (10 calls)", 41, j("r7_freeswitch.json")["load"]["ram_total_peak_mib"]),
        ("Stalwart (1,000 mails)", 85, j("r6_stalwart.json")["load"]["ram_total_peak_mib"]),
        ("Haraka (1,000 mails)", 91, j("r6_haraka.json")["load"]["ram_total_peak_mib"]),
        ("CrowdSec (100k lines)", 158, j("r8_crowdsec.json")["load"]["ram_total_peak_mib"]),
        ("BunkerWeb (proxying)", 290, j("r8_bunkerweb.json")["via_bunkerweb"]["bunkerweb_load"]["ram_total_peak_mib"]),
        ("Phoenix (1,000 spans)", 482, j("r5_phoenix.json")["load"]["ram_total_peak_mib"]),
        ("Postal (1,000 sends)", 455, j("r6_postal.json")["load"]["ram_total_peak_mib"]),
        ("SafeLine (protecting)", 1618, j("r8_safeline_proxy.json")["via_safeline"]["safeline_load"]["ram_total_peak_mib"]),
        ("Langfuse (1,000 spans)", 2549, j("r5_langfuse.json")["load"]["ram_total_peak_mib"]),
        ("Qwen3 embeddings", 2888, 3174),
        ("Opik (1,000 spans)", 2672, j("r5_opik.json")["load"]["ram_total_peak_mib"]),
        ("Voice worker (10 calls)", 1444, 4311),
        ("BGE-M3 embeddings", 4854, 4946),
    ]
    fig, ax = plt.subplots(figsize=(11, 6.5), dpi=160)
    names = [r[0] for r in rows]
    ax.barh(names, [r[2] for r in rows], color="#9CC9F5", label="peak while working")
    ax.barh(names, [r[1] for r in rows], color="#24324A", label="idle")
    for i, r in enumerate(rows):
        ax.text(max(r[1], r[2]) + 40, i, f"{r[2]:,.0f} MiB", va="center", fontsize=8.5)
    ax.set_xlabel("RAM, MiB (measured)")
    ax.set_title("How hungry is each free platform? (RAM, idle vs working)", fontsize=13, fontweight="bold")
    ax.legend(loc="lower right")
    ax.spines[["top", "right"]].set_visible(False)
    save(fig, "10_ram_chart.png")


def cost_chart():
    d = json.loads((RES / "users_per_day_costs.json").read_text())["by_users_per_day"]
    users = ["50", "100", "150"]
    a_lo = [d[u]["A_aws_plan_all_paid"]["low_light_sizes"] for u in users]
    a_hi = [d[u]["A_aws_plan_all_paid"]["high_normal_sizes"] for u in users]
    b = [d[u]["B_namecheap_coolify_paid_voice"]["total"] for u in users]
    c = [d[u]["C_namecheap_coolify_paid_voice_selfhosted_email"]["total"] for u in users]
    fig, ax = plt.subplots(figsize=(11, 5.5), dpi=160)
    x = range(3)
    wdt = 0.26
    ax.bar([i - wdt for i in x], a_lo, wdt, color="#F4A261", label="A: AWS plan, all paid (Light sizes)")
    ax.bar([i - wdt for i in x], [h - lo for h, lo in zip(a_hi, a_lo)], wdt, bottom=a_lo, color="#F4A26155",
           label="A: up to Normal sizes")
    ax.bar(list(x), b, wdt, color="#2A9D8F", label="B: Namecheap + Coolify, paid voice")
    ax.bar([i + wdt for i in x], c, wdt, color="#264653", label="C: B + own email (Postal)")
    for i in x:
        ax.text(i - wdt, a_hi[i] + 15, f"${a_lo[i]:,.0f}-{a_hi[i]:,.0f}", ha="center", fontsize=8.5)
        ax.text(i, b[i] + 15, f"${b[i]:,.0f}", ha="center", fontsize=8.5)
        ax.text(i + wdt, c[i] + 15, f"${c[i]:,.0f}", ha="center", fontsize=8.5)
    ax.set_xticks(list(x), [f"{u} users / day" for u in users])
    ax.set_ylabel("US$ a month")
    ax.set_title("Monthly cost at 50 / 100 / 150 users a day", fontsize=13, fontweight="bold")
    ax.legend(fontsize=8.5)
    ax.spines[["top", "right"]].set_visible(False)
    save(fig, "11_cost_chart.png")


def stacks_chart():
    d = json.loads((RES / "users_per_day_costs.json").read_text())["by_users_per_day"]
    users = ["50", "100", "150"]
    lo = [d[u]["stack_paid"]["total_low"] for u in users]
    hi = [d[u]["stack_paid"]["total_high"] for u in users]
    hy = [d[u]["stack_hybrid"]["total"] for u in users]
    fr = [d[u]["stack_free"]["total"] for u in users]
    fig, ax = plt.subplots(figsize=(11, 5.6), dpi=160)
    wdt = 0.26
    x = list(range(3))
    ax.bar([i - wdt for i in x], lo, wdt, color="#F4A261", label="PAID: AWS plan + all paid services (Light sizes)")
    ax.bar([i - wdt for i in x], [h - l for h, l in zip(hi, lo)], wdt, bottom=lo, color="#F4A26155", label="PAID: up to Normal sizes")
    ax.bar(x, hy, wdt, color="#2A9D8F", label="RECOMMENDED: paid + free mix on Namecheap")
    ax.bar([i + wdt for i in x], fr, wdt, color="#264653", label="MAX FREE: + GPU server for free speech-to-text")
    for i in x:
        ax.text(i - wdt, hi[i] + 15, f"${lo[i]:,.0f}-{hi[i]:,.0f}", ha="center", fontsize=8.5)
        ax.text(i, hy[i] + 15, f"${hy[i]:,.0f}", ha="center", fontsize=9, fontweight="bold")
        ax.text(i + wdt, fr[i] + 15, f"${fr[i]:,.0f}", ha="center", fontsize=8.5)
    ax.set_xticks(x, [f"{u} users / day" for u in users])
    ax.set_ylabel("US$ a month")
    ax.set_title("The three stacks: monthly cost at 50 / 100 / 150 users a day", fontsize=13, fontweight="bold")
    ax.set_ylim(0, max(hi + fr) * 1.12)
    ax.legend(fontsize=8.5, loc="upper center", bbox_to_anchor=(0.5, -0.09), ncol=2, frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    save(fig, "14_stacks_chart.png")


def recommended_map():
    w, h = 15, 7.6
    fig, ax = canvas(w, h, "The recommended stack (green = free on your Namecheap server, orange = paid service)")
    free = [("PostgreSQL + pgvector", "all data"), ("Redis", "job queue"), ("API + Celery (c=3) + beat", "your app"),
            ("BGE-M3 on TEI", "knowledge search"), ("Phoenix", "AI traces"), ("LiveKit server", "voice transport"),
            ("Piper", "text-to-speech"), ("SafeLine + CrowdSec", "firewall"), ("Coolify", "deploy + manage")]
    paid = [("Claude", "writes replies"), ("Meta WhatsApp", "messages"), ("Deepgram", "speech-to-text (no GPU)"),
            ("SES + Postmark", "email"), ("Telnyx", "staff calls"), ("Vercel Pro", "two websites")]
    ax.add_patch(FancyBboxPatch((0.3, 0.8), 9.0, h - 2.0, boxstyle="round,pad=0.02,rounding_size=0.15", fc="#F1FAF4",
                                ec="#2A9D8F", lw=1.5))
    ax.text(4.8, h - 1.45, "ONE NAMECHEAP DEDICATED SERVER (Xeon E-2236, 64 GB, 2 x 960 GB SSD)", ha="center",
            fontsize=10, fontweight="bold", color="#1F6F65")
    for i, (n, s) in enumerate(free):
        col, row = i % 3, i // 3
        box(ax, 0.55 + col * 2.95, h - 2.9 - row * 1.55, 2.7, 1.15, f"{n}\n{s}", C["data"], 9)
    ax.add_patch(FancyBboxPatch((9.8, 0.8), 4.9, h - 2.0, boxstyle="round,pad=0.02,rounding_size=0.15", fc="#FFF6EE",
                                ec="#E76F51", lw=1.5))
    ax.text(12.25, h - 1.45, "PAID OUTSIDE SERVICES", ha="center", fontsize=10, fontweight="bold", color="#9C3D24")
    for i, (n, s) in enumerate(paid):
        col, row = i % 2, i // 2
        box(ax, 10.0 + col * 2.35, h - 2.9 - row * 1.55, 2.2, 1.15, f"{n}\n{s}", C["paid"], 8.8)
    arrow(ax, 9.3, h / 2 - 0.3, 9.8, h / 2 - 0.3, style="<|-|>")
    ax.text(w / 2, 0.35, "Measured RAM need ≈ 19.5 GiB of 64 GB; images and models ≈ 11.4 GB of disk. Paid lines scale "
            "with use; the server is a fixed monthly price.", ha="center", fontsize=8.8, color="#5A5A5A")
    save(fig, "15_recommended_map.png")


def voice_cost_chart():
    r = json.loads((RES / "rank_costs.json").read_text())
    paid = r["voice"]["paid_stt_tts_transport"]
    g6 = r["gpu"]["g6.xlarge"]["usd_month"]
    fig, ax = plt.subplots(figsize=(9, 4.6), dpi=160)
    lv = ["Light", "Normal", "Heavy"]
    ax.bar([i - 0.2 for i in range(3)], paid, 0.4, color="#E76F51", label="Paid: Deepgram + ElevenLabs + LiveKit Cloud")
    ax.bar([i + 0.2 for i in range(3)], [g6] * 3, 0.4, color="#2A9D8F", label="Free chain on AWS UAE GPU (g6.xlarge)")
    for i in range(3):
        ax.text(i - 0.2, paid[i] + 15, f"${paid[i]}", ha="center", fontsize=9)
        ax.text(i + 0.2, g6 + 15, f"${g6:.0f}", ha="center", fontsize=9)
    ax.set_xticks(range(3), lv)
    ax.set_ylabel("US$ a month")
    ax.set_title("Voice: paid vendors vs an always-on GPU server", fontsize=12, fontweight="bold")
    ax.legend(fontsize=8.5)
    ax.spines[["top", "right"]].set_visible(False)
    save(fig, "12_voice_cost_chart.png")


def footprint_chart():
    """Peak RAM of the RECOMMENDED stack (same list as the guide's table), against 32 / 64 GB servers."""
    d = json.loads((RES / "users_per_day_costs.json").read_text())["stack_ram_gib"]["hybrid"]
    items = list(d["items"].items())
    est = [("(E)" in k or "docs minimum" in k) for k, _ in items]
    fig, ax = plt.subplots(figsize=(10, 5.2), dpi=160)
    left = 0
    for (lab, v), e in zip(items, est):
        ax.barh([0], [v], left=left, color="#BBBBBB" if e else "#264653", edgecolor="white")
        if v >= 1.5:
            ax.text(left + v / 2, 0, f"{v:g}", ha="center", va="center", color="white", fontsize=9)
        left += v
    ax.axvline(32, color="#E76F51", ls="--")
    ax.text(32.2, 0.35, "32 GB server", color="#E76F51", fontsize=9)
    ax.axvline(64, color="#2A9D8F", ls="--")
    ax.text(64.2, 0.35, "64 GB server", color="#2A9D8F", fontsize=9)
    ax.set_xlim(0, 68)
    ax.set_yticks([])
    ax.set_xlabel(f"GiB of RAM at peak (dark = measured, grey = estimate or documented minimum); total = {d['total']} GiB")
    ax.set_title("The recommended stack on one server: does it fit?", fontsize=12, fontweight="bold")
    for i, (lab, v) in enumerate(items):
        ax.text(0, -0.75 - i * 0.18, f"{v:g} GiB  {lab}", fontsize=7.5, transform=ax.transData)
    ax.set_ylim(-0.75 - len(items) * 0.18, 0.6)
    ax.spines[["top", "right", "left"]].set_visible(False)
    save(fig, "13_footprint_chart.png")


if __name__ == "__main__":
    for f in (big_picture, chat_flow, voice_flow, email_flow, call_flow, waf_flow, embed_flow, trace_flow, coolify_flow,
              ram_chart, cost_chart, voice_cost_chart, footprint_chart, stacks_chart, recommended_map):
        f()
        print("drew", f.__name__)
