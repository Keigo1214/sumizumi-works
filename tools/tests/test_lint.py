"""T1: factcheck.py --lint（設計書 §8 T1・§1-2・§1-6・§4）。T3（'^\\d{1,2}月$'）も含む。

レビューの再現ケース（c1〜c12）は test_review_regressions.py にある。
"""

from __future__ import annotations

import pytest

import factcheck
from article_lint import LintConfig, lint_article
from conftest import (
    SPEC_BLOCK,
    SPEC_CALC,
    SPEC_INLINE,
    SPEC_P2,
    SPEC_TRACE_FILLED,
    SPEC_TRACE_TEMPLATE,
    make_article,
)

M = "<!-- src: https://a.example.com/ accessed=2026-09-27 -->"


@pytest.fixture
def lc(cfg):
    return LintConfig.from_cfg(cfg)


def kinds(res, severity="error"):
    return [i.kind for i in res.issues if i.severity == severity]


def errs(res):
    return [(i.line_no, i.kind, i.number) for i in res.errors]


# --------------------------------------------------------------------------
# 正常系：設計書の例がそのまま通る
# --------------------------------------------------------------------------

def test_good_article_passes(good_article, lc):
    res = lint_article(good_article, lc)
    assert res.errors == [], [(i.line_no, i.kind, i.detail, i.number) for i in res.errors]
    assert res.warnings == [], [(i.line_no, i.kind, i.detail) for i in res.warnings]
    # nofact と calc-block の件数と行番号が集計される
    assert len(res.nofact_lines) == 2           # 見出し1つ＋試算の条件
    assert len(res.calc_blocks) == 1
    s, e = res.calc_blocks[0]
    lines = good_article.read_text(encoding="utf-8").splitlines()
    assert "calc-block" in lines[s - 1] and "/calc-block" in lines[e - 1]
    assert res.marker_count == 3 + 1 + 2 + 3 + 1     # トレース表3・src-block1・P2 2・入力値の表3・行内1
    assert len(res.content_hash) == 16


@pytest.mark.parametrize("snippet", [SPEC_INLINE, SPEC_BLOCK, SPEC_CALC, SPEC_P2, SPEC_TRACE_FILLED])
def test_each_spec_example_passes_alone(tmp_path, lc, snippet):
    path = make_article(tmp_path, snippet + "\n")
    res = lint_article(path, lc)
    assert kinds(res) == [], [(i.line_no, i.kind, i.detail, i.number) for i in res.errors]


def test_month_in_reader_note_is_ignored_by_t3_pattern(tmp_path, cfg):
    """T3: 読者向けの「2026年9月時点」の 9月 は ignore_patterns で外れる。"""
    assert r"^\d{1,2}月$" in cfg["factcheck"]["ignore_patterns"]
    path = make_article(tmp_path, SPEC_INLINE + "\n")
    assert kinds(lint_article(path, LintConfig.from_cfg(cfg))) == []
    # パターンを外すと「最後のマーカーより後ろの数値」として報告される
    cfg["factcheck"]["ignore_patterns"] = [p for p in cfg["factcheck"]["ignore_patterns"]
                                           if p != r"^\d{1,2}月$"]
    res = lint_article(path, LintConfig.from_cfg(cfg))
    assert kinds(res) == ["after_last_marker"]
    assert res.errors[0].number == "9月"


def test_excluded_regions(tmp_path, lc):
    """front matter・番号付きリストの番号・nofact（許される行）・{{CTA}}・URL・「1施設あたり」の1 は対象外。"""
    body = f"""\
    月額は 500円 です。{M}

    1. 管理画面を開く
    2) 打刻の方法を選ぶ

    試算の条件：10人で利用する場合 <!-- nofact -->

    ## ポイント2：料金 <!-- nofact -->

    詳しくは https://docs.example.com/v2/page123 を参照してください。

    {{{{CTA:prog_001}}}}

    1施設あたりの登録は無制限です。
    """
    path = make_article(tmp_path, body, title="タイトル 2026年版", description="説明")
    res = lint_article(path, lc)
    assert kinds(res) == [], errs(res)
    assert len(res.nofact_lines) == 2


def test_ordered_list_number_only_stripped_for_real_list_items(tmp_path, lc):
    """段落の途中の「2. 5倍」はリストではない（CommonMark）ので、2 と 5 を数値として数える。"""
    path = make_article(tmp_path, f"""\
    月額は 500円 です。{M}
    Xの料金は
    2. 5倍になります。
    """)
    assert [e[2].split()[0] for e in errs(lint_article(path, lc))] == ["2", "5"]


# --------------------------------------------------------------------------
# 異常系：設計書が禁止している書き方
# --------------------------------------------------------------------------

def test_unmarked_number_is_error(tmp_path, lc):
    path = make_article(tmp_path, f"""\
    月額は 1,980円 です。{M}
    導入企業は 12,000社 を超えています。
    """)
    res = lint_article(path, lc)
    assert kinds(res) == ["unmarked_number"]
    assert res.errors[0].line_no == 12      # front matter 10行 + 本文2行目
    assert "12,000社" in res.errors[0].number


def test_heading_number_without_nofact_is_error(tmp_path, lc):
    path = make_article(tmp_path, f"""\
    月額は 1,980円 です。{M}

    ## 選ぶときの比較ポイント5つ
    """)
    res = lint_article(path, lc)
    assert kinds(res) == ["unmarked_number"]
    assert "nofact" in res.errors[0].detail


def test_setext_heading_is_a_heading(tmp_path, lc):
    path = make_article(tmp_path, f"""\
    月額は 1,980円 です。{M}

    必要な3つの要件 <!-- nofact -->
    ---

    - A
    - B
    - C
    """, title="必要な3つの要件")
    assert kinds(lint_article(path, lc)) == []


def test_number_after_last_marker(tmp_path, lc):
    """§1-2 制約4: マーカーの後ろには読者向けの出典表記以外を書かない。"""
    path = make_article(tmp_path, f"""\
    月額は 1,980円 です。{M}年額は 19,800円 です。
    """)
    res = lint_article(path, lc)
    assert kinds(res) == ["after_last_marker"]
    assert res.errors[0].number == "19,800円"


def test_reader_note_before_marker_puts_month_in_range(tmp_path, lc):
    """§1-2 制約4: 出典表記をマーカーの前に置くと、「9月」がそのマーカーの照合範囲に入る。

    config.example.yaml の T3 のパターン（'^\\d{1,2}月$'）があれば 9月 は外れる（T3 はこの保険）。
    パターンがなければ 9月 も出典URLで照合され、不一致として報告される。lint はどちらの場合も通る
    （範囲の中の数値なので「マーカーのない数値」ではない）。
    """
    path = make_article(tmp_path, f"""\
    月額は 1,980円 です（◯◯公式サイト、2026年9月時点）。{M}
    """)
    claims, _ = factcheck.parse_article(path, [], True)
    assert [t.raw for t in claims[0].numbers] == ["1,980円", "9月"]
    claims, _ = factcheck.parse_article(path, [r"^\d{1,2}月$"], True)
    assert [t.raw for t in claims[0].numbers] == ["1,980円"]
    assert kinds(lint_article(path, lc)) == []


def test_missing_accessed(tmp_path, lc):
    path = make_article(tmp_path, """\
    月額は 1,980円 です。<!-- src: https://a.example.com/ -->
    <!-- src-block: https://b.example.com/ -->
    | a | b |
    |---|---|
    | x | 1円 |
    <!-- /src-block -->
    """)
    res = lint_article(path, lc)
    assert kinds(res) == ["accessed_missing", "accessed_missing"]


def test_accessed_bad_format(tmp_path, lc):
    path = make_article(tmp_path, """\
    月額は 1,980円 です。<!-- src: https://a.example.com/ accessed=2026/09/27 -->
    """)
    assert kinds(lint_article(path, lc)) == ["accessed_invalid"]


def test_zero_markers_is_error_and_exit_1(tmp_path, test_config_path):
    path = make_article(tmp_path, "数値のない本文です。\n")
    from common import load_config
    res = lint_article(path, LintConfig.from_cfg(load_config(str(test_config_path))))
    assert kinds(res) == ["no_markers"]
    assert factcheck.main(["--lint", str(path), "-c", str(test_config_path),
                           "-o", str(tmp_path / "r.md")]) == 1


def test_lint_exit_0_for_good_article(good_article, tmp_path, test_config_path):
    out = tmp_path / "lint.md"
    js = tmp_path / "lint.json"
    rc = factcheck.main(["--lint", str(good_article), "-c", str(test_config_path),
                         "-o", str(out), "--json", str(js)])
    assert rc == 0
    report = out.read_text(encoding="utf-8")
    assert "判定: 合格" in report
    assert "nofact: 2 件" in report and "calc-block: 1 件" in report
    assert "正本のハッシュ" in report


def test_lint_does_not_fetch(good_article, monkeypatch, test_config_path):
    def boom(*_a, **_k):
        raise AssertionError("lint はネットワークに出てはいけない")
    monkeypatch.setattr(factcheck, "build_fetcher", boom)
    assert factcheck.main(["--lint", str(good_article), "-c", str(test_config_path),
                           "-o", str(good_article.parent / "o.md")]) == 0


def test_calc_block_without_formula_is_error(tmp_path, lc):
    body = SPEC_CALC.replace("計算式：1人あたり月額 × 利用人数（最低人数を下回る場合は最低人数で計算）\n\n", "")
    path = make_article(tmp_path, body + "\n")
    res = lint_article(path, lc)
    assert "calc_block_no_basis" in kinds(res)
    assert "計算式" in [i for i in res.errors if i.kind == "calc_block_no_basis"][0].detail


def test_calc_block_without_input_table_is_error(tmp_path, lc):
    path = make_article(tmp_path, f"""\
    月額は 1,980円 です。{M}

    計算式：月額 × 利用月数

    <!-- calc-block -->
    | サービス | 年額の試算 |
    |---|---|
    | X社 | 23,760円 |
    <!-- /calc-block -->
    """)
    res = lint_article(path, lc)
    assert kinds(res) == ["calc_block_no_basis"]


def test_calc_block_with_unmarked_input_table_is_error(tmp_path, lc):
    """出典のない数値を calc-block に入れて lint を逃れる抜け道をふさぐ（§1-2）。"""
    body = SPEC_CALC.replace(
        "| Y社 | 0円 | 400円 | なし | 月払い可 |<!-- src: https://y.example.com/price accessed=2026-09-27 -->",
        "| Y社 | 0円 | 400円 | なし | 月払い可 |")
    path = make_article(tmp_path, body + "\n")
    res = lint_article(path, lc)
    assert "calc_block_no_basis" in kinds(res)
    assert "unmarked_number" in kinds(res)


def test_calc_block_with_src_block_input_table_passes(tmp_path, lc):
    path = make_article(tmp_path, """\
    <!-- src-block: https://x.example.com/pricing accessed=2026-09-27 -->
    | プラン | 1人あたり月額 |
    |---|---|
    | ライト | 300円 |
    <!-- /src-block -->
    （X社公式サイト、2026年9月時点・税抜）

    計算式：1人あたり月額 × 利用人数

    <!-- calc-block -->
    | プラン | 月額の試算 |
    |---|---|
    | ライト | 3,000円 |
    <!-- /calc-block -->
    """)
    assert kinds(lint_article(path, lc)) == []


def test_calc_block_rows_must_be_in_input_table(tmp_path, lc):
    """C7: 試算表の1列目の名前は、入力値の表の1列目にあること。"""
    body = SPEC_CALC.replace("| Z社 | 試算の対象外（月払いができないため） |", "| W社 | 5,000円 |")
    path = make_article(tmp_path, body + "\n")
    res = lint_article(path, lc)
    assert kinds(res) == ["calc_row_unknown"]
    assert "W社" in res.errors[0].detail


def test_calc_block_only_table_rows(tmp_path, lc):
    """C7: calc-block の中は表の行だけ（文章は照合も lint もされないため）。"""
    body = SPEC_CALC.replace("| Z社 | 試算の対象外（月払いができないため） |\n",
                             "| Z社 | 試算の対象外（月払いができないため） |\n\nX社は導入企業 50,000社 です。\n")
    path = make_article(tmp_path, body + "\n")
    assert "calc_block_non_table" in kinds(lint_article(path, lc))


def test_calc_block_nested_in_src_block_is_error(tmp_path, lc):
    """§1-2 制約7。"""
    path = make_article(tmp_path, f"""\
    月額は 1,980円 です。{M}
    <!-- src-block: https://b.example.com/ accessed=2026-09-27 -->
    | a | b |
    |---|---|
    | x | 100円 |
    <!-- calc-block -->
    | y | 300円 |
    <!-- /calc-block -->
    <!-- /src-block -->
    """)
    res = lint_article(path, lc)
    assert "nested_block" in kinds(res)


def test_src_block_nested_in_calc_block_is_error(tmp_path, lc):
    path = make_article(tmp_path, SPEC_CALC.replace(
        "<!-- calc-block -->\n",
        "<!-- calc-block -->\n<!-- src-block: https://b.example.com/ accessed=2026-09-27 -->\n").replace(
        "<!-- /calc-block -->", "<!-- /src-block -->\n<!-- /calc-block -->") + "\n")
    res = lint_article(path, lc)
    assert "nested_block" in kinds(res)


def test_footnote_is_error(tmp_path, lc):
    """§1-2: 脚注（[^a]）は使わない。"""
    path = make_article(tmp_path, """\
    導入企業は 12,000社 を超える[^a]。

    [^a]: https://example.com/about accessed=2026-07-20
    """)
    res = lint_article(path, lc)
    assert kinds(res).count("footnote_used") == 2
    assert "no_markers" in kinds(res)   # 脚注はマーカーに数えない


def test_footnote_on_same_line_as_inline_marker_is_error(tmp_path, lc):
    path = make_article(tmp_path, f"""\
    月額は 1,980円 です[^a]。{M}

    [^a]: https://example.com/about accessed=2026-07-20
    """)
    assert "footnote_used" in kinds(lint_article(path, lc))


# --------------------------------------------------------------------------
# nofact（C4）
# --------------------------------------------------------------------------

def test_nofact_with_src_marker_is_error(tmp_path, lc):
    path = make_article(tmp_path, f"""\
    試算の条件：月額は 1,980円 です。{M}<!-- nofact -->
    """)
    assert "nofact_with_src" in kinds(lint_article(path, lc))


def test_nofact_on_table_row_is_error(tmp_path, lc):
    path = make_article(tmp_path, f"""\
    月額は 1,980円 です。{M}

    | サービス | 月額 |
    |---|---|
    | X社 | 2,000円 |<!-- nofact -->
    """)
    assert kinds(lint_article(path, lc)) == ["nofact_in_table"]


@pytest.mark.parametrize("line", [
    "月額はおおよそ 2,000円 前後が相場です。<!-- nofact -->",        # 円
    "導入企業は 10,000社 以上です。<!-- nofact -->",                 # 社
    "継続率は 99.5% です。<!-- nofact -->",                          # %
    "平均の利用人数は 25人 です。<!-- nofact -->",                    # 人
    "見出しの数え上げ用の専用の行：ポイントは3つ <!-- nofact -->",     # 許された書き出しでない
])
def test_nofact_only_on_allowed_prefixes(tmp_path, lc, line):
    """C4: 見出し以外の nofact は「試算の条件：」「計算式：」で始まる行だけ。金額以外の単位でも逃げられない。"""
    path = make_article(tmp_path, f"月額は 1,980円 です。{M}\n{line}\n")
    assert kinds(lint_article(path, lc)) == ["nofact_line_not_allowed"]


def test_nofact_prefixes_configurable(tmp_path, cfg):
    path = make_article(tmp_path, f"月額は 1,980円 です。{M}\n前提：10人で使う <!-- nofact -->\n")
    assert kinds(lint_article(path, LintConfig.from_cfg(cfg))) == ["nofact_line_not_allowed"]
    cfg["factcheck"]["lint"]["nofact_line_prefixes"] = ["前提："]
    assert kinds(lint_article(path, LintConfig.from_cfg(cfg))) == []


@pytest.mark.parametrize("heading,ok", [
    ("## 必要な3つの要件 <!-- nofact -->", True),
    ("## ポイント1：料金 <!-- nofact -->", True),
    ("## 月額300円・導入1万社の勤怠管理X <!-- nofact -->", False),
    ("## 継続率99%の理由 <!-- nofact -->", False),
])
def test_heading_nofact_only_for_enumerations(tmp_path, lc, heading, ok):
    """C4: 見出しの nofact は、見出しの数値がすべて数え上げ（か「ポイントN」）の場合だけ。"""
    path = make_article(tmp_path, f"月額は 1,980円 です。{M}\n\n{heading}\n")
    res = lint_article(path, lc)
    assert (kinds(res) == []) == ok, errs(res)
    if not ok:
        assert set(kinds(res)) == {"nofact_heading_not_enum"}


def test_formula_line_allows_only_unitless_constants(tmp_path, lc):
    """C4: 「計算式：」の行の nofact は、単位のない定数（× 12）だけ許す。"""
    base = SPEC_CALC.replace("計算式：1人あたり月額 × 利用人数（最低人数を下回る場合は最低人数で計算）",
                             "計算式：1人あたり月額 × 利用人数 × 12 <!-- nofact -->")
    assert kinds(lint_article(make_article(tmp_path, base + "\n", name="ok"), lc)) == []
    bad = SPEC_CALC.replace("計算式：1人あたり月額 × 利用人数（最低人数を下回る場合は最低人数で計算）",
                            "計算式：300円 × 利用人数 <!-- nofact -->")
    assert kinds(lint_article(make_article(tmp_path, bad + "\n", name="bad"), lc)) == ["formula_unit_number"]
    no_nofact = SPEC_CALC.replace("計算式：1人あたり月額 × 利用人数（最低人数を下回る場合は最低人数で計算）",
                                  "計算式：1人あたり月額 × 利用人数 × 12")
    res = lint_article(make_article(tmp_path, no_nofact + "\n", name="nn"), lc)
    assert kinds(res) == ["unmarked_number"] and "計算式" in res.errors[0].detail


# --------------------------------------------------------------------------
# 表・コードブロック・HTML（C5・C6）
# --------------------------------------------------------------------------

def test_table_without_leading_pipe_is_a_table(tmp_path, lc):
    """C5: 先頭に | のない表も表として扱う（markdown-it のトークンで判定）。"""
    path = make_article(tmp_path, f"""\
    月額は 1,980円 です。{M}

    サービス | 月額
    ---|---
    X社 | 300円 <!-- nofact -->
    """)
    assert kinds(lint_article(path, lc)) == ["nofact_in_table"]


def test_note_right_after_table_becomes_a_row(tmp_path, lc):
    """表の直後に空行なしで書いた注記は、表の行になる（GFM）。"""
    path = make_article(tmp_path, f"""\
    | サービス | 月額 |
    |---|---|
    | X社 | 300円 |{M}
    （各社公式サイト、2026年9月時点）
    """)
    assert kinds(lint_article(path, lc)) == ["table_row_without_pipe"]


def test_html_table_is_error(tmp_path, lc):
    path = make_article(tmp_path, f"月額は 1,980円 です。{M}\n\n<table><tr><td>X社</td><td>300円</td></tr></table>\n")
    assert "html_table" in kinds(lint_article(path, lc))


@pytest.mark.parametrize("block", [
    "```\nコードの中の 12,000円\n```\n",
    "~~~\n別の書き方 7日\n~~~\n",
    "    インデントのコード 9,999円\n",
])
def test_code_block_is_error(tmp_path, lc, block):
    """C6: 本文のコードブロックは lint のエラー（比較記事には不要）。"""
    path = make_article(tmp_path, f"月額は 1,980円 です。{M}\n\n" + block)
    assert kinds(lint_article(path, lc)) == ["code_block"]


def test_unclosed_fence_is_error(tmp_path, lc):
    path = make_article(tmp_path, f"月額は 1,980円 です。{M}\n\n```\n設定例\n")
    # 閉じていないフェンスは末尾まで続くので、末尾の関連記事の区間もコードになる（part_missing）
    assert kinds(lint_article(path, lc)) == ["code_block", "unclosed_fence", "part_missing"]


def test_inline_code_number_is_error(tmp_path, lc):
    """C2: インラインコードの中の数値も数値。lint はエラーにし、照合でも拾う。"""
    path = make_article(tmp_path, f"月額は `9,999円` です。{M}\n")
    res = lint_article(path, lc)
    assert kinds(res) == ["inline_code_number"]
    claims, _ = factcheck.parse_article(path, [], True)
    assert [t.raw for t in claims[0].numbers] == ["9,999円"]


# --------------------------------------------------------------------------
# 漢数字・禁止表現（C1・C10）
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,found", [
    ("導入企業は一万二千社を超えます。", "一万二千社"),
    ("月額は千九百八十円からです。", "千九百八十円"),
    ("満足度は九割です。", "九割"),
    ("料金は1万5,000円です。", "1万5"),
])
def test_kanji_numbers_are_errors(tmp_path, lc, text, found):
    path = make_article(tmp_path, f"月額は 1,980円 です。{M}\n{text}\n")
    res = lint_article(path, lc)
    assert "kanji_number" in kinds(res)
    assert any(found in i.number for i in res.errors if i.kind == "kanji_number")


def test_kanji_exclusions(tmp_path, cfg):
    text = "一般的に、一部の製品では十分に一度だけ確認します。一覧は下のとおり。一つの方法です。"
    path = make_article(tmp_path, f"月額は 1,980円 です。{M}\n{text}\n")
    assert kinds(lint_article(path, LintConfig.from_cfg(cfg))) == []
    cfg["factcheck"]["lint"]["kanji_exclusions"] = []
    assert "kanji_number" in kinds(lint_article(path, LintConfig.from_cfg(cfg)))


def test_arabic_man_is_not_kanji_number(tmp_path, lc):
    """「1万社」は照合できる1つの数値（§1-2 の例）なので漢数字の対象外。"""
    path = make_article(tmp_path, f"導入企業は 1万社 です。{M}\n")
    assert kinds(lint_article(path, lc)) == []


@pytest.mark.parametrize("text", [
    "X社は業界No.1の勤怠管理システムです。", "Ｎｏ．１の製品です。", "満足度1位の製品です。",
    "最安の製品です。", "業界唯一の機能です。", "一番おすすめの製品です。", "実際に使ってみたところ便利でした。",
])
def test_banned_expressions_in_body(tmp_path, lc, text):
    """§1-6 の禁止表現は本文でもエラー（一覧は config の banned_expressions）。"""
    path = make_article(tmp_path, f"月額は 1,980円 です。{M}\n{text}\n")
    assert "banned_expression" in kinds(lint_article(path, lc))


def test_caution_and_comparison_expressions_are_warnings(tmp_path, lc):
    path = make_article(tmp_path, f"""\
    月額は 1,980円 です。{M}
    必ず導入前に確認しましょう。
    X社の月額が最も低くなります。
    上の試算の条件では、比較した中でX社の月額が最も低くなります。
    """)
    res = lint_article(path, lc)
    assert kinds(res) == []
    assert kinds(res, "warning") == ["caution_expression", "caution_expression"]


def test_banned_expression_in_title(tmp_path, lc):
    path = make_article(tmp_path, f"月額は 300円 です。{M}\n", title="業界No.1の勤怠管理システム")
    assert "banned_expression" in kinds(lint_article(path, lc))


# --------------------------------------------------------------------------
# その他の記法
# --------------------------------------------------------------------------

def test_src_block_with_note_url_and_inline_marker(tmp_path, lc):
    """§1-2 制約5: src-block の中に注記・URL・行内マーカーを書かない。"""
    path = make_article(tmp_path, """\
    <!-- src-block: https://example.com/pricing accessed=2026-09-27 -->
    | プラン | 月額 |
    |---|---|
    | ライト | 1,000円 |<!-- src: https://other.example.com/ accessed=2026-09-27 -->
    | 詳細 | https://example.com/pricing2 |
    <!-- /src-block -->
    """)
    res = lint_article(path, lc)
    assert set(kinds(res)) == {"inline_in_src_block", "url_in_src_block"}


def test_multiple_sentences_on_one_marker_line_is_warned(tmp_path, lc):
    """§1-2 制約1: 1行に複数の文があると前の文の数値まで同じURLで照合される。"""
    path = make_article(tmp_path, f"導入企業は 12,000社 です。月額は 1,980円 です。{M}\n")
    res = lint_article(path, lc)
    assert kinds(res) == []
    assert kinds(res, "warning") == ["multi_sentence"]


def test_line_break_mid_sentence_leaves_number_unmarked(tmp_path, lc):
    """§1-2 制約2: 文の途中で改行すると、前の行の数値はどのマーカーにも属さない。"""
    path = make_article(tmp_path, """\
    スタンダードプランの月額は 1,980円 で、
    ユーザー数の上限は 50人 です。<!-- src: https://example.com/pricing accessed=2026-09-27 -->
    """)
    res = lint_article(path, lc)
    assert kinds(res) == ["unmarked_number"]
    assert "1,980円" in res.errors[0].number


def test_unclosed_and_stray_blocks(tmp_path, lc):
    path = make_article(tmp_path, """\
    <!-- /src-block -->
    <!-- src-block: https://b.example.com/ accessed=2026-09-27 -->
    | a | b |
    |---|---|
    | x | 1円 |
    """)
    res = lint_article(path, lc)
    assert set(kinds(res)) == {"stray_block_end", "unclosed_block"}


def test_block_marker_must_be_alone(tmp_path, lc):
    path = make_article(tmp_path, """\
    <!-- src-block: https://b.example.com/ accessed=2026-09-27 --> | a | 100円 |
    <!-- /src-block -->
    """)
    assert "block_marker_not_alone" in kinds(lint_article(path, lc))


def test_src_marker_inside_calc_block_is_error(tmp_path, lc):
    body = SPEC_CALC.replace("| X社 | 3,000円 |",
                             "| X社 | 3,000円 |<!-- src: https://x.example.com/ accessed=2026-09-27 -->")
    path = make_article(tmp_path, body + "\n")
    assert "src_in_calc_block" in kinds(lint_article(path, lc))


def test_misspelled_marker_is_reported(tmp_path, lc):
    path = make_article(tmp_path, """\
    月額は 1,980円 です。<!-- scr: https://a.example.com/ accessed=2026-09-27 -->
    """)
    res = lint_article(path, lc)
    assert set(kinds(res)) == {"unmarked_number", "no_markers"}
    assert kinds(res, "warning") == ["unknown_comment"]


def test_spec_trace_table_template_is_rejected(tmp_path, lc):
    """§4 の雛形そのまま（URLが入っていない・accessed=…）は lint で止まる。"""
    path = make_article(tmp_path, SPEC_TRACE_TEMPLATE + "\n")
    res = lint_article(path, lc)
    ks = kinds(res)
    assert ks.count("invalid_marker") == 3
    assert "accessed_invalid" in ks and ks.count("accessed_missing") == 2
    assert "placeholder" in kinds(res, "warning")


def test_trace_table_number_in_product_column_is_after_marker(tmp_path, lc):
    """§4: 「製品への影響」の列には数値を書かない（書くと最後のマーカーより後ろの数値になる）。"""
    path = make_article(tmp_path, """\
    | 業種固有の事実（出典つき） | そこから導かれる要件 | 製品への影響 |
    |---|---|---|
    | 歯科診療所の約6割が土曜も診療<!-- src: https://stat.example.go.jp/a accessed=2026-09-27 --> | 土曜のシフト | X：月額300円で対応 |
    """)
    res = lint_article(path, lc)
    assert kinds(res) == ["after_last_marker"]
    assert res.errors[0].number == "300円"


def test_front_matter_missing_and_bad_slug(tmp_path, lc):
    p1 = tmp_path / "no-fm.md"
    p1.write_text(f"月額は 1,980円 です。{M}\n", encoding="utf-8")
    assert kinds(lint_article(p1, lc)) == ["front_matter_missing"]
    fm = "---\ntype: A\nslug: 歯科-kintai\ntitle: t\ndescription: d\n---\n"
    p2 = make_article(tmp_path, f"月額は 1,980円 です。{M}\n", name="bad-slug", front_matter=fm)
    assert kinds(lint_article(p2, lc)) == ["slug_invalid"]


@pytest.mark.parametrize("slug", ['"abc\\n"', "nul", "con", "com1", "lpt9", "Abc"])
def test_slug_rejects_newline_and_windows_reserved(tmp_path, lc, slug):
    """C20: 行末の改行を許さない（fullmatch）。Windows の予約名も不可。"""
    fm = f"---\ntype: A\nslug: {slug}\ntitle: t\ndescription: d\n---\n"
    p = make_article(tmp_path, f"月額は 1,980円 です。{M}\n", name="s", front_matter=fm)
    assert kinds(lint_article(p, lc)) == ["slug_invalid"]


def test_programs_must_exist_in_asp_programs(tmp_path, lc):
    """判断9: front matter の programs の ID が data/asp-programs.csv にあるか。"""
    fm = "---\ntype: A\nslug: p\ntitle: t\ndescription: d\nprograms: [prog_001, prog_999]\n---\n"
    res = lint_article(make_article(tmp_path, f"月額は 1,980円 です。{M}\n", name="p", front_matter=fm), lc)
    assert kinds(res) == ["program_unknown"] and res.errors[0].number == "prog_999"


# --------------------------------------------------------------------------
# title・description の数（C10・C11）
# --------------------------------------------------------------------------

BODY_WITH_ENUM = f"""\
月額は 300円 からです。{M}

## 必要な3つの要件 <!-- nofact -->

### 土曜のシフト
### 短時間勤務
### 複数拠点
"""


def test_title_enum_and_fact_match(tmp_path, lc):
    path = make_article(tmp_path, BODY_WITH_ENUM, title="歯科医院の勤怠管理｜必要な3つの要件と月額300円からの製品",
                        description="必要な3つの要件を整理しました。【2026年最新】9月時点")
    assert kinds(lint_article(path, lc)) == []


def test_title_enum_not_in_headings(tmp_path, lc):
    path = make_article(tmp_path, BODY_WITH_ENUM, title="必要な5つの要件")
    assert kinds(lint_article(path, lc)) == ["title_enum_mismatch"]


def test_title_enum_noun_must_match(tmp_path, lc):
    """C11: 数が同じでも名詞が違う見出し（3つの誤解）では合格にしない。"""
    body = BODY_WITH_ENUM.replace("必要な3つの要件", "よくある3つの誤解")
    path = make_article(tmp_path, body, title="必要な3つの要件と選び方")
    assert kinds(lint_article(path, lc)) == ["title_enum_mismatch"]


def test_title_enum_count_differs_from_child_headings(tmp_path, lc):
    body = BODY_WITH_ENUM.replace("### 複数拠点\n", "")
    path = make_article(tmp_path, body, title="必要な3つの要件")
    assert kinds(lint_article(path, lc)) == ["title_enum_count"]


def test_title_enum_counts_table_rows_or_list_items(tmp_path, lc):
    """C11: 直下に見出しがなければ、表のデータ行、なければリスト項目を数える。"""
    base = f"月額は 300円 からです。{M}\n\n## 必要な3つの要件 <!-- nofact -->\n\n"
    table3 = base + SPEC_TRACE_FILLED + "\n"
    assert kinds(lint_article(make_article(tmp_path, table3, name="t3", title="必要な3つの要件"), lc)) == []
    list2 = base + "- A\n- B\n"
    assert kinds(lint_article(make_article(tmp_path, list2, name="l2", title="必要な3つの要件"), lc)) \
        == ["title_enum_count"]
    bold = base + "**A**\n**B**\n**C**\n"
    assert kinds(lint_article(make_article(tmp_path, bold, name="b3", title="必要な3つの要件"), lc)) \
        == ["title_enum_uncountable"]


def test_title_fact_number_must_be_sourced(tmp_path, lc):
    """§1-2: 事実の数値は本文に出典つきで書いた数値と同じ値に限る。"""
    path = make_article(tmp_path, BODY_WITH_ENUM, title="月額250円から使える勤怠管理")
    res = lint_article(path, lc)
    assert kinds(res) == ["title_fact_unsourced"]
    assert res.errors[0].number == "250円"


def test_title_fact_number_needs_same_unit(tmp_path, lc):
    """C10: 単位のない出典つき数値（表の 300）に、title の「300円」は一致しない。"""
    path = make_article(tmp_path, f"| サービス | 上限 |\n|---|---|\n| X社 | 300 |{M}\n",
                        title="月額300円から使える勤怠管理")
    assert kinds(lint_article(path, lc)) == ["title_fact_unsourced"]


def test_title_does_not_match_per_unit_one(tmp_path, lc):
    """C10: 本文の「1人あたり」の 1 は、title の「1」の比べる相手にならない。"""
    path = make_article(tmp_path, f"1人あたりの月額は 300円 です。{M}\n", title="シェア1社の勤怠管理")
    assert kinds(lint_article(path, lc)) == ["title_fact_unsourced"]


def test_description_fact_number_in_nofact_line_is_not_sourced(tmp_path, lc):
    body = BODY_WITH_ENUM + "\n試算の条件：導入1万社の場合 <!-- nofact -->\n"
    path = make_article(tmp_path, body, description="導入1万社の勤怠管理システム")
    assert "title_fact_unsourced" in kinds(lint_article(path, lc))


def test_title_fact_number_matches_normalized_value(tmp_path, lc):
    body = BODY_WITH_ENUM + f"\n導入企業は 10,000社 です。{M}\n"
    path = make_article(tmp_path, body, description="導入1万社の勤怠管理システム")
    assert kinds(lint_article(path, lc)) == []


def test_enumeration_patterns_are_configurable(tmp_path, cfg):
    """数え上げの書き方は config で変えられる。「5選」を外すと事実の数値として扱う。"""
    body = f"""\
    月額は 300円 です。{M}

    ## おすすめ5選 <!-- nofact -->

    ### A
    ### B
    ### C
    ### D
    ### E
    """
    path = make_article(tmp_path, body, title="勤怠管理システムおすすめ5選")
    assert kinds(lint_article(path, LintConfig.from_cfg(cfg))) == []
    cfg["factcheck"]["lint"]["enumeration_patterns"] = [r"(?P<n>\d+)\s*つ"]
    res = lint_article(path, LintConfig.from_cfg(cfg))
    assert "title_fact_unsourced" in kinds(res) and "nofact_heading_not_enum" in kinds(res)


def test_title_placeholder_is_error(tmp_path, lc):
    path = make_article(tmp_path, BODY_WITH_ENUM, title="{{業種}}の勤怠管理")
    assert "placeholder" in kinds(lint_article(path, lc))


def test_per_unit_one_can_be_disabled(tmp_path, cfg):
    path = make_article(tmp_path, f"月額は 300円 です。{M}\n1人あたりの料金で比べます。\n")
    assert kinds(lint_article(path, LintConfig.from_cfg(cfg))) == []
    cfg["factcheck"]["lint"]["drop_per_unit_one"] = False
    assert kinds(lint_article(path, LintConfig.from_cfg(cfg))) == ["unmarked_number"]


@pytest.mark.parametrize("text,flagged", [
    ("1施設あたりの登録は無制限です。", False),
    ("1人あたりの料金です。", False),
    ("1ヶ月あたりの料金です。", False),
    ("先着1社限定で毎月割引があります。", True),     # C16: 「毎」は外した
    ("導入は1社のみ毎年更新。", True),
    ("1つの拠点あたりの料金です。", True),           # C16: 助数詞のあとに2文字を超える語
])
def test_per_unit_one_is_narrow(tmp_path, lc, text, flagged):
    """C16: 数値の直後に助数詞0〜2文字、その直後が「あたり／当たり」の場合だけ除外。"""
    path = make_article(tmp_path, f"月額は 300円 です。{M}\n{text}\n")
    assert (kinds(lint_article(path, lc)) == ["unmarked_number"]) == flagged


# --------------------------------------------------------------------------
# 型B（C11・C12）
# --------------------------------------------------------------------------

def test_type_b_requires_part_markers(good_article, lc):
    text = good_article.read_text(encoding="utf-8")
    good_article.write_text(text.replace("<!-- part: faq -->\n", "").replace("A. 拠点ごとの集計に対応した製品を選んでください。\n<!-- /part -->",
                                                                            "A. 拠点ごとの集計に対応した製品を選んでください。"),
                            encoding="utf-8")
    res = lint_article(good_article, lc)
    assert kinds(res) == ["part_missing"]
    assert "faq" in res.errors[0].detail


def test_part_marker_errors(tmp_path, lc):
    path = make_article(tmp_path, f"""\
    月額は 300円 です。{M}
    <!-- part: trace -->
    <!-- part: unknown -->
    本文
    <!-- /part -->
    <!-- /part -->
    """)
    ks = kinds(lint_article(path, lc))
    assert "part_invalid" in ks and "nested_block" in ks and "stray_block_end" in ks


def test_type_b_trace_table_rows(good_article, lc):
    text = good_article.read_text(encoding="utf-8")
    lines = [ln for ln in text.split("\n") if "分院を持つ医療法人がある" not in ln]
    good_article.write_text("\n".join(lines), encoding="utf-8")
    res = lint_article(good_article, lc)
    assert "trace_table_rows" in kinds(res)


def test_type_b_max_two_recommendations(good_article, lc):
    """§1-10: 型Bの推薦は最大2つ。「おすすめ」の見出しの直下に3つ以上ならエラー。"""
    text = good_article.read_text(encoding="utf-8")
    text = text.replace("## 導入の流れ", "### 費用を抑えたいなら：Z社\n\n年契約の場合に向いています。\n\n## 導入の流れ")
    good_article.write_text(text, encoding="utf-8")
    assert "too_many_recommendations" in kinds(lint_article(good_article, lc))


def test_html_article_in_directory_is_error(tmp_path, test_config_path):
    """C22: ディレクトリの中の .html を黙って飛ばさない（正本は .md だけ）。"""
    d = tmp_path / "articles"
    d.mkdir()
    make_article(d, f"月額は 1,980円 です。{M}\n", name="a")
    (d / "b.html").write_text("<p>導入企業は 12,000社 です。</p>", encoding="utf-8")
    out = tmp_path / "r.md"
    assert factcheck.main(["--lint", str(d), "-c", str(test_config_path), "-o", str(out)]) == 1
    assert "正本が .md でない" in out.read_text(encoding="utf-8")
