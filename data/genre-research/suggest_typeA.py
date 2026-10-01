"""型A のキーワード選び（2026-10-02）の一次データ：候補の商材ごとに、Google の検索候補を取る。
候補と選び方は typeA-criteria-2026-10-02.json に先に固定してある。検索ボリュームではなく「検索候補に出るか」だけを見る。
使い方: python suggest_typeA.py typeA-criteria-2026-10-02.json suggest-typeA-2026-10-02.json"""
import json, sys, time, datetime
import requests

UA = "SumizumiWorksBot/1.0 (+https://sumizumi-works.com/contact/)"


def suggest(q):  # suggest_matrix.py と同じ取り方
    url = "https://suggestqueries.google.com/complete/search"
    r = requests.get(url, params={"client": "firefox", "hl": "ja", "gl": "jp", "ie": "utf-8", "oe": "utf-8", "q": q},
                     headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    return json.loads(r.content.decode("utf-8"))[1]


INTENT_WORDS = ("比較", "おすすめ", "料金", "価格", "無料", "費用", "ランキング", "安い")
SUFFIXES = ["", " ", " 比較", " おすすめ", " 料金"]

crit = json.load(open(sys.argv[1], encoding="utf-8"))
rows = []
for c in crit["candidates"]:
    per = {}
    for suf in SUFFIXES:
        q = c["seed"] + suf
        try:
            per[q] = suggest(q)
        except Exception as e:
            per[q] = [f"ERROR {e}"]
        time.sleep(1.2)
    base = per[c["seed"]] + per[c["seed"] + " "]
    intent = sorted({x for x in base if any(w in x for w in INTENT_WORDS)})
    rows.append({"seed": c["seed"], "group": c["group"], "family": c["family"], "r2_intent": intent, "suggestions": per})
    print(c["seed"], len(intent), flush=True)

json.dump({"fetched_at": datetime.datetime.now().isoformat(timespec="seconds"), "criteria": sys.argv[1], "rows": rows},
          open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
