"""業種×商材の組み合わせが検索されているかを、Google の検索候補（オートコンプリート）で数える。
仮の候補ジャンル選び（HANDOFF.md §2 の8）の一次データ。検索ボリュームではなく「検索候補に出るか」だけを見る。"""
import json, sys, time, datetime
import requests

UA = "SumizumiWorksBot/1.0 (+https://sumizumi-works.com/contact/)"
PRODUCTS = sys.argv[1].split(",")
INDUSTRIES = sys.argv[2].split(",")
OUT = sys.argv[3]

def suggest(q):
    url = "https://suggestqueries.google.com/complete/search"
    r = requests.get(url, params={"client": "firefox", "hl": "ja", "gl": "jp", "ie": "utf-8", "oe": "utf-8", "q": q},
                     headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    return json.loads(r.content.decode("utf-8"))[1]

rows = []
for p in PRODUCTS:
    for ind in INDUSTRIES:
        q = f"{ind} {p}"
        try:
            s = suggest(q)
        except Exception as e:
            s = [f"ERROR {e}"]
        hit = any(x.lower().replace("　", " ").startswith(q.lower()) for x in s)
        intent = [x for x in s if any(w in x for w in ("比較", "おすすめ", "料金", "価格", "無料", "費用", "ランキング"))]
        rows.append({"product": p, "industry": ind, "query": q, "hit": hit, "intent": intent, "suggestions": s})
        time.sleep(1.2)
    print(p, sum(r["hit"] for r in rows if r["product"] == p), "/", len(INDUSTRIES), flush=True)

json.dump({"fetched_at": datetime.datetime.now().isoformat(timespec="seconds"), "rows": rows},
          open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
