#!/usr/bin/env python
"""
postcheck.py — 公開後の照合（設計書 §8 T6・§6「公開後」）。

正本（articles/{slug}.md）と公開ページのURLを受け取り、次を確かめる。

  1. 数値の突き合わせ: 正本を publish_prep.py と同じ方法で描いた本文（マーカー除去・P7付き。CTA は除く）の
     **全数値の並び**と、公開ページの本文（content_selector。広告表示・目次・CTA は exclude_selectors で除く）の
     全数値の並びを比べ、**足りない数値・ページにだけある数値・置き換わった数値**を前後の文脈つきで報告する
     （並びで比べるので、行の入れ替え・書き換え・書き足しも分かる）
  2. マーカーの残り（本文に「<!--」「-->」「accessed=」「src-block」などが見えている／
     HTMLソースに出典マーカーのコメントが残っている）
  3. 「{{」の残り
  4. 広告表示（docs/legal/ad-disclosure.md のクラス .ad-disclosure）の欠落、文言に「広告」がないこと、
     本文の最初の見出し・表より後ろにあること（HTMLのツリー上の順序で比べる。判定できなければ要確認）
  5. 既知のアフィリエイトのドメインへのリンクで rel="sponsored" も nofollow もないもの
  6. CTA の同期パターン（.cta）の中に数値がないこと、中身がボタン（リンク）と計測用の画像タグだけであること
  7. front matter の programs ごとに、data/asp-programs.csv の計測ドメインの <img>（計測用の画像タグ）が本文にあるか

取得は common.PoliteFetcher（robots.txt 尊重・UA明示・間隔）を使う。自社サイトで robots.txt が
取得を拒んでいる場合に限り --ignore-robots を使う。
ネットワークを使わずに確かめたいとき（保存したHTML）は --html-file。

使い方
  python tools/postcheck.py articles/dental-clinic-attendance-system.md --url https://example.com/dental-clinic-attendance-system/
  python tools/postcheck.py articles/foo.md --html-file saved.html

終了コード: 0 = 問題なし / 1 = 要確認あり / 2 = 実行エラー
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import difflib
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from common import (
    NumberToken,
    ToolError,
    build_fetcher,
    cfg_get,
    expand_path,
    extract_numbers,
    format_decimal,
    html_to_text,
    load_config,
    load_env,
    setup_logging,
    write_text,
)
from article import (
    MARKER_COMMENT_RE,
    find_affiliate_links_without_rel,
    host_matches,
    load_article,
    load_asp_programs,
    split_domains,
)

LOG = logging.getLogger("affiliate.tools.postcheck")

VISIBLE_MARKER_RE = re.compile(r"<!--|-->|accessed\s*=\s*\d{4}|src-block|calc-block|nofact|<!--\s*src:|part:\s*\w")
DEFAULT_EXCLUDE = [".ad-disclosure", "#toc", ".toc", ".cta"]


@dataclasses.dataclass
class PostIssue:
    kind: str
    detail: str
    line_no: int = 0


@dataclasses.dataclass
class PostConfig:
    content_selector: str = ".entry-content"
    ad_selector: str = ".ad-disclosure"
    ad_text: str = "広告"
    cta_selector: str = ".cta"
    strict_units: bool = True
    exclude_selectors: tuple[str, ...] = tuple(DEFAULT_EXCLUDE)
    affiliate_domains: tuple[str, ...] = ()
    asp_programs_file: Optional[Path] = None
    cfg: dict[str, Any] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> "PostConfig":
        node = cfg_get(cfg, "postcheck", {}) or {}
        asp = cfg_get(cfg, "paths.asp_programs_file", "../data/asp-programs.csv")
        return cls(
            content_selector=str(node.get("content_selector", ".entry-content") or ""),
            ad_selector=str(node.get("ad_disclosure_selector", ".ad-disclosure")),
            ad_text=str(node.get("ad_disclosure_text", "広告")),
            cta_selector=str(node.get("cta_selector", ".cta") or ""),
            strict_units=bool(node.get("strict_units", True)),
            exclude_selectors=tuple(node.get("exclude_selectors", DEFAULT_EXCLUDE) or ()),
            affiliate_domains=tuple(cfg_get(cfg, "affiliate_domains", []) or []),
            asp_programs_file=expand_path(asp) if asp else None,
            cfg=cfg,
        )


def _key(tok: NumberToken, strict_units: bool) -> tuple[str, Optional[str]]:
    return (format_decimal(tok.value), tok.unit if strict_units else None)


def canonical_text(path: Path, cfg: dict[str, Any]) -> str:
    """正本を publish_prep と同じ方法で描いた本文の文字（CTA は描かない。品質ゲートは確かめない）。"""
    from publish_prep import prepare

    res = prepare(path, cfg, None, gate=False)
    if res.article.front_matter is None:
        raise ToolError(f"正本の front matter を読めません: {path}")
    return html_to_text(res.content_html)


def _describe(tok: NumberToken) -> str:
    return f"{tok.raw}（…{tok.context.strip()}…）"


def compare_numbers(canon: list[NumberToken], page: list[NumberToken], strict_units: bool) -> list[PostIssue]:
    a = [_key(t, strict_units) for t in canon]
    b = [_key(t, strict_units) for t in page]
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    issues: list[PostIssue] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        missing = canon[i1:i2]
        extra = page[j1:j2]
        if op == "replace":
            issues.append(PostIssue(
                "number_changed",
                "正本と公開ページで数値が違います。正本: " + "、".join(_describe(t) for t in missing)
                + " ／ 公開ページ: " + "、".join(_describe(t) for t in extra)))
        elif op == "delete":
            issues.append(PostIssue(
                "number_missing", "正本の数値が公開ページにありません: " + "、".join(_describe(t) for t in missing)))
        elif op == "insert":
            issues.append(PostIssue(
                "number_extra", "公開ページにだけある数値です（WordPress 上での書き足し・テーマの挿入の可能性）: "
                + "、".join(_describe(t) for t in extra)))
    return issues


def check_page(canonical: Path, html: str, pc: PostConfig) -> tuple[list[PostIssue], list[str]]:
    """公開ページのHTMLを確かめる。戻り値: (要確認, 情報)"""
    issues: list[PostIssue] = []
    info: list[str] = []

    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError as exc:
        raise ToolError("beautifulsoup4 が未インストールです。`pip install -r tools/requirements.txt`") from exc

    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one(pc.content_selector) if pc.content_selector else None
    if pc.content_selector and root is None:
        issues.append(PostIssue("content_selector_missing",
                                f"本文の範囲 {pc.content_selector} が見つかりません（ページ全体で照合したため、"
                                f"サイドバー等の数値も比べています）"))
    scope = root if root is not None else (soup.body or soup)

    # 1. 数値（本文から広告表示・目次・CTA を除いて比べる）
    scope_copy = copy.copy(scope)
    for sel in pc.exclude_selectors:
        for el in scope_copy.select(sel):
            el.decompose()
    page_text = html_to_text(str(scope_copy))
    canon = extract_numbers(canonical_text(canonical, pc.cfg), skip_year_like=False)
    page = extract_numbers(page_text, skip_year_like=False)
    issues += compare_numbers(canon, page, pc.strict_units)
    info.append(f"正本の数値 {len(canon)} 個・公開ページの数値 {len(page)} 個を並びで比べました"
                f"（除外: {', '.join(pc.exclude_selectors) or 'なし'}）")

    # 2. マーカーの残り
    visible = html_to_text(str(scope))
    for m in VISIBLE_MARKER_RE.finditer(visible):
        s = max(0, m.start() - 20)
        issues.append(PostIssue("marker_visible", f"本文にマーカーの残りが見えています: …{visible[s:m.end() + 20]}…"))
    for m in MARKER_COMMENT_RE.finditer(html):
        issues.append(PostIssue("marker_in_source",
                                f"HTMLソースに出典マーカーのコメントが残っています: {html[m.start():m.start() + 60]}"))

    # 3. {{ の残り
    for m in re.finditer(r"\{\{", visible):
        s = max(0, m.start() - 20)
        issues.append(PostIssue("placeholder_visible", f"本文に {{{{ が残っています: …{visible[s:m.start() + 30]}…"))

    # 4. 広告表示（ツリー上の順序で比べる）
    ads = soup.select(pc.ad_selector) if pc.ad_selector else []
    if not ads:
        issues.append(PostIssue("ad_disclosure_missing",
                                f"広告表示（{pc.ad_selector}）がありません（docs/legal/ad-disclosure.md。"
                                f"固定ページで作っていないか確認する。§1-3）"))
    else:
        ad = ads[0]
        if pc.ad_text and pc.ad_text not in ad.get_text():
            issues.append(PostIssue("ad_disclosure_text", f"広告表示に「{pc.ad_text}」の文字がありません"))
        order = {id(el): i for i, el in enumerate(soup.find_all(True))}
        first_block = root.find(["h2", "h3", "table"]) if root is not None else None
        if root is None or first_block is None:
            issues.append(PostIssue("ad_disclosure_position_unknown",
                                    "広告表示の位置を判定できません（本文の範囲か、本文の見出し・表が見つからない）。"
                                    "スマホで開いて、スクロールせずに読めるかを人が確かめる"))
        elif order[id(ad)] > order[id(first_block)]:
            issues.append(PostIssue("ad_disclosure_position",
                                    "広告表示が本文の最初の見出し・表より後ろにあります（ファーストビューに入らない可能性）"))
        info.append("広告表示がソース上にあることを確かめました（スマホのファーストビューに入るかは人が確かめる）")

    # 5. rel のないアフィリエイトリンク（ページ全体。CTAの同期パターンの中のリンクもここで確かめられる）
    for p in find_affiliate_links_without_rel(html, pc.affiliate_domains):
        issues.append(PostIssue("affiliate_rel_missing",
                                f"アフィリエイトリンクに rel=\"sponsored\"（または nofollow）がありません: "
                                f"{p.href}（「{p.text}」rel=\"{p.rel}\"）"))

    # 6. CTA の同期パターン（.cta）の中身: 数値を書かない・ボタンと計測用の画像タグだけ（設計書 §2 P4b）
    issues += check_cta(scope, pc)

    # 7. 計測用の画像タグ
    issues += check_tracking_images(canonical, scope, pc, info)
    return issues, info


CTA_ALLOWED_TAGS = {"a", "img", "span", "div", "br", "figure"}


def _tracking_domains(pc: PostConfig) -> list[str]:
    if pc.asp_programs_file is None:
        return []
    try:
        rows = load_asp_programs(pc.asp_programs_file)
    except ToolError:
        return []
    return [d for row in rows.values() for d in split_domains(row.get("tracking_domains", ""))]


def check_cta(scope, pc: PostConfig) -> list[PostIssue]:
    """.cta の中の数値はすべてエラー。中の要素はボタン（リンク）と計測用の画像タグだけ。

    同期パターンは正本の外にあり出典の照合を通らないため、料金などの数値を書かせない（設計書 §2 P4b）。
    """
    issues: list[PostIssue] = []
    domains = _tracking_domains(pc)
    for cta in scope.select(pc.cta_selector) if pc.cta_selector else []:
        for tok in extract_numbers(cta.get_text(" ", strip=True), skip_year_like=False):
            issues.append(PostIssue("cta_number",
                                    f"CTA（{pc.cta_selector}）の中に数値があります: {tok.raw}（…{tok.context.strip()}…）。"
                                    f"同期パターンには数値を書かない（料金は本文に出典つきで書く）"))
        for el in cta.find_all(True):
            if el.name not in CTA_ALLOWED_TAGS:
                issues.append(PostIssue("cta_element",
                                        f"CTA の中にボタンと計測用の画像タグ以外の要素 <{el.name}> があります"))
            elif el.name == "img":
                host = urlparse(el.get("src", "")).hostname or ""
                if not host_matches(host, domains):
                    issues.append(PostIssue("cta_element",
                                            f"CTA の中の画像 {el.get('src', '')[:60]} は計測用の画像タグ"
                                            f"（data/asp-programs.csv の tracking_domains）ではありません"))
        for text in cta.find_all(string=True):
            if text.strip() and text.find_parent("a") is None:
                issues.append(PostIssue("cta_element",
                                        f"CTA の中に、ボタン（リンク）の外の文字があります: 「{text.strip()[:40]}」"))
    return issues


def check_tracking_images(canonical: Path, scope, pc: PostConfig, info: list[str]) -> list[PostIssue]:
    art = load_article(canonical)
    programs = [str(p) for p in (art.fm("programs") or [])]
    if not programs:
        return []
    if pc.asp_programs_file is None:
        return []
    try:
        known = load_asp_programs(pc.asp_programs_file)
    except ToolError as exc:
        return [PostIssue("tracking_img_unknown", str(exc))]
    issues = []
    srcs = [img.get("src", "") for img in scope.find_all("img")]
    for pid in programs:
        row = known.get(pid)
        if row is None:
            issues.append(PostIssue("tracking_img_unknown", f"案件ID {pid} が {pc.asp_programs_file.name} にありません"))
            continue
        domains = split_domains(row.get("tracking_domains", ""))
        if not domains:
            info.append(f"{pid}: 計測ドメインが未記入のため、計測用の画像タグは確かめていません")
            continue
        if not any(host_matches(urlparse(s).hostname or "", domains) for s in srcs):
            issues.append(PostIssue("tracking_img_missing",
                                    f"{pid} の計測用の画像タグ（{', '.join(domains)} の <img>）が本文にありません"
                                    f"（§6 計測用の画像タグが残っている）"))
    return issues


def render_report(canonical: Path, source: str, issues: list[PostIssue], info: list[str]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# 公開後の照合レポート（postcheck.py）",
        "",
        f"生成日時: {now}",
        f"正本: {canonical}",
        f"公開ページ: {source}",
        f"**要確認: {len(issues)} 件**",
        "",
    ]
    if issues:
        lines += ["## 要確認", ""]
        for i in issues:
            loc = f"（正本 L{i.line_no}）" if i.line_no else ""
            lines.append(f"- [{i.kind}]{loc} {i.detail}")
        lines.append("")
    else:
        lines += ["正本と公開ページの数値の並びが一致し、マーカー・{{ の残り、広告表示の欠落、rel のないリンク、"
                  "計測用の画像タグの欠落はありません。", ""]
    if info:
        lines += ["## 情報", ""] + [f"- {x}" for x in info] + [""]
    lines += [
        "## 人が確かめること（このツールでは確かめられない）",
        "",
        "- スマホで開いた瞬間、スクロールせずに広告表示が読める（§6 公開後）",
        "- スマホで比較表が読める",
        "- ASPリンクのクリックが計測されている",
        "",
    ]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="正本と公開ページを突き合わせる（公開後の照合）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("canonical", help="正本（articles/{slug}.md）")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="公開ページのURL")
    src.add_argument("--html-file", help="公開ページのHTMLを保存したファイル（ネットワークを使わない）")
    parser.add_argument("-c", "--config", help="設定ファイル（既定: tools/config.yaml）")
    parser.add_argument("--selector", help="本文の範囲のCSSセレクタ（既定: postcheck.content_selector）")
    parser.add_argument("--ignore-robots", action="store_true",
                        help="robots.txt を無視する（自社サイトの場合のみ）")
    parser.add_argument("-o", "--out", help="Markdownレポートの出力先")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    try:
        load_env()
        cfg = load_config(args.config)
        pc = PostConfig.from_cfg(cfg)
        if args.selector is not None:
            pc.content_selector = args.selector
        canonical = Path(args.canonical)
        if not canonical.exists():
            raise ToolError(f"正本が見つかりません: {canonical}")
        if args.html_file:
            html = Path(args.html_file).read_text(encoding="utf-8")
            source = args.html_file
        else:
            fetcher = build_fetcher(cfg, ignore_robots=args.ignore_robots)
            result = fetcher.fetch(args.url)
            if not result.ok:
                raise ToolError(f"公開ページを取得できません: {args.url}: {result.error}")
            if result.redirected:
                LOG.warning("公開ページがリダイレクトしました: %s → %s", args.url, result.url)
            html = result.text
            source = result.url
        issues, info = check_page(canonical, html, pc)
        report = render_report(canonical, source, issues, info)
        if args.out:
            write_text(Path(args.out), report)
        else:
            sys.stdout.write(report)
        return 1 if issues else 0
    except ToolError as exc:
        LOG.error("%s", exc)
        return 2
    except Exception:
        LOG.exception("想定外のエラーで停止しました")
        return 2


if __name__ == "__main__":
    sys.exit(main())
