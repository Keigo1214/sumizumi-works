"""独立レビュー（再レビュー）の再現ケース c13〜c18 をそのまま回帰テストにしたもの。

入力はレビュアーのハーネス（scratchpad/review/c13.py〜c18.py）と同じ。記事は test_review_regressions.py と同じ
`write`（h.py の FM）で作る。make_article は関連記事の区間（型A・型Bとも必須）を末尾に足す。
"""

from __future__ import annotations

import re

import pytest

import factcheck
import publish_prep
from article import find_expressions, find_kanji_hits, DEFAULT_BANNED_EXPRESSIONS, DEFAULT_CAUTION_EXPRESSIONS
from article_lint import LintConfig, lint_article
from common import html_to_text
from conftest import (
    CTA_HTML,
    FakeFetcher,
    good_article_text,
    html_page,
    published_page,
    stamp_checks,
    write_related_targets,
    write_test_receipt,
)
from postcheck import PostConfig, check_page
from test_review_regressions import FM, M, write

M_P = M


@pytest.fixture
def lc(cfg):
    return LintConfig.from_cfg(cfg)


def errs(p, lc):
    return [(i.line_no, i.kind, i.number) for i in lint_article(p, lc).errors]


def warns(p, lc):
    return [(i.kind, i.number) for i in lint_article(p, lc).warnings]


# --------------------------------------------------------------------------
# c13 A: 普通の言い回し（C28）
# --------------------------------------------------------------------------

C13_NORMAL = [
    "スタッフ一人ひとりの打刻を管理できます。", "一人で運営している医院でも使えます。", "一人あたりの料金で比べます。",
    "一日の勤務時間を集計します。", "一年を通して繁忙期があります。", "一社ずつ比較しました。",
    "二段階認証に対応しています。", "三つのポイントを押さえます。", "一回の打刻で記録されます。",
    "一か月単位で契約できます。", "一時間単位で残業を集計します。", "週一回の締め作業が必要です。",
    "月一回の給与計算に連携します。", "二重打刻を防げます。", "一元管理できます。", "一括で登録できます。",
    "一人二役をこなすスタッフもいます。", "三交代制のシフトに対応します。", "二交代制の病棟", "一名から契約できます。",
    "必ず無料トライアルで確かめてください。", "絶対に必要な機能ではありません。",
    "最安のプランは機能が限られます。", "第一に、打刻方法を確認します。", "一番の違いは料金体系です。",
    "36協定の上限を超えないように管理します。", "労働基準法第32条", "百貨店", "八百屋", "九州の拠点",
    "四半期ごとに見直します。", "五十音順に並べています。", "一つひとつ確認します", "千葉県の医院",
]
# 方針（C28）: エラーにするのは二以上の漢数字と、金額・割合・万千億を伴うもの。「一」＋助数詞は警告
C13_EXPECTED_KANJI = {
    "一人で運営している医院でも使えます。": [("一人", "warning")],
    "一日の勤務時間を集計します。": [("一日", "warning")],
    "一年を通して繁忙期があります。": [("一年", "warning")],
    "一社ずつ比較しました。": [("一社", "warning")],
    "三つのポイントを押さえます。": [("三つ", "error")],          # 二以上は算用数字で（3つ）
    "一回の打刻で記録されます。": [("一回", "warning")],
    "一か月単位で契約できます。": [("一か月", "warning")],
    "一時間単位で残業を集計します。": [("一時間", "warning")],    # 除外の「一時」は「一時間」に当たらない
    "一名から契約できます。": [("一名", "warning")],
}


@pytest.mark.parametrize("sentence", C13_NORMAL)
def test_c13_a_normal_phrases(sentence):
    """c13 A / C28: 普通の言い回しで、漢数字のエラーは「三つ」だけ。決まった言い回しは data/terms.yaml で除外。"""
    hits = [(h.text, h.severity) for h in find_kanji_hits(sentence)]
    assert hits == C13_EXPECTED_KANJI.get(sentence, []), hits


def test_c13_a_expressions():
    """c13 A: 禁止表現は「最安」、注意（警告）は「必ず」「絶対」。§1-6 の一覧どおり（方針として残す）。"""
    banned = {s: find_expressions(s, DEFAULT_BANNED_EXPRESSIONS) for s in C13_NORMAL}
    caution = {s: find_expressions(s, DEFAULT_CAUTION_EXPRESSIONS) for s in C13_NORMAL}
    assert {s: b for s, b in banned.items() if b} == {"最安のプランは機能が限られます。": ["最安"]}
    assert {s: c for s, c in caution.items() if c} == {
        "必ず無料トライアルで確かめてください。": ["必ず"], "絶対に必要な機能ではありません。": ["絶対"]}


def test_c13_a_terms_in_lint(tmp_path, lc):
    """c13 A / C28: 「36協定」「二段階認証」「週一回」は data/terms.yaml の語なので、lint でも数値にならない。"""
    p = write(tmp_path, "terms", f"月額は 1,980円 です。{M}\n36協定の上限を管理し、二段階認証に対応します。週一回の締めがあります。\n")
    assert errs(p, lc) == [] and warns(p, lc) == []


def test_c13_b_calc_facts(tmp_path, lc):
    """c13 B / C29: 試算の条件に金額・社数、試算表の見出しとセルに事実の数値を入れられない。"""
    p = write(tmp_path, "calc-facts", f"""\
    試算の条件：導入企業 50,000社 のX社を、月額 2,000円 の相場で、10人で利用 <!-- nofact -->

    | サービス | 1人あたり月額 |
    |---|---|
    | X社 | 300円 |{M}

    計算式：1人あたり月額 × 利用人数

    <!-- calc-block -->
    | サービス | 月額の試算（導入50,000社・継続率99%） |
    |---|---|
    | X社 | 3,000円（導入企業 50,000社） |
    <!-- /calc-block -->
    """)
    assert errs(p, lc) == [
        (11, "condition_unit", "50,000社"), (11, "condition_unit", "2,000円"),
        (20, "calc_header_number", "50,000社"), (20, "calc_header_number", "99%"),
        (22, "calc_cell_format", "3,000円（導入企業 50,000社）")]


def test_c13_c_ordinal(tmp_path, lc):
    """c13 C / C30: 「ポイントN」は、N の後ろに単位がなく区切りが続くときだけ順番とみなす。"""
    p = write(tmp_path, "ordinal", f"""\
    月額は 1,980円 です。{M}

    ## ポイント1,000円分を還元 <!-- nofact -->

    ## 最大ポイント10倍 <!-- nofact -->

    ## 3つの要件（導入企業 12,000社） <!-- nofact -->
    """)
    assert errs(p, lc) == [(13, "nofact_heading_not_enum", "1,000円"), (15, "nofact_heading_not_enum", "10"),
                           (17, "nofact_heading_not_enum", "12,000社")]


def test_c13_d_zero_width(tmp_path, lc, cfg):
    """c13 D / C33: ゼロ幅スペースは lint のエラー。照合は除いてから数値を読む（1 と 2,000社 に分かれない）。"""
    p = write(tmp_path, "zwsp", f"導入企業は 1​2,000社 です。{M}\n")
    claims, _ = factcheck.parse_article(p, [], True)
    assert [[t.raw for t in c.numbers] for c in claims] == [["12,000社"]]
    assert errs(p, lc) == [(11, "invisible_char", "U+200B")]
    html = publish_prep.prepare(p, cfg, {"prog_001": 123}, gate=False).content_html
    assert "​" not in html


# --------------------------------------------------------------------------
# c14: CTA の同期パターン（C32）
# --------------------------------------------------------------------------

TOC = ('<div id="toc" class="toc tnt-number toc-center"><input type="checkbox" class="toc-checkbox" id="toc-checkbox-1" checked>'
       '<label class="toc-title" for="toc-checkbox-1">目次</label><div class="toc-content"><ol class="toc-list open">'
       '<li><a href="#toc1">歯科医院で勤怠管理システム選びが難しい理由</a></li><li><a href="#toc2">歯科医院に必要な3つの要件</a></li></ol></div></div>')
CTA_COCOON = ('<div class="wp-block-cocoon-blocks-button-1 button-block"><a href="https://px.a8.net/svt/ejp?a8mat=XXXX" '
              'class="btn btn-l btn-red" rel="nofollow sponsored">30日間の無料トライアル</a>'
              '<img src="https://www17.a8.net/0.gif?a8mat=XXXX" width="1" height="1" alt=""></div>')


@pytest.fixture
def wp_page_parts(good_article, cfg):
    r = publish_prep.prepare(good_article, cfg, {"prog_001": 123})
    assert r.errors == [], [m.detail for m in r.errors]
    wp = r.content_html.replace("<table>", '<figure class="wp-block-table"><table>').replace("</table>", "</table></figure>")
    wp = wp.replace("<h2>", TOC + "<h2>", 1)
    return good_article, wp


def kinds(issues):
    return sorted(i.kind for i in issues)


def test_c14_cocoon_like_page(wp_page_parts, cfg):
    """c14 / C32: 表を figure で包み、目次を入れた WordPress 風のページでも誤検知しない。"""
    p, wp = wp_page_parts
    assert check_page(p, published_page(wp), PostConfig.from_cfg(cfg))[0] == []


def test_c14_cta_without_class(wp_page_parts, cfg):
    """c14: .cta のないボタン（Cocoon のボタンブロック）の中の数値は、ページにだけある数値として出る。"""
    p, wp = wp_page_parts
    page = published_page(wp).replace(CTA_HTML, CTA_COCOON)
    issues = check_page(p, page, PostConfig.from_cfg(cfg))[0]
    assert kinds(issues) == ["number_extra"] and "30日" in issues[0].detail


def test_c14_number_inside_cta(wp_page_parts, cfg):
    """c14 / C32: .cta の中の数値（同期パターンに書いた料金）はすべてエラー。"""
    p, wp = wp_page_parts
    page = published_page(wp).replace("公式サイトで詳しく見る", "公式サイトを見る（月額100円〜・導入50,000社）")
    assert kinds(check_page(p, page, PostConfig.from_cfg(cfg))[0]) == ["cta_number", "cta_number"]


def test_c14_text_added_into_cta(wp_page_parts, cfg):
    """c14 / C32: WordPress 上で .cta のグループに本文を書き足すと、数値と余分な要素のエラーになる。"""
    p, wp = wp_page_parts
    page = published_page(wp).replace('<div class="wp-block-group cta">',
                                      '<div class="wp-block-group cta"><p>X社は導入企業 50,000社・継続率 99% です。</p>', 1)
    ks = kinds(check_page(p, page, PostConfig.from_cfg(cfg))[0])
    assert ks.count("cta_number") == 2 and "cta_element" in ks


# --------------------------------------------------------------------------
# c15: part: related（v0.4 の書き方）
# --------------------------------------------------------------------------

def test_c15_related_part(tmp_path, lc):
    """c15: related は知らない部分の名前ではなくなった。v0.4 の例（前置きなし・リンク先なし）は型Aの形・リンク先の検査で止まる。"""
    fm = FM.format(slug="related-v04", title="テスト記事", desc="テストの説明")
    body = f"""\
    月額は 1,980円 です。{M}

    ## 関連記事

    <!-- part: related -->
    - [歯科医院の勤怠管理](/dental-clinic-attendance-system/)
    <!-- /part -->
    """
    from conftest import make_article
    p = make_article(tmp_path, body, name="related-v04", front_matter=fm, related=False)
    ks = [k for _n, k, _ in errs(p, lc)]
    assert "part_invalid" not in ks
    assert ks == ["related_invalid", "related_invalid"]          # 型Aの形でない・リンク先の正本がない
    # 型Aの決まった形・存在する公開済みの記事なら通る
    write_related_targets(tmp_path)
    p.write_text(p.read_text(encoding="utf-8").replace(
        "- [歯科医院の勤怠管理](/dental-clinic-attendance-system/)",
        "- 歯科医院：[歯科医院の勤怠管理システムの選び方](/dental-sample/)"), encoding="utf-8")
    assert errs(p, lc) == []


# --------------------------------------------------------------------------
# c16: history の note（C31）
# --------------------------------------------------------------------------

def test_c16_history_note(tmp_path, lc, cfg):
    """c16 / C31: note の数値・漢数字・禁止表現はエラー。publish_prep は lint で止まる。"""
    fm = """---
type: A
keyword: テスト
slug: hist-num
title: "テスト記事"
description: "テストの説明"
updated: 2026-10-01
history:
  - {date: 2026-10-01, note: "X社は業界最安の月額250円、導入一万社（使ってみた結果を追記）"}
---
"""
    p = write(tmp_path, "hist-num", f"月額は 1,980円 です。{M}\n", fm=fm)
    ks = sorted(k for _n, k, _ in errs(p, lc))
    assert ks == ["banned_expression", "banned_expression", "history_note", "kanji_number"]
    res = publish_prep.prepare(p, cfg, {"prog_001": 123})
    assert any("history_note" in m.detail for m in res.errors)


# --------------------------------------------------------------------------
# c17: 偶然の一致の運用（C27）
# --------------------------------------------------------------------------

C17_PAGES = {
    "https://example.com/pricing": html_page("<table><tr><td>ライト</td><td>1,000円</td><td>10人</td></tr><tr><td>スタンダード</td><td>2,980円</td><td>50人</td></tr></table><p>月額 1,980円</p>"),
    "https://x.example.com/pricing": html_page("<table><tr><th>初期費用</th><td>0円</td></tr><tr><th>月額</th><td>300円/人</td></tr><tr><th>最低利用人数</th><td>10人から</td></tr></table>"),
    "https://y.example.com/price": html_page("<p>初期費用 0円</p><p>1ユーザーあたり月額400円</p>"),
    "https://z.example.com/plan": html_page("<p>初期費用0円、月額250円（年契約）</p>"),
    "https://stat.example.go.jp/dental": html_page("<p>土曜日に診療を行う歯科診療所は約6割</p>"),
    "https://stat.example.go.jp/hygienist": html_page("<p>非常勤の歯科衛生士は約3割</p>"),
    "https://law.example.go.jp/corp": html_page("<p>医療法人は分院を開設できる</p>"),
}


def test_c17_candidates(tmp_path, cfg):
    """c17: 正しい記事でも候補は9件出る（0円×5・6割・3割・10人と10人から×2）。仕様どおりの検出。"""
    write_related_targets(tmp_path)
    p = tmp_path / "dental-clinic-attendance-system.md"
    p.write_text(good_article_text(), encoding="utf-8")
    cs, _ = factcheck.parse_article(p, cfg["factcheck"]["ignore_patterns"], True)
    iss, cos = factcheck.verify_claims(cs, FakeFetcher(C17_PAGES), False, 0.0, True, factcheck.CoincidenceConfig())
    assert iss == [] and len(cos) == 9
    assert all(c.page_hash for c in cos)


def test_c17_operation(tmp_path, monkeypatch, test_config_path):
    """c17 / C27: 関門では候補があれば不合格、週次は報告だけ。別セッションが種類「偶然の一致」で記録すれば合格し、
    受領ファイルが書かれる。出典ページの本文が変われば記録は無効になる。"""
    from conftest import CHECKS_DIR
    write_related_targets(tmp_path)
    p = tmp_path / "dental-clinic-attendance-system.md"
    p.write_text(stamp_checks(good_article_text(), tmp_path), encoding="utf-8")
    srcdir = tmp_path / "sources"
    srcdir.mkdir()
    receipt = CHECKS_DIR / "dental-clinic-attendance-system.json"
    if receipt.exists():
        receipt.unlink()

    def run(pages, *extra):
        monkeypatch.setattr(factcheck, "build_fetcher", lambda cfg, ignore_robots=False: FakeFetcher(pages))
        out = tmp_path / "r.md"
        rc = factcheck.main([str(p), "-c", str(test_config_path), "-o", str(out), "--sources-dir", str(srcdir),
                             "--today", "2026-10-01", *extra])
        return rc, out.read_text(encoding="utf-8")

    rc, report = run(C17_PAGES)
    assert rc == 1 and "偶然の一致の候補: 9 件" in report and not receipt.exists()
    rc, report = run(C17_PAGES, "--mode", "weekly")
    assert rc == 0 and "週次の照合" in report and not receipt.exists()

    # 別セッションのファクトチェックが、ページのハッシュつきで記録する
    cs, _ = factcheck.parse_article(p, [r"^\d{1,2}月$"], True)
    _iss, cos = factcheck.verify_claims(cs, FakeFetcher(C17_PAGES), False, 0.0, True, factcheck.CoincidenceConfig())
    rows = sorted({f"| {c.url} | {c.token.raw} | 偶然の一致 | factcheck_session | 2026-10-01 | 偶然の一致の確認 | {c.page_hash} | |"
                   for c in cos})
    (srcdir / "dental-clinic-attendance-system.md").write_text(
        "## 手動確認の記録\n\n| URL | 数値 | 種類 | 確認者 | 確認日 | 理由 | ページのハッシュ | メモ |\n"
        "|---|---|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n", encoding="utf-8")
    rc, report = run(C17_PAGES)
    assert rc == 0, report
    assert receipt.exists()
    # 出典ページの本文が変わると、その記録は無効になり候補に戻る
    changed = dict(C17_PAGES)
    changed["https://y.example.com/price"] = html_page("<p>初期費用 0円（改定）</p><p>1ユーザーあたり月額400円</p>")
    receipt.unlink()
    rc, report = run(changed)
    assert rc == 1 and "本文が変わった" in report and not receipt.exists()


# --------------------------------------------------------------------------
# c18: 部分の目印を狭める（C34）
# --------------------------------------------------------------------------

def test_c18_narrow_parts(tmp_path, lc):
    """c18 / C34: 目印を中身のない1行だけに狭め、本物の FAQ・チェックリストを外に出すとエラー。"""
    write_related_targets(tmp_path)
    t = good_article_text()
    t2 = re.sub(r"<!-- part: faq -->\n(.*?)<!-- /part -->",
                lambda m: "<!-- part: faq -->\nQ. 使えますか？\n<!-- /part -->\n" + m.group(1), t, flags=re.S)
    t2 = re.sub(r"<!-- part: checklist -->\n(.*?)<!-- /part -->",
                lambda m: "<!-- part: checklist -->\n- 必須：打刻\n<!-- /part -->\n" + m.group(1), t2, flags=re.S)
    p = tmp_path / "part-narrow.md"
    p.write_text(t2, encoding="utf-8")
    ks = sorted(k for _n, k, _ in errs(p, lc))
    assert ks.count("part_not_whole") == 2 and ks.count("part_too_small") == 2
    assert html_to_text  # 未使用 import の防止
    assert write_test_receipt and M_P
