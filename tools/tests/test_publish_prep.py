"""T5・T8: publish_prep.py。レビューの再現ケースは test_review_regressions.py。"""

from __future__ import annotations

import json
import re

import pytest

import publish_prep
from conftest import (
    DATA_DIR,
    SPEC_BLOCK,
    SPEC_CALC,
    SPEC_INLINE,
    SPEC_P2,
    make_article,
)
from publish_prep import join_text, load_cta_patterns, prepare

CTA_MAP = {"prog_001": 123}
M = "<!-- src: https://a.example.com/ accessed=2026-09-27 -->"


def errors(res):
    return [m.detail for m in res.errors]


@pytest.fixture
def cta_file(tmp_path):
    p = tmp_path / "cta_patterns.yaml"
    p.write_text("patterns:\n  prog_001:\n    pattern_id: 123\n    name: X社\n  prog_002: 456\n",
                 encoding="utf-8")
    return p


def art(tmp_path, body, **kw):
    """品質ゲート（lint・checks）を通る記事を作る。"""
    kw.setdefault("checks", True)
    return make_article(tmp_path, body, **kw)


# --------------------------------------------------------------------------
# 正常系
# --------------------------------------------------------------------------

def test_good_article(good_article, cfg):
    res = prepare(good_article, cfg, CTA_MAP)
    assert errors(res) == []
    html, md = res.content_html, res.content_md

    # マーカーはすべて除去（CTA の同期パターン参照だけが HTML コメントとして残る。
    # カスタムHTMLブロックの区切り <!-- wp:html --> は除いて数える）
    comments = [c for c in re.findall(r"<!--.*?-->", html, re.S)
                if c not in ("<!-- wp:html -->", "<!-- /wp:html -->")]
    assert comments == ['<!-- wp:block {"ref":123} /-->']
    for word in ("src:", "src-block", "calc-block", "nofact", "accessed=", "part:"):
        assert word not in md
    assert "{{" not in html

    # 表が壊れていない（行末の行内マーカーを除去しても行が残る）
    tables = re.findall(r"<table>.*?</table>", html, re.S)
    assert len(tables) == 5           # トレース表・src-block・P2・入力値・試算
    p2 = [t for t in tables if "月額（1人あたり）" in t][0]
    assert p2.count("<tr>") == 3 and "300円" in p2 and "400円" in p2
    calc = [t for t in tables if "月額の試算" in t][0]
    assert "3,000円" in calc and calc.count("<tr>") == 4
    # src-block の直後の注記が表の行にならない
    assert "<p>（◯◯公式サイト、2026年9月時点・税抜）</p>" in html
    # 行内マーカーの後ろの読者向けの出典表記は残る
    assert "1,980円 です。（◯◯公式サイト、2026年9月時点）" in html

    # P7（既定はURLを文字で出す）
    assert "<h2>参考資料・更新履歴</h2>" in html
    urls = [r.url for r in res.references]
    assert len(urls) == len(set(urls))                  # 重複URLは1つ
    assert urls.count("https://x.example.com/pricing") == 1
    assert "<li>https://x.example.com/pricing（参照日：2026-09-27）</li>" in html
    assert '<a href="https://x.example.com/pricing"' not in html
    assert "<li>2026-10-01：公開</li>" in html
    assert "最終更新日：2026-10-01" in html

    # T8: 同じ段落の改行をつなぐ（日本語の間に空白を入れない）
    assert ("勤怠管理システムの選び方が一般の企業と違います。この記事では、必要な要件と、"
            "要件を満たす製品を整理します。") in html
    assert "<p>Q. 非常勤のスタッフだけでも使えますか？A. 使えます。" in html

    # meta.json
    m = res.meta
    assert m["title"].startswith("歯科医院の勤怠管理システム")
    assert m["slug"] == "dental-clinic-attendance-system"
    assert m["description"]
    assert m["updated"] == "2026-10-01" and m["display_updated"] == "2026-10-01"
    assert m["post_type"] == "post" and m["p7_links"] == "text"
    assert m["content_hash"] == res.content_hash and len(res.content_hash) == 16
    assert m["ctas"] == [{"program_id": "prog_001", "pattern_id": 123, "line": m["ctas"][0]["line"]}]


def test_p7_links_per_article(tmp_path, cfg):
    """判断7: 既定は文字。front matter に p7_links: link と書いた記事だけリンクにする。"""
    fm = ("---\ntype: A\nslug: linked\ntitle: t\ndescription: d\nupdated: 2026-10-01\np7_links: link\n"
          "history:\n  - {date: 2026-10-01, note: 公開}\n---\n")
    path = art(tmp_path, f"A は 1,000円 です。{M}\n", name="linked", front_matter=fm)
    res = prepare(path, cfg, CTA_MAP)
    assert errors(res) == []
    assert '<a href="https://a.example.com/">https://a.example.com/</a>（参照日：2026-09-27）' in res.content_html


def test_marker_lines_do_not_merge_blocks(tmp_path, cfg):
    path = art(tmp_path, SPEC_BLOCK + "\n")
    res = prepare(path, cfg, CTA_MAP)
    assert errors(res) == []
    assert res.content_html.count("<tr>") == 3


def test_join_text_rules():
    assert join_text("日本語の文です。", "次の文です。") == "日本語の文です。次の文です。"
    assert join_text("English sentence.", "Next one.") == "English sentence. Next one."
    assert join_text("価格は", "USD 10") == "価格はUSD 10"


def test_hard_break_lists_and_quotes(tmp_path, cfg):
    # 行末の空白2つ（ハード改行）はエディタで消えやすいので、文字列で組み立てる
    body = "\n".join([
        "住所：東京都" + "  ",
        "電話：なし",
        "会社名：例" + "\\",
        "所在地：なし",
        "",
        "- 項目の最初の行です。",
        "  続きの行です。",
        "- 次の項目です。",
        "",
        "> 引用の最初の行です。",
        "> 引用の次の行です。",
        f"月額は 1,980円 です。{M}",
        "",
    ])
    res = prepare(art(tmp_path, body), cfg, CTA_MAP)
    assert errors(res) == []
    html = res.content_html
    assert "東京都<br />\n電話：なし" in html          # 行末の空白2つのハード改行は残す
    assert "例<br />\n所在地：なし" in html            # バックスラッシュのハード改行も残す
    assert "<li>項目の最初の行です。続きの行です。</li>" in html
    assert "<li>次の項目です。</li>" in html
    # 引用の直後の行は（CommonMark の遅延継続で）引用の段落に入るので、つなぐ
    assert "引用の次の行です。月額は 1,980円 です。" in html


def test_setext_heading_and_inline_html_join(tmp_path, cfg):
    """C17: Setext 見出しを壊さない。行頭がインライン HTML の行も段落の中ならつなぐ。"""
    body = f"""\
    月額は 1,980円 です。{M}
    <strong>注意：</strong>年契約のみです。

    まとめの見出し
    ===

    本文です。
    """
    res = prepare(art(tmp_path, body), cfg, CTA_MAP)
    assert errors(res) == []
    html = res.content_html
    assert "<p>月額は 1,980円 です。<strong>注意：</strong>年契約のみです。</p>" in html
    assert "<h1>まとめの見出し</h1>" in html


def test_standalone_marker_between_list_items_keeps_tight_list(tmp_path, cfg):
    """C17: リスト項目の間の単独の行のマーカーを空行にすると、ゆるいリストに変わる。行ごと消す。"""
    body = f"""\
    月額は 1,980円 です。{M}

    - 項目A
    <!-- part: checklist -->
    - 項目B
    <!-- /part -->
    """
    res = prepare(art(tmp_path, body), cfg, CTA_MAP)
    assert "<li>項目A</li>\n<li>項目B</li>" in res.content_html
    assert "<p>項目A</p>" not in res.content_html


def test_reference_dedupe_keeps_latest_accessed(tmp_path, cfg):
    path = art(tmp_path, """\
    A は 1,000円 です。<!-- src: https://a.example.com/ accessed=2026-09-01 -->
    B は 2,000円 です。<!-- src: https://a.example.com/ accessed=2026-09-27 -->
    C は 3,000円 です。<!-- src: https://c.example.com/ accessed=2026-09-10 -->
    """)
    res = prepare(path, cfg, CTA_MAP)
    assert [(r.url, r.accessed) for r in res.references] == [
        ("https://a.example.com/", "2026-09-27"), ("https://c.example.com/", "2026-09-10")]


def test_history_sorted_newest_first(tmp_path, cfg):
    fm = """\
---
type: A
slug: hist
title: t
description: d
updated: 2026-11-01
history:
  - {date: 2026-10-01, note: 公開}
  - {date: 2026-11-01, note: X社の料金改定を反映}
---
"""
    path = art(tmp_path, f"A は 1,000円 です。{M}\n", name="hist", front_matter=fm)
    html = prepare(path, cfg, CTA_MAP).content_html
    assert html.index("2026-11-01：X社の料金改定を反映") < html.index("2026-10-01：公開")


def test_affiliate_link_with_rel_is_ok(tmp_path, cfg):
    path = art(tmp_path, f"""\
    A は 1,000円 です。{M}

    <a href="https://px.a8.net/svt/ejp?a8mat=XXXX" rel="nofollow sponsored">公式サイト</a>
    """)
    assert errors(prepare(path, cfg, CTA_MAP)) == []


# --------------------------------------------------------------------------
# 品質ゲート（C3）
# --------------------------------------------------------------------------

def test_lint_errors_block_output(tmp_path, cfg):
    """C3: lint のエラーがある記事は書き出さない。"""
    path = art(tmp_path, f"月額は 1,980円 です。{M}\n導入企業は 12,000社 です。\n")
    errs = errors(prepare(path, cfg, CTA_MAP))
    assert any("lint のエラーが 1 件" in e and "unmarked_number" in e for e in errs)


def test_checks_required(tmp_path, cfg):
    """C3: 必須の checks がなければエラー。"""
    path = make_article(tmp_path, f"A は 1,000円 です。{M}\n")
    assert any("checks がありません" in e for e in errors(prepare(path, cfg, CTA_MAP)))


def test_checks_must_pass_and_have_current_hash(tmp_path, cfg):
    path = art(tmp_path, f"A は 1,000円 です。{M}\n")
    assert errors(prepare(path, cfg, CTA_MAP)) == []
    # 正本を1文字でも変えると、記録したハッシュと合わなくなる（古い合格の流用を防ぐ）
    path.write_text(path.read_text(encoding="utf-8").replace("1,000円", "1,100円"), encoding="utf-8")
    errs = errors(prepare(path, cfg, CTA_MAP))
    assert sum("今の正本のハッシュ" in e for e in errs) == 3     # checks 2つ＋受領ファイル
    # result が pass でない
    path2 = art(tmp_path, f"A は 1,000円 です。{M}\n", name="fail")
    path2.write_text(path2.read_text(encoding="utf-8").replace("owner_review: {result: pass",
                                                               "owner_review: {result: pending"), encoding="utf-8")
    assert any("checks.owner_review.result" in e for e in errors(prepare(path2, cfg, CTA_MAP)))


def test_checks_changes_do_not_change_hash(tmp_path, cfg):
    """checks の中身はハッシュに含まれない（循環しない）。改行コードの違いも影響しない。"""
    from article import content_hash, load_article
    p1 = art(tmp_path, f"A は 1,000円 です。{M}\n", name="h1")
    p2 = make_article(tmp_path, f"A は 1,000円 です。{M}\n", name="h1b",
                      front_matter=p1.read_text(encoding="utf-8").split("---\n")[1].join(["---\n", "---\n"])
                      .replace("date: 2026-10-01, hash", "date: 2026-10-02, hash"), newline="\r\n")
    assert content_hash(load_article(p1)) == content_hash(load_article(p2))


def test_type_b_requires_trace_table_check(good_article, cfg):
    text = good_article.read_text(encoding="utf-8")
    text = re.sub(r"  trace_table: \{[^}]*\}.*\n", "", text)
    good_article.write_text(text, encoding="utf-8")
    assert any("checks.trace_table がありません" in e for e in errors(prepare(good_article, cfg, CTA_MAP)))


def test_hash_command(good_article, capsys, test_config_path):
    from article import content_hash, load_article
    assert publish_prep.main(["--hash", str(good_article), "-c", str(test_config_path)]) == 0
    out = capsys.readouterr().out
    assert content_hash(load_article(good_article)) in out


# --------------------------------------------------------------------------
# 異常系
# --------------------------------------------------------------------------

def test_missing_cta_mapping_is_error(tmp_path, cfg):
    path = art(tmp_path, f"A は 1,000円 です。{M}\n\n{{{{CTA:prog_999}}}}\n")
    assert any("prog_999" in e for e in errors(prepare(path, cfg, CTA_MAP)))


def test_inline_cta_is_error(tmp_path, cfg):
    path = art(tmp_path, f"詳しくは {{{{CTA:prog_001}}}} から。A は 1,000円 です。{M}\n")
    assert any("単独の行" in e for e in errors(prepare(path, cfg, CTA_MAP)))


def test_cta_inside_paragraph_is_error(tmp_path, cfg):
    path = art(tmp_path, f"A は 1,000円 です。{M}\n{{{{CTA:prog_001}}}}\n")
    assert any("前後に空行" in e for e in errors(prepare(path, cfg, CTA_MAP)))


def test_leftover_placeholder_is_error(tmp_path, cfg):
    path = art(tmp_path, f"{{{{業種}}}}では A は 1,000円 です。{M}\n")
    assert any("置き換え漏れ" in e for e in errors(prepare(path, cfg, CTA_MAP)))


def test_placeholder_in_title_is_error(tmp_path, cfg):
    path = art(tmp_path, f"A は 1,000円 です。{M}\n", title="{{業種}}の勤怠管理")
    assert any("title" in e for e in errors(prepare(path, cfg, CTA_MAP)))


@pytest.mark.parametrize("link", [
    '<a href="https://px.a8.net/svt/ejp?a8mat=XXXX">公式サイト</a>',
    '[公式サイト](https://af.moshimo.com/af/c/click?a_id=1)',
    '<a href="https://h.accesstrade.net/sp/cc?rk=1" rel="noopener">申し込む</a>',
])
def test_affiliate_link_without_rel_is_error(tmp_path, cfg, link):
    path = art(tmp_path, f"A は 1,000円 です。{M}\n\n{link}\n")
    assert any("rel=" in e for e in errors(prepare(path, cfg, CTA_MAP)))


@pytest.mark.parametrize("heading", ["## 参考資料", "## 参考資料一覧", "## 更新・訂正履歴", "### 参考資料・更新履歴"])
def test_p7_heading_in_canonical_is_error(tmp_path, cfg, heading):
    """C19: P7 の見出し（履歴を含む）が正本にあればエラー（前方一致）。"""
    path = art(tmp_path, f"A は 1,000円 です。{M}\n\n{heading}\n\n- x\n")
    assert any("P7" in e for e in errors(prepare(path, cfg, CTA_MAP)))


def test_missing_accessed_and_history(tmp_path, cfg):
    fm = "---\ntype: A\nslug: nohist\ntitle: t\ndescription: d\nupdated: 2026-10-01\n---\n"
    path = art(tmp_path, "A は 1,000円 です。<!-- src: https://a.example.com/ -->\n",
               name="nohist", front_matter=fm)
    errs = errors(prepare(path, cfg, CTA_MAP))
    assert any("accessed" in e for e in errs)
    assert any("history" in e for e in errs)


def test_history_note_is_escaped(tmp_path, cfg):
    """C18: history の note に HTML があっても、そのまま出力しない。"""
    fm = ('---\ntype: A\nslug: esc\ntitle: t\ndescription: d\nupdated: 2026-10-01\nhistory:\n'
          '  - {date: 2026-10-01, note: "<script>alert(1)</script>"}\n---\n')
    path = art(tmp_path, f"A は 1,000円 です。{M}\n", name="esc", front_matter=fm)
    html = prepare(path, cfg, CTA_MAP).content_html
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_front_matter_required(tmp_path, cfg):
    p = tmp_path / "no-fm.md"
    p.write_text("本文だけ\n", encoding="utf-8")
    assert any("front matter" in e for e in errors(prepare(p, cfg, CTA_MAP)))
    path = art(tmp_path, f"A は 1,000円 です。{M}\n", name="Bad_Slug")
    assert any("slug" in e for e in errors(prepare(path, cfg, CTA_MAP)))


# --------------------------------------------------------------------------
# CTA の対応表・main
# --------------------------------------------------------------------------

def test_load_cta_patterns(cta_file, tmp_path):
    assert load_cta_patterns(cta_file) == {"prog_001": 123, "prog_002": 456}
    bad = tmp_path / "bad.yaml"
    bad.write_text("patterns:\n  prog_001: {name: x}\n", encoding="utf-8")
    with pytest.raises(Exception):
        load_cta_patterns(bad)
    with pytest.raises(Exception):
        load_cta_patterns(tmp_path / "missing.yaml")


def test_data_template_cta_patterns_loads():
    assert load_cta_patterns(DATA_DIR / "cta_patterns.yaml") == {"prog_example_001": 99999}


def test_main_writes_outputs(good_article, tmp_path, cta_file, test_config_path):
    out = tmp_path / "build"
    rc = publish_prep.main([str(good_article), "-c", str(test_config_path), "--out-dir", str(out),
                            "--cta-patterns", str(cta_file)])
    assert rc == 0
    d = out / "dental-clinic-attendance-system"
    assert (d / "content.html").exists() and (d / "content.md").exists()
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["display_updated"] == "2026-10-01" and meta["content_html_file"] == "content.html"


def test_main_does_not_write_on_error(tmp_path, cta_file, test_config_path):
    path = art(tmp_path, f"{{{{業種}}}} A は 1,000円 です。{M}\n", name="broken")
    out = tmp_path / "build"
    rc = publish_prep.main([str(path), "-c", str(test_config_path), "--out-dir", str(out),
                            "--cta-patterns", str(cta_file)])
    assert rc == 1
    assert not (out / "broken").exists()


def test_spec_examples_render(tmp_path, cfg):
    """§1-2・§2 の例を並べても、マーカーが残らず表が崩れない。"""
    body = "\n\n".join([SPEC_INLINE, SPEC_BLOCK, SPEC_CALC, SPEC_P2]) + "\n"
    res = prepare(art(tmp_path, body), cfg, CTA_MAP)
    assert errors(res) == []
    # カスタムHTMLブロックの区切りを除けば、HTMLコメントは残らない
    unwrapped = res.content_html.replace("<!-- wp:html -->", "").replace("<!-- /wp:html -->", "")
    assert "<!--" not in unwrapped
    assert res.content_html.count("<table>") == 4
