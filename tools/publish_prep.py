#!/usr/bin/env python
"""
publish_prep.py — 正本から WordPress に反映する成果物を作る（設計書 §8 T5・T8）。

入力: articles/{slug}.md（正本。front matter＋出典マーカー入り）
出力: build/{slug}/
        content.html  … 本文のHTML（REST API の content に渡す／ブロックエディタのコードエディタに貼る）
        content.md    … 本文のMarkdown（マーカー除去・CTA置換・P7追加・段落の改行をつないだもの）
        meta.json     … title・slug・description・表示する更新日など（status は常に draft）
        gate-summary.md … 関門の要約（lint・照合の受領ファイル・checks・手動確認の記録・T7 の扱い）

書き出す前に確かめること（1つでも欠ければエラーで、書き出さない。設計書 §8 T10）
  1. lint（factcheck.py --lint と同じ検査）を自分で実行し、エラーがない
  2. 照合の合格の**受領ファイル** data/checks/{slug}.json（factcheck.py が関門モードで合格したときに自分で書く）が
     あり、その content_hash が今の正本のハッシュと一致する
  3. front matter の checks に、必須の判定の証跡（factcheck_session・owner_review、型Bは trace_table も）がそろい、
     すべて result: pass、date があり、hash が**今の正本のハッシュと一致**する（古い合格の流用を防ぐ）。
     ハッシュは --hash で表示する（lint と照合が合格しているときだけ表示する）
  成果物は常に下書き（meta.json の status: draft）。実行者が読む「関門の要約」（gate-summary.md）を添える。
  **この仕組みは古い記録の流用と記録漏れを防ぐものであり、偽造への防御ではない。最後の保証は、
  実行者が関門の要約を読み、下書きを公開に切り替えることである**（設計書 §7 工程10b）

処理
  1. マーカー（src・src-block・calc-block・nofact・part）とその他のHTMLコメントをすべて除去する
     単独の行のマーカーは、前後がどちらも空行でなく、リスト項目の間でもないときだけ空行にし、それ以外は行ごと消す
  2. {{CTA:案件ID}} を、data/cta_patterns.yaml の対応表で同期パターンの参照に置き換える。対応がなければエラー
  3. P7（参考資料・更新履歴）を、マーカーのURLと accessed=、front matter の history から生成して末尾に付ける
     参考資料のURLは既定で文字として出す（front matter に p7_links: link と書いた記事だけリンクにする）
  4. {{ の置き換え漏れを検出したらエラー
  5. 同じ段落内の文と文の間の改行をつなぐ（§1-2 制約2）。段落の範囲は markdown-it のトークンで決める
  6. 既知のアフィリエイトのドメインへのリンクで rel="sponsored" も nofollow もないものを報告（エラー）
  7. meta.json に表示する更新日（front matter の updated）などを書く

使い方
  python tools/publish_prep.py articles/dental-clinic-attendance-system.md
  python tools/publish_prep.py articles/*.md --out-dir build
  python tools/publish_prep.py --hash articles/dental-clinic-attendance-system.md   # checks に書くハッシュ（合格時だけ）

終了コード: 0 = 成功 / 1 = エラーのある記事がある / 2 = 実行エラー
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import html as html_mod
import logging
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from common import ToolError, cfg_get, expand_path, load_config, setup_logging, write_json, write_text
from article import (
    ANY_COMMENT_RE,
    BLOCK_START_RE,
    CTA_RE,
    INLINE_SRC_RE,
    MARKER_COMMENT_RE,
    Article,
    content_hash,
    date_str,
    find_affiliate_links_without_rel,
    load_article,
    remove_comments,
    split_marker_body,
    to_date,
    validate_slug,
)

LOG = logging.getLogger("affiliate.tools.publish_prep")

DEFAULT_CTA_TEMPLATE = '<!-- wp:block {{"ref":{pattern_id}}} /-->'
# lint は publish_prep が自分で実行し、照合（factcheck_tool）は受領ファイルで確かめるので、ここには入れない
DEFAULT_REQUIRED_CHECKS = ["factcheck_session", "owner_review"]
DEFAULT_REQUIRED_CHECKS_TYPE_B = ["trace_table"]
CJK_RE = re.compile(r"[　-〿぀-ヿ㐀-䶿一-鿿＀-￯]")


@dataclasses.dataclass
class Message:
    severity: str        # error / warning
    line_no: int
    detail: str


@dataclasses.dataclass
class Reference:
    url: str
    accessed: Optional[str]
    first_line: int


@dataclasses.dataclass
class PrepResult:
    article: Article
    messages: list[Message] = dataclasses.field(default_factory=list)
    content_md: str = ""
    content_html: str = ""
    meta: dict[str, Any] = dataclasses.field(default_factory=dict)
    references: list[Reference] = dataclasses.field(default_factory=list)
    ctas: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    content_hash: str = ""
    gate_summary: str = ""

    @property
    def errors(self) -> list[Message]:
        return [m for m in self.messages if m.severity == "error"]

    @property
    def warnings(self) -> list[Message]:
        return [m for m in self.messages if m.severity == "warning"]

    def err(self, line_no: int, detail: str) -> None:
        self.messages.append(Message("error", line_no, detail))

    def warn(self, line_no: int, detail: str) -> None:
        self.messages.append(Message("warning", line_no, detail))


@dataclasses.dataclass
class PrepConfig:
    cta_template: str = DEFAULT_CTA_TEMPLATE
    p7_heading: str = "参考資料・更新履歴"
    references_heading: str = "参考資料"
    history_heading: str = "更新・訂正履歴"
    reference_link_mode: str = "text"
    affiliate_domains: tuple[str, ...] = ()
    required_checks: tuple[str, ...] = tuple(DEFAULT_REQUIRED_CHECKS)
    required_checks_type_b: tuple[str, ...] = tuple(DEFAULT_REQUIRED_CHECKS_TYPE_B)
    checks_dir: Optional[Path] = None
    sources_dir: Optional[Path] = None
    wrap_html_blocks: bool = True

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> "PrepConfig":
        node = cfg_get(cfg, "publish_prep", {}) or {}
        return cls(
            wrap_html_blocks=bool(node.get("wrap_html_blocks", True)),
            cta_template=str(node.get("cta_template", DEFAULT_CTA_TEMPLATE)),
            p7_heading=str(node.get("p7_heading", "参考資料・更新履歴")),
            references_heading=str(node.get("references_heading", "参考資料")),
            history_heading=str(node.get("history_heading", "更新・訂正履歴")),
            reference_link_mode=str(node.get("reference_link_mode", "text")),
            affiliate_domains=tuple(cfg_get(cfg, "affiliate_domains", []) or []),
            required_checks=tuple(node.get("required_checks", DEFAULT_REQUIRED_CHECKS)),
            required_checks_type_b=tuple(node.get("required_checks_type_b", DEFAULT_REQUIRED_CHECKS_TYPE_B)),
            checks_dir=expand_path(cfg_get(cfg, "paths.checks_dir", "../data/checks")),
            sources_dir=expand_path(cfg_get(cfg, "paths.sources_dir", "../data/sources")),
        )


# --------------------------------------------------------------------------
# CTA の対応表
# --------------------------------------------------------------------------

def load_cta_patterns(path: Path) -> dict[str, Any]:
    """data/cta_patterns.yaml を読む。戻り値: 案件ID → 同期パターンのID

    書式:
        patterns:
          prog_001:
            pattern_id: 123        # WordPress の同期パターン（wp_block）の投稿ID
            name: X社 CTAボタン     # 任意（人が読むため）
          prog_002: 456            # 省略形
    """
    if not path.exists():
        raise ToolError(f"CTA の対応表が見つかりません: {path}（data/cta_patterns.yaml を作る）")
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise ToolError("PyYAML が未インストールです。`pip install -r tools/requirements.txt`") from exc
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ToolError(f"CTA の対応表を読めません（文字コードを確認）: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ToolError(f"CTA の対応表のトップレベルはマッピングにする: {path}")
    patterns = data.get("patterns", {}) if "patterns" in data else data
    if patterns is None:
        patterns = {}
    if not isinstance(patterns, dict):
        raise ToolError(f"CTA の対応表の patterns はマッピングにする: {path}")
    out: dict[str, Any] = {}
    for key, value in patterns.items():
        pid = value.get("pattern_id") if isinstance(value, dict) else value
        if pid in (None, ""):
            raise ToolError(f"CTA の対応表の {key} に pattern_id がありません: {path}")
        out[str(key)] = pid
    return out


# --------------------------------------------------------------------------
# 改行をつなぐ
# --------------------------------------------------------------------------

def _hard_break(text: str) -> bool:
    return text.endswith("  ") or text.endswith("\\") or bool(re.search(r"<br\s*/?>\s*$", text, re.I))


def join_text(a: str, b: str) -> str:
    """段落内の2行をつなぐ。どちらかの境目が日本語なら空白を入れない。"""
    a = a.rstrip()
    b = b.strip()
    if not a:
        return b
    if not b:
        return a
    if CJK_RE.match(a[-1]) or CJK_RE.match(b[0]):
        return a + b
    return a + " " + b


# --------------------------------------------------------------------------
# 本体
# --------------------------------------------------------------------------

def collect_references(art: Article, res: PrepResult) -> list[Reference]:
    """出典マーカー（src・src-block）の URL と accessed を出現順に集め、重複URLを1つにする。

    同じURLに異なる accessed があれば、新しい日付を参照日にする。
    """
    refs: dict[str, Reference] = {}
    for no, line in art.body_lines():
        bodies = [m.group("body") for m in INLINE_SRC_RE.finditer(line)]
        bodies += [m.group("body") for m in BLOCK_START_RE.finditer(line)]
        for body in bodies:
            url, accessed = split_marker_body(body)
            if url is None:
                res.err(no, f"出典マーカーのURLが不正です（P7を作れません）: {body.strip()[:80]}")
                continue
            if accessed is None:
                res.err(no, f"出典マーカーに accessed=YYYY-MM-DD がありません（P7の参照日を作れません）: {url}")
            if url in refs:
                old = refs[url].accessed
                if accessed and (old is None or accessed > old):
                    refs[url].accessed = accessed
                continue
            refs[url] = Reference(url, accessed, no)
    return list(refs.values())


def build_p7(art: Article, refs: list[Reference], pc: PrepConfig, res: PrepResult) -> list[str]:
    esc = html_mod.escape
    mode = str(art.fm("p7_links") or pc.reference_link_mode)
    lines = [f"## {pc.p7_heading}", "", f"### {pc.references_heading}", ""]
    if not refs:
        res.err(0, "出典マーカーが1つもないため P7（参考資料）を作れません")
    for r in refs:
        acc = f"（参照日：{esc(r.accessed)}）" if r.accessed else ""
        if mode == "link":
            lines.append(f"- [{esc(r.url)}](<{r.url}>){acc}")
        else:
            lines.append(f"- {esc(r.url)}{acc}")
    lines += ["", f"### {pc.history_heading}", ""]
    updated = date_str(art.fm("updated"))
    lines += [f"最終更新日：{esc(str(updated))}", ""]
    history = art.fm("history") or []
    if not isinstance(history, list) or not history:
        res.err(0, "front matter の history がありません（P7の更新・訂正履歴を作れません）")
        history = []
    entries = []
    for i, h in enumerate(history):
        if not isinstance(h, dict) or not h.get("date") or not h.get("note"):
            res.err(0, f"front matter の history の {i + 1} 件目に date と note がありません: {h}")
            continue
        d = to_date(h.get("date"))
        if d is None:
            res.err(0, f"history の日付が YYYY-MM-DD ではありません: {h.get('date')}")
            continue
        entries.append((d, str(h.get("note")).strip()))
    entries.sort(key=lambda e: e[0], reverse=True)
    for d, note in entries:
        lines.append(f"- {d.isoformat()}：{esc(note)}")
    upd = to_date(art.fm("updated"))
    if entries and upd and entries[0][0] > upd:
        res.warn(0, f"history の最新の日付 {entries[0][0]} が updated {upd} より新しい")
    return lines


def validate_front_matter(art: Article, res: PrepResult) -> None:
    if art.front_matter_error:
        res.err(1, art.front_matter_error)
        return
    if art.front_matter is None:
        res.err(1, "front matter がありません（§1-1）")
        return
    for key in ("type", "slug", "title", "description", "updated"):
        if art.fm(key) in (None, ""):
            res.err(1, f"front matter に {key} がありません")
    for key in ("title", "description"):
        if "{{" in str(art.fm(key) or ""):
            res.err(1, f"front matter の {key} に {{{{ が残っています")
    slug = art.fm("slug")
    if slug not in (None, ""):
        problem = validate_slug(slug)
        if problem:
            res.err(1, problem)
        elif slug != art.path.stem:
            res.warn(1, f"slug（{slug}）とファイル名（{art.path.stem}）が違います")
    if art.fm("updated") not in (None, "") and to_date(art.fm("updated")) is None:
        res.err(1, f"updated が YYYY-MM-DD ではありません: {art.fm('updated')}")


def validate_checks(art: Article, pc: PrepConfig, res: PrepResult, current_hash: str) -> None:
    """品質ゲートの証跡（§6）。必須の checks がそろい、pass で、今の正本のハッシュと一致するか。"""
    checks = art.fm("checks")
    required = list(pc.required_checks)
    if str(art.fm("type") or "") == "B":
        required += list(pc.required_checks_type_b)
    if not isinstance(checks, dict):
        res.err(1, "front matter に checks がありません（品質ゲートの証跡。§6）。必須: " + ", ".join(required))
        return
    for name in required:
        node = checks.get(name)
        where = f"checks.{name}"
        if not isinstance(node, dict):
            res.err(1, f"{where} がありません（必須の証跡）")
            continue
        if str(node.get("result")).lower() != "pass":
            res.err(1, f"{where}.result が pass ではありません: {node.get('result')}")
        if to_date(node.get("date")) is None:
            res.err(1, f"{where}.date がないか YYYY-MM-DD ではありません")
        recorded = str(node.get("hash") or "")
        if not recorded:
            res.err(1, f"{where}.hash がありません（この正本で確かめた証跡か分からない。"
                       f"`publish_prep.py --hash` の値を、確かめたときに書く）")
        elif recorded != current_hash:
            res.err(1, f"{where}.hash（{recorded}）が今の正本のハッシュ（{current_hash}）と違います。"
                       f"確かめた後に正本が変わっています。確かめ直して書き直す")
    for name, node in checks.items():
        if name in required:
            continue
        result = node.get("result") if isinstance(node, dict) else node
        if str(result).lower() != "pass":
            res.warn(1, f"checks.{name} が pass ではありません: {result}")


def validate_receipt(art: Article, pc: PrepConfig, res: PrepResult, current_hash: str) -> Optional[dict]:
    """照合の合格の受領ファイル（data/checks/{slug}.json）を確かめる。"""
    from factcheck import RECEIPT_TOOL, read_receipt, receipt_path

    slug = str(art.fm("slug") or art.path.stem)
    if validate_slug(slug) or pc.checks_dir is None:
        return None
    path = receipt_path(pc.checks_dir, slug)
    try:
        data = read_receipt(pc.checks_dir, slug)
    except ToolError as exc:
        res.err(1, str(exc))
        return None
    if data is None:
        res.err(1, f"照合の受領ファイル {path} がありません（factcheck.py を関門モードで実行し、合格させる）")
        return None
    problems = []
    if data.get("result") != "pass" or data.get("mode") != "gate" or data.get("tool") != RECEIPT_TOOL:
        problems.append("合格（result: pass・mode: gate・tool: factcheck.py）の受領ファイルではありません")
    if data.get("slug") != slug:
        problems.append(f"受領ファイルの slug（{data.get('slug')}）が違います")
    if data.get("content_hash") != current_hash:
        problems.append(f"受領ファイルのハッシュ（{data.get('content_hash')}）が今の正本のハッシュ（{current_hash}）と違います。"
                        f"照合の後に正本が変わっています。factcheck.py をやり直す")
    for pr in problems:
        res.err(1, f"{path.name}: {pr}")
    return None if problems else data


def render_gate_summary(res: "PrepResult", lint, receipt: Optional[dict], pc: PrepConfig, cfg: dict[str, Any]) -> str:
    """実行者が公開の前に読む「関門の要約」（build/{slug}/gate-summary.md）。"""
    art = res.article
    slug = str(art.fm("slug") or art.path.stem)
    lines = [
        f"# 関門の要約：{slug}",
        "",
        f"- 生成日時: {datetime.now().isoformat(timespec='seconds')}",
        f"- 正本: {art.path}",
        f"- 正本のハッシュ: `{res.content_hash}`",
        "",
        "> **この成果物は下書き（status: draft）として WordPress に入れる。公開への切り替えは実行者だけが行う**"
        "（設計書 §7 工程10b）。",
        "> この関門は、古い記録の流用と記録漏れを機械で防ぐものであり、**偽造への防御ではない**"
        "（Claude はツールを実行し、記録のファイルも編集できる）。最後の保証は、実行者がこの要約と下書きを読んで公開することである。",
        "",
        "## lint（publish_prep が実行）",
        "",
        f"- 結果: {'合格' if lint and not lint.errors else '不合格'}（警告 {len(lint.warnings) if lint else '?'} 件）",
    ]
    if lint:
        lines.append(f"- nofact の行: {', '.join(f'L{n}' for n in lint.nofact_lines) or 'なし'}")
        lines.append(f"- calc-block: {', '.join(f'L{a}–L{b}' for a, b in lint.calc_blocks) or 'なし'}")
        for w in lint.warnings:
            lines.append(f"- 警告 L{w.line_no} {w.kind}: {w.number} — {w.detail}")
    lines += ["", "## 照合（受領ファイル）", ""]
    if receipt:
        for key in ("checked_at", "mode", "content_hash", "report_sha256", "report_path", "claims", "numbers",
                    "manual_suppressed", "coincidences_suppressed", "lint_warnings"):
            lines.append(f"- {key}: {receipt.get(key)}")
    else:
        lines.append("- 受領ファイルなし")
    lines += ["", "## 判定の証跡（front matter の checks）", "", "| 項目 | 結果 | 日付 | ハッシュの一致 |", "|---|---|---|---|"]
    checks = art.fm("checks") or {}
    if isinstance(checks, dict):
        for name, node in checks.items():
            node = node if isinstance(node, dict) else {}
            ok = "一致" if str(node.get("hash") or "") == res.content_hash else "不一致・なし"
            lines.append(f"| {name} | {node.get('result')} | {node.get('date')} | {ok} |")
    lines += ["", "## 手動確認の記録（data/sources/{slug}.md）".replace("{slug}", slug), ""]
    try:
        from manual_checks import (DEFAULT_DENIED_CHECKERS, DEFAULT_SESSION_CHECKERS, KIND_COINCIDENCE,
                                   parse_manual_records, sources_file_for)
        src = sources_file_for(pc.sources_dir, slug) if pc.sources_dir else None
        recs = []
        if src is not None and src.exists():
            recs, _w = parse_manual_records(
                src, cfg_get(cfg, "factcheck.manual_check_allowed_checkers", []) or [],
                cfg_get(cfg, "factcheck.manual_check_denied_checkers", None) or DEFAULT_DENIED_CHECKERS,
                cfg_get(cfg, "factcheck.manual_check_session_checkers", None) or DEFAULT_SESSION_CHECKERS)
        n_co = sum(1 for r in recs if r.kind == KIND_COINCIDENCE)
        lines.append(f"- 記録: {len(recs)} 件（不一致・取得失敗 {len(recs) - n_co} 件 / 偶然の一致 {n_co} 件。"
                     f"書式の誤りで無効 {sum(1 for r in recs if r.problem)} 件）")
        if recs:
            lines += ["", "| 種類 | URL | 数値 | 確認者 | 確認日 | 理由 |", "|---|---|---|---|---|---|"]
            for r in recs:
                lines.append(f"| {r.kind} | {r.url} | {r.number_raw} | {r.checker} | {r.checked_on_raw} | {r.reason} |")
    except ToolError as exc:
        lines.append(f"- 記録を読めません: {exc}")
    lines += [
        "",
        "## 偶然の一致（T7）の扱い",
        "",
        "- 照合は関門モードで、偶然の一致の候補が残っていれば合格にならない。受領ファイルがある＝候補はすべて記録で外れている",
        f"- 記録で外した候補: {receipt.get('coincidences_suppressed') if receipt else '?'} 件"
        f"（中身は上の「手動確認の記録」の種類「偶然の一致」の行。出典ページの本文が変わるまで有効）",
        "",
        "## 実行者が確かめること",
        "",
        "- 上の手動確認の記録の中身（特に「不一致」を外した理由）が妥当か",
        "- 表現・掲載ルール・推薦ルール（CLAUDE.md ルール7・10）",
        "- 下書きのプレビューで、表・CTA・広告表示・参考資料が崩れていないか",
        "- 公開後24時間以内に postcheck.py とスマホでの確認（設計書 §6 公開後）",
        "",
    ]
    return "\n".join(lines)


def _marker_only(line: str) -> bool:
    return bool(ANY_COMMENT_RE.search(line)) and not ANY_COMMENT_RE.sub("", line).strip()


def transform_body(art: Article, cta_map: Optional[dict[str, Any]], pc: PrepConfig,
                   res: PrepResult) -> list[str]:
    """マーカー除去・CTA置換・段落の改行をつなぐ。戻り値は Markdown の行。"""
    st = art.struct
    lines = art.lines
    n = len(lines)
    out: dict[int, Optional[str]] = {}
    unknown_comments = 0
    p7_titles = (pc.p7_heading, pc.references_heading, pc.history_heading)
    cta_lines: set[int] = set()

    for h in st.headings:
        text = remove_comments(h.text).strip()
        if any(text.startswith(t) for t in p7_titles):
            res.err(h.line_no, f"正本に P7 の見出し「{text}」があります。P7（参考資料・更新履歴）は"
                               f"publish_prep.py が自動生成するので正本に書かない")

    for idx in range(art.body_start, n):
        no = idx + 1
        line = lines[idx]
        if st.zones[idx] == "code":
            out[idx] = line
            continue
        if _marker_only(line):
            for c in ANY_COMMENT_RE.findall(line):
                if not MARKER_COMMENT_RE.match(c):
                    unknown_comments += 1
            prev = lines[idx - 1] if idx - 1 >= art.body_start else ""
            nxt = lines[idx + 1] if idx + 1 < n else ""
            between_items = (idx - 1) in st.list_item_lines and (idx + 1) in st.list_item_lines
            if not prev.strip() or not nxt.strip() or between_items:
                out[idx] = None             # 行ごと消す
            else:
                out[idx] = ""               # 前後のブロックがつながらないように空行にする
            continue
        text = line
        comments = ANY_COMMENT_RE.findall(line)
        if comments:
            unknown_comments += sum(1 for c in comments if not MARKER_COMMENT_RE.match(c))
            text = ANY_COMMENT_RE.sub("", line).rstrip()
        if "<!--" in text:
            res.err(no, "閉じていないHTMLコメントがあります")

        cta = CTA_RE.search(text)
        if cta:
            if len(CTA_RE.findall(text)) != 1 or text.strip() != cta.group(0):
                res.err(no, "{{CTA:…}} は単独の行に書く（同期パターンはブロック単位で置き換えるため）")
                out[idx] = text
                continue
            cta_lines.add(idx)
            pid = cta.group("id")
            if cta_map is None:              # postcheck 用の描画: CTA は描かない
                out[idx] = ""
                continue
            if pid not in cta_map:
                res.err(no, f"案件ID {pid} の同期パターンが data/cta_patterns.yaml にありません")
                out[idx] = text
                continue
            programs = [str(p) for p in (art.fm("programs") or [])]
            if programs and pid not in programs:
                res.warn(no, f"案件ID {pid} が front matter の programs にありません")
            res.ctas.append({"program_id": pid, "pattern_id": cta_map[pid], "line": no})
            out[idx] = pc.cta_template.format(pattern_id=cta_map[pid])
            continue
        out[idx] = text

    if unknown_comments:
        res.warn(0, f"マーカー以外のHTMLコメント {unknown_comments} 件を除去しました")

    # 段落（markdown-it の paragraph の範囲）の中だけ改行をつなぐ
    para_at = {p.start: p for p in st.paragraphs if p.end - p.start > 1}
    for p in st.paragraphs:
        if any(p.start <= c < p.end for c in cta_lines) and p.end - p.start > 1:
            res.err(p.start + 1, "{{CTA:…}} の前後に空行を入れる（段落の途中に置けない）")
    result: list[str] = []
    idx = art.body_start
    while idx < n:
        p = para_at.get(idx)
        if p is not None and st.zones[idx] != "code":
            cur: Optional[str] = None
            for j in range(p.start, p.end):
                t = out.get(j)
                if t is None:
                    continue
                if cur is None:
                    cur = t
                    continue
                if _hard_break(cur):
                    result.append(cur)
                    cur = t
                    continue
                cont = t.lstrip()
                if p.in_quote:
                    cont = re.sub(r"^(?:>\s?)+", "", cont)
                cur = join_text(cur, cont)
            if cur is not None:
                result.append(cur)
            idx = p.end
            continue
        t = out.get(idx)
        if t is not None:
            if idx in cta_lines and t:
                result += ["", t, ""]
            else:
                result.append(t)
        idx += 1

    collapsed: list[str] = []
    for t in result:
        if not t.strip():
            if collapsed and not collapsed[-1].strip():
                continue
            collapsed.append("")
        else:
            collapsed.append(t)
    while collapsed and not collapsed[0].strip():
        collapsed.pop(0)
    while collapsed and not collapsed[-1].strip():
        collapsed.pop()
    # 見えない書式文字（Cf 類）は出力に残さない（lint もエラーにする。二重の備え）
    collapsed = ["".join(ch for ch in t if unicodedata.category(ch) != "Cf") for t in collapsed]
    for i, t in enumerate(collapsed):
        if "{{" in t:
            res.err(0, f"{{{{ の置き換え漏れがあります: {t.strip()[:60]}")
    return collapsed


def render_html(md_text: str) -> str:
    try:
        from markdown_it import MarkdownIt  # type: ignore
    except ImportError as exc:
        raise ToolError(
            "markdown-it-py が未インストールです。`pip install -r tools/requirements.txt` を実行してください。"
        ) from exc
    md = MarkdownIt("commonmark", {"html": True}).enable("table")
    return md.render(md_text)


# 自己完結のブロックの区切り（例: CTA の同期パターンの参照 <!-- wp:block {"ref":123} /-->）だけの行
_SELF_CLOSING_BLOCK_LINE_RE = re.compile(r"^\s*<!--\s*wp:[a-z0-9-]+(?:/[a-z0-9-]+)?(?:\s+\{.*\})?\s*/-->\s*$")


def wrap_in_html_blocks(html: str) -> str:
    """本文のHTMLを「カスタムHTML」ブロック（core/html）で包む。

    区切りのない生のHTMLのままだと、ブロックエディタが読み込み時にクラシックブロックとして
    autop（段落の自動整形）をかけ、保存で本文が変わりうる。また WordPress は本文に区切りが
    1つもないときだけ表示時に wpautop をかけるので、CTA の有無で表示が変わる。
    包むことで、エディタでも表示でもHTMLがそのまま使われ、CTA の有無にかかわらず表示が揃う。
    同期パターンの参照などの区切りの行は包まずに残す。
    """
    out: list[str] = []
    seg: list[str] = []

    def flush() -> None:
        text = "\n".join(seg).strip("\n")
        if text.strip():
            out.append("<!-- wp:html -->\n" + text + "\n<!-- /wp:html -->")
        seg.clear()

    for line in html.split("\n"):
        if _SELF_CLOSING_BLOCK_LINE_RE.match(line):
            flush()
            out.append(line.strip())
        else:
            seg.append(line)
    flush()
    return "\n\n".join(out) + "\n"


def prepare(path: Path, cfg: dict[str, Any], cta_map: Optional[dict[str, Any]], *,
            gate: bool = True) -> PrepResult:
    """正本から成果物を作る。gate=False は品質ゲート（lint・checks）を確かめない（postcheck の比較用）。

    cta_map=None のときは {{CTA:…}} を描かない（postcheck の比較用）。
    """
    pc = PrepConfig.from_cfg(cfg)
    art = load_article(path)
    res = PrepResult(article=art)
    if art.encoding_note:
        res.warn(1, art.encoding_note)
    validate_front_matter(art, res)
    if art.front_matter is None:
        return res
    res.content_hash = content_hash(art)

    lint = None
    receipt = None
    if gate:
        from article_lint import LintConfig, lint_article
        lint = lint_article(path, LintConfig.from_cfg(cfg))
        if lint.errors:
            head = "; ".join(f"L{i.line_no} {i.kind}{(' ' + i.number) if i.number else ''}"
                             for i in lint.errors[:5])
            res.err(0, f"lint のエラーが {len(lint.errors)} 件あります（factcheck.py --lint で確かめる）: {head}"
                       + (" ほか" if len(lint.errors) > 5 else ""))
        receipt = validate_receipt(art, pc, res, res.content_hash)
        validate_checks(art, pc, res, res.content_hash)

    res.references = collect_references(art, res)
    body = transform_body(art, cta_map, pc, res)
    p7 = build_p7(art, res.references, pc, res)
    res.content_md = "\n".join(body + [""] + p7).rstrip() + "\n"
    res.content_html = render_html(res.content_md)

    if MARKER_COMMENT_RE.search(res.content_html):
        res.err(0, "出力のHTMLに出典マーカーが残っています（ツールの不具合の可能性）")
    if "{{" in res.content_html:
        res.err(0, "出力のHTMLに {{ が残っています")
    for p in find_affiliate_links_without_rel(res.content_html, pc.affiliate_domains):
        res.err(0, f"アフィリエイトリンクに rel=\"sponsored\"（または nofollow）がありません: "
                   f"{p.href}（リンクテキスト「{p.text}」、rel=\"{p.rel}\"）。§1-8")
    if pc.wrap_html_blocks:
        res.content_html = wrap_in_html_blocks(res.content_html)

    fm = art.front_matter or {}
    res.meta = {
        "slug": fm.get("slug"),
        "title": fm.get("title"),
        "description": fm.get("description"),
        "type": fm.get("type"),
        "keyword": fm.get("keyword"),
        "parent": fm.get("parent"),
        "industry": fm.get("industry"),
        "programs": fm.get("programs") or [],
        "published": date_str(fm.get("published")),
        "updated": date_str(fm.get("updated")),
        # 表示する更新日。WordPress 側でどの項目に入れるかは未確認（README の確認手順）
        "display_updated": date_str(fm.get("updated")),
        "verified": date_str(fm.get("verified")),
        "post_type": "post",       # 必ず「投稿」で作る（§1-3。固定ページだと広告表示が出ない）
        "status": "draft",
        "content_html_file": "content.html",
        "content_markdown_file": "content.md",
        "p7_links": str(fm.get("p7_links") or pc.reference_link_mode),
        "ctas": res.ctas,
        "references": [dataclasses.asdict(r) for r in res.references],
        "source": str(path),
        "content_hash": res.content_hash,
        "source_sha256": hashlib.sha256(art.raw.encode("utf-8")).hexdigest(),
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "warnings": [f"L{m.line_no}: {m.detail}" for m in res.warnings],
    }
    if gate:
        res.gate_summary = render_gate_summary(res, lint, receipt, pc, cfg)
    return res


def write_outputs(res: PrepResult, out_root: Path) -> Path:
    slug = str(res.meta.get("slug") or res.article.path.stem)
    problem = validate_slug(slug)
    if problem:
        raise ToolError(problem)
    out_dir = out_root / slug
    write_text(out_dir / "content.md", res.content_md)
    write_text(out_dir / "content.html", res.content_html)
    write_json(out_dir / "meta.json", res.meta)
    if res.gate_summary:
        write_text(out_dir / "gate-summary.md", res.gate_summary)
    return out_dir


def _expand_inputs(items: list[str]) -> list[Path]:
    paths: list[Path] = []
    for item in items:
        paths += sorted(Path().glob(item)) if any(ch in item for ch in "*?[") else [Path(item)]
    return paths


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="正本から WordPress に反映する成果物（content.html / content.md / meta.json）を作る",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("inputs", nargs="+", help="正本（articles/{slug}.md）")
    parser.add_argument("-c", "--config", help="設定ファイル（既定: tools/config.yaml）")
    parser.add_argument("--out-dir", help="出力先（既定: config の paths.build_dir、なければ build/）")
    parser.add_argument("--cta-patterns", help="CTA の対応表（既定: config の publish_prep.cta_patterns_file）")
    parser.add_argument("--hash", action="store_true",
                        help="正本のハッシュ（front matter の checks.*.hash に書く値）を表示して終わる")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    try:
        cfg = load_config(args.config)
        if args.hash:
            # ハッシュは「lint が合格し、照合の受領ファイルが今の正本と一致する」ときだけ出す（C35）
            from article_lint import LintConfig, lint_article
            pc = PrepConfig.from_cfg(cfg)
            rc = 0
            for path in _expand_inputs(args.inputs):
                if not path.exists():
                    LOG.error("正本が見つかりません: %s", path)
                    rc = 1
                    continue
                art = load_article(path)
                if art.front_matter is None:
                    LOG.error("%s: front matter を読めません: %s", path, art.front_matter_error)
                    rc = 1
                    continue
                h = content_hash(art)
                lint = lint_article(path, LintConfig.from_cfg(cfg))
                tmp = PrepResult(article=art)
                receipt = validate_receipt(art, pc, tmp, h)
                if lint.errors or receipt is None:
                    why = [f"lint のエラー {len(lint.errors)} 件"] if lint.errors else []
                    why += [m.detail for m in tmp.errors]
                    LOG.error("%s: 合格していないのでハッシュを出しません: %s", path, " / ".join(why))
                    rc = 1
                    continue
                sys.stdout.write(f"{path}\t{h}\n")
            return rc

        out_root = Path(args.out_dir) if args.out_dir else expand_path(cfg_get(cfg, "paths.build_dir", "../build"))
        cta_path = Path(args.cta_patterns) if args.cta_patterns else expand_path(
            cfg_get(cfg, "publish_prep.cta_patterns_file", "../data/cta_patterns.yaml"))
        cta_map = load_cta_patterns(cta_path)

        failed = 0
        for path in _expand_inputs(args.inputs):
            if not path.exists():
                LOG.error("正本が見つかりません: %s", path)
                failed += 1
                continue
            res = prepare(path, cfg, cta_map)
            for m in res.warnings:
                LOG.warning("%s:L%s %s", path, m.line_no, m.detail)
            for m in res.errors:
                LOG.error("%s:L%s %s", path, m.line_no, m.detail)
            if res.errors:
                LOG.error("%s: エラー %s 件のため成果物を書き出しません", path, len(res.errors))
                failed += 1
                continue
            out_dir = write_outputs(res, out_root)
            LOG.info("%s → %s", path, out_dir)
        return 1 if failed else 0
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
