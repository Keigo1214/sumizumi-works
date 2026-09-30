#!/usr/bin/env python
"""
factcheck.py — 記事内の「数値 × 出典URL」を機械照合する。

記事（Markdown / HTML）から出典マーカーと、そのマーカーが担当する範囲の数値を抽出し、
出典URLを取得して当該数値が参照先ページに実在するかを照合する。
**一致したものは報告しない。** 不一致・URLの異常（404 / リダイレクト / 取得失敗）だけを
レポートに出す。人間は不一致アラートのみを確認する運用を想定している。

--lint を付けると、出典URLを取得せずに記事の書き方だけを検査する（設計書 §8 T1）。

出典マーカーの書式（記事テンプレート docs/article-template.md §1-2 と同じ）
--------------------------------------------------------------------------
1) 行内マーカー — 同じ行の、行頭（または直前のマーカー）からこのマーカーまでに適用される
       月額料金は 1,980円 です。<!-- src: https://example.com/pricing accessed=2026-07-20 -->

2) ブロックマーカー — 開始タグから終了タグまでの全行に適用される（出典が1つだけの表向け）
       <!-- src-block: https://example.com/pricing accessed=2026-07-20 -->
       | プラン | 月額 |
       |---|---|
       | ライト | 1,000円 |
       <!-- /src-block -->

3) 計算した値 — calc-block の中は照合しない（lint も対象外。入力値の表と計算式を直前に置く）
       <!-- calc-block -->
       | サービス | 月額の試算 |
       ...
       <!-- /calc-block -->

4) 脚注マーカー（[^キー]）— 照合モードは互換のため読むが、記事テンプレートでは**禁止**。
   --lint ではエラーになる。

accessed= は記事テンプレートでは必須（--lint で欠落を検出する）。照合には使わない。
URL は http/https のみ。1つのマーカーに複数URLは書けない（複数出典なら分割する）。

照合の対象外にしたい数値がある場合
--------------------------------------------------------------------------
  * config.yaml の factcheck.ignore_patterns に正規表現を書く
  * 西暦らしき4桁（1900〜2100、単位なし）は既定で除外（--no-skip-years で無効化）
  * 記事側で数値を `<!-- nofact -->` を含む行に置くとその行全体を除外
  * front matter・コードブロック・番号付きリストの行頭の番号は対象外（.md のみ）

手動確認の記録（T2）: data/sources/{slug}.md の「## 手動確認の記録」の表（manual_checks.py）
偶然の一致（T7）: 0〜一桁・単位なしの数値、出典側に単位がない一致、修飾語（約・以上 など）の食い違いは、
  一致しても出典ページ側の文脈をレポートに出し、既定で終了コード1にする（--no-fail-on-coincidence で変更可）
.md には照合モードでも lint を実行し、結果をレポートに併記する（lint のエラーがあれば終了コード1）

使い方
--------------------------------------------------------------------------
  python tools/factcheck.py articles/*.md
  python tools/factcheck.py articles/ --recursive --out tools/reports/factcheck.md
  python tools/factcheck.py articles/foo.md --json tools/reports/factcheck.json -v
  python tools/factcheck.py --lint articles/foo.md

終了コード: 0 = 問題なし / 1 = 要確認あり / 2 = 実行エラー
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from common import (
    FetchResult,
    NumberToken,
    PoliteFetcher,
    ToolError,
    build_fetcher,
    cfg_get,
    expand_path,
    format_decimal,
    html_to_text,
    iter_number_matches,
    load_config,
    load_env,
    normalize_whitespace,
    numbers_match,
    setup_logging,
    write_json,
    write_text,
)
from article import (  # noqa: F401  (INLINE_SRC_RE などは互換のため再公開する)
    ACCESSED_RE,
    BLOCK_END_RE,
    BLOCK_START_RE,
    CALC_END_RE,
    CALC_START_RE,
    FOOTNOTE_DEF_RE,
    FOOTNOTE_REF_RE,
    INLINE_SRC_RE,
    NOFACT_RE,
    PER_UNIT_SUFFIX_DEFAULT,
    analyze_structure,
    is_per_unit_one,
    read_text_with_note,
    split_front_matter,
    split_lines,
    split_marker_body,
    strip_list_number,
    mask_terms,
    strip_md_keep_code,
    validate_slug,
)

# 数値の前後の修飾語（T7。記事と出典で食い違えば文脈を出す）
QUALIFIER_PREFIXES = ("約", "最大", "最低", "最小", "最長", "最短")
QUALIFIER_SUFFIXES = ("以上", "以下", "未満", "超", "〜", "~", "から")


def qualifiers_at(normalized: str, start: int, end: int) -> frozenset[str]:
    before = normalized[max(0, start - 3):start].rstrip()
    after = normalized[end:end + 3].lstrip()
    out = {q for q in QUALIFIER_PREFIXES if before.endswith(q)}
    out |= {q for q in QUALIFIER_SUFFIXES if after.startswith(q)}
    # 「〜」「~」「から」は同じ修飾語（起点を表す）として扱う
    for same in ("~", "から"):
        if same in out:
            out.discard(same)
            out.add("〜")
    return frozenset(out)

LOG = logging.getLogger("affiliate.tools.factcheck")

DEFAULT_SOURCES_DIR = "../data/sources"


@dataclasses.dataclass
class Claim:
    """1つの出典URLと、それが裏付けるべき数値群。"""
    url: str
    accessed: Optional[str]
    marker_kind: str          # inline / block / footnote
    source_file: Path
    line_no: int
    numbers: list[NumberToken] = dataclasses.field(default_factory=list)
    snippet: str = ""
    # numbers のうち「1人あたり」の 1 のように偶然一致の報告（T7）から外すものの index
    per_unit_idx: set[int] = dataclasses.field(default_factory=set)
    # numbers と同じ順の、数値の前後の修飾語（約・以上 など。T7）
    qualifiers: list[frozenset] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Issue:
    kind: str                 # url_error / url_redirect / number_mismatch / no_numbers / empty_page
    source_file: Path
    line_no: int
    url: str
    detail: str
    number_raw: str = ""
    number_context: str = ""
    final_url: str = ""
    number_value: str = ""    # 正規化後の値（手動確認の記録との照合用）
    number_unit: str = ""


@dataclasses.dataclass
class Coincidence:
    """一致はしたが、偶然の一致かもしれない数値（T7）。"""
    source_file: Path
    line_no: int
    url: str
    token: NumberToken
    page_contexts: list[str]
    reason: str
    page_hash: str = ""       # 出典ページから抜き出した本文のハッシュ（偶然の一致の記録に書く値）


def page_text_hash(text: str) -> str:
    """出典ページから抜き出した本文（html_to_text の結果）のハッシュ（16進16文字）。"""
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# 記事のパース
# --------------------------------------------------------------------------

def _is_marker_comment(body: str) -> bool:
    return bool(INLINE_SRC_RE.match(body) or BLOCK_START_RE.match(body)
                or BLOCK_END_RE.match(body) or NOFACT_RE.match(body)
                or CALC_START_RE.match(body) or CALC_END_RE.match(body))


def _html_to_checkable_text(html: str) -> str:
    """HTML からタグを落としつつ、出典マーカー（HTMLコメント）は残す。"""
    text = re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>", " ", html)
    placeholders: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        body = m.group(0)
        if _is_marker_comment(body):
            placeholders.append(body)
            return f"\x00SRC{len(placeholders) - 1}\x00"
        return " "

    text = re.sub(r"(?s)<!--.*?-->", _stash, text)
    # HTML ではソース中の改行は単なる空白。先に潰しておかないと、
    # 「数値とマーカーがソース上で別行」というだけで担当範囲が切れてしまう。
    text = re.sub(r"\s*\n\s*", " ", text)
    # 改行はブロックレベル要素の境界からのみ作る
    text = re.sub(
        r"(?is)<\s*(br|/p|/div|/tr|/li|/h[1-6]|/section|/article|/table|/ul|/ol)\s*/?>",
        "\n", text,
    )
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    import html as html_mod
    text = html_mod.unescape(text)

    def _restore(m: re.Match[str]) -> str:
        return placeholders[int(m.group(1))]

    text = re.sub(r"\x00SRC(\d+)\x00", _restore, text)
    # ブロック系マーカーは1行に独立させる（HTML ではタグの前後に改行がないことがある）
    text = re.sub(r"(<!--\s*/?(?:src-block|calc-block)\b[^>]*-->)", r"\n\1\n", text)
    return text


def _read_article_text(path: Path) -> str:
    """UTF-8（BOM可）で読み、改行を LF にそろえる。cp932 で読み直したら警告する。"""
    text, note = read_text_with_note(path)
    if note:
        LOG.warning("%s: %s", path, note)
    return text


def parse_article(path: Path, ignore_patterns: list[str], skip_years: bool,
                  terms: tuple[str, ...] | list[str] = ()) -> tuple[list[Claim], list[Issue]]:
    """記事ファイルから Claim（URL + 担当する数値）を抽出する。

    .md では front matter とコードブロックを読み飛ばし、番号付きリストの行頭の番号を除く
    （どちらも markdown-it のトークンで判定する）。
    calc-block の中は（src-block の中にあっても）照合しない。インラインコードの中の数値も照合する。
    """
    raw = _read_article_text(path)
    is_html = path.suffix.lower() in (".html", ".htm")
    body = _html_to_checkable_text(raw) if is_html else raw

    # 脚注定義を先に回収して本文からは除去（行は残るので行番号はずれない）
    footnotes: dict[str, tuple[str, Optional[str]]] = {}
    for m in FOOTNOTE_DEF_RE.finditer(body):
        accessed = ACCESSED_RE.search(m.group("meta") or "")
        footnotes[m.group("key")] = (
            m.group("url").rstrip(".,、。"),
            accessed.group("date") if accessed else None,
        )
    body_wo_defs = FOOTNOTE_DEF_RE.sub("", body)

    claims: list[Claim] = []
    issues: list[Issue] = []

    lines = split_lines(body_wo_defs)
    ordered_first: set[int] = set()
    if is_html:
        zones = ["body"] * len(lines)
    else:
        raw_lines = split_lines(raw)
        _fm, fm_lines, _err = split_front_matter(raw_lines)
        struct = analyze_structure(raw_lines, fm_lines)
        zones = struct.zones + ["body"] * max(0, len(lines) - len(struct.zones))
        ordered_first = struct.ordered_item_first_lines

    current_idx = [0]

    def prep(text: str, at_line_start: bool) -> str:
        if not is_html and at_line_start and (current_idx[0] - 1) in ordered_first:
            return strip_list_number(text)
        return text

    block_stack: list[tuple[str, Optional[str], int, list[str]]] = []  # url, accessed, line_no, buffer
    calc_depth = 0

    for idx, line in enumerate(lines, start=1):
        current_idx[0] = idx
        if zones[idx - 1] != "body":
            continue

        # calc-block（計算した値）は照合しない。src-block の中にあっても同じ
        has_calc_start = bool(CALC_START_RE.search(line))
        has_calc_end = bool(CALC_END_RE.search(line))
        if has_calc_start and has_calc_end:
            continue
        if has_calc_start:
            calc_depth += 1
            continue
        if has_calc_end:
            if calc_depth == 0:
                LOG.warning("%s:%s 対応する calc-block 開始がない終了タグです", path.name, idx)
            calc_depth = max(0, calc_depth - 1)
            continue
        if calc_depth:
            continue

        if NOFACT_RE.search(line):
            continue

        # ブロック終了
        if BLOCK_END_RE.search(line):
            if not block_stack:
                LOG.warning("%s:%s 対応する src-block 開始がない終了タグです", path.name, idx)
            else:
                url, accessed, start_line, buf = block_stack.pop()
                claims.append(_make_claim(
                    url, accessed, "block", path, start_line,
                    "\n".join(buf), ignore_patterns, skip_years, terms,
                ))
            continue

        # ブロック開始
        bstart = BLOCK_START_RE.search(line)
        if bstart:
            url, accessed = split_marker_body(bstart.group("body"))
            if url is None:
                issues.append(Issue(
                    kind="invalid_marker", source_file=path, line_no=idx, url="",
                    detail=f"src-block マーカーのURLが不正です: {bstart.group(0)}",
                ))
                continue
            block_stack.append((url, accessed, idx, []))
            continue

        if block_stack:
            block_stack[-1][3].append(prep(line, True))
            continue

        # 行内マーカー: 直前のマーカー以降のテキストが担当範囲
        inline_matches = list(INLINE_SRC_RE.finditer(line))
        if inline_matches:
            cursor = 0
            for m in inline_matches:
                segment = prep(line[cursor:m.start()], cursor == 0)
                cursor = m.end()
                url, accessed = split_marker_body(m.group("body"))
                if url is None:
                    issues.append(Issue(
                        kind="invalid_marker", source_file=path, line_no=idx, url="",
                        detail=f"src マーカーのURLが不正です: {m.group(0)}",
                    ))
                    continue
                claims.append(_make_claim(
                    url, accessed, "inline", path, idx, segment,
                    ignore_patterns, skip_years, terms,
                ))
            continue

        # 脚注参照
        refs = list(FOOTNOTE_REF_RE.finditer(line))
        if refs:
            segment = prep(FOOTNOTE_REF_RE.sub("", line), True)
            for m in refs:
                key = m.group("key")
                if key not in footnotes:
                    issues.append(Issue(
                        kind="undefined_footnote", source_file=path, line_no=idx, url="",
                        detail=f"脚注 [^{key}] の定義行 ([^{key}]: https://...) が見つかりません",
                    ))
                    continue
                url, accessed = footnotes[key]
                claims.append(_make_claim(
                    url, accessed, "footnote", path, idx, segment, ignore_patterns, skip_years, terms,
                ))

    for url, _accessed, start_line, _buf in block_stack:
        issues.append(Issue(
            kind="unclosed_block", source_file=path, line_no=start_line, url=url,
            detail="src-block が <!-- /src-block --> で閉じられていません",
        ))

    return claims, issues


def _make_claim(
    url: str,
    accessed: Optional[str],
    kind: str,
    path: Path,
    line_no: int,
    segment: str,
    ignore_patterns: list[str],
    skip_years: bool,
    terms: tuple[str, ...] | list[str] = (),
) -> Claim:
    # インラインコードの中身も照合する（記号だけ落とす）。決まった言い回し（terms）の中の数字は数えない
    text = strip_md_keep_code(mask_terms(segment, terms) if terms else segment)
    matches = iter_number_matches(text, skip_year_like=skip_years, ignore_patterns=ignore_patterns)
    numbers = [tok for tok, _s, _e, _n in matches]
    per_unit = {i for i, (tok, _s, end, norm) in enumerate(matches)
                if is_per_unit_one(tok, end, norm)}
    quals = [qualifiers_at(norm, s, e) for _tok, s, e, norm in matches]
    snippet = normalize_whitespace(text)[:200]
    return Claim(
        url=url, accessed=accessed, marker_kind=kind, source_file=path,
        line_no=line_no, numbers=numbers, snippet=snippet, per_unit_idx=per_unit,
        qualifiers=quals,
    )


# --------------------------------------------------------------------------
# 照合
# --------------------------------------------------------------------------

@dataclasses.dataclass
class CoincidenceConfig:
    """T7 の設定。config.yaml の factcheck.coincidence。"""
    enabled: bool = True
    max_small_value: float = 9          # この値以下（0〜一桁）の数値は文脈を出す
    unitless: bool = True               # 単位のない数値も文脈を出す
    per_unit_suffixes: tuple[str, ...] = PER_UNIT_SUFFIX_DEFAULT
    context_chars: int = 40
    max_contexts: int = 3

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> "CoincidenceConfig":
        node = cfg_get(cfg, "factcheck.coincidence", {}) or {}
        return cls(
            enabled=bool(node.get("enabled", True)),
            max_small_value=float(node.get("max_small_value", 9)),
            unitless=bool(node.get("unitless", True)),
            per_unit_suffixes=tuple(node.get("per_unit_suffixes", PER_UNIT_SUFFIX_DEFAULT)),
            context_chars=int(node.get("context_chars", 40)),
            max_contexts=int(node.get("max_contexts", 3)),
        )


def _coincidence_reason(token: NumberToken, cc: CoincidenceConfig) -> Optional[str]:
    reasons = []
    if abs(token.value) <= cc.max_small_value and abs(token.value) < 10:
        reasons.append("0か一桁の数値")
    if cc.unitless and token.unit is None:
        reasons.append("単位のない数値")
    return "・".join(reasons) or None


def _fmt_quals(q: frozenset) -> str:
    return "・".join(sorted(q)) or "なし"


def verify_claims(
    claims: list[Claim],
    fetcher: PoliteFetcher,
    strict_units: bool,
    rel_tolerance: float,
    warn_on_redirect: bool,
    coincidence: Optional[CoincidenceConfig] = None,
    page_hashes: Optional[dict[str, str]] = None,
) -> tuple[list[Issue], list[Coincidence]]:
    """照合して (Issue, 偶然の一致の候補) を返す。

    page_hashes を渡すと、URL（末尾の / を除いたもの）→ 出典ページの本文のハッシュを書き込む。
    """
    if page_hashes is None:
        page_hashes = {}
    issues: list[Issue] = []
    coincidences: list[Coincidence] = []
    page_cache: dict[str, tuple[FetchResult, list[NumberToken], list[frozenset]]] = {}
    ctx_chars = coincidence.context_chars if coincidence else 30

    # URL 単位でまとめ、同一URLの重複取得を避ける（アクセス回数を最小化）
    by_url: dict[str, list[Claim]] = {}
    for claim in claims:
        by_url.setdefault(claim.url, []).append(claim)

    LOG.info("照合対象: %s クレーム / %s ユニークURL", len(claims), len(by_url))

    for i, (url, url_claims) in enumerate(by_url.items(), start=1):
        LOG.info("[%s/%s] 取得中: %s", i, len(by_url), url)
        if url in page_cache:
            result, page_numbers, page_quals = page_cache[url]
        else:
            result = fetcher.fetch(url)
            page_numbers, page_quals = [], []
            if result.ok:
                try:
                    text = html_to_text(result.text)
                except ToolError as exc:
                    LOG.error("本文抽出に失敗しました (%s): %s", url, exc)
                    text = ""
                if not text.strip():
                    for claim in url_claims:
                        issues.append(Issue(
                            kind="empty_page", source_file=claim.source_file,
                            line_no=claim.line_no, url=url, final_url=result.url,
                            detail="ページ本文を抽出できませんでした（JS描画ページの可能性）。手動確認が必要です",
                        ))
                else:
                    page_hashes[url.strip().rstrip("/")] = page_text_hash(text)
                    ms = iter_number_matches(text, context_chars=ctx_chars, skip_year_like=False)
                    page_numbers = [m[0] for m in ms]
                    page_quals = [qualifiers_at(m[3], m[1], m[2]) for m in ms]
                    LOG.debug("%s から %s 個の数値を抽出しました", url, len(page_numbers))
            page_cache[url] = (result, page_numbers, page_quals)

        for claim in url_claims:
            if not result.ok:
                issues.append(Issue(
                    kind="url_error", source_file=claim.source_file, line_no=claim.line_no,
                    url=url, final_url=result.url,
                    detail=f"出典URLを取得できません: {result.error}",
                ))
                continue

            if warn_on_redirect and result.redirected:
                chain = " → ".join(result.redirect_chain + [result.url])
                issues.append(Issue(
                    kind="url_redirect", source_file=claim.source_file, line_no=claim.line_no,
                    url=url, final_url=result.url,
                    detail=f"リダイレクトされています: {chain}（出典URLの差し替えを検討）",
                ))

            if not claim.numbers:
                LOG.debug("%s:%s 数値のないマーカーです（照合スキップ）",
                          claim.source_file.name, claim.line_no)
                continue

            for t_idx, token in enumerate(claim.numbers):
                hit = numbers_match(token, page_numbers, strict_units, rel_tolerance)
                if hit is None:
                    issues.append(Issue(
                        kind="number_mismatch", source_file=claim.source_file,
                        line_no=claim.line_no, url=url, final_url=result.url,
                        number_raw=token.display(), number_context=token.context,
                        detail="この数値が参照先ページ内に見つかりませんでした",
                        number_value=format_decimal(token.value), number_unit=token.unit or "",
                    ))
                    continue
                if not coincidence or not coincidence.enabled:
                    continue
                if t_idx in claim.per_unit_idx:
                    continue
                hits = [j for j, pt in enumerate(page_numbers)
                        if numbers_match(token, [pt], strict_units, rel_tolerance) is not None]
                reasons = []
                base = _coincidence_reason(token, coincidence)
                if base:
                    reasons.append(base)
                if token.unit is not None and all(page_numbers[j].unit is None for j in hits):
                    reasons.append("出典側の数値に単位がない（型番・電話番号などとの一致の可能性）")
                aq = claim.qualifiers[t_idx] if t_idx < len(claim.qualifiers) else frozenset()
                if not any(page_quals[j] == aq for j in hits):
                    pq = sorted({_fmt_quals(page_quals[j]) for j in hits})
                    reasons.append(f"修飾語の食い違い（記事: {_fmt_quals(aq)}／出典: {'、'.join(pq)}）")
                if not reasons:
                    continue
                contexts = [page_numbers[j].context for j in hits[:coincidence.max_contexts]]
                coincidences.append(Coincidence(
                    source_file=claim.source_file, line_no=claim.line_no, url=url,
                    token=token, page_contexts=contexts, reason="・".join(reasons),
                    page_hash=page_hashes.get(url.strip().rstrip("/"), ""),
                ))

    return issues, coincidences


def verify(
    claims: list[Claim],
    fetcher: PoliteFetcher,
    strict_units: bool,
    rel_tolerance: float,
    warn_on_redirect: bool,
) -> list[Issue]:
    """互換用。Issue だけを返す（偶然の一致の候補は返さない）。"""
    issues, _co = verify_claims(claims, fetcher, strict_units, rel_tolerance, warn_on_redirect, None)
    return issues


# --------------------------------------------------------------------------
# レポート
# --------------------------------------------------------------------------

KIND_LABEL = {
    "url_error": "出典URLが取得できない（404等）",
    "url_redirect": "出典URLがリダイレクトしている",
    "number_mismatch": "数値が参照先に見つからない",
    "empty_page": "参照先の本文を抽出できない",
    "undefined_footnote": "脚注の定義がない",
    "unclosed_block": "src-block が閉じられていない",
    "invalid_marker": "出典マーカーの書式が不正",
}


def render_report(issues: list[Issue], checked_files: list[Path], claim_count: int,
                  number_count: int, coincidences: Optional[list[Coincidence]] = None,
                  suppressed: Optional[list[Issue]] = None,
                  unused_records: Optional[list] = None,
                  record_warnings: Optional[list[str]] = None,
                  lint_results: Optional[list] = None,
                  hashes: Optional[dict[str, str]] = None,
                  fail_on_coincidence: bool = True) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    coincidences = coincidences or []
    suppressed = suppressed or []
    lint_results = lint_results or []
    lint_errors = sum(len(r.errors) for r in lint_results)
    lines = [
        "# ファクトチェック機械照合レポート",
        "",
        f"生成日時: {now}",
        f"対象ファイル: {len(checked_files)} 件 / 出典マーカー: {claim_count} 件 / 照合数値: {number_count} 個",
        f"**要確認: {len(issues)} 件** / 偶然の一致の候補: {len(coincidences)} 件"
        f"{'（終了コード1の対象）' if fail_on_coincidence and coincidences else ''}"
        f" / lint のエラー: {lint_errors} 件",
        f"**手動確認の記録で報告から外したもの: {len(suppressed)} 件**（下の一覧で、外してよい組かを確かめる）",
        "",
    ]
    if hashes:
        lines += ["合格した正本（受領ファイル data/checks/{slug}.json を書いた。ハッシュは合格のときだけ出す）:", ""]
        lines += [f"- {f}: `{h}`" for f, h in hashes.items()]
        lines.append("")
    extra = _render_extra(coincidences, suppressed, unused_records or [], record_warnings or [])
    extra += _render_lint(lint_results)
    blocking_co = bool(coincidences) and fail_on_coincidence
    if not issues:
        if lint_errors:
            lines += ["出典との照合で不一致はありませんが、**lint のエラーがあります**"
                      "（マーカーのない数値などは照合の対象にならないため、照合だけでは確かめられない）。", ""]
        elif blocking_co:
            lines += ["出典との照合で不一致はありませんが、**偶然の一致の候補があります**。文脈を確かめ、"
                      "確かめた組は data/sources/{slug}.md の手動確認の記録に書く。", ""]
        elif suppressed:
            lines += ["すべての数値が出典先で確認できました（**手動確認の記録によるものを含む**）。", ""]
        else:
            lines += ["すべての数値が出典先で確認できました。", ""]
        return "\n".join(lines + extra)

    lines += [
        "> 一致した数値は本レポートに出しません。以下は**すべて人間の確認が必要**です。",
        "",
        "## 種別ごとの件数",
        "",
        "| 種別 | 件数 |",
        "|---|---:|",
    ]
    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue.kind] = counts.get(issue.kind, 0) + 1
    for kind, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {KIND_LABEL.get(kind, kind)} | {count} |")
    lines.append("")

    by_file: dict[Path, list[Issue]] = {}
    for issue in issues:
        by_file.setdefault(issue.source_file, []).append(issue)

    for path, file_issues in sorted(by_file.items(), key=lambda kv: str(kv[0])):
        lines += [f"## {path}", ""]
        for issue in sorted(file_issues, key=lambda i: i.line_no):
            lines.append(f"### L{issue.line_no} — {KIND_LABEL.get(issue.kind, issue.kind)}")
            lines.append("")
            if issue.number_raw:
                lines.append(f"- 記事側の数値: **{issue.number_raw}**")
                lines.append(f"- 文脈: `…{issue.number_context}…`")
            if issue.url:
                lines.append(f"- 出典URL: {issue.url}")
            if issue.final_url and issue.final_url != issue.url:
                lines.append(f"- 最終URL: {issue.final_url}")
            lines.append(f"- 内容: {issue.detail}")
            lines.append("")

    lines += extra
    lines += [
        "---",
        "",
        "### 誤検知が多い場合",
        "",
        "- 参照先がJavaScriptで数値を描画している → `empty_page` として出る。手動確認して",
        "  `data/sources/{slug}.md` の「手動確認の記録」に書く（有効期限つき）",
        "- 記事側が丸めた数値を使っている（例: 公式1,980円 → 記事約2,000円）→ 記事側を実数に直すか、",
        "  `config.yaml` の `factcheck.rel_tolerance` を上げる（既定 0 = 完全一致）",
        "- 型番・電話番号・ID など照合不要の数値 → `factcheck.ignore_patterns` に正規表現を追加",
        "",
    ]
    return "\n".join(lines)


def _render_extra(coincidences: list[Coincidence], suppressed: list[Issue],
                  unused_records: list, record_warnings: list[str]) -> list[str]:
    out: list[str] = []
    if coincidences:
        out += [
            "## 偶然の一致の可能性（一致したが、文脈の確認が必要）",
            "",
            "> 0か一桁の数値、または単位のない数値は、出典ページのどこかに同じ数字があるだけで一致になる。",
            "> 出典ページ側の文脈が記事の主張と同じ意味かを確かめる（設計書 §8 T7）。",
            "> 確かめた組は data/sources/{slug}.md に種類「偶然の一致」・ページのハッシュつきで記録する"
            "（確認者 factcheck_session で書ける。出典ページの本文が変わるまで有効）。",
            "",
        ]
        for co in sorted(coincidences, key=lambda c: (str(c.source_file), c.line_no)):
            out.append(f"- {co.source_file}:L{co.line_no} **{co.token.raw}**（{co.reason}）"
                       f" 出典: {co.url}（ページのハッシュ `{co.page_hash or '取得できず'}`）")
            out.append(f"  - 記事側: `…{co.token.context}…`")
            for ctx in co.page_contexts:
                out.append(f"  - 出典側: `…{ctx}…`")
        out.append("")
    if suppressed:
        out += [
            f"## 手動確認の記録で報告から外したもの（{len(suppressed)} 件）",
            "",
        ]
        for issue in sorted(suppressed, key=lambda i: (str(i.source_file), i.line_no)):
            num = f" {issue.number_raw}" if issue.number_raw else ""
            out.append(f"- {issue.source_file}:L{issue.line_no} {KIND_LABEL.get(issue.kind, issue.kind)}"
                       f"{num} {issue.url} — {issue.detail}")
        out.append("")
    if unused_records:
        out += [
            "## 使われなかった手動確認の記録（記事側の数値が変わった可能性）",
            "",
        ]
        for rec in unused_records:
            out.append(f"- {rec.source_file.name}:L{rec.line_no} {rec.url} {rec.number_raw}"
                       f"（{rec.checked_on} {rec.checker}）")
        out.append("")
    if record_warnings:
        out += ["## 手動確認の記録の書式の警告", ""]
        out += [f"- {w}" for w in record_warnings]
        out.append("")
    return out


def _render_lint(lint_results: list) -> list[str]:
    if not lint_results:
        return []
    from article_lint import KIND_LABEL as LINT_LABEL
    out = ["## lint の結果（照合モードでも .md には lint を実行する）", ""]
    for r in lint_results:
        out.append(f"- {r.path}: {'不合格' if r.errors else '合格'}（エラー {len(r.errors)} / 警告 {len(r.warnings)}）")
        for i in sorted(r.errors, key=lambda x: x.line_no):
            num = f" `{i.number}`" if i.number else ""
            out.append(f"  - L{i.line_no} [{LINT_LABEL.get(i.kind, i.kind)}]{num} — {i.detail}")
    out.append("")
    return out


# --------------------------------------------------------------------------
# エントリポイント
# --------------------------------------------------------------------------

def collect_files(inputs: list[str], recursive: bool) -> list[Path]:
    exts = {".md", ".markdown", ".html", ".htm"}
    files: list[Path] = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            pattern = "**/*" if recursive else "*"
            files += [f for f in sorted(p.glob(pattern)) if f.suffix.lower() in exts]
        elif p.exists():
            files.append(p)
        else:
            matched = sorted(Path().glob(item))
            if not matched:
                LOG.warning("入力が見つかりませんでした: %s", item)
            files += [f for f in matched if f.suffix.lower() in exts]
    return files


def article_slug(path: Path) -> str:
    """front matter の slug（なければファイル名）。手動確認の記録のファイル名に使う。

    slug は検証する（英小文字・数字・ハイフンだけ。`../` などでほかの場所のファイルを読ませない）。
    不正なら ToolError。
    """
    slug = path.stem
    if path.suffix.lower() in (".md", ".markdown"):
        fm, _n, _err = split_front_matter(split_lines(_read_article_text(path)))
        if fm and fm.get("slug"):
            slug = fm["slug"]
    problem = validate_slug(slug)
    if problem:
        raise ToolError(f"{path}: {problem}")
    return str(slug)


def load_records_for(files: list[Path], sources_dir: Path, allowed_checkers=(),
                     denied_checkers=None, session_checkers=None) -> tuple[dict[str, list], list[str]]:
    from manual_checks import (DEFAULT_DENIED_CHECKERS, DEFAULT_SESSION_CHECKERS, parse_manual_records,
                               sources_file_for)

    denied = DEFAULT_DENIED_CHECKERS if denied_checkers is None else denied_checkers
    session = DEFAULT_SESSION_CHECKERS if session_checkers is None else session_checkers
    records_by_file: dict[str, list] = {}
    warnings: list[str] = []
    for path in files:
        try:
            slug = article_slug(path)
        except ToolError as exc:
            warnings.append(f"{exc}。手動確認の記録を読みません")
            continue
        src = sources_file_for(sources_dir, slug)
        if not src.exists():
            continue
        recs, warns = parse_manual_records(src, allowed_checkers, denied, session)
        records_by_file[str(path)] = recs
        warnings += warns
        LOG.info("手動確認の記録を %s 件読み込みました: %s", len(recs), src)
    return records_by_file, warnings


def parse_today(value: Optional[str]) -> date:
    if not value:
        return date.today()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ToolError(f"--today は YYYY-MM-DD で指定してください: {value}") from exc


DEFAULT_CHECKS_DIR = "../data/checks"
DEFAULT_TERMS_FILE = "../data/terms.yaml"
RECEIPT_TOOL = "factcheck.py"


def receipt_path(checks_dir: Path, slug: str) -> Path:
    return checks_dir / f"{slug}.json"


def write_receipt(checks_dir: Path, slug: str, content_hash_value: str, report_text: str,
                  stats: dict[str, Any]) -> Path:
    """照合の合格の受領ファイル（data/checks/{slug}.json）を書く。publish_prep.py が読む（設計書 §8 T10）。

    形式（README §11.4）:
      {"slug", "result": "pass", "mode": "gate", "tool": "factcheck.py", "content_hash",
       "checked_at", "report_sha256", "claims", "numbers", "manual_suppressed", "coincidences_suppressed",
       "lint_warnings"}
    """
    import hashlib
    problem = validate_slug(slug)
    if problem:
        raise ToolError(problem)
    data = {
        "slug": slug,
        "result": "pass",
        "mode": "gate",
        "tool": RECEIPT_TOOL,
        "content_hash": content_hash_value,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "report_sha256": hashlib.sha256(report_text.encode("utf-8")).hexdigest(),
        **stats,
    }
    path = receipt_path(checks_dir, slug)
    write_json(path, data)
    return path


def read_receipt(checks_dir: Path, slug: str) -> Optional[dict[str, Any]]:
    import json
    path = receipt_path(checks_dir, slug)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError(f"受領ファイルを読めません: {path}: {exc}") from exc
    return data if isinstance(data, dict) else None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="記事内の数値と出典URLを機械照合し、不一致のみを報告する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("inputs", nargs="+", help="記事ファイルまたはディレクトリ")
    parser.add_argument("--lint", action="store_true",
                        help="出典URLを取得せず、記事の書き方だけを検査する（設計書 §8 T1）")
    parser.add_argument("-r", "--recursive", action="store_true", help="ディレクトリを再帰探索")
    parser.add_argument("-c", "--config", help="設定ファイル（既定: tools/config.yaml）")
    parser.add_argument("-o", "--out", help="Markdownレポートの出力先")
    parser.add_argument("--json", dest="json_out", help="JSONレポートの出力先")
    parser.add_argument("--strict-units", action="store_true",
                        help="単位が取れない数値を一致とみなさない（誤検知は減るが見逃しも減る）")
    parser.add_argument("--no-skip-years", action="store_true",
                        help="西暦らしき4桁も照合対象にする")
    parser.add_argument("--no-redirect-warning", action="store_true",
                        help="リダイレクトを警告しない")
    parser.add_argument("--ignore-robots", action="store_true",
                        help="robots.txt を無視する（自社サイト等の正当な理由がある場合のみ）")
    parser.add_argument("--sources-dir",
                        help="手動確認の記録のディレクトリ（既定: config の paths.sources_dir、なければ data/sources）")
    parser.add_argument("--checks-dir",
                        help="受領ファイルのディレクトリ（既定: config の paths.checks_dir、なければ data/checks）")
    parser.add_argument("--no-manual-records", action="store_true",
                        help="手動確認の記録を使わない（全件を報告する）")
    parser.add_argument("--mode", choices=("gate", "weekly"),
                        help="gate（既定）= 公開前の関門。偶然の一致の候補があれば終了コード1、合格した記事の"
                             "受領ファイルを書く。weekly = 週次の全記事の照合。偶然の一致の候補は報告だけ、受領ファイルは書かない")
    parser.add_argument("--no-fail-on-coincidence", action="store_true",
                        help="--mode weekly と同じ（互換のため残している）")
    parser.add_argument("--today", help="有効期限の判定に使う日付（YYYY-MM-DD。既定: 今日）")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    try:
        load_env()
        cfg = load_config(args.config)

        files = collect_files(args.inputs, args.recursive)
        if not files:
            raise ToolError("照合対象のファイルが1件も見つかりませんでした（.md / .html）")
        LOG.info("対象ファイル: %s 件", len(files))

        if args.lint:
            from article_lint import run_lint
            return run_lint(files, cfg, out=args.out, json_out=args.json_out,
                            skip_years=not args.no_skip_years)

        mode = args.mode or ("weekly" if args.no_fail_on_coincidence else "gate")
        fail_co = mode == "gate"
        ignore_patterns = cfg_get(cfg, "factcheck.ignore_patterns", []) or []
        skip_years = not args.no_skip_years
        strict_units = args.strict_units or bool(cfg_get(cfg, "factcheck.strict_units", False))
        rel_tolerance = float(cfg_get(cfg, "factcheck.rel_tolerance", 0.0))
        warn_redirect = not args.no_redirect_warning and bool(
            cfg_get(cfg, "factcheck.warn_on_redirect", True)
        )
        today = parse_today(args.today)
        valid_days = int(cfg_get(cfg, "factcheck.manual_check_valid_days", 90))
        coincidence = CoincidenceConfig.from_cfg(cfg)
        from article import load_terms
        terms = load_terms(expand_path(cfg_get(cfg, "paths.terms_file", DEFAULT_TERMS_FILE)))
        checks_dir = (Path(args.checks_dir) if args.checks_dir
                      else expand_path(cfg_get(cfg, "paths.checks_dir", DEFAULT_CHECKS_DIR)))

        all_claims: list[Claim] = []
        all_issues: list[Issue] = []
        for path in files:
            try:
                claims, issues = parse_article(path, ignore_patterns, skip_years, terms)
            except ToolError as exc:
                LOG.error("%s のパースに失敗しました: %s", path, exc)
                continue
            if not claims:
                LOG.warning("%s に出典マーカーが1つもありません。"
                            "CLAUDE.md ルール2（全数値に出典URL）を満たしていない可能性があります。", path)
            all_claims += claims
            all_issues += issues

        number_count = sum(len(c.numbers) for c in all_claims)
        fetcher = build_fetcher(cfg, ignore_robots=args.ignore_robots)
        page_hashes: dict[str, str] = {}
        v_issues, coincidences = verify_claims(all_claims, fetcher, strict_units, rel_tolerance,
                                               warn_redirect, coincidence, page_hashes)
        all_issues += v_issues

        suppressed: list[Issue] = []
        supp_co: list[Coincidence] = []
        unused: list = []
        record_warnings: list[str] = []
        if not args.no_manual_records:
            from manual_checks import apply_manual_records
            # コマンドラインのパスは実行したディレクトリから、config のパスは tools/ から見た位置
            sources_dir = (Path(args.sources_dir) if args.sources_dir
                           else expand_path(cfg_get(cfg, "paths.sources_dir", DEFAULT_SOURCES_DIR)))
            records_by_file, record_warnings = load_records_for(
                files, sources_dir,
                allowed_checkers=cfg_get(cfg, "factcheck.manual_check_allowed_checkers", []) or [],
                denied_checkers=cfg_get(cfg, "factcheck.manual_check_denied_checkers", None),
                session_checkers=cfg_get(cfg, "factcheck.manual_check_session_checkers", None))
            outcome = apply_manual_records(all_issues, all_claims, coincidences, records_by_file,
                                           today, valid_days, record_warnings, page_hashes)
            all_issues, suppressed = outcome.kept, outcome.suppressed
            coincidences, supp_co, unused = (outcome.kept_coincidences, outcome.suppressed_coincidences,
                                             outcome.unused_records)

        # 正本（.md）には lint も実行する。マーカーのない数値は照合の対象にならないため
        from article import content_hash, load_article
        from article_lint import LintConfig, lint_article
        lc = LintConfig.from_cfg(cfg, skip_years=skip_years)
        lint_by_file: dict[str, Any] = {}
        for path in files:
            if path.suffix.lower() in (".md", ".markdown"):
                lint_by_file[str(path)] = lint_article(path, lc)
        lint_results = list(lint_by_file.values())
        lint_errors = sum(len(r.errors) for r in lint_results)

        # 記事ごとの合否（照合の不一致・lint のエラー・（gate では）偶然の一致の候補がないこと）
        passed: dict[str, str] = {}
        for path in files:
            key = str(path)
            if key not in lint_by_file or lint_by_file[key].errors:
                continue
            if any(str(i.source_file) == key for i in all_issues):
                continue
            if fail_co and any(str(c.source_file) == key for c in coincidences):
                continue
            try:
                passed[key] = content_hash(load_article(path))
            except ToolError:
                pass
        hashes = passed if mode == "gate" else {}

        report = render_report(all_issues, files, len(all_claims), number_count,
                               coincidences, suppressed, unused, record_warnings,
                               lint_results=lint_results, hashes=hashes, fail_on_coincidence=fail_co)
        report = report.replace("# ファクトチェック機械照合レポート\n",
                                f"# ファクトチェック機械照合レポート（{'公開前の関門' if mode == 'gate' else '週次の照合'}）\n", 1)
        out_path = Path(args.out) if args.out else None
        if out_path:
            write_text(out_path, report)
        else:
            sys.stdout.write(report)

        receipts: list[str] = []
        if mode == "gate":
            for key, h in passed.items():
                path = Path(key)
                slug = article_slug(path)
                stats = {
                    "claims": sum(1 for c in all_claims if str(c.source_file) == key),
                    "numbers": sum(len(c.numbers) for c in all_claims if str(c.source_file) == key),
                    "manual_suppressed": sum(1 for i in suppressed if str(i.source_file) == key),
                    "coincidences_suppressed": sum(1 for c in supp_co if str(c.source_file) == key),
                    "lint_warnings": len(lint_by_file[key].warnings),
                    "report_path": str(out_path) if out_path else "",
                }
                receipts.append(str(write_receipt(checks_dir, slug, h, report, stats)))

        if args.json_out:
            write_json(Path(args.json_out), {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "mode": mode,
                "files": [str(f) for f in files],
                "claims": len(all_claims),
                "numbers": number_count,
                "issues": [dataclasses.asdict(i) for i in all_issues],
                "coincidences": [
                    {"file": str(c.source_file), "line": c.line_no, "url": c.url,
                     "number": c.token.raw, "reason": c.reason, "page_contexts": c.page_contexts,
                     "page_hash": c.page_hash}
                    for c in coincidences
                ],
                "manual_suppressed": [dataclasses.asdict(i) for i in suppressed],
                "lint_errors": lint_errors,
                "passed": hashes,
                "receipts": receipts,
                "manual_unused": [
                    {"file": str(r.source_file), "line": r.line_no, "url": r.url,
                     "number": r.number_raw, "checked_on": str(r.checked_on)}
                    for r in unused
                ],
            })

        if all_issues:
            LOG.warning("要確認 %s 件です。レポートを確認してください。", len(all_issues))
            return 1
        if lint_errors:
            LOG.warning("lint のエラーが %s 件あります。レポートを確認してください。", lint_errors)
            return 1
        if coincidences and fail_co:
            LOG.warning("偶然の一致の候補が %s 件あります。文脈を確かめ、確かめた組は手動確認の記録に書いてください。",
                        len(coincidences))
            return 1
        if coincidences:
            LOG.info("偶然の一致の候補が %s 件あります（週次の照合のため報告だけ）。", len(coincidences))
        LOG.info("問題は見つかりませんでした。")
        return 0

    except ToolError as exc:
        LOG.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        LOG.error("中断されました")
        return 2
    except Exception:
        LOG.exception("想定外のエラーで停止しました")
        return 2


if __name__ == "__main__":
    sys.exit(main())
