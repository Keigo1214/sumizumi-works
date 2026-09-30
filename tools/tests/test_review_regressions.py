"""独立レビューの再現ケース（scratchpad/review/c1.py〜c12.py）をそのまま回帰テストにしたもの。

入力（記事の本文・front matter）はレビュアーのハーネス h.py と同じ形で作る。
各テストの docstring に、ケース番号（c1-1 など）と指摘番号（C1 など）を書く。
"""

from __future__ import annotations

import os
import random
import re
import time

import pytest

import factcheck
import postcheck
import publish_prep
import similarity as sim
from article_lint import LintConfig, lint_article
from conftest import AD_HTML, FakeFetcher, good_article_text, html_page, make_article
from postcheck import PostConfig, check_page

M = "<!-- src: https://a.example.com/p accessed=2026-09-27 -->"

# h.py の FM と同じ（型A・checks なし）
FM = """---
type: A
keyword: テスト
slug: {slug}
title: "{title}"
description: "{desc}"
updated: 2026-10-01
history:
  - {{date: 2026-10-01, note: 公開}}
---
"""


def write(tmp_path, name, body, title="テスト記事", desc="テストの説明", fm=None, enc="utf-8", nl="\n"):
    fm = fm if fm is not None else FM.format(slug=name, title=title, desc=desc)
    return make_article(tmp_path, body, name=name, front_matter=fm, encoding=enc, newline=nl)


@pytest.fixture
def lc(cfg):
    return LintConfig.from_cfg(cfg)


def lint(p, lc):
    r = lint_article(p, lc)
    return [(i.line_no, i.kind, i.number) for i in r.errors], [i.kind for i in r.warnings]


def ekinds(p, lc):
    return sorted(k for _n, k, _num in lint(p, lc)[0])


def prep_errors(p, cfg):
    return [m.detail for m in publish_prep.prepare(p, cfg, {"prog_001": 123}).errors]


def lint_blocked(p, cfg):
    return any(e.startswith("lint のエラーが") for e in prep_errors(p, cfg))


# --------------------------------------------------------------------------
# c1
# --------------------------------------------------------------------------

def test_c1_1_kanji(tmp_path, lc, cfg):
    """c1-1 / C1: 漢数字（一万二千社・千九百八十円・九割）は lint のエラー。publish_prep も止まる。"""
    p = write(tmp_path, "kanji", f"""\
    月額は 1,980円 です。{M}
    導入企業は一万二千社を超え、月額は千九百八十円からです。
    満足度は九割です。
    """)
    errs, _ = lint(p, lc)
    assert [(n, k, num) for n, k, num in errs] == [
        (12, "kanji_number", "一万二千社"), (12, "kanji_number", "千九百八十円"), (13, "kanji_number", "九割")]
    assert lint_blocked(p, cfg)


def test_c1_2_zenkaku(tmp_path, lc):
    """c1-2: 全角数字・約・〜・以上も、マーカーがなければ報告される。"""
    p = write(tmp_path, "zenkaku", f"""\
    月額は 1,980円 です。{M}
    導入企業は約１２，０００社以上です。
    料金は3,000円〜です。
    """)
    errs, _ = lint(p, lc)
    assert [(n, k, num.split()[0]) for n, k, num in errs] == [
        (12, "unmarked_number", "12,000社"), (13, "unmarked_number", "3,000円")]


def test_c1_3_inline_code(tmp_path, lc, cfg):
    """c1-3 / C2: インラインコードの中の数値を消さない。lint はエラー、照合は数値を拾う。"""
    p = write(tmp_path, "inlinecode", f"""\
    月額は `9,999円` です。{M}
    導入企業は `50,000社` です。
    """)
    ks = ekinds(p, lc)
    assert ks.count("inline_code_number") == 2 and "unmarked_number" in ks
    claims, _ = factcheck.parse_article(p, [], True)
    assert [[t.raw for t in c.numbers] for c in claims] == [["9,999円"]]
    assert lint_blocked(p, cfg)


def test_c1_4_nofact_escape(tmp_path, lc, cfg):
    """c1-4 / C4: 金額以外の単位の nofact、事実の数値を入れた見出しの nofact はエラー。"""
    p = write(tmp_path, "nofact-escape", f"""\
    月額は 1,980円 です。{M}
    導入企業は 10,000社 以上で、継続率は 99.5% です。<!-- nofact -->
    導入企業は 10,000社 以上です。<!-- nofact -->

    ## 月額300円・導入1万社の勤怠管理X <!-- nofact -->
    """)
    errs, _ = lint(p, lc)
    assert sorted((n, k) for n, k, _ in errs) == [
        (12, "nofact_line_not_allowed"), (13, "nofact_line_not_allowed"),
        (15, "nofact_heading_not_enum"), (15, "nofact_heading_not_enum")]
    assert lint_blocked(p, cfg)


def test_c1_5_table_without_leading_pipe(tmp_path, lc, cfg):
    """c1-5 / C5: 先頭に | のない表の行の nofact もエラー（表の判定は markdown-it のトークン）。"""
    p = write(tmp_path, "table-nopipe", f"""\
    月額は 1,980円 です。{M}

    サービス | 月額 | 最低人数
    ---|---|---
    X社 | 300円 | 10人 <!-- nofact -->
    Y社 | 400円 | 5人 <!-- nofact -->
    """)
    assert ekinds(p, lc) == ["nofact_in_table", "nofact_in_table"]
    res = publish_prep.prepare(p, cfg, None, gate=False)
    assert "<table>" in res.content_html and res.content_html.count("<tr>") == 3


def test_c1_6_fake_fence(tmp_path, lc, cfg):
    """c1-6 / C6: CommonMark でフェンスにならない行の数値も lint の対象。閉じていないフェンスはエラー。"""
    p = write(tmp_path, "fake-fence", f"""\
    月額は 1,980円 です。{M}

    ```注`意
    導入企業は 12,000社 で、満足度は 98% です。
    ```
    """)
    errs, _ = lint(p, lc)
    ks = sorted(k for _n, k, _ in errs)
    # 末尾に足した関連記事の区間も、閉じていないフェンスの中に入るので part_missing も出る
    assert ks == ["code_block", "part_missing", "unclosed_fence", "unmarked_number", "unmarked_number"]
    assert lint_blocked(p, cfg)


# --------------------------------------------------------------------------
# c2
# --------------------------------------------------------------------------

def test_c2_7_crlf_bom(tmp_path, lc):
    """c2-7 / C21: BOM・CRLF の記事で、照合モードが slug を front matter から取れる。"""
    body = f"""\
    月額は 1,980円 です。{M}
    導入企業は 12,000社 です。
    """
    p = write(tmp_path, "crlf-bom", body, enc="utf-8-sig", nl="\r\n")
    assert ekinds(p, lc) == ["unmarked_number"]
    assert factcheck.article_slug(p) == "crlf-bom"
    p2 = write(tmp_path, "crlf-bom-2", body, fm=FM.format(slug="other-slug", title="t", desc="d"),
               enc="utf-8-sig", nl="\r\n")
    assert factcheck.article_slug(p2) == "other-slug"
    p3 = write(tmp_path, "bom-3", "月額は 1,980円 です。" + M + "\n", title="導入1万社の勤怠管理", enc="utf-8-sig")
    assert lint(p3, lc)[0] == [(1, "title_fact_unsourced", "1万社")]


def test_c2_8_calc_prose(tmp_path, lc):
    """c2-8 / C7: calc-block の中の文章（出典のない事実）はエラー。計算式の定数は nofact がなければ報告。"""
    p = write(tmp_path, "calc-prose", f"""\
    | サービス | 月額 |
    |---|---|
    | X社 | 300円 |{M}

    計算式：月額 × 12

    <!-- calc-block -->
    X社は導入企業 50,000社、継続率 99% で業界No.1です。
    <!-- /calc-block -->
    """)
    assert ekinds(p, lc) == ["banned_expression", "calc_block_non_table", "unmarked_number"]


def test_c2_9_title_unitless(tmp_path, lc):
    """c2-9 / C10: 単位のない出典つき数値（300）に、title の「300円」を一致させない。"""
    p = write(tmp_path, "title-unitless", f"""\
    | サービス | 項目 | 値 |
    |---|---|---|
    | X社 | 上限 | 300 |{M}
    """, title="月額300円から使える勤怠管理")
    assert lint(p, lc)[0] == [(1, "title_fact_unsourced", "300円")]


def test_c2_10_title_enum_noun(tmp_path, lc):
    """c2-10 / C11: 名詞が違う見出し（3つの誤解）では、title の「3つの要件」は合格にしない。"""
    p = write(tmp_path, "title-enum", f"""\
    月額は 1,980円 です。{M}

    ## よくある3つの誤解 <!-- nofact -->

    本文。
    """, title="必要な3つの要件と選び方")
    assert ekinds(p, lc) == ["title_enum_mismatch"]


def test_c2_11_title_enum_bold_paragraphs(tmp_path, lc):
    """c2-11 / C11: 見出しの代わりに太字の段落では項目を数えられない → エラー（以前は警告だけ）。"""
    p = write(tmp_path, "title-enum2", f"""\
    月額は 1,980円 です。{M}

    ## おすすめ5選 <!-- nofact -->

    **A社**
    **B社**
    """, title="勤怠管理システムおすすめ5選")
    assert ekinds(p, lc) == ["title_enum_uncountable"]


def test_c2_12_per_unit(tmp_path, lc):
    """c2-12 / C16: 「1社限定で毎月」「1社のみ毎年」の 1 は除外しない。"""
    p = write(tmp_path, "per-unit", f"""\
    月額は 1,980円 です。{M}
    先着1社限定で毎月割引があります。
    導入は1社のみ毎年更新。
    """)
    assert [(n, k) for n, k, _ in lint(p, lc)[0]] == [(12, "unmarked_number"), (13, "unmarked_number")]


# --------------------------------------------------------------------------
# c3・c4
# --------------------------------------------------------------------------

def test_c3_8b_calc_prose_with_nofact_formula(tmp_path, lc, cfg):
    """c3-8b / C7: 計算式に nofact を付けても、calc-block の文章はエラーで、publish_prep は止まる。"""
    p = write(tmp_path, "calc-prose2", f"""\
    | サービス | 月額 |
    |---|---|
    | X社 | 300円 |{M}

    計算式：月額 × 12 <!-- nofact -->

    <!-- calc-block -->
    X社は導入企業 50,000社、継続率 99% で業界No.1です。
    <!-- /calc-block -->
    """)
    assert ekinds(p, lc) == ["banned_expression", "calc_block_non_table"]
    assert lint_blocked(p, cfg)


def test_c3_13_no_lint_and_checks(tmp_path, cfg):
    """c3-13 / C3: lint を通っていない記事・checks が fail の記事は書き出さない。"""
    p = write(tmp_path, "no-lint", "導入企業は 12,000社 です。\n")
    errs = prep_errors(p, cfg)
    assert any(e.startswith("lint のエラーが 2 件") for e in errs)
    assert any("checks がありません" in e for e in errs)
    fm_fail = """---
type: A
slug: checks-fail
title: t
description: d
updated: 2026-10-01
checks:
  lint: {result: fail, date: 2026-10-01}
  owner_review: {result: pending}
history:
  - {date: 2026-10-01, note: 公開}
---
"""
    p = write(tmp_path, "checks-fail", f"月額は 1,980円 です。{M}\n", fm=fm_fail)
    errs = prep_errors(p, cfg)
    # C35: lint は publish_prep が自分で実行し、照合は受領ファイルで確かめる（checks.lint・factcheck_tool は必須でない）
    for needle in ("照合の受領ファイル", "checks.factcheck_session がありません",
                   "checks.owner_review.result が pass ではありません", "checks.owner_review.hash がありません"):
        assert any(needle in e for e in errs), needle


@pytest.mark.parametrize("slug", ['"abc\\n"', "nul", "con", '"a\\n"'])
def test_c3_14_c4_14b_slug(tmp_path, lc, cfg, slug, test_config_path):
    """c3-14・c4-14b / C20: 行末の改行を含む slug・Windows の予約名は、lint と publish_prep でエラー。書き出さない。"""
    fm = FM.format(slug=slug, title="t", desc="d")
    p = write(tmp_path, "slug-" + re.sub(r"\W", "", slug), f"月額は 1,980円 です。{M}\n", fm=fm)
    assert ekinds(p, lc) == ["slug_invalid"]
    assert any("slug" in e for e in prep_errors(p, cfg))
    cta = tmp_path / "cta.yaml"
    cta.write_text("patterns:\n  prog_001: 123\n", encoding="utf-8")
    build = tmp_path / "build"
    rc = publish_prep.main([str(p), "-c", str(test_config_path), "--out-dir", str(build), "--cta-patterns", str(cta)])
    assert rc == 1
    assert not build.exists() or os.listdir(build) == []


def test_c3_15_history_html(tmp_path, cfg):
    """c3-15 / C18: history の note の HTML はエスケープして出す。"""
    fm = """---
type: A
slug: hist-html
title: t
description: d
updated: 2026-10-01
history:
  - {date: 2026-10-01, note: "<script>alert(1)</script><img src=x onerror=alert(2)>"}
---
"""
    p = write(tmp_path, "hist-html", f"月額は 1,980円 です。{M}\n", fm=fm)
    html = publish_prep.prepare(p, cfg, {"prog_001": 123}).content_html
    assert "<script>" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in html


def test_c3_16_p7_duplicate_headings(tmp_path, cfg):
    """c3-16 / C19: 正本の「更新・訂正履歴」「参考資料一覧」の見出しも P7 の重複としてエラー。"""
    p = write(tmp_path, "p7-dup", f"""\
    月額は 1,980円 です。{M}

    ## 更新・訂正履歴

    - 変更点の説明

    ## 参考資料一覧

    - https://a.example.com/p
    """)
    errs = prep_errors(p, cfg)
    assert sum("P7 の見出し" in e for e in errs) == 2


def test_c3_17_unclosed_fence(tmp_path, lc, cfg):
    """c3-17 / C6: 閉じていないフェンスは lint のエラーで、P7 がコードブロックに入った成果物を書き出さない。"""
    p = write(tmp_path, "unclosed-fence", f"""\
    月額は 1,980円 です。{M}

    ```
    設定例
    """)
    assert ekinds(p, lc) == ["code_block", "part_missing", "unclosed_fence"]
    assert lint_blocked(p, cfg)


def test_c4_13b_publish_prep_main_blocks_lint_errors(tmp_path, test_config_path):
    """c4-13b / C3: publish_prep.main は lint エラーの記事を書き出さない（終了コード1）。"""
    p = write(tmp_path, "no-lint2", f"""\
    月額は 1,980円 です。{M}
    導入企業は 12,000社 で、業界シェアは 45% です。
    """)
    cta = tmp_path / "cta.yaml"
    cta.write_text("patterns:\n  prog_001: 123\n", encoding="utf-8")
    build = tmp_path / "build"
    rc = publish_prep.main([str(p), "-c", str(test_config_path), "--out-dir", str(build), "--cta-patterns", str(cta)])
    assert rc == 1 and not (build / "no-lint2" / "content.html").exists()
    assert factcheck.main(["--lint", str(p), "-c", str(test_config_path), "-o", str(tmp_path / "l.md")]) == 1


# --------------------------------------------------------------------------
# c5（postcheck）
# --------------------------------------------------------------------------

@pytest.fixture
def post_a(tmp_path, cfg):
    p = write(tmp_path, "post-a", f"""\
    ## 月額300円から使える製品 <!-- nofact -->

    X社の月額は 300円 です。{M}
    """)
    return p, publish_prep.prepare(p, cfg, {"prog_001": 123}, gate=False).content_html


def page(content, ad='<div class="ad-disclosure">本記事には広告が含まれます。</div>', head=""):
    return (f"<html><body><header>{head}</header><article><h1>T</h1>"
            f"<div class=\"entry-content\">{ad}{content}</div></article></body></html>")


def test_c5_postcheck_edits(post_a, cfg):
    """c5 / C8: 公開ページでの数値の書き換えと、出典のない数値の書き足しを報告する。"""
    p, content = post_a
    pc = PostConfig.from_cfg(cfg)
    assert check_page(p, page(content), pc)[0] == []
    edited = page(content.replace("月額は 300円", "月額は 350円") + "<p>導入企業は 50,000社、業界No.1です。</p>")
    issues = check_page(p, edited, pc)[0]
    ks = sorted(i.kind for i in issues)
    assert ks == ["number_changed", "number_extra"]
    assert any("350円" in i.detail for i in issues) and any("50,000社" in i.detail for i in issues)


def test_c5_postcheck_ad_position(post_a, cfg):
    """c5 / C9: 単引用符のソース・ヘッダの h2・フッタの広告表示でも、位置を正しく判定する。"""
    p, content = post_a
    pc = PostConfig.from_cfg(cfg)
    single = (f"<html><body><article><h1>T</h1><div class=\"entry-content\">{content}"
              f"<div class='ad-disclosure'>本記事には広告が含まれます。</div></div></article></body></html>")
    assert [i.kind for i in check_page(p, single, pc)[0]] == ["ad_disclosure_position"]
    double = single.replace("<div class='ad-disclosure'>", '<div class="ad-disclosure">')
    assert [i.kind for i in check_page(p, double, pc)[0]] == ["ad_disclosure_position"]
    assert check_page(p, page(content, head="<h2>サイトのキャッチコピー</h2>"), pc)[0] == []
    ad = '<div class="ad-disclosure">本記事には広告が含まれます。</div>'
    foot = (f"<html><body><article><h1>T</h1><div class=\"entry-content\">{content}</div></article>"
            f"<footer>{ad}</footer></body></html>")
    assert [i.kind for i in check_page(p, foot, pc)[0]] == ["ad_disclosure_position"]


# --------------------------------------------------------------------------
# c6
# --------------------------------------------------------------------------

def test_c6_t7_unit(tmp_path):
    """c6 / C14: 記事の 2,480円 が、出典側の単位のない 2480（問い合わせ番号）に一致したら候補に出す。"""
    p = write(tmp_path, "t7-unit", f"月額は 2,480円 です。{M}\n")
    cs, _ = factcheck.parse_article(p, [], True)
    iss, cos = factcheck.verify_claims(
        cs, FakeFetcher({"https://a.example.com/p": html_page("<p>月額 1,980円。お問い合わせ番号 2480</p>")}),
        False, 0.0, True, factcheck.CoincidenceConfig())
    assert iss == [] and len(cos) == 1 and "単位がない" in cos[0].reason


def test_c6_verify_message(tmp_path, monkeypatch, test_config_path):
    """c6 / C15: マーカーのない数値があれば、照合モードでも「確認不要」とは出さず、終了コード1。"""
    p = write(tmp_path, "verify-msg", f"月額は 1,980円 です。{M}\n導入企業は 12,000社 です。\n")
    monkeypatch.setattr(factcheck, "build_fetcher", lambda cfg, ignore_robots=False:
                        FakeFetcher({"https://a.example.com/p": html_page("<p>1,980円</p>")}))
    out = tmp_path / "v.md"
    rc = factcheck.main([str(p), "-c", str(test_config_path), "-o", str(out), "--sources-dir", str(tmp_path / "nosrc")])
    head = out.read_text(encoding="utf-8").splitlines()[3:7]
    assert rc == 1
    assert any("lint のエラー: 1 件" in h for h in head)
    assert "確認できました" not in out.read_text(encoding="utf-8")


def test_c6_lint_performance(tmp_path, test_config_path):
    """c6: 正本 300本の lint が現実的な時間で終わる（レビュー時は1,000本を計測）。"""
    d = tmp_path / "perf"
    d.mkdir()
    t = good_article_text()
    for i in range(300):
        (d / f"a{i}.md").write_text(t.replace("dental-clinic-attendance-system", f"a{i}"), encoding="utf-8")
    t0 = time.time()
    factcheck.main(["--lint", str(d), "-c", str(test_config_path), "-o", str(tmp_path / "perf.md")])
    assert time.time() - t0 < 60


# --------------------------------------------------------------------------
# c7（similarity）
# --------------------------------------------------------------------------

@pytest.fixture
def ind(tmp_path):
    p = tmp_path / "ind.csv"
    p.write_text("id,name,parent,name_en\ndental-clinic,歯科医院,medical,dental\nvet,動物病院,medical,vet\n",
                 encoding="utf-8")
    return p


def test_c7_renamed_headings(tmp_path, ind, test_config_path):
    """c7 / C12: 見出しを変えても、同じ中身は検出される（目印で部分を取り出す）。"""
    d = tmp_path / "simA"
    d.mkdir()
    t = good_article_text()
    (d / "a.md").write_text(t, encoding="utf-8")
    t2 = (t.replace("dental-clinic-attendance-system", "vet-attendance-system").replace("歯科医院", "動物病院")
          .replace("## 動物病院に必要な3つの要件", "## 動物病院で押さえる3つの条件").replace("## よくある質問", "## Q&A"))
    (d / "b.md").write_text(t2, encoding="utf-8")
    out = tmp_path / "sim.md"
    rc = sim.main([str(d), "-c", str(test_config_path), "--industries", str(ind), "-o", str(out)])
    assert rc == 1 and "超過" in out.read_text(encoding="utf-8")
    # 同じ中身・見出しそのまま（業種名だけ違う）
    (d / "b.md").write_text(t.replace("dental-clinic-attendance-system", "vet-attendance-system")
                            .replace("歯科医院", "動物病院"), encoding="utf-8")
    rc = sim.main([str(d), "-c", str(test_config_path), "--industries", str(ind), "-o", str(tmp_path / "sim2.md")])
    assert rc == 1


def test_c7_missing_parts_fail(tmp_path, ind, test_config_path):
    """c7 / C12: 部分がない型Bは、警告だけで終了コード0にしない。"""
    d = tmp_path / "simB"
    d.mkdir()
    t = re.sub(r"<!-- /?part(: \w+)? -->\n", "", good_article_text())
    (d / "a.md").write_text(t, encoding="utf-8")
    rc = sim.main([str(d), "-c", str(test_config_path), "--industries", str(ind), "-o", str(tmp_path / "s.md")])
    assert rc == 1


def test_c7_performance_400_docs(tmp_path, ind, cfg):
    """c7 / C25: 同じ parent の型B 400本（1本600語）の比較。レビュー時は68秒。"""
    d2 = tmp_path / "simperf"
    d2.mkdir()
    random.seed(1)
    words = "土曜 シフト 非常勤 打刻 拠点 集計 残業 深夜 有給 申請 承認 医院 スタッフ 管理 勤怠 休憩 月末 締め".split()
    t = good_article_text()
    for i in range(400):
        body = " ".join(random.choice(words) for _ in range(600))
        (d2 / f"b{i}.md").write_text(t.replace("dental-clinic-attendance-system", f"b{i}")
                                     .replace("非常勤のスタッフだけでも使えますか？", body), encoding="utf-8")
    sc = sim.SimConfig.from_cfg(cfg)
    t0 = time.time()
    docs, errors = sim.build_docs(sorted(d2.glob("*.md")), sc, sim.load_industry_names(ind))
    res = sim.compare(docs, sc)
    assert errors == [] and len(res) == 79800
    assert time.time() - t0 < 40


# --------------------------------------------------------------------------
# c8（改行をつなぐ）
# --------------------------------------------------------------------------

def test_c8_join(tmp_path, lc, cfg):
    """c8 / C17: Setext 見出し・インライン HTML・先頭に | のない表・リストの間のマーカーを壊さない。"""
    p = write(tmp_path, "join", f"""\
    月額は 1,980円 です。{M}
    <strong>注意：</strong>年契約のみです。

    まとめの見出し
    ===

    Xの料金は
    2. 5倍になります。

    サービス | 月額
    ---|---
    X社 | 300円 |{M}

    | サービス | 月額 |
    |---|---|
    | Y社 | 400円 |{M}
    （各社公式サイト）
    次の段落の文です。

    - 項目A
    <!-- nofact -->
    - 項目B
    """)
    ks = ekinds(p, lc)
    assert ks.count("table_row_without_pipe") == 2          # 表の直後の注記が表の行になっている
    assert "nofact_line_not_allowed" in ks
    html = publish_prep.prepare(p, cfg, None, gate=False).content_html
    assert "<p>月額は 1,980円 です。<strong>注意：</strong>年契約のみです。</p>" in html
    assert "<h1>まとめの見出し</h1>" in html
    assert "<li>項目A</li>\n<li>項目B</li>" in html        # ゆるいリストに変わらない
    assert html.count("<table>") == 2


# --------------------------------------------------------------------------
# c9
# --------------------------------------------------------------------------

def test_c9_18_traversal(tmp_path, monkeypatch, test_config_path):
    """c9-18 / C13: slug のパストラバーサルで、ほかの場所の記録を読ませない。"""
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "evil.md").write_text("## 手動確認の記録\n\n| URL | 数値 | 確認者 | 確認日 |\n|---|---|---|---|\n"
                                   "| https://a.example.com/p | 9,999円 | Claude | 2026-09-27 |\n", encoding="utf-8")
    fm = FM.format(slug="../elsewhere/evil", title="t", desc="d")
    p = write(tmp_path, "trav", f"月額は 9,999円 です。{M}\n", fm=fm)
    srcdir = tmp_path / "sources"
    srcdir.mkdir()
    monkeypatch.setattr(factcheck, "build_fetcher", lambda cfg, ignore_robots=False:
                        FakeFetcher({"https://a.example.com/p": html_page("<p>1,980円</p>")}))
    out = tmp_path / "trav.md"
    rc = factcheck.main([str(p), "-c", str(test_config_path), "-o", str(out), "--sources-dir", str(srcdir),
                         "--today", "2026-10-01"])
    assert rc == 1
    assert "手動確認の記録で報告から外したもの: 0 件" in out.read_text(encoding="utf-8")


def test_c9_19_u2028(tmp_path, lc, cfg):
    """c9-19 / C6: U+2028 で lint の行と描画の行がずれない（行は \\n だけで分ける）。"""
    p = write(tmp_path, "u2028", f"月額は 1,980円 です。{M}\n\n``` 導入企業は 12,000社 です。 ```\n")
    ks = ekinds(p, lc)
    assert "inline_code_number" in ks and "unmarked_number" in ks
    assert lint_blocked(p, cfg)


def test_c9_20_mixed_dir(tmp_path, test_config_path):
    """c9-20 / C22: ディレクトリの中の .html はエラー（黙って飛ばさない）。"""
    d = tmp_path / "mixed"
    d.mkdir()
    write(d, "a", f"月額は 1,980円 です。{M}\n")
    (d / "b.html").write_text("<p>導入企業は 12,000社 です。</p>", encoding="utf-8")
    assert factcheck.main(["--lint", str(d), "-c", str(test_config_path), "-o", str(tmp_path / "m.md")]) == 1


def test_c9_21_manual_record_by_claude(tmp_path, monkeypatch, test_config_path):
    """c9-21 / C13: 確認者「Claude」・理由なしの記録で、取得できたページの不一致を消せない。"""
    srcdir2 = tmp_path / "src2"
    srcdir2.mkdir()
    p = write(tmp_path, "mm", f"月額は 9,999円 です。{M}\n")
    (srcdir2 / "mm.md").write_text("## 手動確認の記録\n\n| URL | 数値 | 確認者 | 確認日 |\n|---|---|---|---|\n"
                                   "| https://a.example.com/p | 9,999円 | Claude | 2026-09-27 |\n", encoding="utf-8")
    monkeypatch.setattr(factcheck, "build_fetcher", lambda cfg, ignore_robots=False:
                        FakeFetcher({"https://a.example.com/p": html_page("<p>1,980円</p>")}))
    out = tmp_path / "mm-report.md"
    rc = factcheck.main([str(p), "-c", str(test_config_path), "-o", str(out), "--sources-dir", str(srcdir2),
                         "--today", "2026-10-01"])
    assert rc == 1
    assert "列 理由 がありません" in out.read_text(encoding="utf-8")   # 理由の列は必須


# --------------------------------------------------------------------------
# c10〜c12
# --------------------------------------------------------------------------

def test_c10_no1(tmp_path, lc):
    """c10 / C10: 「No.1」「1位」は禁止表現。「1人あたり」の 1 に title の 1 を一致させない。"""
    p = write(tmp_path, "no1", f"1人あたりの月額は 300円 です。{M}\n", title="業界No.1の勤怠管理システム", desc="シェア1位")
    ks = ekinds(p, lc)
    assert ks.count("banned_expression") == 2 and "title_fact_unsourced" in ks
    p = write(tmp_path, "no1b", f"月額は 300円 です。{M}\n", title="業界No.1の勤怠管理システム")
    assert "banned_expression" in ekinds(p, lc)


def test_c11_bad_byte(tmp_path, lc, cfg):
    """c11 / C26: UTF-8 でも cp932 でも読めない正本は、原因（文字コード）を示してエラーにする。"""
    p = write(tmp_path, "badbyte", f"月額は 1,980円 です。{M}\n導入の流れを説明します。\n")
    p.write_bytes(p.read_bytes().replace("導入の流れ".encode(), "導入の流れ".encode() + b"\xff"))
    errs, _ = lint(p, lc)
    assert [k for _n, k, _ in errs] == ["encoding"]
    res = publish_prep.prepare(p, cfg, {"prog_001": 123})
    assert any("文字コード" in m.detail for m in res.warnings)
    assert any("encoding" in m.detail for m in res.errors)


def test_c11_cp932_is_warning(tmp_path, lc):
    """C26: cp932 で読めた正本は警告（UTF-8 で保存し直す）。"""
    p = write(tmp_path, "sjis", f"月額は 1,980円 です。{M}\n", enc="cp932")
    errs, warns = lint(p, lc)
    assert errs == [] and warns == ["encoding"]


def test_c12_html_table(tmp_path, lc, cfg):
    """c12 / C5・C6: HTML の表とコードブロックは lint のエラー（nofact で逃がさない）。"""
    p = write(tmp_path, "html-table", f"""\
    月額は 1,980円 です。{M}

    <table><tr><td>X社</td><td>導入 12,000社</td><td>最低 10人</td></tr></table> <!-- nofact -->

    ```
    Y社の月額 9,999円（2026年9月時点）
    ```
    """)
    assert ekinds(p, lc) == ["code_block", "html_table", "nofact_line_not_allowed"]
    assert lint_blocked(p, cfg)


def test_postcheck_uses_same_rendering(tmp_path, cfg):
    """C8 の前提: postcheck の比較元は publish_prep と同じ描画（マーカー除去・P7付き）。"""
    p = write(tmp_path, "render", f"月額は 1,980円 です。{M}\n")
    text = postcheck.canonical_text(p, cfg)
    assert "1,980円" in text and "参照日" in text and "src:" not in text
    assert AD_HTML  # conftest の部品を使っている（未使用 import の防止）
