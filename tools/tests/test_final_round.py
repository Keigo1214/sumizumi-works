"""最終回の修正（C27〜C35・C4/C7 の残り・part: related・参考）の機能ごとのテスト。

レビュアーの再現ケース c13〜c18 は test_review_regressions_2.py。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

import factcheck
import publish_prep
from article import content_hash, find_kanji_hits, load_article, related_range
from article_lint import LintConfig, lint_article
from conftest import (
    SPEC_CALC,
    FakeFetcher,
    html_page,
    make_article,
    write_related_targets,
)
from factcheck import CoincidenceConfig, parse_article, verify_claims
from manual_checks import checker_problem, denied_word

M = "<!-- src: https://a.example.com/ accessed=2026-09-27 -->"


@pytest.fixture
def lc(cfg):
    return LintConfig.from_cfg(cfg)


def kinds(p, lc, severity="error"):
    return [i.kind for i in lint_article(p, lc).issues if i.severity == severity]


# --------------------------------------------------------------------------
# C27 手動確認の記録の種類・偶然の一致の記録・「から」
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name,kind,ok", [
    ("実行者", "不一致", True),
    ("factcheck_session", "不一致", False),      # 不一致・取得失敗の記録は実行者だけ
    ("factcheck_session", "偶然の一致", True),    # 偶然の一致の記録は別セッションも書ける
    ("実行者", "偶然の一致", True),
    ("Claude", "偶然の一致", False),
])
def test_record_kind_and_checker(name, kind, ok):
    assert (checker_problem(name, ["実行者"], ("Claude", "AI"), kind) is None) == ok


@pytest.mark.parametrize("name,denied", [
    ("Claude", True), ("claude-code", True), ("ClaudeCode", True), ("ChatGPT", True), ("AI", True),
    ("Ai", False),        # 参考: 2文字の語は大文字小文字を区別する（「Ai」という名前の人は拒否しない）
    ("Aiko", False), ("田中", False), ("Kaito", False),
])
def test_denied_words_match_as_words(name, denied):
    assert (denied_word(name, ("Claude", "AI", "GPT", "ChatGPT", "Gemini", "Copilot", "bot")) is not None) == denied


def test_kara_equals_tilde(tmp_path):
    """C27: 「から」と「〜」は同じ修飾語。"""
    p = make_article(tmp_path, f"最低人数は 10人〜 です。{M}\n")
    cs, _ = parse_article(p, [], True)
    _iss, cos = verify_claims(cs, FakeFetcher({"https://a.example.com/": html_page("<p>10人から</p>")}),
                              False, 0.0, True, CoincidenceConfig())
    assert cos == []


def _records(d, rows):
    d.mkdir(parents=True, exist_ok=True)
    (d / "zero.md").write_text(
        "## 手動確認の記録\n\n| URL | 数値 | 種類 | 確認者 | 確認日 | 理由 | ページのハッシュ | メモ |\n"
        "|---|---|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n", encoding="utf-8")


def _run(tmp_path, monkeypatch, cfgp, art, pages, *extra):
    monkeypatch.setattr(factcheck, "build_fetcher", lambda c, ignore_robots=False: FakeFetcher(pages))
    out = tmp_path / "r.md"
    rc = factcheck.main([str(art), "-c", str(cfgp), "-o", str(out), "--sources-dir", str(tmp_path / "src"),
                         "--today", "2026-10-01", *extra])
    return rc, out.read_text(encoding="utf-8")


def test_coincidence_record_needs_page_hash_and_expires_by_page(tmp_path, monkeypatch, test_config_path):
    art = make_article(tmp_path, f"初期費用は 0円 です。{M}\n", name="zero")
    page = html_page("<p>初期費用 0円</p>")
    from common import html_to_text
    h = factcheck.page_text_hash(html_to_text(page))
    # ページのハッシュのない偶然の一致の記録は無効
    _records(tmp_path / "src", ["| https://a.example.com/ | 0円 | 偶然の一致 | factcheck_session | 2026-10-01 | 確認 | | |"])
    rc, report = _run(tmp_path, monkeypatch, test_config_path, art, {"https://a.example.com/": page})
    assert rc == 1 and "ページのハッシュ" in report
    # ハッシュがあれば有効。90日を過ぎても（ページが同じなら）有効
    _records(tmp_path / "src", [f"| https://a.example.com/ | 0円 | 偶然の一致 | factcheck_session | 2026-01-01 | 確認 | {h} | |"])
    rc, report = _run(tmp_path, monkeypatch, test_config_path, art, {"https://a.example.com/": page})
    assert rc == 0, report
    # 偶然の一致の記録は、数値の不一致は消さない
    rc, report = _run(tmp_path, monkeypatch, test_config_path, art,
                      {"https://a.example.com/": html_page("<p>初期費用 5,000円</p>")})
    assert rc == 1 and "数値が参照先に見つからない" in report


def test_mismatch_record_by_session_is_invalid(tmp_path, monkeypatch, test_config_path):
    art = make_article(tmp_path, f"月額は 9,999円 です。{M}\n", name="zero")
    _records(tmp_path / "src", ["| https://a.example.com/ | 9,999円 | 不一致 | factcheck_session | 2026-10-01 | JS描画 | | |"])
    rc, report = _run(tmp_path, monkeypatch, test_config_path, art, {"https://a.example.com/": html_page("<p>読み込み中</p>")})
    assert rc == 1 and "manual_check_allowed_checkers" in report


# --------------------------------------------------------------------------
# C35 受領ファイル・ハッシュの表示・関門の要約
# --------------------------------------------------------------------------

def test_receipt_written_only_on_gate_pass(tmp_path, monkeypatch, test_config_path):
    from conftest import CHECKS_DIR
    art = make_article(tmp_path, f"月額は 1,980円 です。{M}\n", name="rcpt")
    rp = CHECKS_DIR / "rcpt.json"
    if rp.exists():
        rp.unlink()
    good = {"https://a.example.com/": html_page("<p>月額 1,980円</p>")}
    rc, report = _run(tmp_path, monkeypatch, test_config_path, art, good, "--mode", "weekly")
    assert rc == 0 and not rp.exists() and "合格した正本" not in report
    rc, report = _run(tmp_path, monkeypatch, test_config_path, art, {"https://a.example.com/": html_page("<p>2,000円</p>")})
    assert rc == 1 and not rp.exists() and content_hash(load_article(art)) not in report   # 不合格ならハッシュを出さない
    rc, report = _run(tmp_path, monkeypatch, test_config_path, art, good)
    assert rc == 0 and rp.exists()
    data = json.loads(rp.read_text(encoding="utf-8"))
    assert data["result"] == "pass" and data["mode"] == "gate" and data["tool"] == "factcheck.py"
    assert data["content_hash"] == content_hash(load_article(art)) and data["content_hash"] in report
    assert len(data["report_sha256"]) == 64


def test_lint_report_shows_hash_only_on_pass(tmp_path, test_config_path):
    ok = make_article(tmp_path, f"月額は 1,980円 です。{M}\n", name="ok")
    bad = make_article(tmp_path, f"月額は 1,980円 です。{M}\n導入は 12,000社 です。\n", name="bad")
    out = tmp_path / "l.md"
    factcheck.main(["--lint", str(ok), str(bad), "-c", str(test_config_path), "-o", str(out)])
    text = out.read_text(encoding="utf-8")
    assert content_hash(load_article(ok)) in text and content_hash(load_article(bad)) not in text


def test_publish_prep_requires_receipt(tmp_path, cfg):
    from conftest import CHECKS_DIR
    p = make_article(tmp_path, f"A は 1,000円 です。{M}\n", name="needs-receipt", checks=True)
    assert publish_prep.prepare(p, cfg, {}).errors == []
    (CHECKS_DIR / "needs-receipt.json").unlink()
    errs = [m.detail for m in publish_prep.prepare(p, cfg, {}).errors]
    assert any("受領ファイル" in e and "ありません" in e for e in errs)
    # 他人の受領ファイル・古い受領ファイル
    factcheck.write_receipt(CHECKS_DIR, "needs-receipt", "0" * 16, "r", {})
    errs = [m.detail for m in publish_prep.prepare(p, cfg, {}).errors]
    assert any("受領ファイルのハッシュ" in e for e in errs)


def test_checks_lint_not_required(tmp_path, cfg):
    """C35: checks.lint・factcheck_tool は必須でない（lint は publish_prep が実行、照合は受領ファイル）。"""
    p = make_article(tmp_path, f"A は 1,000円 です。{M}\n", name="nolint", checks=True)
    text = p.read_text(encoding="utf-8")
    assert "  lint:" not in text and "factcheck_tool" not in text
    assert publish_prep.prepare(p, cfg, {}).errors == []


def test_hash_command_refuses_without_pass(tmp_path, test_config_path, capsys):
    from conftest import CHECKS_DIR
    p = make_article(tmp_path, f"A は 1,000円 です。{M}\n", name="hashcmd", checks=True)
    assert publish_prep.main(["--hash", str(p), "-c", str(test_config_path)]) == 0
    assert content_hash(load_article(p)) in capsys.readouterr().out
    (CHECKS_DIR / "hashcmd.json").unlink()
    assert publish_prep.main(["--hash", str(p), "-c", str(test_config_path)]) == 1
    assert content_hash(load_article(p)) not in capsys.readouterr().out
    bad = make_article(tmp_path, f"A は 1,000円 です。{M}\nB は 2,000円 です。\n", name="hashbad", checks=True)
    assert publish_prep.main(["--hash", str(bad), "-c", str(test_config_path)]) == 1


def test_gate_summary_and_draft(good_article, tmp_path, test_config_path):
    cta = tmp_path / "cta.yaml"
    cta.write_text("patterns:\n  prog_001: 123\n", encoding="utf-8")
    src = tmp_path / "sources-for-summary"
    rc = publish_prep.main([str(good_article), "-c", str(test_config_path), "--out-dir", str(tmp_path / "build"),
                            "--cta-patterns", str(cta)])
    assert rc == 0 and src
    d = tmp_path / "build" / "dental-clinic-attendance-system"
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "draft"
    summary = (d / "gate-summary.md").read_text(encoding="utf-8")
    for needle in ("関門の要約", "下書き", "偽造への防御ではない", "## lint", "## 照合（受領ファイル）",
                   "## 判定の証跡", "## 手動確認の記録", "## 偶然の一致（T7）の扱い", content_hash(load_article(good_article))):
        assert needle in summary, needle


# --------------------------------------------------------------------------
# part: related
# --------------------------------------------------------------------------

def related_article(tmp_path, lines, typ="B", name="rel", extra_fm=""):
    write_related_targets(tmp_path)
    fm = (f"---\ntype: {typ}\nslug: {name}\ntitle: t\ndescription: d\nparent: p\nindustry: dental-clinic\n{extra_fm}---\n")
    body = f"月額は 1,980円 です。{M}\n\n## 関連記事\n\n<!-- part: related -->\n" + "\n".join(lines) + "\n<!-- /part -->\n"
    if typ == "B":
        body = body.replace("## 関連記事", "## 関連記事")
    p = tmp_path / f"{name}.md"
    p.write_text(fm + body, encoding="utf-8")
    return p


def rel_errors(p, lc):
    """関連記事に関係するエラーだけ（型Bの trace・checklist・faq の欠落は、この試験の対象外）。"""
    return [i.kind for i in lint_article(p, lc).errors
            if i.kind in ("related_invalid", "part_invalid", "nested_block", "unclosed_block")
            or (i.kind == "part_missing" and "related" in i.detail)]


def test_related_valid_forms(tmp_path, lc):
    assert rel_errors(related_article(tmp_path, ["- [勤怠管理システムの比較](/attendance-system-comparison/)"]), lc) == []
    a = related_article(tmp_path, ["- 歯科医院：[歯科医院の勤怠管理システムの選び方](/dental-sample/)"], typ="A", name="rel-a")
    assert rel_errors(a, lc) == []


@pytest.mark.parametrize("line", [
    "### 見出し",
    "関連記事の説明の段落です。",
    "| a | b |",
    "<a href=\"/dental-sample/\">歯科医院</a>",
    "- ![画像](/x.png)",
    "{{CTA:prog_001}}",
    "- [勤怠管理システムの比較](/attendance-system-comparison/)<!-- src: https://a.example.com/ accessed=2026-09-27 -->",
    "- [勤怠管理システムの比較](/attendance-system-comparison/) <!-- nofact -->",
    "https://example.com/attendance-system-comparison/",                        # URL だけの行（ブログカード）
    "- [勤怠管理システムの比較](/attendance-system-comparison/)と[別](/dental-sample/)",
    "- [勤怠管理システムの比較・導入1万社](/attendance-system-comparison/)",     # title と一致しない（数値の紛れ込み）
    "- [勤怠管理システムの比較](/no-such-article/)",
    "- [勤怠管理システムの比較](https://other.example.com/attendance-system-comparison/)",
    "- [勤怠管理システムの比較](https://px.a8.net/attendance-system-comparison/)",
    "- [勤怠管理システムの比較](attendance-system-comparison/)",
    "- 親記事：[勤怠管理システムの比較](/attendance-system-comparison/)",      # 型Bに前置きは書けない
])
def test_related_rejects(tmp_path, lc, line):
    assert "related_invalid" in rel_errors(related_article(tmp_path, [line]), lc)


def test_related_type_a_needs_industry_prefix(tmp_path, lc):
    assert "related_invalid" in rel_errors(related_article(
        tmp_path, ["- [歯科医院の勤怠管理システムの選び方](/dental-sample/)"], typ="A", name="ra"), lc)
    assert "related_invalid" in rel_errors(related_article(
        tmp_path, ["- 美容室：[歯科医院の勤怠管理システムの選び方](/dental-sample/)"], typ="A", name="rb"), lc)


def test_related_target_must_be_published(tmp_path, lc):
    write_related_targets(tmp_path)
    (tmp_path / "future.md").write_text("---\ntype: B\nslug: future\ntitle: \"未来\"\npublished: 2099-01-01\n---\n",
                                        encoding="utf-8")
    assert "related_invalid" in rel_errors(related_article(tmp_path, ["- [未来](/future/)"]), lc)


def test_related_site_domain_allowed(tmp_path, cfg):
    cfg["site_domains"] = ["mysite.example.com"]
    p = related_article(tmp_path, ["- [勤怠管理システムの比較](https://mysite.example.com/attendance-system-comparison/)"])
    assert rel_errors(p, LintConfig.from_cfg(cfg)) == []


def test_related_required_for_both_types(tmp_path, lc):
    for typ in ("A", "B"):
        fm = f"---\ntype: {typ}\nslug: norel-{typ.lower()}\ntitle: t\ndescription: d\n---\n"
        p = make_article(tmp_path, f"月額は 1,980円 です。{M}\n", name=f"norel-{typ.lower()}", front_matter=fm,
                         related=False)
        assert "part_missing" in kinds(p, lc)


def test_related_excluded_from_hash(tmp_path, lc):
    """§7 工程11: 関連記事の区間の中だけを変えても、ハッシュは変わらない。目印の位置や区間の外を変えると変わる。"""
    p = related_article(tmp_path, ["- [勤怠管理システムの比較](/attendance-system-comparison/)"])
    h0 = content_hash(load_article(p))
    text = p.read_text(encoding="utf-8")
    p.write_text(text.replace("- [勤怠管理システムの比較](/attendance-system-comparison/)",
                              "- [勤怠管理システムの比較](/attendance-system-comparison/)\n"
                              "- [歯科医院の勤怠管理システムの選び方](/dental-sample/)"), encoding="utf-8")
    assert content_hash(load_article(p)) == h0
    assert rel_errors(p, lc) == []
    p.write_text(p.read_text(encoding="utf-8").replace("## 関連記事", "## 関連記事の一覧"), encoding="utf-8")
    assert content_hash(load_article(p)) != h0


@pytest.mark.parametrize("mutate", [
    lambda t: t + "\n<!-- part: related -->\n- [勤怠管理システムの比較](/attendance-system-comparison/)\n<!-- /part -->\n",  # 重複
    lambda t: t.replace("<!-- /part -->\n", ""),                                                                     # 閉じていない
    lambda t: t.replace("<!-- part: related -->\n", "<!-- part: related -->\n<!-- part: faq -->\n"),                 # 入れ子
])
def test_invalid_related_is_not_excluded_from_hash(tmp_path, lc, mutate):
    p = related_article(tmp_path, ["- [勤怠管理システムの比較](/attendance-system-comparison/)"])
    p.write_text(mutate(p.read_text(encoding="utf-8")), encoding="utf-8")
    art = load_article(p)
    assert related_range(art) is None
    assert lint_article(p, lc).errors                                  # lint もエラー
    h0 = content_hash(art)
    p.write_text(p.read_text(encoding="utf-8").replace("勤怠管理システムの比較]", "勤怠管理システムの比較 ]", 1),
                 encoding="utf-8")
    assert content_hash(load_article(p)) != h0                         # 何も外さない


def test_related_link_stays_in_output(good_article, cfg):
    html = publish_prep.prepare(good_article, cfg, {"prog_001": 123}).content_html
    assert '<a href="/attendance-system-comparison/">勤怠管理システムの比較</a>' in html
    assert "part:" not in html


# --------------------------------------------------------------------------
# C28・C29・C30・C33・C34・参考
# --------------------------------------------------------------------------

def test_kanji_exclusion_word_boundary():
    """C28: 除外の「一時」は「一時間」に当たらない（語の境界まで一致）。"""
    assert [(h.text, h.severity) for h in find_kanji_hits("一時的に停止", terms=[])] == []
    assert [(h.text, h.severity) for h in find_kanji_hits("一時間ごと", terms=[])] == [("一時間", "warning")]


def test_terms_configurable(tmp_path, cfg):
    t = tmp_path / "terms.yaml"
    t.write_text("terms:\n  - 24時間サポート\n", encoding="utf-8")
    p = make_article(tmp_path, f"月額は 1,980円 です。{M}\n24時間サポートがあります。\n")
    assert "unmarked_number" in kinds(p, LintConfig.from_cfg(cfg))
    cfg["paths"]["terms_file"] = str(t)
    assert kinds(p, LintConfig.from_cfg(cfg)) == []


def test_terms_in_verify(tmp_path):
    p = make_article(tmp_path, f"36協定の上限は 45時間 です。{M}\n")
    cs, _ = parse_article(p, [], True, ["36協定"])
    assert [t.raw for t in cs[0].numbers] == ["45時間"]


@pytest.mark.parametrize("cond,ok", [
    ("試算の条件：10人で3か月利用する場合", True),
    ("試算の条件：月額 2,000円 の場合", False),
    ("試算の条件：導入 50社 の場合", False),
    ("試算の条件：割引率 10% の場合", False),
    ("試算の条件：10ユーザーの場合", False),         # 単位のない数値も不可（10人 と書く）
])
def test_condition_units(tmp_path, lc, cond, ok):
    p = make_article(tmp_path, f"月額は 1,980円 です。{M}\n{cond} <!-- nofact -->\n")
    assert ("condition_unit" not in kinds(p, lc)) == ok


@pytest.mark.parametrize("cell,ok", [
    ("3,000円", True), ("3,000円〜", True), ("試算の対象外（月払いができないため）", True), ("試算の対象外", True),
    ("約3,000円", False), ("3,000円（税込）", False), ("なし", False), ("", False),
])
def test_calc_cell_format(tmp_path, lc, cell, ok):
    body = SPEC_CALC.replace("| Y社 | 4,000円 |", f"| Y社 | {cell} |")
    p = make_article(tmp_path, body + "\n")
    assert ("calc_cell_format" not in kinds(p, lc)) == ok


@pytest.mark.parametrize("heading,ok", [
    ("## ポイント1：料金 <!-- nofact -->", True), ("## ポイント2 <!-- nofact -->", True),
    ("## ポイント3（操作性） <!-- nofact -->", True),
    ("## ポイント1,000円分を還元 <!-- nofact -->", False), ("## 最大ポイント10倍 <!-- nofact -->", False),
    ("## ポイント5%還元 <!-- nofact -->", False),
])
def test_ordinal_headings(tmp_path, lc, heading, ok):
    p = make_article(tmp_path, f"月額は 1,980円 です。{M}\n\n{heading}\n")
    assert ("nofact_heading_not_enum" not in kinds(p, lc)) == ok


def test_invisible_chars_in_title_and_note(tmp_path, lc):
    fm = ("---\ntype: A\nslug: inv\ntitle: \"勤怠​管理\"\ndescription: d\nupdated: 2026-10-01\n"
          "history:\n  - {date: 2026-10-01, note: \"公開­\"}\n---\n")
    p = make_article(tmp_path, f"月額は 1,980円 です。{M}\n", name="inv", front_matter=fm)
    assert kinds(p, lc).count("invisible_char") == 2


def test_image_alt_checked(tmp_path, lc):
    """参考: 画像の alt も、数値・禁止表現・漢数字の検査の対象。"""
    p = make_article(tmp_path, f"月額は 1,980円 です。{M}\n\n![月額300円・業界No.1の画面](/x.png)\n\n"
                               f"<img src=\"/y.png\" alt=\"九割が満足\">\n")
    ks = kinds(p, lc)
    assert "image_alt_number" in ks and "banned_expression" in ks and "kanji_number" in ks


def test_part_whole_section_chain(good_article, lc):
    """C34: trace と checklist を続けて置き、合わせて見出しの区間全体を覆えば合格。間に本文を挟むとエラー。"""
    assert lint_article(good_article, lc).errors == []
    text = good_article.read_text(encoding="utf-8")
    good_article.write_text(text.replace("<!-- /part -->\n<!-- part: checklist -->",
                                         "<!-- /part -->\n目印の外の本文。\n<!-- part: checklist -->"), encoding="utf-8")
    assert "part_not_whole" in kinds(good_article, lc)


def test_today_is_used_for_related(tmp_path, cfg):
    lc = LintConfig.from_cfg(cfg)
    lc.today = date(2026, 8, 1)          # リンク先の published（2026-09-01）より前
    p = related_article(tmp_path, ["- [勤怠管理システムの比較](/attendance-system-comparison/)"])
    assert "related_invalid" in [i.kind for i in lint_article(p, lc).errors]
