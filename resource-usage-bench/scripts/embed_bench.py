"""Embedding-server bench (TEI, OpenAI-compatible /v1/embeddings): speed, and whether the right answer comes back.

  python embed_bench.py --url http://localhost:28080 --format qwen3 --out results/r4_embed_qwen3.json
  --format qwen3 | e5 | plain   (how each model expects queries and passages to be written)

* indexing: 48 catalogue chunks (~450-550 tokens, 6 languages), sent 4 per request like a re-index job;
* queries: 30 short buyer questions one by one, then 10 at once (ten users asking at the same moment);
* quality: 8 Arifa-style facts written in 4 languages (32 passages) and 24 buyer questions in en/ar/hi/zh.
  top1_all  = the best passage (any language) is about the right fact;
  top1_en_only = only the English passages are searched (a buyer writes Arabic, the catalogue is English);
  mrr_all   = mean reciprocal rank of the first correct passage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from pathlib import Path

import httpx

BASE = {
    "en": "Food-grade aluminium container made from coil 8011, suitable for freezer, oven and takeaway use. ",
    "ar": "حاوية ألمنيوم آمنة للطعام مصنوعة من لفائف 8011، مناسبة للفريزر والفرن وطلبات التوصيل. ",
    "hi": "खाद्य-ग्रेड एल्यूमिनियम कंटेनर, 8011 कॉइल से बना, फ्रीज़र, ओवन और टेकअवे के लिए उपयुक्त। ",
    "ta": "உணவுத் தர அலுமினியக் கொள்கலன், 8011 சுருளால் செய்யப்பட்டது, உறைவிப்பானுக்கு ஏற்றது. ",
    "zh": "食品级铝箔容器，采用8011铝卷制造，适用于冷冻、烤箱和外卖。可配纸盖或塑料盖，可按要求定制印刷。",
    "si": "ආහාර ශ්‍රේණියේ ඇලුමිනියම් බහාලුම, 8011 දඟරයෙන් සාදන ලද, ශීතකරණ භාවිතයට සුදුසුය. ",
}
QUERIES = ["750 ml container with lid", "do you print logos", "minimum order quantity", "delivery to Muscat",
           "حاويات ألمنيوم مع غطاء", "铝箔餐盒 带盖", "ढक्कन वाला कंटेनर", "oven safe trays", "pieces per carton",
           "food grade certificate"] * 3

# fact id -> passage per language
FACTS: dict[str, dict[str, str]] = {
    "lid": {
        "en": "The 750 ml aluminium container is sold with a matching lid. Paper-board, plastic and aluminium lids are available.",
        "ar": "تُباع حاوية الألمنيوم سعة 750 مل مع غطاء مناسب. تتوفر أغطية من الورق المقوى والبلاستيك والألمنيوم.",
        "hi": "750 मिली एल्यूमिनियम कंटेनर मिलते-जुलते ढक्कन के साथ बेचा जाता है। कागज़-बोर्ड, प्लास्टिक और एल्यूमिनियम ढक्कन उपलब्ध हैं।",
        "zh": "750毫升铝箔容器配套出售盖子，可选纸板盖、塑料盖和铝盖。",
    },
    "print": {
        "en": "We print your logo or brand on lids and cartons. Custom printing needs at least 50,000 pieces.",
        "ar": "نطبع شعارك أو علامتك التجارية على الأغطية والكراتين. تتطلب الطباعة المخصصة 50,000 قطعة على الأقل.",
        "hi": "हम ढक्कनों और कार्टनों पर आपका लोगो या ब्रांड छापते हैं। कस्टम प्रिंटिंग के लिए कम से कम 50,000 पीस चाहिए।",
        "zh": "我们可以在盖子和纸箱上印刷您的标志或品牌，定制印刷最少50,000个。",
    },
    "moq": {
        "en": "The minimum order quantity is 20,000 pieces per size; trial orders can mix sizes in one pallet.",
        "ar": "الحد الأدنى للطلب هو 20,000 قطعة لكل مقاس، ويمكن للطلبات التجريبية خلط المقاسات في منصة واحدة.",
        "hi": "न्यूनतम ऑर्डर मात्रा प्रति साइज़ 20,000 पीस है; ट्रायल ऑर्डर में एक पैलेट में अलग-अलग साइज़ मिलाए जा सकते हैं।",
        "zh": "每个尺寸的最低起订量为20,000个，试订单可以在一个托盘内混装不同尺寸。",
    },
    "delivery": {
        "en": "We deliver across the UAE in 2-3 days and ship by road to Oman, Saudi Arabia, Qatar, Bahrain and Kuwait.",
        "ar": "نوصل داخل الإمارات خلال 2-3 أيام ونشحن براً إلى عُمان والسعودية وقطر والبحرين والكويت.",
        "hi": "हम पूरे यूएई में 2-3 दिनों में डिलीवरी करते हैं और सड़क मार्ग से ओमान, सऊदी अरब, कतर, बहरीन और कुवैत भेजते हैं।",
        "zh": "我们在阿联酋境内2-3天送达，并通过陆运发往阿曼、沙特、卡塔尔、巴林和科威特。",
    },
    "oven": {
        "en": "Our trays are oven safe up to 250 °C and freezer safe down to -40 °C. Do not use them in a microwave.",
        "ar": "صوانينا آمنة في الفرن حتى 250 درجة مئوية وفي الفريزر حتى -40 درجة. لا تستخدمها في الميكروويف.",
        "hi": "हमारी ट्रे 250 °C तक ओवन में और -40 °C तक फ्रीज़र में सुरक्षित हैं। इन्हें माइक्रोवेव में इस्तेमाल न करें।",
        "zh": "我们的托盘可耐烤箱250°C高温，可冷冻至-40°C，请勿用于微波炉。",
    },
    "carton": {
        "en": "Each carton holds 1,000 containers in 10 sleeves of 100 pieces. A carton weighs about 9 kg.",
        "ar": "يحتوي كل كرتون على 1,000 حاوية في 10 أكياس من 100 قطعة. يزن الكرتون حوالي 9 كغ.",
        "hi": "हर कार्टन में 100 पीस की 10 स्लीव में 1,000 कंटेनर होते हैं। एक कार्टन का वज़न लगभग 9 किलो है।",
        "zh": "每箱装1,000个容器，分10条，每条100个，每箱重约9公斤。",
    },
    "cert": {
        "en": "All products use food-grade 8011 alloy and come with ISO 22000 and Dubai Municipality food-contact certificates.",
        "ar": "جميع المنتجات مصنوعة من سبيكة 8011 الآمنة للغذاء ومرفقة بشهادات ISO 22000 وبلدية دبي لملامسة الغذاء.",
        "hi": "सभी उत्पाद फूड-ग्रेड 8011 मिश्रधातु से बने हैं और ISO 22000 तथा दुबई नगरपालिका के फूड-कॉन्टैक्ट प्रमाणपत्र के साथ आते हैं।",
        "zh": "所有产品均采用食品级8011合金，并附有ISO 22000和迪拜市政府食品接触认证。",
    },
    "payment": {
        "en": "Payment terms are 50% advance and 50% before dispatch; regular customers can apply for 30-day credit.",
        "ar": "شروط الدفع 50% مقدماً و50% قبل الشحن، ويمكن للعملاء الدائمين طلب ائتمان لمدة 30 يوماً.",
        "hi": "भुगतान की शर्तें 50% अग्रिम और 50% डिस्पैच से पहले हैं; नियमित ग्राहक 30 दिन के क्रेडिट के लिए आवेदन कर सकते हैं।",
        "zh": "付款条件为预付50%，发货前付清余下50%；老客户可申请30天账期。",
    },
}
LABELLED = [
    ("lid", "en", "does the 750ml box come with a cover"), ("lid", "ar", "هل تأتي العلبة مع غطاء"),
    ("lid", "zh", "750毫升的盒子有盖子吗"),
    ("print", "en", "can you put our company logo on the lids"), ("print", "ar", "هل يمكنكم طباعة شعار شركتنا"),
    ("print", "hi", "क्या आप हमारा लोगो छाप सकते हैं"),
    ("moq", "en", "what is the smallest quantity I can order"), ("moq", "ar", "ما هو الحد الأدنى للطلب"),
    ("moq", "zh", "最少要订多少个"),
    ("delivery", "en", "do you ship to Muscat"), ("delivery", "ar", "هل توصلون إلى السعودية"),
    ("delivery", "hi", "क्या आप ओमान में डिलीवरी करते हैं"),
    ("oven", "en", "can I put the tray in the oven"), ("oven", "ar", "هل الصواني تتحمل حرارة الفرن"),
    ("oven", "zh", "可以放进烤箱吗"),
    ("carton", "en", "how many pieces in one carton"), ("carton", "ar", "كم قطعة في الكرتون الواحد"),
    ("carton", "hi", "एक कार्टन में कितने पीस होते हैं"),
    ("cert", "en", "is it certified food safe"), ("cert", "ar", "هل لديكم شهادة سلامة الغذاء"),
    ("cert", "zh", "有食品级认证吗"),
    ("payment", "en", "do you give credit or need advance payment"), ("payment", "ar", "ما هي شروط الدفع"),
    ("payment", "hi", "भुगतान की शर्तें क्या हैं"),
]
QWEN3_INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"  # as in the app


def as_query(fmt: str, q: str) -> str:
    if fmt == "qwen3":
        return f"Instruct: {QWEN3_INSTRUCTION}\nQuery:{q}"  # exactly what HttpEmbedder.embed_query sends
    return f"query: {q}" if fmt == "e5" else q


def as_passage(fmt: str, p: str) -> str:
    return f"passage: {p}" if fmt == "e5" else p


def cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


async def embed(c: httpx.AsyncClient, model: str, texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for k in range(0, len(texts), 4):
        r = await c.post("/v1/embeddings", json={"input": texts[k:k + 4], "model": model})
        r.raise_for_status()
        out += [d["embedding"] for d in r.json()["data"]]
    return out


async def quality(c: httpx.AsyncClient, model: str, fmt: str) -> dict[str, object]:
    passages = [(fid, lang, txt) for fid, langs in FACTS.items() for lang, txt in langs.items()]
    pv = await embed(c, model, [as_passage(fmt, t) for _, _, t in passages])
    qv = await embed(c, model, [as_query(fmt, q) for _, _, q in LABELLED])
    top1 = top1_en = 0
    rr: list[float] = []
    misses = []
    for (fid, lang, q), v in zip(LABELLED, qv):
        ranked = sorted(range(len(passages)), key=lambda i: -cos(v, pv[i]))
        first = next(n for n, i in enumerate(ranked, 1) if passages[i][0] == fid)
        rr.append(1 / first)
        top1 += passages[ranked[0]][0] == fid
        en = [i for i in ranked if passages[i][1] == "en"]
        top1_en += passages[en[0]][0] == fid
        if passages[ranked[0]][0] != fid:
            misses.append({"q": q, "want": fid, "got": passages[ranked[0]][0]})
    n = len(LABELLED)
    return {"questions": n, "top1_all": round(top1 / n, 3), "top1_en_only": round(top1_en / n, 3),
            "mrr_all": round(sum(rr) / n, 3), "misses": misses}


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:28080")
    p.add_argument("--model", default="bench")
    p.add_argument("--format", choices=["qwen3", "e5", "plain"], default="qwen3")
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    async with httpx.AsyncClient(base_url=a.url, timeout=600) as c:
        for _ in range(120):
            try:
                if (await c.post("/v1/embeddings", json={"input": ["warm"], "model": a.model})).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(5)
        chunks = [(BASE[lang] * 12)[:1800] for lang in BASE for _ in range(8)]  # 48 chunks
        t0 = time.perf_counter()
        for k in range(0, len(chunks), 4):
            r = await c.post("/v1/embeddings", json={"input": [as_passage(a.format, x) for x in chunks[k:k + 4]],
                                                     "model": a.model})
            r.raise_for_status()
        index_s = time.perf_counter() - t0
        tokens = 0
        for k in range(0, len(chunks), 8):  # TEI caps the batch size of /tokenize too
            r = await c.post("/tokenize", json={"inputs": chunks[k:k + 8]})
            if r.status_code == 200:
                tokens += sum(len(t) for t in r.json())
        single = []
        for q in QUERIES:
            t = time.perf_counter()
            (await c.post("/v1/embeddings", json={"input": [as_query(a.format, q)], "model": a.model})).raise_for_status()
            single.append(time.perf_counter() - t)

        async def one(q: str) -> float:
            t = time.perf_counter()
            (await c.post("/v1/embeddings", json={"input": [as_query(a.format, q)], "model": a.model})).raise_for_status()
            return time.perf_counter() - t

        burst = []
        for _ in range(3):
            burst += await asyncio.gather(*(one(q) for q in QUERIES[:10]))
        dims = len((await c.post("/v1/embeddings", json={"input": ["x"], "model": a.model})).json()["data"][0]["embedding"])
        qual = await quality(c, a.model, a.format)
    res = {
        "format": a.format,
        "chunks": len(chunks), "chunk_tokens_total": tokens, "index_seconds": round(index_s, 1),
        "index_tokens_per_s": round(tokens / index_s, 1) if tokens else None,
        "index_seconds_per_chunk": round(index_s / len(chunks), 2),
        "query_single_p50_s": round(statistics.median(single), 3), "query_single_max_s": round(max(single), 3),
        "query_10_at_once_p50_s": round(statistics.median(burst), 3), "query_10_at_once_max_s": round(max(burst), 3),
        "queries_over_2s_app_timeout": sum(1 for x in single + burst if x > 2.0),
        "dims": dims, "vector_bytes_float32": dims * 4,
        "quality": qual,
    }
    a.out.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(res, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
