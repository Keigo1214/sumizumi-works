"""照合モード: calc-block の除外（T1）、手動確認の記録（T2）、偶然の一致（T7）、既存の挙動。"""

from __future__ import annotations

from pathlib import Path

import pytest

import factcheck
from common import FetchResult
from conftest import (
    SPEC_BLOCK,
    SPEC_CALC,
    SPEC_INLINE,
    FakeFetcher,
    html_page,
    make_article,
)
from factcheck import CoincidenceConfig, parse_article, verify, verify_claims

IGN = [r"^\d{1,2}月$"]
CFG: Path = Path()


@pytest.fixture(autouse=True)
def _cfg_path(test_config_path):
    """main を呼ぶテストはテスト用の config（確認者の許可リストあり）を使う。"""
    global CFG
    CFG = test_config_path


def numbers_of(claims):
    return [[t.raw for t in c.numbers] for c in claims]


# --------------------------------------------------------------------------
# パース（既存の挙動＋calc-block）
# --------------------------------------------------------------------------

def test_inline_marker_range_is_from_line_start(tmp_path):
    path = make_article(tmp_path, SPEC_INLINE + "\n")
    claims, issues = parse_article(path, IGN, True)
    assert issues == []
    assert numbers_of(claims) == [["1,980円"]]
    assert claims[0].url == "https://example.com/pricing"
    assert claims[0].accessed == "2026-09-27"


def test_multiple_inline_markers_on_one_line_keep_existing_behavior(tmp_path):
    path = make_article(tmp_path, "A社は 1,980円、<!-- src: https://a.example.com/price --> "
                                  "B社は 2,480円。<!-- src: https://b.example.com/price -->\n")
    claims, _ = parse_article(path, IGN, True)
    assert [(c.url, [t.raw for t in c.numbers]) for c in claims] == [
        ("https://a.example.com/price", ["1,980円"]),
        ("https://b.example.com/price", ["2,480円"]),
    ]


def test_src_block_covers_all_rows(tmp_path):
    path = make_article(tmp_path, SPEC_BLOCK + "\n")
    claims, _ = parse_article(path, IGN, True)
    assert numbers_of(claims) == [["1,000円", "10人", "2,980円", "50人"]]
    assert claims[0].marker_kind == "block"


def test_calc_block_is_not_verified(tmp_path):
    path = make_article(tmp_path, SPEC_CALC + "\n")
    claims, _ = parse_article(path, IGN, True)
    all_nums = [t.raw for c in claims for t in c.numbers]
    assert "3,000円" not in all_nums and "4,000円" not in all_nums
    assert sorted(all_nums) == sorted(["0円", "300円", "10人", "0円", "400円", "0円", "250円"])


def test_calc_block_inside_src_block_is_not_verified(tmp_path):
    """今までは src-block の中に calc-block を入れると照合されていた（§8 T1）。"""
    path = make_article(tmp_path, """\
    <!-- src-block: https://b.example.com/ accessed=2026-09-27 -->
    | a | b |
    |---|---|
    | x | 100円 |
    <!-- calc-block -->
    | y | 300円 |
    <!-- /calc-block -->
    <!-- /src-block -->
    """)
    claims, _ = parse_article(path, IGN, True)
    assert numbers_of(claims) == [["100円"]]


def test_front_matter_code_block_and_list_numbers_are_skipped(tmp_path):
    path = make_article(tmp_path, """\
    1. 月額は 1,980円 です。<!-- src: https://a.example.com/ accessed=2026-09-27 -->

    ```
    コードの中 <!-- src: https://code.example.com/ --> 999円
    ```
    """, title="月額300円")
    claims, _ = parse_article(path, IGN, True)
    assert [(c.url, [t.raw for t in c.numbers]) for c in claims] == [
        ("https://a.example.com/", ["1,980円"])]


def test_footnotes_still_parsed_in_verify_mode(tmp_path):
    """脚注は設計書で禁止（lint でエラー）だが、照合モードは互換のため読む。"""
    path = make_article(tmp_path, """\
    導入企業は 12,000社 を超える[^a]。

    [^a]: https://example.com/about accessed=2026-07-20
    """)
    claims, issues = parse_article(path, IGN, True)
    assert issues == []
    assert [(c.marker_kind, c.url, [t.raw for t in c.numbers]) for c in claims] == [
        ("footnote", "https://example.com/about", ["12,000社"])]


def test_html_article_with_markers(tmp_path):
    path = tmp_path / "a.html"
    path.write_text(
        "<p>月額は\n1,980円 です。<!-- src: https://a.example.com/ accessed=2026-09-27 --></p>"
        "<!-- calc-block --><table><tr><td>試算 5,000円</td></tr></table><!-- /calc-block -->"
        "<!-- src-block: https://b.example.com/ --><table><tr><td>ライト</td><td>1,000円</td></tr>"
        "</table><!-- /src-block -->", encoding="utf-8")
    claims, _ = parse_article(path, IGN, True)
    assert [(c.url, [t.raw for t in c.numbers]) for c in claims] == [
        ("https://a.example.com/", ["1,980円"]),
        ("https://b.example.com/", ["1,000円"]),
    ]


def test_invalid_marker_and_unclosed_block(tmp_path):
    path = make_article(tmp_path, """\
    月額は 1,980円 です。<!-- src: example.com/pricing -->
    <!-- src-block: https://b.example.com/ -->
    | a | 1円 |
    """)
    _claims, issues = parse_article(path, IGN, True)
    assert sorted(i.kind for i in issues) == ["invalid_marker", "unclosed_block"]


# --------------------------------------------------------------------------
# 照合（既存の挙動）
# --------------------------------------------------------------------------

def test_verify_reports_only_mismatches(tmp_path):
    path = make_article(tmp_path, SPEC_BLOCK + "\n" + SPEC_INLINE + "\n")
    claims, _ = parse_article(path, IGN, True)
    fetcher = FakeFetcher({"https://example.com/pricing": html_page(
        "<table><tr><td>ライト</td><td>1,000円</td><td>10人</td></tr>"
        "<tr><td>スタンダード</td><td>2,980円</td><td>50人</td></tr></table><p>月額 1,980円</p>")})
    assert verify(claims, fetcher, False, 0.0, True) == []
    assert fetcher.calls == ["https://example.com/pricing"]     # 同じURLは1回だけ取得


def test_verify_mismatch_url_error_redirect_empty(tmp_path):
    path = make_article(tmp_path, """\
    A は 1,980円 です。<!-- src: https://a.example.com/ accessed=2026-09-27 -->
    B は 2,000円 です。<!-- src: https://gone.example.com/ accessed=2026-09-27 -->
    C は 3,000円 です。<!-- src: https://moved.example.com/ accessed=2026-09-27 -->
    D は 4,000円 です。<!-- src: https://js.example.com/ accessed=2026-09-27 -->
    """)
    claims, _ = parse_article(path, IGN, True)
    fetcher = FakeFetcher({
        "https://a.example.com/": html_page("<p>月額 2,480円</p>"),
        "https://moved.example.com/": FetchResult(
            "https://moved.example.com/new", "https://moved.example.com/", 200,
            html_page("<p>3,000円</p>"), True, redirected=True,
            redirect_chain=["https://moved.example.com/"]),
        "https://js.example.com/": html_page("<script>var p=4000;</script>"),
    })
    issues = verify(claims, fetcher, False, 0.0, True)
    # 本文を抽出できないページは、既存の挙動どおり empty_page と number_mismatch の両方が出る
    assert sorted((i.kind, i.line_no) for i in issues) == [
        ("empty_page", 14), ("number_mismatch", 11), ("number_mismatch", 14),
        ("url_error", 12), ("url_redirect", 13)]


# --------------------------------------------------------------------------
# T7 偶然の一致
# --------------------------------------------------------------------------

def _co(tmp_path, body, page_html, **cc):
    path = make_article(tmp_path, body)
    claims, _ = parse_article(path, IGN, True)
    fetcher = FakeFetcher({"https://a.example.com/": html_page(page_html)})
    return verify_claims(claims, fetcher, False, 0.0, True, CoincidenceConfig(**cc))


def test_small_value_match_reports_page_context(tmp_path):
    issues, cos = _co(tmp_path,
                      "初期費用は 0円 です。<!-- src: https://a.example.com/ accessed=2026-09-27 -->\n",
                      "<p>キャンペーン期間中は事務手数料 0円</p><p>初期費用 50,000円</p>")
    assert issues == []
    assert len(cos) == 1
    assert cos[0].token.raw == "0円"
    assert "0円" in cos[0].reason or "一桁" in cos[0].reason
    assert any("事務手数料" in c for c in cos[0].page_contexts)


def test_unitless_match_reports_context(tmp_path):
    _issues, cos = _co(tmp_path,
                       "満足度は 85 点満点中です。<!-- src: https://a.example.com/ accessed=2026-09-27 -->\n",
                       "<p>会員数 85 名様突破</p>")
    assert [c.reason for c in cos] == ["単位のない数値"]


def test_single_digit_with_unit_reports_context(tmp_path):
    _issues, cos = _co(tmp_path,
                       "導入は 3社 です。<!-- src: https://a.example.com/ accessed=2026-09-27 -->\n",
                       "<p>全 3社</p>")
    assert len(cos) == 1 and cos[0].reason == "0か一桁の数値"


def test_per_unit_one_is_not_reported(tmp_path):
    """「1施設あたり」「1人あたり」の 1 は対象外（ノイズになるため）。「1日毎」の 1 は C16 で対象に戻した。"""
    _issues, cos = _co(
        tmp_path,
        "1施設あたり 300円、1人あたり 200円、1日毎に 500円 です。"
        "<!-- src: https://a.example.com/ accessed=2026-09-27 -->\n",
        "<p>1施設あたり 300円</p><p>1人あたり 200円</p><p>1日毎 500円</p>")
    assert [c.token.raw for c in cos] == ["1日"]


def test_large_value_with_unit_is_not_reported(tmp_path):
    _issues, cos = _co(tmp_path,
                       "月額は 1,980円 です。<!-- src: https://a.example.com/ accessed=2026-09-27 -->\n",
                       "<p>1,980円</p>")
    assert cos == []


def test_coincidence_can_be_disabled(tmp_path):
    _issues, cos = _co(tmp_path,
                       "初期費用は 0円 です。<!-- src: https://a.example.com/ accessed=2026-09-27 -->\n",
                       "<p>0円</p>", enabled=False)
    assert cos == []


# --------------------------------------------------------------------------
# main（照合モード全体。ネットワークは FakeFetcher）
# --------------------------------------------------------------------------

def run_main(tmp_path, monkeypatch, article: Path, pages: dict, *extra: str,
             sources: Path | None = None) -> tuple[int, str]:
    monkeypatch.setattr(factcheck, "build_fetcher", lambda cfg, ignore_robots=False: FakeFetcher(pages))
    out = tmp_path / "report.md"
    args = [str(article), "-c", str(CFG), "-o", str(out),
            "--sources-dir", str(sources or (tmp_path / "sources")), *extra]
    rc = factcheck.main(args)
    return rc, out.read_text(encoding="utf-8") if out.exists() else ""


def test_main_exit_codes_and_coincidence_flag(tmp_path, monkeypatch):
    article = make_article(tmp_path, "初期費用は 0円 です。<!-- src: https://a.example.com/ accessed=2026-09-27 -->\n")
    pages = {"https://a.example.com/": html_page("<p>手数料 0円</p>")}
    rc, report = run_main(tmp_path, monkeypatch, article, pages)
    assert rc == 1           # 判断5: 候補があれば既定で終了コード1
    assert "偶然の一致の可能性" in report and "手数料" in report
    assert "すべての数値が出典先で確認できました" not in report
    rc, _ = run_main(tmp_path, monkeypatch, article, pages, "--no-fail-on-coincidence")
    assert rc == 0


# --------------------------------------------------------------------------
# T2 手動確認の記録
# --------------------------------------------------------------------------

JS_ARTICLE = """\
スタンダードの月額は 1,980円 で、上限は 50人 です。<!-- src: https://js.example.com/pricing accessed=2026-09-27 -->
"""
JS_PAGES = {"https://js.example.com/pricing": html_page("<div id='app'>読み込み中</div>")}


def write_records(dir_: Path, slug: str, rows: list[str]) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / f"{slug}.md"
    path.write_text(
        "# 出典\n\n## 手動確認の記録\n\n| URL | 数値 | 確認者 | 確認日 | 理由 | メモ |\n|---|---|---|---|---|---|\n"
        + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_manual_records_suppress_js_page_mismatch(tmp_path, monkeypatch):
    art = make_article(tmp_path, JS_ARTICLE, name="js-article")
    rc, report = run_main(tmp_path, monkeypatch, art, JS_PAGES, "--today", "2026-10-01")
    assert rc == 1 and report.count("数値が参照先に見つからない") >= 1

    write_records(tmp_path / "sources", "js-article", [
        "| https://js.example.com/pricing | 1,980円 | 実行者 | 2026-09-27 | JavaScript描画 | |",
        "| https://js.example.com/pricing/ | 50人 | 実行者 | 2026-09-27 | JavaScript描画 | |",
    ])
    rc, report = run_main(tmp_path, monkeypatch, art, JS_PAGES, "--today", "2026-10-01")
    assert rc == 0, report
    assert "手動確認の記録で報告から外したもの（2 件）" in report


def test_sources_dir_relative_to_cwd(tmp_path, monkeypatch):
    """--sources-dir の相対パスは、実行したディレクトリから見た位置（tools/ からではない）。"""
    art = make_article(tmp_path, JS_ARTICLE, name="js-article")
    write_records(tmp_path / "srcs", "js-article", [
        "| https://js.example.com/pricing | 1,980円 | 実行者 | 2026-09-27 | JavaScript描画 | |",
        "| https://js.example.com/pricing | 50人 | 実行者 | 2026-09-27 | JavaScript描画 | |",
    ])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(factcheck, "build_fetcher", lambda c, ignore_robots=False: FakeFetcher(JS_PAGES))
    rc = factcheck.main([str(art), "-c", str(CFG), "-o", "r.md",
                         "--sources-dir", "srcs", "--today", "2026-10-01"])
    assert rc == 0


def test_default_sources_dir_is_data_sources(monkeypatch, tmp_path):
    """--sources-dir を省略すると config の paths.sources_dir（既定 ../data/sources = affiliate/data/sources）。"""
    from common import expand_path, load_config
    from conftest import EXAMPLE_CONFIG
    cfg = load_config(str(EXAMPLE_CONFIG))
    assert expand_path(cfg["paths"]["sources_dir"]) == (Path(__file__).resolve().parents[2] / "data" / "sources")


def test_manual_record_invalid_when_article_number_changes(tmp_path, monkeypatch):
    art = make_article(tmp_path, JS_ARTICLE.replace("1,980円", "2,480円"), name="js-article")
    write_records(tmp_path / "sources", "js-article", [
        "| https://js.example.com/pricing | 1,980円 | 実行者 | 2026-09-27 | JavaScript描画 | |",
        "| https://js.example.com/pricing | 50人 | 実行者 | 2026-09-27 | JavaScript描画 | |",
    ])
    rc, report = run_main(tmp_path, monkeypatch, art, JS_PAGES, "--today", "2026-10-01")
    assert rc == 1
    assert "2,480円" in report
    assert "使われなかった手動確認の記録" in report and "1,980円" in report


def test_manual_record_expires_after_90_days(tmp_path, monkeypatch):
    art = make_article(tmp_path, JS_ARTICLE, name="js-article")
    write_records(tmp_path / "sources", "js-article", [
        "| https://js.example.com/pricing | 1,980円 | 実行者 | 2026-07-01 | JavaScript描画 | |",
        "| https://js.example.com/pricing | 50人 | 実行者 | 2026-07-01 | JavaScript描画 | |",
    ])
    rc, _ = run_main(tmp_path, monkeypatch, art, JS_PAGES, "--today", "2026-09-29")   # 90日目
    assert rc == 0
    rc, report = run_main(tmp_path, monkeypatch, art, JS_PAGES, "--today", "2026-09-30")  # 91日目
    assert rc == 1
    assert "期限切れ" in report


def test_manual_record_validity_days_configurable(tmp_path, monkeypatch, cfg):
    import yaml
    cfg["factcheck"]["manual_check_valid_days"] = 30
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    art = make_article(tmp_path, JS_ARTICLE, name="js-article")
    write_records(tmp_path / "sources", "js-article", [
        "| https://js.example.com/pricing | 1,980円 | 実行者 | 2026-09-01 | JavaScript描画 | |",
        "| https://js.example.com/pricing | 50人 | 実行者 | 2026-09-01 | JavaScript描画 | |",
    ])
    monkeypatch.setattr(factcheck, "build_fetcher", lambda c, ignore_robots=False: FakeFetcher(JS_PAGES))
    out = tmp_path / "r.md"
    rc = factcheck.main([str(art), "-c", str(cfg_path), "-o", str(out), "--sources-dir",
                         str(tmp_path / "sources"), "--today", "2026-10-02"])
    assert rc == 1 and "期限切れ（30日" in out.read_text(encoding="utf-8")


def test_manual_records_for_robots_blocked_url(tmp_path, monkeypatch):
    """robots.txt 拒否（url_error）は、そのマーカーの全数値に記録があるときだけ外れる。"""
    art = make_article(tmp_path, JS_ARTICLE, name="js-article")
    blocked = {"https://js.example.com/pricing": FetchResult(
        "https://js.example.com/pricing", "https://js.example.com/pricing", 0, "", False,
        error="robots.txt により禁止されています")}
    write_records(tmp_path / "sources", "js-article", [
        "| https://js.example.com/pricing | 1,980円 | 実行者 | 2026-09-27 | robots.txt 拒否 | |",
    ])
    rc, report = run_main(tmp_path, monkeypatch, art, blocked, "--today", "2026-10-01")
    assert rc == 1 and "出典URLが取得できない" in report
    write_records(tmp_path / "sources", "js-article", [
        "| https://js.example.com/pricing | 1,980円 | 実行者 | 2026-09-27 | robots.txt 拒否 | |",
        "| https://js.example.com/pricing | 50人 | 実行者 | 2026-09-27 | robots.txt 拒否 | |",
    ])
    rc, _ = run_main(tmp_path, monkeypatch, art, blocked, "--today", "2026-10-01")
    assert rc == 0


def test_manual_record_dash_for_numberless_source(tmp_path, monkeypatch):
    art = make_article(tmp_path, "分院を持つ医療法人がある。<!-- src: https://law.example.go.jp/x accessed=2026-09-27 -->\n",
                       name="law-article")
    blocked = {"https://law.example.go.jp/x": FetchResult(
        "https://law.example.go.jp/x", "https://law.example.go.jp/x", 0, "", False, error="robots.txt により禁止されています")}
    rc, _ = run_main(tmp_path, monkeypatch, art, blocked, "--today", "2026-10-01")
    assert rc == 1
    write_records(tmp_path / "sources", "law-article", [
        "| https://law.example.go.jp/x | - | 実行者 | 2026-09-27 | robots.txt 拒否 | |"])
    rc, _ = run_main(tmp_path, monkeypatch, art, blocked, "--today", "2026-10-01")
    assert rc == 0


def test_invalid_manual_record_is_ignored_and_warned(tmp_path, monkeypatch):
    art = make_article(tmp_path, JS_ARTICLE, name="js-article")
    write_records(tmp_path / "sources", "js-article", [
        "| https://js.example.com/pricing | 1,980円 |  | 2026/09/27 | | |",
        "| https://js.example.com/pricing | 50人 | 実行者 | 2026-12-01 | JavaScript描画 | |",
    ])
    rc, report = run_main(tmp_path, monkeypatch, art, JS_PAGES, "--today", "2026-10-01")
    assert rc == 1
    assert "書式の警告" in report and "確認者が空" in report
    assert "理由が空" in report
    assert "未来の日付" in report


def test_manual_record_suppresses_coincidence(tmp_path, monkeypatch):
    art = make_article(tmp_path, "初期費用は 0円 です。<!-- src: https://a.example.com/ accessed=2026-09-27 -->\n",
                       name="zero")
    pages = {"https://a.example.com/": html_page("<p>手数料 0円</p>")}
    write_records(tmp_path / "sources", "zero", [
        "| https://a.example.com/ | 0円 | 実行者 | 2026-09-27 | 偶然の一致の確認 | 初期費用の欄で確認 |"])
    rc, report = run_main(tmp_path, monkeypatch, art, pages, "--today", "2026-10-01")
    assert rc == 0 and "偶然の一致の可能性" not in report


def test_no_manual_records_flag(tmp_path, monkeypatch):
    art = make_article(tmp_path, JS_ARTICLE, name="js-article")
    write_records(tmp_path / "sources", "js-article", [
        "| https://js.example.com/pricing | 1,980円 | 実行者 | 2026-09-27 | JavaScript描画 | |",
        "| https://js.example.com/pricing | 50人 | 実行者 | 2026-09-27 | JavaScript描画 | |",
    ])
    rc, _ = run_main(tmp_path, monkeypatch, art, JS_PAGES, "--today", "2026-10-01", "--no-manual-records")
    assert rc == 1


def test_template_file_parses(tmp_path):
    from manual_checks import parse_manual_records
    tpl = Path(__file__).resolve().parents[2] / "data" / "sources" / "_template.md"
    recs, warns = parse_manual_records(tpl, allowed_checkers=["実行者"])
    assert warns == []
    assert [(r.kind, r.number_raw, r.checker) for r in recs] == [
        ("不一致", "300円", "実行者"), ("偶然の一致", "0円", "factcheck_session")]
    # 既定（許可リストが空）では、どの記録も無効
    recs, warns = parse_manual_records(tpl)
    assert recs[0].problem and "manual_check_allowed_checkers" in recs[0].problem


# --------------------------------------------------------------------------
# レビューの指摘（C13・C14・C15・C21）
# --------------------------------------------------------------------------

@pytest.mark.parametrize("checker,reason,ok", [
    ("実行者", "JavaScript描画", True),
    ("Claude", "JavaScript描画", False),           # 既定で不可の名前
    ("claude-code", "JavaScript描画", False),
    ("AI", "JavaScript描画", False),
    ("田中", "JavaScript描画", False),             # 許可リストにない
    ("実行者", "", False),                         # 理由は必須
])
def test_manual_record_checker_and_reason(tmp_path, monkeypatch, checker, reason, ok):
    """C13: 確認者は許可リストの名前だけ（Claude・AI は不可）。理由の列は必須。"""
    art = make_article(tmp_path, JS_ARTICLE, name="js-article")
    write_records(tmp_path / "sources", "js-article", [
        f"| https://js.example.com/pricing | 1,980円 | {checker} | 2026-09-27 | {reason} | |",
        f"| https://js.example.com/pricing | 50人 | {checker} | 2026-09-27 | {reason} | |",
    ])
    rc, report = run_main(tmp_path, monkeypatch, art, JS_PAGES, "--today", "2026-10-01")
    assert (rc == 0) == ok, report


def test_suppressed_count_in_report_header(tmp_path, monkeypatch):
    """C13: 記録で消した件数をレポートの冒頭に出す。"""
    art = make_article(tmp_path, JS_ARTICLE, name="js-article")
    write_records(tmp_path / "sources", "js-article", [
        "| https://js.example.com/pricing | 1,980円 | 実行者 | 2026-09-27 | JavaScript描画 | |",
        "| https://js.example.com/pricing | 50人 | 実行者 | 2026-09-27 | JavaScript描画 | |",
    ])
    rc, report = run_main(tmp_path, monkeypatch, art, JS_PAGES, "--today", "2026-10-01")
    head = "\n".join(report.splitlines()[:8])
    assert rc == 0 and "手動確認の記録で報告から外したもの: 2 件" in head


def test_slug_traversal_is_rejected(tmp_path, monkeypatch):
    """C13（c9 #18）: slug の ../ で、ほかの場所の記録を読ませない。"""
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "evil.md").write_text(
        "## 手動確認の記録\n\n| URL | 数値 | 確認者 | 確認日 | 理由 |\n|---|---|---|---|---|\n"
        "| https://a.example.com/p | 9,999円 | 実行者 | 2026-09-27 | JavaScript描画 |\n", encoding="utf-8")
    fm = "---\ntype: A\nslug: ../elsewhere/evil\ntitle: t\ndescription: d\n---\n"
    art = make_article(tmp_path, "月額は 9,999円 です。<!-- src: https://a.example.com/p accessed=2026-09-27 -->\n",
                       name="trav", front_matter=fm)
    (tmp_path / "sources").mkdir()
    rc, report = run_main(tmp_path, monkeypatch, art, {"https://a.example.com/p": html_page("<p>1,980円</p>")},
                          "--today", "2026-10-01")
    assert rc == 1
    assert "手動確認の記録で報告から外したもの: 0 件" in report
    assert "記録を読みません" in report


def test_bom_crlf_article_slug(tmp_path):
    """C21: BOM・CRLF の記事でも slug を front matter から取れる。"""
    body = "月額は 1,980円 です。<!-- src: https://a.example.com/p accessed=2026-09-27 -->\n"
    fm = "---\ntype: A\nslug: other-slug\ntitle: t\ndescription: d\n---\n"
    p = make_article(tmp_path, body, name="crlf-bom", front_matter=fm, encoding="utf-8-sig", newline="\r\n")
    assert factcheck.article_slug(p) == "other-slug"
    claims, _ = parse_article(p, IGN, True)
    assert [(c.line_no, [t.raw for t in c.numbers]) for c in claims] == [(7, ["1,980円"])]


def test_verify_runs_lint_for_md(tmp_path, monkeypatch):
    """C15: マーカーのない数値があれば、照合で不一致がなくても「確認不要」とは出さず、終了コード1。"""
    art = make_article(tmp_path, "月額は 1,980円 です。<!-- src: https://a.example.com/p accessed=2026-09-27 -->\n"
                                 "導入企業は 12,000社 です。\n")
    rc, report = run_main(tmp_path, monkeypatch, art, {"https://a.example.com/p": html_page("<p>1,980円</p>")})
    assert rc == 1
    assert "lint のエラー: 1 件" in report
    assert "確認できました。" not in report
    assert "12,000社" in report


def test_coincidence_when_page_number_has_no_unit(tmp_path):
    """C14（c6）: 記事の 2,480円 が、出典側の単位のない 2480（問い合わせ番号）に一致したら候補に出す。"""
    _issues, cos = _co(tmp_path, "月額は 2,480円 です。<!-- src: https://a.example.com/ accessed=2026-09-27 -->\n",
                       "<p>月額 1,980円。お問い合わせ番号 2480</p>")
    assert len(cos) == 1 and "単位がない" in cos[0].reason
    assert any("お問い合わせ番号" in c for c in cos[0].page_contexts)


@pytest.mark.parametrize("article,page,expect", [
    ("月額は 1,980円 です。", "<p>月額 1,980円〜</p>", True),        # 出典は「〜」、記事にない
    ("最大 50人 まで使えます。", "<p>最低 50人 から</p>", True),
    ("約 12,000社 が導入。", "<p>12,000社 以上</p>", True),
    ("月額は 1,980円〜 です。", "<p>月額 1,980円〜</p>", False),     # 同じ修飾語
    ("月額は 1,980円 です。", "<p>月額 1,980円</p>", False),
])
def test_coincidence_on_qualifier_mismatch(tmp_path, article, page, expect):
    """C14: 「以上・以下・最大・最低・約・〜」の食い違いも文脈表示の対象。"""
    _issues, cos = _co(tmp_path, article + "<!-- src: https://a.example.com/ accessed=2026-09-27 -->\n", page)
    assert bool(cos) == expect, [c.reason for c in cos]
    if expect:
        assert "修飾語の食い違い" in cos[0].reason
