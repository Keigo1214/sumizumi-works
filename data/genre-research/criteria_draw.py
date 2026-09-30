"""仮の候補ジャンルの切り替えの基準（docs/phase0.5-provisional-genre.md §7-4）の材料を固定し、無作為の組と検索語を引く。
結果を見る前に一度だけ実行し、出力（criteria-2026-10-01.json）をそのまま使う。引き直さない。"""
import json
import random

SEED = 20261001

# A：店舗のレジ。§2-1 の18業種から、保険診療・保険の業種（歯科・クリニック・介護）を除いた15業種
A_INDUSTRIES = ["飲食店", "居酒屋", "カフェ", "美容室", "ネイルサロン", "エステ", "整体", "小売店", "アパレル",
                "学習塾", "ジム", "不動産", "建設業", "税理士", "ホテル"]
# A の課題。「税の表示と帳票（インボイス・軽減税率など）」は条件2では数えない（税務のため）
A_ISSUES = ["導入費用と月額", "決済手数料と入金サイクル", "注文の伝達（ハンディ・キッチン・モバイルオーダー）",
            "会計の方式（テーブル会計・セルフレジ・前払い）", "在庫と商品の管理（色・サイズ・量り売りなど）",
            "複数店舗の管理", "予約・顧客管理との連携", "回数券・会員・ポイント", "売上分析"]
A_EXCLUDED_ISSUES = ["税の表示と帳票（インボイス・軽減税率・電子帳簿保存法・免税販売）"]

# B：バーチャルオフィス。事業形態は suggest-vo-2026-10-01.json の20語（後から選んだ語だが、ここで固定する）
B_FORMS = ["行政書士", "税理士", "司法書士", "弁護士", "社労士", "ネットショップ", "EC", "古物商", "建設業", "人材派遣",
           "不動産", "フリーランス", "副業", "個人事業主", "デザイナー", "コンサル", "美容サロン", "法人登記", "特商法", "外国人"]
# B の課題のうち、法律・金融・税務の判断に触れないもの（条件2で数える）
B_ISSUES = ["月額と初期費用", "郵便物の転送（頻度・料金）", "荷物（宅配便）の受け取り", "電話番号・電話代行",
            "会議室・来客対応", "住所の場所（都市・地区）", "契約期間と解約", "支払い方法"]
B_EXCLUDED_ISSUES = ["法人登記の可否", "特定商取引法の住所表示", "本人確認と契約の手続き（犯罪収益移転防止法）",
                     "法人口座の開設", "納税地", "許認可・士業登録の事務所要件"]

rng = random.Random(SEED)
a_pairs = [(i, t) for i in A_INDUSTRIES for t in A_ISSUES]
b_pairs = [(f, t) for f in B_FORMS for t in B_ISSUES]
a_sample = rng.sample(a_pairs, 20)
b_sample = rng.sample(b_pairs, 20)
a_words = [f"{i} POSレジ" for i in rng.sample(A_INDUSTRIES, 6)]
b_words = [f"{f} バーチャルオフィス" for f in rng.sample(B_FORMS, 6)]

out = {
    "seed": SEED,
    "A": {"industries": A_INDUSTRIES, "issues": A_ISSUES, "excluded_issues": A_EXCLUDED_ISSUES,
          "pairs_total": len(a_pairs), "sample_pairs": a_sample, "serp_words": a_words},
    "B": {"forms": B_FORMS, "issues": B_ISSUES, "excluded_issues": B_EXCLUDED_ISSUES,
          "pairs_total": len(b_pairs), "sample_pairs": b_sample, "serp_words": b_words},
}
json.dump(out, open("criteria-2026-10-01.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(json.dumps({k: {"pairs_total": v["pairs_total"], "serp_words": v["serp_words"]} for k, v in out.items() if k in "AB"}, ensure_ascii=False))
