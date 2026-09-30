"""
article_lint.py — `factcheck.py --lint` の本体（設計書 §8 T1）。

出典URLは取得しない。正本（articles/{slug}.md）の書き方だけを検査する。
行の役割（表・見出し・リスト・コードブロック）は markdown-it のトークンで判定する（article.py）。
報告の一覧と規則は tools/README.md §3.6。severity=error が1件でもあれば終了コード1。
"""

from __future__ import annotations

import dataclasses
import logging
import re
import sys
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from common import (
    NumberToken, ToolError, cfg_get, expand_path, format_decimal, iter_number_matches, normalize_text,
    write_json, write_text,
)
from article import (
    ACCESSED_ANY_RE,
    ACCESSED_RE,
    ANY_COMMENT_RE,
    BARE_URL_RE,
    BLOCK_END_RE,
    BLOCK_START_RE,
    CALC_END_RE,
    CALC_START_RE,
    CODE_SPAN_RE,
    CTA_RE,
    DEFAULT_BANNED_EXPRESSIONS,
    DEFAULT_CAUTION_EXPRESSIONS,
    DEFAULT_COMPARISON_EXPRESSIONS,
    DEFAULT_KANJI_COUNTERS,
    DEFAULT_KANJI_EXCLUSIONS,
    FOOTNOTE_ANY_DEF_RE,
    FOOTNOTE_REF_RE,
    HTML_TABLE_RE,
    INLINE_SRC_RE,
    MARKER_COMMENT_RE,
    NOFACT_RE,
    PART_END_RE,
    PART_START_RE,
    PER_UNIT_SUFFIX_DEFAULT,
    PLACEHOLDER_RE,
    Article,
    Heading,
    checkable_numbers,
    child_headings,
    find_expressions,
    find_invisible,
    find_kanji_hits,
    host_matches,
    image_alts,
    is_per_unit_one,
    load_article,
    load_asp_programs,
    load_terms,
    mask_terms,
    part_ranges,
    related_range,
    remove_comments,
    section_range,
    split_cells,
    split_marker_body,
    to_date,
    validate_slug,
)

LOG = logging.getLogger("affiliate.tools.lint")

DEFAULT_ENUMERATION_PATTERNS = [
    r"(?P<n>\d+)\s*つの\s*(?P<noun>[一-龥ァ-ヶーA-Za-z]+)",
    r"(?P<n>\d+)\s*つ",
    r"(?P<n>\d+)\s*選",
    r"(?P<n>\d+)\s*(?:項目|ステップ|問|か条|カ条|ヶ条)",
    r"(?P<n>\d+)\s*社(?:を|の)?(?:徹底)?比較",
]
# 「ポイント1：」「ポイント2」（行末）のように、番号の後ろに単位がなく区切りが続くものだけを順番とみなす
# （「ポイント1,000円分」「最大ポイント10倍」は順番ではない）
DEFAULT_HEADING_ORDINAL_PATTERNS = [r"ポイント\s*(?P<n>\d+)(?![\d,.]*\d)(?=\s*(?:[:：.．、(（)）」]|$))"]
DEFAULT_NOFACT_PREFIXES = ["試算の条件：", "計算式："]
# 「試算の条件：」の行に書いてよい単位（人数・期間・回数だけ）
DEFAULT_CONDITION_UNITS = ["人", "名", "か月", "ヶ月", "カ月", "月", "回", "年", "日", "時間"]
# 試算表（calc-block）のデータのセルに書いてよい単位
DEFAULT_CALC_CELL_UNITS = ["円", "人", "名", "件", "%", "時間", "日", "か月", "ヶ月", "カ月", "年", "回", "社", "倍"]
DEFAULT_PARTS = ["trace", "checklist", "faq"]
DEFAULT_REQUIRED_PARTS_ALL = ["related"]
RELATED = "related"
FORMULA_PREFIX = "計算式:"
CONDITION_PREFIX = "試算の条件:"
QUESTION_RE = re.compile(r"^(?:Q|Ｑ)\s*(?:[.．:：]|\d)")
RELATED_LINE_RE = re.compile(
    r"^\s*[-*+]\s+(?:(?P<ind>[^\[\]：:]+?)\s*[：:]\s*)?\[(?P<text>[^\]]+)\]\((?P<url>[^)\s]+)\)\s*$")


@dataclasses.dataclass
class LintConfig:
    ignore_patterns: list[str]
    skip_years: bool = True
    drop_per_unit_one: bool = True
    per_unit_suffixes: tuple[str, ...] = PER_UNIT_SUFFIX_DEFAULT
    enumeration_patterns: list[str] = dataclasses.field(
        default_factory=lambda: list(DEFAULT_ENUMERATION_PATTERNS))
    heading_ordinal_patterns: list[str] = dataclasses.field(
        default_factory=lambda: list(DEFAULT_HEADING_ORDINAL_PATTERNS))
    nofact_line_prefixes: list[str] = dataclasses.field(
        default_factory=lambda: list(DEFAULT_NOFACT_PREFIXES))
    calc_basis_max_note_lines: int = 2
    kanji_counters: list[str] = dataclasses.field(default_factory=lambda: list(DEFAULT_KANJI_COUNTERS))
    kanji_exclusions: list[str] = dataclasses.field(default_factory=lambda: list(DEFAULT_KANJI_EXCLUSIONS))
    banned_expressions: list[str] = dataclasses.field(default_factory=lambda: list(DEFAULT_BANNED_EXPRESSIONS))
    caution_expressions: list[str] = dataclasses.field(default_factory=lambda: list(DEFAULT_CAUTION_EXPRESSIONS))
    comparison_expressions: list[str] = dataclasses.field(
        default_factory=lambda: list(DEFAULT_COMPARISON_EXPRESSIONS))
    comparison_condition_word: str = "条件"
    parts: list[str] = dataclasses.field(default_factory=lambda: list(DEFAULT_PARTS))
    trace_min_rows: int = 3
    recommend_heading_patterns: list[str] = dataclasses.field(default_factory=lambda: ["おすすめ"])
    max_recommendations: int = 2
    asp_programs_file: Optional[Path] = None
    terms: list[str] = dataclasses.field(default_factory=list)
    nofact_condition_units: list[str] = dataclasses.field(default_factory=lambda: list(DEFAULT_CONDITION_UNITS))
    calc_cell_units: list[str] = dataclasses.field(default_factory=lambda: list(DEFAULT_CALC_CELL_UNITS))
    required_parts_all: list[str] = dataclasses.field(default_factory=lambda: list(DEFAULT_REQUIRED_PARTS_ALL))
    faq_min_questions: int = 3
    checklist_min_items: int = 2
    site_domains: list[str] = dataclasses.field(default_factory=list)
    affiliate_domains: list[str] = dataclasses.field(default_factory=list)
    industries_file: Optional[Path] = None
    today: date = dataclasses.field(default_factory=date.today)

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any], skip_years: bool = True) -> "LintConfig":
        node = cfg_get(cfg, "factcheck.lint", {}) or {}
        asp = cfg_get(cfg, "paths.asp_programs_file", "../data/asp-programs.csv")
        terms_file = cfg_get(cfg, "paths.terms_file", "../data/terms.yaml")
        ind = cfg_get(cfg, "similarity.industries_file", "../data/industries.csv")
        return cls(
            terms=load_terms(expand_path(terms_file)) if terms_file else [],
            nofact_condition_units=list(node.get("nofact_condition_units", DEFAULT_CONDITION_UNITS)),
            calc_cell_units=list(node.get("calc_cell_units", DEFAULT_CALC_CELL_UNITS)),
            required_parts_all=list(node.get("required_parts_all", DEFAULT_REQUIRED_PARTS_ALL)),
            faq_min_questions=int(node.get("faq_min_questions", 3)),
            checklist_min_items=int(node.get("checklist_min_items", 2)),
            site_domains=list(cfg_get(cfg, "site_domains", []) or []),
            affiliate_domains=list(cfg_get(cfg, "affiliate_domains", []) or []),
            industries_file=expand_path(ind) if ind else None,
            ignore_patterns=list(cfg_get(cfg, "factcheck.ignore_patterns", []) or []),
            skip_years=skip_years,
            drop_per_unit_one=bool(node.get("drop_per_unit_one", True)),
            per_unit_suffixes=tuple(node.get("per_unit_suffixes", PER_UNIT_SUFFIX_DEFAULT)),
            enumeration_patterns=list(node.get("enumeration_patterns", DEFAULT_ENUMERATION_PATTERNS)),
            heading_ordinal_patterns=list(node.get("heading_ordinal_patterns", DEFAULT_HEADING_ORDINAL_PATTERNS)),
            nofact_line_prefixes=list(node.get("nofact_line_prefixes", DEFAULT_NOFACT_PREFIXES)),
            calc_basis_max_note_lines=int(node.get("calc_basis_max_note_lines", 2)),
            kanji_counters=list(node.get("kanji_counters", DEFAULT_KANJI_COUNTERS)),
            kanji_exclusions=list(node.get("kanji_exclusions", DEFAULT_KANJI_EXCLUSIONS)),
            banned_expressions=list(node.get("banned_expressions", DEFAULT_BANNED_EXPRESSIONS)),
            caution_expressions=list(node.get("caution_expressions", DEFAULT_CAUTION_EXPRESSIONS)),
            comparison_expressions=list(node.get("comparison_expressions", DEFAULT_COMPARISON_EXPRESSIONS)),
            comparison_condition_word=str(node.get("comparison_condition_word", "条件")),
            parts=list(node.get("parts", DEFAULT_PARTS)),
            trace_min_rows=int(node.get("trace_min_rows", 3)),
            recommend_heading_patterns=list(node.get("recommend_heading_patterns", ["おすすめ"])),
            max_recommendations=int(node.get("max_recommendations", 2)),
            asp_programs_file=expand_path(asp) if asp else None,
        )


@dataclasses.dataclass
class LintIssue:
    severity: str          # error / warning
    kind: str
    line_no: int
    detail: str
    number: str = ""


@dataclasses.dataclass
class LintResult:
    path: Path
    issues: list[LintIssue] = dataclasses.field(default_factory=list)
    inline_markers: int = 0
    block_markers: int = 0
    nofact_lines: list[int] = dataclasses.field(default_factory=list)
    calc_blocks: list[tuple[int, int]] = dataclasses.field(default_factory=list)
    content_hash: str = ""

    @property
    def marker_count(self) -> int:
        return self.inline_markers + self.block_markers

    @property
    def errors(self) -> list[LintIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[LintIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def kinds(self, severity: Optional[str] = None) -> list[str]:
        return [i.kind for i in self.issues if severity is None or i.severity == severity]


KIND_LABEL = {
    "unmarked_number": "マーカーのない行の数値",
    "after_last_marker": "最後のマーカーより後ろの数値",
    "accessed_missing": "accessed= の欠落",
    "accessed_invalid": "accessed= の書式の誤り",
    "accessed_future": "accessed= が未来の日付",
    "no_markers": "出典マーカーが0件",
    "invalid_marker": "マーカーのURLが不正",
    "calc_block_no_basis": "calc-block の直前に入力値の表・計算式がない",
    "calc_block_non_table": "calc-block の中の表以外の行",
    "calc_row_unknown": "試算表の行が入力値の表にない",
    "nested_block": "calc-block・src-block・part の入れ子",
    "unclosed_block": "ブロックが閉じられていない",
    "stray_block_end": "開始のないブロックの終了タグ",
    "block_marker_not_alone": "ブロックマーカーが単独の行にない",
    "footnote_used": "脚注記法（禁止）",
    "src_in_calc_block": "calc-block の中の出典マーカー",
    "inline_in_src_block": "src-block の中の行内マーカー",
    "url_in_src_block": "src-block の中のURL",
    "note_in_src_block": "src-block の中の表以外の行",
    "nofact_with_src": "nofact と出典マーカーが同じ行",
    "nofact_in_table": "表の行の nofact",
    "nofact_in_src_block": "src-block の中の nofact",
    "nofact_not_at_end": "nofact が行末にない",
    "nofact_heading_not_enum": "見出しの nofact に数え上げ以外の数値",
    "nofact_line_not_allowed": "nofact を置けない行",
    "formula_unit_number": "計算式の行の単位付きの数値",
    "multi_sentence": "1行に複数の文（前の文の数値も同じURLで照合される）",
    "unknown_comment": "マーカー以外のHTMLコメント",
    "broken_comment": "閉じていないHTMLコメント",
    "placeholder": "{{ }} の残り",
    "kanji_number": "漢数字の数値",
    "inline_code_number": "インラインコードの中の数値",
    "code_block": "本文のコードブロック",
    "unclosed_fence": "閉じていないコードブロック",
    "html_table": "HTMLの表",
    "table_row_without_pipe": "表の行として扱われる注記",
    "banned_expression": "禁止表現（§1-6）",
    "caution_expression": "注意する表現（§1-6）",
    "front_matter_missing": "front matter がない",
    "front_matter_invalid": "front matter を読めない",
    "front_matter_field": "front matter の項目",
    "encoding": "文字コード",
    "slug_invalid": "slug が不正",
    "title_missing": "title / description がない",
    "title_enum_mismatch": "title・description の数え上げが本文の見出しにない",
    "title_enum_count": "title・description の数え上げの数と項目の数が違う",
    "title_enum_uncountable": "数え上げの項目の数を数えられない",
    "title_fact_unsourced": "title・description の事実の数値が本文の出典つき数値にない",
    "too_many_recommendations": "型Bの推薦が3つ以上",
    "part_missing": "型Bの部分の目印がない",
    "part_invalid": "部分の目印の誤り",
    "trace_table_rows": "要件トレース表の行数・出典",
    "program_unknown": "front matter の programs が案件の一覧にない",
    "html_article": "正本が .md でない",
    "kanji_number_one": "「一」＋助数詞の漢数字",
    "invisible_char": "見えない文字",
    "image_alt_number": "画像の alt の数値",
    "history_note": "history の note",
    "condition_unit": "試算の条件の行の単位",
    "calc_header_number": "試算表の見出し行の数値",
    "calc_cell_format": "試算表のセルの形",
    "part_not_whole": "部分の目印が区間の全体を囲んでいない",
    "part_too_small": "FAQ の質問・チェックリストの項目が少ない",
    "related_invalid": "関連記事の区間の書き方",
}


def _is_marker_comment(c: str) -> bool:
    return bool(MARKER_COMMENT_RE.match(c))


def _line_head(text: str) -> str:
    """行頭の記号（リスト・引用・強調）を落とし、NFKC にした行（書き出しの判定用）。"""
    t = unicodedata.normalize("NFKC", remove_comments(text))
    t = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+|>\s*)*", "", t)
    t = re.sub(r"[*_]", "", t)
    return t.strip()


class _Linter:
    def __init__(self, art: Article, lc: LintConfig) -> None:
        self.art = art
        self.lc = lc
        self.res = LintResult(path=art.path)
        self._enum_rx = [re.compile(p) for p in lc.enumeration_patterns]
        self._ord_rx = [re.compile(p) for p in lc.heading_ordinal_patterns]
        self._prefixes = [unicodedata.normalize("NFKC", p) for p in lc.nofact_line_prefixes]

    # -- helpers ------------------------------------------------------------

    def add(self, severity: str, kind: str, line_no: int, detail: str, number: str = "") -> None:
        self.res.issues.append(LintIssue(severity, kind, line_no, detail, number))

    def nums(self, text: str, strip_list: bool) -> list[NumberToken]:
        return checkable_numbers(
            text, self.lc.ignore_patterns, self.lc.skip_years,
            strip_list=strip_list, drop_per_unit_one=self.lc.drop_per_unit_one,
            per_unit_suffixes=self.lc.per_unit_suffixes, terms=self.lc.terms,
        )

    def is_list_first(self, line_no: int) -> bool:
        return (line_no - 1) in self.art.struct.ordered_item_first_lines

    def check_marker_body(self, body: str, line_no: int, kind: str) -> None:
        url, _acc = split_marker_body(body)
        if url is None:
            self.add("error", "invalid_marker", line_no,
                     f"{kind} マーカーのURLが http/https ではありません: {body.strip()[:80]}")
        m_any = ACCESSED_ANY_RE.search(body)
        if not m_any:
            self.add("error", "accessed_missing", line_no, f"{kind} マーカーに accessed= がありません（§1-2 制約6）")
            return
        m = ACCESSED_RE.search(body)
        if not m:
            self.add("error", "accessed_invalid", line_no,
                     f"accessed= が YYYY-MM-DD ではありません: accessed={m_any.group('value')}")
            return
        try:
            d = datetime.strptime(m.group("date"), "%Y-%m-%d").date()
        except ValueError:
            self.add("error", "accessed_invalid", line_no, f"accessed= の日付が不正です: {m.group('date')}")
            return
        if d > self.lc.today:
            self.add("warning", "accessed_future", line_no, f"accessed={d} が未来の日付です")

    def check_text_rules(self, text: str, line_no: int, where: str = "") -> None:
        """漢数字・禁止表現・見えない文字・画像の alt（本文・title・description・history 共通）。"""
        terms = self.lc.terms
        for h in find_kanji_hits(text, self.lc.kanji_counters, self.lc.kanji_exclusions, terms,
                                 self.lc.per_unit_suffixes):
            if h.severity == "error":
                self.add("error", "kanji_number", line_no,
                         f"{where}数値は算用数字＋桁区切りで書く（§1-2 数値の書き方。漢数字や「1万5,000円」は照合できない）",
                         h.text)
            else:
                self.add("warning", "kanji_number_one", line_no,
                         f"{where}「一」＋助数詞。事実の数なら算用数字で書き出典を付ける。決まった言い回しなら "
                         f"data/terms.yaml に登録する", h.text)
        for e in find_expressions(text, self.lc.banned_expressions, terms):
            self.add("error", "banned_expression", line_no,
                     f"{where}禁止表現です（§1-6 根拠のない最上級・体験の偽装。config の banned_expressions）", e)
        for e in find_expressions(text, self.lc.caution_expressions, terms):
            self.add("warning", "caution_expression", line_no, f"{where}断定・誇大表現になっていないか確かめる（§1-6）", e)
        cond = self.lc.comparison_condition_word
        for e in find_expressions(text, self.lc.comparison_expressions, terms):
            if cond not in text:
                self.add("warning", "caution_expression", line_no,
                         f"{where}比較優位の表現は、比較の条件を同じ文に書く（§1-6）", e)
        inv = find_invisible(text)
        if inv:
            self.add("error", "invisible_char", line_no,
                     f"{where}見えない文字（{', '.join(inv)}）があります。数値の間に入ると照合が分かれるので消す"
                     f"（コピー＆ペーストで混入しやすい）", ", ".join(inv))
        for alt in image_alts(text):
            for tok in self.nums(alt, False):
                self.add("error", "image_alt_number", line_no,
                         f"{where}画像の alt に数値を書かない（出典の照合を通らないため）", tok.raw)

    # -- main pass ----------------------------------------------------------

    def run(self) -> LintResult:
        art = self.art
        st = art.struct
        if art.encoding_note:
            self.add("error" if art.encoding_fatal else "warning", "encoding", 1, art.encoding_note)
        if art.front_matter_error:
            self.add("error", "front_matter_invalid", 1, art.front_matter_error)
        elif art.front_matter is None:
            self.add("error", "front_matter_missing", 1, "正本の先頭に front matter（YAML）がありません（§1-1）")
        else:
            self.check_front_matter_fields()

        for cb in st.code_blocks:
            self.add("error", "code_block", cb.start + 1,
                     "本文にコードブロックを書かない（比較記事には不要。中の数値が照合・lint の対象から外れるため）")
            if not cb.closed:
                self.add("error", "unclosed_fence", cb.start + 1,
                         "コードブロックが閉じられていません（以降の本文と P7 がすべてコードブロックになる）")

        heading_by_line = {h.line_no: h for h in st.headings}
        src_open: Optional[int] = None
        calc_open: Optional[int] = None
        calc_inputs: Optional[set[str]] = None
        part_open: Optional[tuple[str, int]] = None
        parts_seen: dict[str, int] = {}

        for no, line in art.body_lines():
            no_comments = remove_comments(line)
            is_row = art.is_table_row(no)
            heading = heading_by_line.get(no)
            if "<!--" in no_comments or ("-->" in no_comments and "<!--" not in line):
                self.add("error", "broken_comment", no, "HTMLコメント（マーカー）が同じ行で閉じていません")
            for c in ANY_COMMENT_RE.findall(line):
                if not _is_marker_comment(c):
                    self.add("warning", "unknown_comment", no,
                             f"マーカー以外のHTMLコメントです（綴りの誤りなら照合されません）: {c[:60]}")
            if FOOTNOTE_REF_RE.search(no_comments) or FOOTNOTE_ANY_DEF_RE.match(line):
                self.add("error", "footnote_used", no,
                         "脚注記法 [^x] は使わない（§1-2）。行内マーカーかブロックマーカーに書き換える")
            for m in PLACEHOLDER_RE.finditer(no_comments):
                if not CTA_RE.fullmatch(m.group(0)):
                    self.add("warning", "placeholder", no, f"置き換えていない {m.group(0)} があります")
            if HTML_TABLE_RE.search(no_comments):
                self.add("error", "html_table", no,
                         "HTMLの表は使わない。Markdown の表（区切り行つき）にする（表の行の規則を確かめられないため）")
            if is_row and art.table_role(no) == "data" and "|" not in no_comments:
                self.add("error", "table_row_without_pipe", no,
                         "表の直後の行が表の行として扱われます（GFM の仕様）。表の後に空行を入れる")
            for m in CODE_SPAN_RE.finditer(no_comments):
                if re.search(r"[0-9０-９〇一二三四五六七八九十百千万億]", m.group("body")):
                    self.add("error", "inline_code_number", no,
                             "インラインコード（`…`）の中に数値を書かない（照合の対象から外れやすい）", m.group(0)[:40])
            self.check_text_rules(line, no)

            calc_s, calc_e = CALC_START_RE.search(line), CALC_END_RE.search(line)
            blk_s, blk_e = BLOCK_START_RE.search(line), BLOCK_END_RE.search(line)
            prt_s, prt_e = PART_START_RE.search(line), PART_END_RE.search(line)
            if calc_s or calc_e or blk_s or blk_e or prt_s or prt_e:
                if ANY_COMMENT_RE.sub("", line).strip() or len(ANY_COMMENT_RE.findall(line)) > 1:
                    self.add("error", "block_marker_not_alone", no,
                             "src-block / calc-block / part のマーカーは単独の行に書く（同じ行の本文は照合されない）")

            # ---- part ------------------------------------------------------
            if prt_s:
                name = prt_s.group("name")
                known = list(self.lc.parts) + list(self.lc.required_parts_all)
                if name not in known:
                    self.add("error", "part_invalid", no, f"知らない部分の名前です: {name}（{', '.join(known)}）")
                if part_open is not None:
                    self.add("error", "nested_block", no, f"part（L{part_open[1]} から）の中に part があります")
                if name in parts_seen:
                    self.add("error", "part_invalid", no, f"part: {name} が2回あります（L{parts_seen[name]}）")
                parts_seen.setdefault(name, no)
                part_open = (name, no)
                continue
            if prt_e:
                if part_open is None:
                    self.add("error", "stray_block_end", no, "開始のない <!-- /part --> です")
                part_open = None
                continue
            if part_open is not None and part_open[0] == RELATED:
                self.check_related_line(no, line)
                continue

            # ---- calc-block ------------------------------------------------
            if calc_s:
                if calc_open is not None:
                    self.add("error", "nested_block", no, f"calc-block の中に calc-block があります（L{calc_open} から）")
                if src_open is not None:
                    self.add("error", "nested_block", no,
                             f"src-block（L{src_open} から）の中に calc-block があります（§1-2 制約7）")
                calc_open = no
                basis, calc_inputs = self.check_calc_basis(no)
                if basis:
                    self.add("error", "calc_block_no_basis", no, basis)
                continue
            if calc_e:
                if calc_open is None:
                    self.add("error", "stray_block_end", no, "開始のない <!-- /calc-block --> です")
                else:
                    self.res.calc_blocks.append((calc_open, no))
                    calc_open = None
                continue

            # ---- src-block -------------------------------------------------
            if blk_s:
                self.res.block_markers += 1
                self.check_marker_body(blk_s.group("body"), no, "src-block")
                if calc_open is not None:
                    self.add("error", "nested_block", no,
                             f"calc-block（L{calc_open} から）の中に src-block があります（§1-2 制約7）")
                if src_open is not None:
                    self.add("error", "nested_block", no, f"src-block（L{src_open} から）の中に src-block があります")
                src_open = no
                continue
            if blk_e:
                if src_open is None:
                    self.add("error", "stray_block_end", no, "開始のない <!-- /src-block --> です")
                src_open = None
                continue

            inline = list(INLINE_SRC_RE.finditer(line))
            nofact = NOFACT_RE.search(line)

            if calc_open is not None:
                if inline:
                    self.add("error", "src_in_calc_block", no,
                             "calc-block の中の出典マーカーは照合されません。入力値の表に移す")
                    self.res.inline_markers += len(inline)
                if line.strip() and not is_row:
                    self.add("error", "calc_block_non_table", no,
                             "calc-block の中は試算結果の表の行だけにする（文章や出典のない事実を入れない。§1-2）")
                elif is_row and art.table_role(no) == "header":
                    for tok in self.nums(line, False):
                        self.add("error", "calc_header_number", no,
                                 "試算表の見出し行に数値を書かない（「1人あたり」の1を除く。§1-2）", tok.raw)
                elif is_row and art.table_role(no) == "data":
                    cells = split_cells(line)
                    name = cells[0] if cells else ""
                    if calc_inputs is not None and name and name not in calc_inputs:
                        self.add("error", "calc_row_unknown", no,
                                 f"試算表の「{name}」が入力値の表の1列目にありません（入力値の表にある行だけを試算する）")
                    for cell in cells[1:]:
                        if not self.calc_cell_ok(cell):
                            self.add("error", "calc_cell_format", no,
                                     "試算表のデータのセルは「数値＋単位」か「試算の対象外（理由）」だけ（§1-2）", cell[:40])
                continue

            # ---- nofact ----------------------------------------------------
            if nofact:
                self.res.nofact_lines.append(no)
                if inline:
                    self.res.inline_markers += len(inline)
                    self.add("error", "nofact_with_src", no,
                             "nofact と出典マーカーを同じ行に置かない（行全体が照合対象外になる）")
                if is_row:
                    self.add("error", "nofact_in_table", no,
                             "表の行に nofact は置けない（置けるのは専用の行と見出しの行だけ。§1-2）")
                if src_open is not None:
                    self.add("error", "nofact_in_src_block", no,
                             "src-block の中に nofact を置くと、その行が照合から漏れます")
                if line[nofact.end():].strip():
                    self.add("warning", "nofact_not_at_end", no, "nofact は行末に置く")
                if heading is not None:
                    self.check_heading_nofact(heading)
                elif not is_row:
                    self.check_nofact_line(line, no)
                continue

            # ---- src-block の中 --------------------------------------------
            if src_open is not None:
                if inline:
                    self.res.inline_markers += len(inline)
                    self.add("error", "inline_in_src_block", no,
                             "src-block の中に行内マーカーを書かない（§1-2 制約3・5）")
                if BARE_URL_RE.search(no_comments):
                    self.add("error", "url_in_src_block", no,
                             "src-block の中にURLを書かない（URLの数字が照合対象に混入する。§1-2 制約5）")
                if line.strip() and not is_row:
                    self.add("warning", "note_in_src_block", no,
                             "src-block の中は表だけにする。注記はブロックの外に書く（§1-2 制約5）")
                continue

            # ---- 通常の行 ----------------------------------------------------
            strip_list = self.is_list_first(no)
            if inline:
                self.res.inline_markers += len(inline)
                cursor = 0
                for m in inline:
                    self.check_marker_body(m.group("body"), no, "src")
                    if not is_row:
                        self.check_multi_sentence(line[cursor:m.start()], no, strip_list and cursor == 0)
                    cursor = m.end()
                for tok in self.nums(line[cursor:], False):
                    self.add("error", "after_last_marker", no,
                             "最後のマーカーより後ろの数値はどの照合範囲にも入りません。"
                             "マーカーの後ろには読者向けの出典表記だけを書く（§1-2 制約4）", tok.raw)
                continue

            for tok in self.nums(line, strip_list):
                if heading is not None:
                    hint = "見出しの数え上げなら行末に <!-- nofact -->"
                elif _line_head(line).startswith(FORMULA_PREFIX):
                    hint = "計算式の単位のない定数なら行末に <!-- nofact -->"
                else:
                    hint = "出典マーカーを付ける"
                self.add("error", "unmarked_number", no, f"出典マーカーのない数値です（{hint}）",
                         f"{tok.raw}  …{tok.context}…")

        if src_open is not None:
            self.add("error", "unclosed_block", src_open, "src-block が <!-- /src-block --> で閉じられていません")
        if calc_open is not None:
            self.add("error", "unclosed_block", calc_open, "calc-block が <!-- /calc-block --> で閉じられていません")
        if part_open is not None:
            self.add("error", "unclosed_block", part_open[1], f"part: {part_open[0]} が <!-- /part --> で閉じられていません")
        if self.res.marker_count == 0:
            self.add("error", "no_markers", 1,
                     "出典マーカーが1つもありません（CLAUDE.md ルール2。issue として扱い終了コード1）")

        if art.front_matter is not None:
            self.check_title_numbers()
            for name in self.lc.required_parts_all:
                if name not in parts_seen:
                    self.add("error", "part_missing", 1,
                             f"<!-- part: {name} --> 〜 <!-- /part --> が必要です（型A・型Bとも。関連記事の区間。§4）")
            if RELATED in parts_seen and related_range(art) is None:
                # content_hash と同じ関数で判定する。不正なら区間はハッシュから外れない
                self.add("error", "related_invalid", parts_seen[RELATED],
                         "関連記事の区間が重複・未閉じ・入れ子になっています（このままでは区間をハッシュから外さない）")
            if str(art.fm("type") or "") == "B":
                self.check_type_b(parts_seen)
            self.check_part_coverage()
            self.check_programs()
        return self.res

    # -- 個別の検査 ---------------------------------------------------------

    def check_front_matter_fields(self) -> None:
        fm = self.art.front_matter or {}
        for key in ("title", "description"):
            value = str(fm.get(key) or "")
            if not value.strip():
                self.add("error", "title_missing", 1, f"front matter に {key} がありません")
                continue
            if "{{" in value:
                self.add("error", "placeholder", 1, f"front matter の {key} に {{{{ }}}} が残っています")
            self.check_text_rules(value, 1, where=f"{key}: ")
        problem = validate_slug(fm.get("slug"))
        if problem:
            self.add("error", "slug_invalid", 1, problem)
        typ = str(fm.get("type") or "")
        if typ not in ("A", "B"):
            self.add("warning", "front_matter_field", 1, f"type は A か B: {typ or '（なし）'}")
        if typ == "B":
            for key in ("parent", "industry"):
                if not fm.get(key):
                    self.add("warning", "front_matter_field", 1, f"型Bの front matter に {key} がありません")
        links = fm.get("p7_links")
        if links not in (None, "link", "text"):
            self.add("error", "front_matter_field", 1, f"p7_links は link か text: {links}")
        history = fm.get("history") or []
        if isinstance(history, list):
            for i, h in enumerate(history):
                note = str(h.get("note") or "") if isinstance(h, dict) else ""
                if not note:
                    continue
                where = f"history の {i + 1} 件目の note: "
                if re.search(r"\d", unicodedata.normalize("NFKC", mask_terms(note, self.lc.terms))):
                    self.add("error", "history_note", 1,
                             f"{where}数値を書かない（P7 にそのまま出るが、出典の照合を通らない。"
                             f"例:「X社の料金改定を反映」）", note[:40])
                self.check_text_rules(note, 1, where=where)

    def check_programs(self) -> None:
        programs = self.art.fm("programs") or []
        if not programs:
            return
        if not isinstance(programs, list):
            self.add("error", "program_unknown", 1, "front matter の programs はリストにする")
            return
        if self.lc.asp_programs_file is None:
            return
        try:
            known = load_asp_programs(self.lc.asp_programs_file)
        except ToolError as exc:
            self.add("error", "program_unknown", 1, str(exc))
            return
        for pid in programs:
            if str(pid) not in known:
                self.add("error", "program_unknown", 1,
                         f"案件ID {pid} が {self.lc.asp_programs_file.name} にありません", str(pid))

    def check_heading_nofact(self, h: Heading) -> None:
        text = normalize_text(remove_comments(h.text))
        starts = {s for _pi, _n, s, _noun in self._enum_matches(text)}
        for rx in self._ord_rx:
            for m in rx.finditer(text):
                starts.add(m.start("n"))
        for tok, s, e, norm in iter_number_matches(text, skip_year_like=self.lc.skip_years,
                                                   ignore_patterns=self.lc.ignore_patterns):
            if self.lc.drop_per_unit_one and is_per_unit_one(tok, e, norm, self.lc.per_unit_suffixes):
                continue
            if s not in starts:
                self.add("error", "nofact_heading_not_enum", h.line_no,
                         "見出しの nofact は、数え上げ（「3つの要件」「5選」「ポイント1」など）の数だけに使う。"
                         "事実の数値は本文に出典つきで書く", tok.raw)

    def check_nofact_line(self, line: str, no: int) -> None:
        head = _line_head(line)
        if not any(head.startswith(p) for p in self._prefixes):
            self.add("error", "nofact_line_not_allowed", no,
                     "見出し以外の nofact は「" + "」「".join(self.lc.nofact_line_prefixes)
                     + "」で始まる専用の行だけに置ける（config の nofact_line_prefixes）")
            return
        if head.startswith(FORMULA_PREFIX):
            for tok in self.nums(line, False):
                if tok.unit is not None:
                    self.add("error", "formula_unit_number", no,
                             "計算式の行に単位付きの数値を書かない（入力値は入力値の表に出典つきで書く）。"
                             "単位のない定数（× 12 など）だけ書ける", tok.raw)
        elif head.startswith(CONDITION_PREFIX):
            units = sorted(self.lc.nofact_condition_units, key=len, reverse=True)
            for tok in self.nums(line, False):
                raw = unicodedata.normalize("NFKC", tok.raw).replace(" ", "")
                if not any(raw.endswith(u) and re.fullmatch(r"[\d,.]+" + re.escape(u), raw) for u in units):
                    self.add("error", "condition_unit", no,
                             "「試算の条件：」の行に書ける数値は、人数・期間・回数（" + "・".join(self.lc.nofact_condition_units)
                             + "）だけ。金額・社数・割合は入力値の表に出典つきで書く", tok.raw)

    def check_multi_sentence(self, segment: str, line_no: int, strip_list: bool) -> None:
        text = remove_comments(segment)
        pieces = [p for p in re.split(r"(?<=[。！？!?])", text) if p.strip()]
        if len(pieces) < 2:
            return
        if self.nums("".join(pieces[:-1]), strip_list):
            self.add("warning", "multi_sentence", line_no,
                     "1行に複数の文があり、前の文の数値も同じURLで照合されます。1文を1行にする（§1-2 制約1）")

    def check_calc_basis(self, calc_line_no: int) -> tuple[Optional[str], Optional[set[str]]]:
        """calc-block の直前に「入力値の表（マーカー付き）」と「計算式：」の行があるか。

        戻り値: (エラー文 or None, 入力値の表の1列目の値の集合)
        規則（README §3.6）:
          1. calc-block の直前（空行を除く）の行が「計算式：」で始まる
          2. その前（空行と、数値を含まない注記行を最大 calc_basis_max_note_lines 行まで挟んでよい）が表
          3. 表のデータ行がすべて行内マーカー付き、または表が src-block で囲まれている
        """
        art = self.art
        lines, zones = art.lines, art.zones
        j = calc_line_no - 2
        while j >= 0 and zones[j] == "body" and not lines[j].strip():
            j -= 1
        if j < 0 or zones[j] != "body" or not _line_head(lines[j]).startswith(FORMULA_PREFIX):
            return ("calc-block の直前（空行を除く）に「計算式：」の行がありません。"
                    "入力値の表 → 計算式： → calc-block の順に書く（§1-2 計算した値）"), None
        k = j - 1
        notes = 0
        while k >= 0 and zones[k] == "body":
            s = lines[k]
            if not s.strip():
                k -= 1
                continue
            if art.is_table_row(k + 1) or BLOCK_END_RE.search(s):
                break
            if (art.heading_at(k + 1) or CALC_END_RE.search(s) or CALC_START_RE.search(s)
                    or BLOCK_START_RE.search(s)):
                return "「計算式：」の前に入力値の表がありません（見出しや別のブロックが先にあります）", None
            if self.nums(s, False):
                return f"入力値の表と「計算式：」の間に数値を含む行があります（L{k + 1}）", None
            notes += 1
            if notes > self.lc.calc_basis_max_note_lines:
                return "「計算式：」の直前に入力値の表がありません", None
            k -= 1
        if k < 0 or zones[k] != "body":
            return "「計算式：」の前に入力値の表がありません", None

        if BLOCK_END_RE.search(lines[k]):
            m = k - 1
            names: set[str] = set()
            rows = 0
            while m >= 0 and not BLOCK_START_RE.search(lines[m]):
                if art.table_role(m + 1) == "data":
                    rows += 1
                    cells = split_cells(lines[m])
                    if cells:
                        names.add(cells[0])
                m -= 1
            if m < 0:
                return "入力値の表の src-block の開始が見つかりません", None
            if rows < 1:
                return "入力値の表（src-block）にデータ行がありません", None
            return None, names

        data: list[tuple[int, str]] = []
        while k >= 0 and zones[k] == "body" and art.is_table_row(k + 1):
            if art.table_role(k + 1) == "data":
                data.insert(0, (k + 1, lines[k]))
            if art.table_role(k + 1) == "header":
                break
            k -= 1
        if not data:
            return "入力値の表にデータ行がありません", None
        missing = [n for n, r in data if not INLINE_SRC_RE.search(r)]
        if missing:
            return ("入力値の表のデータ行に出典マーカーがありません（L"
                    + ", L".join(str(n) for n in missing) + "）。各行の行末にマーカーを置く"), None
        return None, {split_cells(r)[0] for _n, r in data if split_cells(r)}

    def calc_cell_ok(self, cell: str) -> bool:
        c = unicodedata.normalize("NFKC", cell).strip()
        if re.fullmatch(r"試算の対象外(?:[(（].*[)）])?", c):
            return True
        units = "|".join(re.escape(u) for u in sorted(self.lc.calc_cell_units, key=len, reverse=True))
        return bool(re.fullmatch(r"[\d,]+(?:\.\d+)?\s*(?:" + units + r")\s*[〜~]?", c))

    # -- 部分の目印（C34）・関連記事（related） -----------------------------------------------

    def _part_blocks(self) -> list[tuple[str, int, int]]:
        """(名前, 開始の目印の行 index, 終了の目印の行 index)。入れ子・閉じていないものは除く。"""
        blocks = []
        cur: Optional[tuple[str, int]] = None
        for no, line in self.art.body_lines():
            m = PART_START_RE.search(line)
            if m:
                cur = None if cur is not None else (m.group("name"), no - 1)
                continue
            if PART_END_RE.search(line) and cur is not None:
                blocks.append((cur[0], cur[1], no - 1))
                cur = None
        return blocks

    def check_part_coverage(self) -> None:
        """目印は、見出しの直後から次の同じ（以上の）階層の見出しの直前まで、区間の全体を囲む（§4）。

        続けて置いた part（例: trace の直後に checklist）は、合わせて1つの区間を覆えばよい。
        """
        art = self.art
        lines = art.lines
        headings = art.struct.headings
        heading_line = {}
        for h in headings:
            for i in range(h.line_no - 1, h.end_line):
                heading_line[i] = h

        def prev_nonblank(i: int) -> int:
            i -= 1
            while i >= art.body_start and not lines[i].strip():
                i -= 1
            return i

        def next_nonblank(i: int) -> int:
            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            return i

        prev_block: Optional[tuple[int, Any]] = None      # (終了の目印の行 index, その part の見出し)
        for name, s, e in self._part_blocks():
            pv = prev_nonblank(s)
            chained = prev_block is not None and pv == prev_block[0]
            if chained:
                owner = prev_block[1]          # 続けて置いた part は、前の part と同じ見出しの区間
            else:
                owner = None
                for h in headings:
                    if h.line_no - 1 < s:
                        owner = h
            prev_ok = chained or pv < art.body_start or pv in heading_line
            if owner is None and not chained and pv >= art.body_start:
                prev_ok = False
            prev_block = (e, owner)
            nx = next_nonblank(e)
            if nx >= len(lines):
                next_ok = True
            elif PART_START_RE.search(lines[nx]):
                next_ok = True
            elif nx in heading_line:
                next_ok = owner is None or heading_line[nx].level <= owner.level
            else:
                next_ok = False
            crossing = [h for h in headings if s < h.line_no - 1 < e and owner is not None and h.level <= owner.level]
            if not (prev_ok and next_ok) or crossing:
                where = f"見出し「{remove_comments(owner.text).strip()}」" if owner else "記事の先頭"
                self.add("error", "part_not_whole", s + 1,
                         f"part: {name} の目印は、{where}の直後から次の同じ階層の見出しの直前まで、区間の全体を囲む"
                         f"（区間の中に目印の外の本文を残さない。続けて置いた part はまとめて1つの区間とみなす。§4）")

    def _industry_names(self) -> set[str]:
        import csv
        path = self.lc.industries_file
        if path is None or not path.exists():
            return set()
        names: set[str] = set()
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                for key in ("name", "aliases"):
                    for part in re.split(r"[|｜]", (row.get(key) or "")):
                        part = unicodedata.normalize("NFKC", part).strip()
                        if part:
                            names.add(part)
        return names

    def check_related_line(self, no: int, line: str) -> None:
        """関連記事の区間の1行（§4・§7 工程11）。"""
        if not line.strip():
            return
        m = RELATED_LINE_RE.match(line)
        typ = str(self.art.fm("type") or "")
        form = ("- 業種名：[リンク先のタイトル](/slug/)" if typ == "A" else "- [リンク先のタイトル](/slug/)")
        if not m:
            self.add("error", "related_invalid", no,
                     f"関連記事の区間に書けるのは「{form}」の行だけ（1項目にリンク1つ。見出し・段落・表・画像・CTA・"
                     f"出典マーカー・nofact・URLだけの行は書けない）", line.strip()[:60])
            return
        ind, text, url = m.group("ind"), m.group("text").strip(), m.group("url")
        if typ == "A":
            if not ind:
                self.add("error", "related_invalid", no, f"型Aの関連記事は「{form}」の形にする", line.strip()[:60])
            elif unicodedata.normalize("NFKC", ind.strip()) not in self._industry_names():
                self.add("error", "related_invalid", no, f"「{ind}」が data/industries.csv の業種名にありません", ind)
        elif ind:
            self.add("error", "related_invalid", no, f"型Bの関連記事は「{form}」の形にする（前置きの文字は書かない）",
                     line.strip()[:60])
        from urllib.parse import urlparse
        u = urlparse(url)
        if u.scheme or u.netloc:
            if u.scheme not in ("http", "https") or not host_matches(u.hostname or "", self.lc.site_domains):
                self.add("error", "related_invalid", no,
                         "関連記事のリンク先は相対パス（/slug/）か、config の site_domains のドメインだけ", url)
                return
        elif not u.path.startswith("/"):
            self.add("error", "related_invalid", no, "関連記事のリンク先は / で始まる相対パスにする", url)
            return
        if host_matches(u.hostname or "", self.lc.affiliate_domains):
            self.add("error", "related_invalid", no, "関連記事にアフィリエイトのリンクは置けない", url)
            return
        segs = [x for x in u.path.split("/") if x]
        slug = segs[-1] if segs else ""
        problem = validate_slug(slug)
        if problem:
            self.add("error", "related_invalid", no, f"リンク先の slug が不正です: {problem}", url)
            return
        target = self.art.path.parent / f"{slug}.md"
        if not target.exists():
            self.add("error", "related_invalid", no, f"リンク先の正本 {target.name} がありません", url)
            return
        try:
            tart = load_article(target)
        except ToolError as exc:
            self.add("error", "related_invalid", no, f"リンク先の正本を読めません: {exc}", url)
            return
        pub = to_date(tart.fm("published"))
        if pub is None or pub > self.lc.today:
            self.add("error", "related_invalid", no,
                     f"リンク先 {slug} は公開済みではありません（published が今日以前でない）", url)
        title = str(tart.fm("title") or "").strip()
        if text != title:
            self.add("error", "related_invalid", no,
                     f"リンクの文字列は、リンク先の title と完全に一致させる（リンク先: 「{title}」）", text[:60])

    # -- 型B ----------------------------------------------------------------

    def check_type_b(self, parts_seen: dict[str, int]) -> None:
        for name in self.lc.parts:
            if name not in parts_seen:
                self.add("error", "part_missing", 1,
                         f"型Bの正本には <!-- part: {name} --> 〜 <!-- /part --> が必要です"
                         f"（類似度チェック T4 で比べる部分の目印）")
        ranges = part_ranges(self.art)
        if "trace" in ranges:
            s, e = ranges["trace"][0]
            best = 0
            unmarked: list[int] = []
            for t in self.art.struct.tables:
                if s - 1 <= t.start and t.end <= e:
                    rows = [i + 1 for i in range(t.start, t.end) if self.art.table_role(i + 1) == "data"]
                    if len(rows) > best:
                        best = len(rows)
                        unmarked = [r for r in rows if not INLINE_SRC_RE.search(self.art.lines[r - 1])]
            if best < self.lc.trace_min_rows:
                self.add("error", "trace_table_rows", s,
                         f"要件トレース表は{self.lc.trace_min_rows}行以上（§4 合格ライン）。今は {best} 行")
            if unmarked:
                self.add("error", "trace_table_rows", unmarked[0],
                         "要件トレース表の各行の事実に出典マーカーを付ける（§4）: L" + ", L".join(map(str, unmarked)))
        if "faq" in ranges:
            s, e = ranges["faq"][0]
            qs = 0
            for i in range(s, e + 1):
                ln = self.art.lines[i - 1]
                h = self.art.heading_at(i)
                head = _line_head(ln)
                if QUESTION_RE.match(head) or (h is not None and remove_comments(h.text).strip().endswith(("？", "?"))):
                    qs += 1
            if qs < self.lc.faq_min_questions:
                self.add("error", "part_too_small", s,
                         f"FAQ の質問は{self.lc.faq_min_questions}問以上（§4。「Q.」で始まる行か、？で終わる見出しを数える）。"
                         f"今は {qs} 問")
        if "checklist" in ranges:
            s, e = ranges["checklist"][0]
            items = [(b, d) for b, d in self.art.struct.list_items if s - 1 <= b.start <= e - 1]
            top = min((d for _b, d in items), default=0)
            n = sum(1 for _b, d in items if d == top)
            if n < self.lc.checklist_min_items:
                self.add("error", "part_too_small", s,
                         f"判断基準チェックリストの項目は{self.lc.checklist_min_items}つ以上（リストで書く。§4）。今は {n} 個")
        rec_rx = [re.compile(p) for p in self.lc.recommend_heading_patterns]
        for h in self.art.struct.headings:
            if any(rx.search(remove_comments(h.text)) for rx in rec_rx):
                children = child_headings(self.art, h)
                if len(children) > self.lc.max_recommendations:
                    self.add("error", "too_many_recommendations", h.line_no,
                             f"型Bの推薦は最大{self.lc.max_recommendations}つ（§1-10）。"
                             f"この見出しの直下に {len(children)} 個あります")

    # -- title / description -------------------------------------------------

    def _enum_matches(self, text: str) -> list[tuple[int, int, int, str]]:
        """(pattern index, n, n の開始位置, 名詞)。同じ位置では最初に当たったパターンだけ使う。"""
        taken: dict[int, tuple[int, int, int, str]] = {}
        for pi, rx in enumerate(self._enum_rx):
            for m in rx.finditer(text):
                try:
                    n = int(m.group("n"))
                except (IndexError, ValueError):
                    continue
                s = m.start("n")
                if s in taken:
                    continue
                noun = m.groupdict().get("noun") or ""
                taken[s] = (pi, n, s, noun)
        return sorted(taken.values(), key=lambda x: x[2])

    def check_title_numbers(self) -> None:
        fm = self.art.front_matter or {}
        from factcheck import parse_article  # 循環 import を避けるためここで読む

        claims, _issues = parse_article(self.art.path, self.lc.ignore_patterns, self.lc.skip_years, self.lc.terms)
        sourced = [t for c in claims for i, t in enumerate(c.numbers) if i not in c.per_unit_idx]
        heading_enums: list[tuple[Heading, int, int, str]] = []
        for h in self.art.struct.headings:
            htext = normalize_text(remove_comments(h.text))
            for pi, n, _s, noun in self._enum_matches(htext):
                heading_enums.append((h, pi, n, noun))

        for field in ("title", "description"):
            value = fm.get(field)
            if not value:
                continue
            text = normalize_text(mask_terms(remove_comments(str(value)), self.lc.terms))
            text = PLACEHOLDER_RE.sub(" ", text)
            enums = {s: (pi, n, noun) for pi, n, s, noun in self._enum_matches(text)}
            for tok, start, end, norm in iter_number_matches(
                    text, skip_year_like=self.lc.skip_years, ignore_patterns=self.lc.ignore_patterns):
                if self.lc.drop_per_unit_one and is_per_unit_one(tok, end, norm, self.lc.per_unit_suffixes):
                    continue
                if start in enums:
                    pi, n, noun = enums[start]
                    self._check_enum(field, tok, pi, n, noun, heading_enums)
                    continue
                if tok.unit is None:
                    self.add("error", "title_fact_unsourced", 1,
                             f"{field} の数値に単位がありません。事実の数値は単位まで本文の出典つき数値と一致させる（§1-2）",
                             tok.raw)
                    continue
                key = (format_decimal(tok.value), tok.unit)
                if not any((format_decimal(t.value), t.unit) == key for t in sourced):
                    self.add("error", "title_fact_unsourced", 1,
                             f"{field} の数値は、本文に出典つきで書いた数値と同じ値・同じ単位に限る（§1-2）"
                             f"。本文の出典つき数値に見つかりません", tok.raw)

    def _count_items(self, h: Heading) -> Optional[tuple[int, str]]:
        """見出しの区間の項目の数（直下の見出し → 最初の表のデータ行 → 最上位のリスト項目）。"""
        children = child_headings(self.art, h)
        if children:
            return len(children), "直下の見出し"
        start, end = section_range(self.art, h)
        for t in self.art.struct.tables:
            if start - 1 <= t.start < end:
                rows = sum(1 for i in range(t.start, t.end) if self.art.table_role(i + 1) == "data")
                return rows, f"L{t.start + 1} の表のデータ行"
        items = [(b, d) for b, d in self.art.struct.list_items if start - 1 <= b.start < end]
        if items:
            top = min(d for _b, d in items)
            return sum(1 for _b, d in items if d == top), "リストの項目"
        return None

    def _check_enum(self, field: str, tok: NumberToken, pi: int, n: int, noun: str,
                    heading_enums: list[tuple[Heading, int, int, str]]) -> None:
        def noun_ok(a: str, b: str) -> bool:
            return a == b or (a and b and (a in b or b in a))

        hits = [h for h, hpi, hn, hnoun in heading_enums if hpi == pi and hn == n and noun_ok(noun, hnoun)]
        if not hits:
            what = f"「{tok.raw}…{noun}」" if noun else f"「{tok.raw}…」"
            self.add("error", "title_enum_mismatch", 1,
                     f"{field} の数え上げ{what}と同じ数・同じ名詞を書いた見出しが本文にありません"
                     f"（見出しに同じ数え上げを書き、行末に nofact を付ける）", tok.raw)
            return
        h = hits[0]
        counted = self._count_items(h)
        if counted is None:
            self.add("error", "title_enum_uncountable", h.line_no,
                     f"{field} の「{tok.raw}」の項目の数を数えられません。見出し L{h.line_no} の下に、"
                     f"項目を直下の見出し・表・リストのどれかで書く", tok.raw)
            return
        count, how = counted
        if count != n:
            self.add("error", "title_enum_count", h.line_no,
                     f"{field} と見出し L{h.line_no} は {n} だが、{how}は {count} 個です", tok.raw)


def lint_article(path: Path, lc: LintConfig) -> LintResult:
    from article import content_hash

    art = load_article(path)
    res = _Linter(art, lc).run()
    try:
        res.content_hash = content_hash(art)
    except Exception:  # front matter が壊れていてもハッシュは出す必要がない
        res.content_hash = ""
    return res


# --------------------------------------------------------------------------
# レポート
# --------------------------------------------------------------------------

def render_lint_report(results: list[LintResult]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    n_err = sum(len(r.errors) for r in results)
    n_warn = sum(len(r.warnings) for r in results)
    lines = [
        "# lint レポート（factcheck.py --lint）",
        "",
        f"生成日時: {now}",
        f"対象ファイル: {len(results)} 件 / エラー: {n_err} 件 / 警告: {n_warn} 件",
        f"**判定: {'不合格' if n_err else '合格'}**（エラーが1件でもあれば不合格。警告は人が確認）",
        "",
    ]
    for r in results:
        lines += [f"## {r.path}", ""]
        lines.append(f"- 判定: **{'不合格' if r.errors else '合格'}**"
                     f"（エラー {len(r.errors)} / 警告 {len(r.warnings)}）")
        if r.content_hash and not r.errors:
            lines.append(f"- 正本のハッシュ: `{r.content_hash}`（lint は合格。ハッシュは合格のときだけ出す）")
        lines.append(f"- 出典マーカー: {r.marker_count} 件（行内 {r.inline_markers} / src-block {r.block_markers}）")
        nf = ", ".join(f"L{n}" for n in r.nofact_lines) or "なし"
        lines.append(f"- nofact: {len(r.nofact_lines)} 件（{nf}）")
        cb = ", ".join(f"L{s}–L{e}" for s, e in r.calc_blocks) or "なし"
        lines.append(f"- calc-block: {len(r.calc_blocks)} 件（{cb}）")
        lines.append("")
        for sev, title in (("error", "エラー"), ("warning", "警告")):
            items = [i for i in r.issues if i.severity == sev]
            if not items:
                continue
            lines += [f"### {title}", ""]
            for i in sorted(items, key=lambda x: x.line_no):
                num = f" `{i.number}`" if i.number else ""
                lines.append(f"- L{i.line_no} [{KIND_LABEL.get(i.kind, i.kind)}]{num} — {i.detail}")
            lines.append("")
    return "\n".join(lines)


def run_lint(files: list[Path], cfg: dict[str, Any], out: Optional[str] = None,
             json_out: Optional[str] = None, skip_years: bool = True) -> int:
    lc = LintConfig.from_cfg(cfg, skip_years=skip_years)
    results = []
    for path in files:
        if path.suffix.lower() not in (".md", ".markdown"):
            r = LintResult(path=path)
            r.issues.append(LintIssue("error", "html_article", 1,
                                      "正本は .md だけ（articles/ に .html を置かない。公開ページの照合は postcheck.py）"))
            results.append(r)
            continue
        results.append(lint_article(path, lc))
    if not results:
        LOG.error("lint の対象がありません")
        return 2
    report = render_lint_report(results)
    if out:
        write_text(Path(out), report)
    else:
        sys.stdout.write(report)
    if json_out:
        write_json(Path(json_out), {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "results": [{
                "file": str(r.path),
                "pass": not r.errors,
                "content_hash": r.content_hash if not r.errors else "",
                "markers": r.marker_count,
                "nofact_lines": r.nofact_lines,
                "calc_blocks": r.calc_blocks,
                "issues": [dataclasses.asdict(i) for i in r.issues],
            } for r in results],
        })
    if any(r.errors for r in results):
        LOG.warning("lint のエラーがあります。レポートを確認してください。")
        return 1
    LOG.info("lint は合格です（警告 %s 件）", sum(len(r.warnings) for r in results))
    return 0
