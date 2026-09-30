"""
article.py — 正本（articles/{slug}.md）を読むための共通部品。

factcheck.py（照合・lint）、publish_prep.py、similarity.py、postcheck.py が使う。
記法の仕様は docs/article-template.md §1-2（出典の記法）と §8（T1〜T8）。

ここで扱うもの
  * 正本の読み込み（UTF-8。BOM・CRLF を吸収。行は "\\n" だけで分ける）
  * front matter（先頭の YAML）の分離と読み込み
  * 行の分類。**markdown-it のトークン**（CommonMark＋GFM の表）から作るので、
    WordPress に反映する HTML（publish_prep.py も markdown-it で変換する）と解釈が一致する
  * 出典マーカー・nofact・calc-block・part の正規表現
  * 「照合・lint の対象になる数値」を取り出す前処理、漢数字・禁止表現の検出
  * 正本のハッシュ（front matter の checks に記録する。§ README 11.4）
  * slug の検証、アフィリエイトリンクの rel 検査
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from common import NumberToken, ToolError, iter_number_matches

LOG = logging.getLogger("affiliate.tools.article")

# --------------------------------------------------------------------------
# マーカーの正規表現（factcheck.py もここから import する）
# --------------------------------------------------------------------------

# 本文は '>' を含まないため、'-->' が必ず終端になる。URLとメタは後段で分割する。
INLINE_SRC_RE = re.compile(r"<!--\s*src:\s*(?P<body>[^>]*?)\s*-->")
BLOCK_START_RE = re.compile(r"<!--\s*src-block:\s*(?P<body>[^>]*?)\s*-->")
BLOCK_END_RE = re.compile(r"<!--\s*/src-block\s*-->")
CALC_START_RE = re.compile(r"<!--\s*calc-block\s*-->")
CALC_END_RE = re.compile(r"<!--\s*/calc-block\s*-->")
NOFACT_RE = re.compile(r"<!--\s*nofact\s*-->")
# 型Bの比べる部分の目印（similarity.py・lint）。<!-- part: trace --> 〜 <!-- /part -->
PART_START_RE = re.compile(r"<!--\s*part:\s*(?P<name>[A-Za-z0-9_-]+)\s*-->")
PART_END_RE = re.compile(r"<!--\s*/part\s*-->")
FOOTNOTE_REF_RE = re.compile(r"\[\^(?P<key>[^\]\s]+)\]")
FOOTNOTE_DEF_RE = re.compile(
    r"^[ \t]*\[\^(?P<key>[^\]\s]+)\]:[ \t]*(?P<url>https?://\S+)[ \t]*(?P<meta>.*)$", re.M
)
# 定義行の URL が http でない場合も「脚注の使用」として検出するための緩い版
FOOTNOTE_ANY_DEF_RE = re.compile(r"^\s*\[\^[^\]\s]+\]:")
ACCESSED_RE = re.compile(r"accessed\s*=\s*(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})")
ACCESSED_ANY_RE = re.compile(r"accessed\s*=\s*(?P<value>\S*)")
ANY_COMMENT_RE = re.compile(r"(?s)<!--.*?-->")
MARKER_COMMENT_RE = re.compile(
    r"<!--\s*(?:src:|src-block:|/src-block\s*-->|calc-block\s*-->|/calc-block\s*-->|nofact\s*-->"
    r"|part:|/part\s*-->)"
)

TABLE_ROW_RE = re.compile(r"^\s*\|")
LIST_NUMBER_RE = re.compile(r"^(?P<indent>\s*)\d+[.)]\s+")
PLACEHOLDER_RE = re.compile(r"\{\{.*?\}\}")
CTA_RE = re.compile(r"\{\{\s*CTA\s*:\s*(?P<id>[^\s{}]+?)\s*\}\}")
BARE_URL_RE = re.compile(r"https?://[^\s<>()\[\]「」『』（）]+")
HTML_TAG_RE = re.compile(r"(?s)<[^>]+>")
HTML_TABLE_RE = re.compile(r"<\s*(?:table|thead|tbody|tr|td|th)\b", re.I)
CODE_SPAN_RE = re.compile(r"(?P<ticks>`+)(?P<body>.+?)(?P=ticks)")

SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                    *(f"lpt{i}" for i in range(1, 10))}

# 「1人あたり」「1施設あたり」の 1（数え上げの単位であって事実の主張ではない）
PER_UNIT_SUFFIX_DEFAULT = ("あたり", "当たり")


def split_marker_body(body: str) -> tuple[Optional[str], Optional[str]]:
    """マーカー本文 'URL accessed=YYYY-MM-DD' を (URL, 参照日) に分ける。

    URL が http/https でなければ (None, None) を返す（呼び出し側で記法ミスとして報告する）。
    """
    parts = body.strip().split(None, 1)
    if not parts:
        return None, None
    url = parts[0].rstrip(".,、。")
    if not re.match(r"^https?://", url):
        return None, None
    meta = parts[1] if len(parts) > 1 else ""
    accessed = ACCESSED_RE.search(meta)
    return url, (accessed.group("date") if accessed else None)


def validate_slug(slug: Any) -> Optional[str]:
    """slug が英小文字・数字・ハイフンだけで、Windows の予約名でないか。問題があればその説明。"""
    if not isinstance(slug, str) or not slug:
        return "slug がありません"
    if not SLUG_RE.fullmatch(slug):
        return f"slug は英小文字・数字・ハイフンだけにする（§1-4）: {slug!r}"
    if slug in WINDOWS_RESERVED:
        return f"slug に Windows の予約名は使えない（ファイル名にできない）: {slug}"
    return None


# --------------------------------------------------------------------------
# 読み込み
# --------------------------------------------------------------------------

def decode_bytes(data: bytes) -> tuple[str, Optional[str]]:
    """UTF-8（BOM可）で読む。読めなければ cp932 を試す。戻り値: (文字列, 文字コードの注意書き)"""
    try:
        return data.decode("utf-8-sig"), None
    except UnicodeDecodeError as exc:
        first = exc
    try:
        return data.decode("cp932"), (
            f"UTF-8 として読めなかったため cp932 として読みました（{first.reason} at byte {first.start}）。"
            f"正本は UTF-8 で保存する")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), (
            f"UTF-8 としても cp932 としても読めません（byte {first.start} 付近）。読めない文字を置き換えて続けました。"
            f"文字コードを確認する")


def read_text_file(path: Path) -> str:
    """ファイルを読む（UTF-8 / BOM / cp932）。文字コードの注意は警告ログに出す。"""
    text, note = read_text_with_note(path)
    if note:
        LOG.warning("%s: %s", path, note)
    return text


def read_text_with_note(path: Path) -> tuple[str, Optional[str]]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ToolError(f"ファイルの読み込みに失敗しました: {path}: {exc}") from exc
    text, note = decode_bytes(data)
    return normalize_newlines(text), note


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def split_lines(text: str) -> list[str]:
    """"\\n" だけで行に分ける（str.splitlines は U+2028 などでも分けてしまい、CommonMark とずれる）。"""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def split_front_matter(lines: list[str]) -> tuple[Optional[dict[str, Any]], int, Optional[str]]:
    """先頭の YAML front matter を読む。

    戻り値: (front matter の dict または None, front matter が占める行数, エラー文)
    1行目が '---' でなければ front matter なしとみなす。
    """
    if not lines or lines[0].strip() != "---":
        return None, 0, None
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return None, 0, "front matter が '---' で閉じられていません"
    yaml_text = "\n".join(lines[1:end])
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise ToolError(
            "PyYAML が未インストールです。`pip install -r tools/requirements.txt` を実行してください。"
        ) from exc
    try:
        data = yaml.safe_load(yaml_text) or {}
    except Exception as exc:  # yaml.YAMLError
        return None, end + 1, (f"front matter の YAML を読めません: {exc}"
                               f"（文字コードが UTF-8 でない場合にも起きる。文字コードを確認する）")
    if not isinstance(data, dict):
        return None, end + 1, "front matter のトップレベルがマッピングではありません"
    return data, end + 1, None


# --------------------------------------------------------------------------
# 構造（markdown-it のトークンから）
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Heading:
    line_no: int          # 見出しの文字がある行（1始まり）
    end_line: int         # 見出しの最後の行（Setext の下線を含む）
    level: int
    text: str             # インラインの中身（HTMLコメントを含みうる）
    raw: str


@dataclasses.dataclass
class Block:
    start: int            # 0始まりの行 index（含む）
    end: int              # 0始まりの行 index（含まない）


@dataclasses.dataclass
class CodeBlock(Block):
    kind: str = "fence"   # fence / code_block
    closed: bool = True


@dataclasses.dataclass
class Paragraph(Block):
    in_quote: bool = False
    in_list: bool = False


@dataclasses.dataclass
class Structure:
    zones: list[str]                                  # front_matter / code / body
    table_role: dict[int, str]                        # 行 index → header / sep / data
    tables: list[Block]
    headings: list[Heading]
    paragraphs: list[Paragraph]
    code_blocks: list[CodeBlock]
    ordered_item_first_lines: set[int]
    list_item_lines: set[int]
    list_items: list[tuple[Block, int]]               # (範囲, 入れ子の深さ)
    html_blocks: list[Block]


def _markdown_parser():
    try:
        from markdown_it import MarkdownIt  # type: ignore
    except ImportError as exc:
        raise ToolError(
            "markdown-it-py が未インストールです。`pip install -r tools/requirements.txt` を実行してください。"
        ) from exc
    return MarkdownIt("commonmark", {"html": True}).enable("table")


def _is_closing_fence(line: str, markup: str) -> bool:
    s = line.strip()
    return bool(s) and set(s) == {markup[0]} and len(s) >= len(markup)


def analyze_structure(lines: list[str], front_matter_lines: int = 0) -> Structure:
    """markdown-it で解析し、行ごとの役割を返す。front matter の行は空行として解析する。"""
    md = _markdown_parser()
    text = "\n".join([""] * front_matter_lines + lines[front_matter_lines:]) + "\n"
    tokens = md.parse(text)
    n = len(lines)
    zones = ["front_matter" if i < front_matter_lines else "body" for i in range(n)]
    table_role: dict[int, str] = {}
    tables: list[Block] = []
    headings: list[Heading] = []
    paragraphs: list[Paragraph] = []
    code_blocks: list[CodeBlock] = []
    ordered_first: set[int] = set()
    list_item_lines: set[int] = set()
    list_items: list[tuple[Block, int]] = []
    html_blocks: list[Block] = []

    quote_depth = 0
    list_stack: list[str] = []
    list_depth = 0
    for i, tok in enumerate(tokens):
        if tok.type == "blockquote_open":
            quote_depth += 1
        elif tok.type == "blockquote_close":
            quote_depth -= 1
        elif tok.type in ("bullet_list_open", "ordered_list_open"):
            list_stack.append(tok.type)
        elif tok.type in ("bullet_list_close", "ordered_list_close"):
            list_stack.pop()
        elif tok.type == "list_item_open":
            list_depth += 1
            if tok.map:
                s, e = tok.map
                list_items.append((Block(s, e), list_depth))
                list_item_lines.update(range(s, e))
                if list_stack and list_stack[-1] == "ordered_list_open":
                    ordered_first.add(s)
        elif tok.type == "list_item_close":
            list_depth -= 1
        elif tok.type in ("fence", "code_block") and tok.map:
            s, e = tok.map
            closed = True
            if tok.type == "fence":
                closed = e - 1 > s and e - 1 < n and _is_closing_fence(lines[e - 1], tok.markup)
            code_blocks.append(CodeBlock(s, min(e, n), kind=tok.type, closed=closed))
            for j in range(s, min(e, n)):
                zones[j] = "code"
        elif tok.type == "table_open" and tok.map:
            s, e = tok.map
            tables.append(Block(s, min(e, n)))
            for j in range(s, min(e, n)):
                table_role[j] = "data"
            table_role[s] = "header"
            if s + 1 < e:
                table_role[s + 1] = "sep"
        elif tok.type == "heading_open" and tok.map:
            s, e = tok.map
            inline = tokens[i + 1] if i + 1 < len(tokens) else None
            content = inline.content if inline is not None else ""
            headings.append(Heading(line_no=s + 1, end_line=e, level=int(tok.tag[1]),
                                    text=content, raw=lines[s] if s < n else ""))
        elif tok.type == "paragraph_open" and tok.map:
            s, e = tok.map
            paragraphs.append(Paragraph(s, min(e, n), in_quote=quote_depth > 0, in_list=bool(list_stack)))
        elif tok.type == "html_block" and tok.map:
            s, e = tok.map
            html_blocks.append(Block(s, min(e, n)))
    return Structure(zones, table_role, tables, headings, paragraphs, code_blocks,
                     ordered_first, list_item_lines, list_items, html_blocks)


# --------------------------------------------------------------------------
# 正本
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Article:
    path: Path
    raw: str
    lines: list[str]                       # 全行（front matter を含む。行番号 = index + 1）
    struct: Structure
    front_matter: Optional[dict[str, Any]]  # front matter が無ければ None
    front_matter_error: Optional[str] = None
    body_start: int = 0                    # 本文の最初の行の index
    encoding_note: Optional[str] = None

    @property
    def encoding_fatal(self) -> bool:
        """UTF-8 としても cp932 としても読めなかった（中身が壊れている）か。"""
        return bool(self.encoding_note) and self.encoding_note.startswith("UTF-8 としても")

    @property
    def zones(self) -> list[str]:
        return self.struct.zones

    @property
    def slug(self) -> str:
        fm = self.front_matter or {}
        slug = fm.get("slug")
        return str(slug) if slug else self.path.stem

    def fm(self, key: str, default: Any = None) -> Any:
        return (self.front_matter or {}).get(key, default)

    def body_lines(self) -> list[tuple[int, str]]:
        """(行番号, 行) のうち本文（front matter・コードブロック以外）だけ。"""
        return [(i + 1, line) for i, line in enumerate(self.lines) if self.zones[i] == "body"]

    def is_table_row(self, line_no: int) -> bool:
        return (line_no - 1) in self.struct.table_role

    def table_role(self, line_no: int) -> Optional[str]:
        return self.struct.table_role.get(line_no - 1)

    def heading_at(self, line_no: int) -> Optional[Heading]:
        for h in self.struct.headings:
            if h.line_no == line_no:
                return h
        return None


def load_article(path: Path) -> Article:
    raw, note = read_text_with_note(path)
    lines = split_lines(raw)
    fm, fm_lines, fm_error = split_front_matter(lines)
    if fm_error and note:
        fm_error += f"（{note}）"
    struct = analyze_structure(lines, fm_lines)
    return Article(path=path, raw=raw, lines=lines, struct=struct, front_matter=fm,
                   front_matter_error=fm_error, body_start=fm_lines, encoding_note=note)


def related_range(article: Article) -> Optional[tuple[int, int]]:
    """関連記事の区間（<!-- part: related --> 〜 <!-- /part -->）の目印の行 index (開始, 終了)。

    related がちょうど1つで、閉じていて、ほかの part と入れ子になっていないときだけ返す。
    それ以外（なし・重複・閉じていない・入れ子）は None。lint と content_hash が同じこの関数を使う。
    """
    found: list[tuple[int, int]] = []
    open_name: Optional[str] = None
    open_idx = -1
    broken = False
    for no, line in article.body_lines():
        idx = no - 1
        m = PART_START_RE.search(line)
        if m:
            if open_name is not None:
                if open_name == "related" or m.group("name") == "related":
                    broken = True
            else:
                open_name, open_idx = m.group("name"), idx
            continue
        if PART_END_RE.search(line):
            if open_name == "related":
                found.append((open_idx, idx))
            open_name = None
    if open_name == "related":
        broken = True
    if broken or len(found) != 1:
        return None
    return found[0]


def content_hash(article: Article) -> str:
    """正本のハッシュ（checks・受領ファイルに記録する値）。

    定義: sha256( front matter から checks を除いた dict を JSON（キー順・UTF-8）にしたもの
                  + "\\n---\\n" + 本文 ) の16進の先頭16文字。
          本文は front matter の後の全行を "\\n" でつなぎ、末尾の改行を除いたもの。ただし関連記事の区間
          （related_range が返す範囲）は、**開始と終了の目印の間の行だけ**を除く（目印の行そのものと位置は含める）。
          related が不正（重複・閉じていない・入れ子）なら何も除かない。
    checks 自体を含めないので、checks に書き込んでも値は変わらない（循環しない）。
    改行コード（CRLF/LF）と BOM の違いは値に影響しない。
    data/cta_patterns.yaml・config.yaml・data/terms.yaml など、正本の外のファイルの変更は値を変えない。
    """
    fm = dict(article.front_matter or {})
    fm.pop("checks", None)
    fm_json = json.dumps(fm, sort_keys=True, ensure_ascii=False, default=str)
    rr = related_range(article)
    kept = [article.lines[i] for i in range(article.body_start, len(article.lines))
            if not (rr and rr[0] < i < rr[1])]
    body = "\n".join(kept).rstrip("\n")
    return hashlib.sha256((fm_json + "\n---\n" + body).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# 行の判定（構造を持たない文字列用の簡易版。正本の判定は Structure を使う）
# --------------------------------------------------------------------------

def is_table_row(line: str) -> bool:
    return bool(TABLE_ROW_RE.match(line))


def strip_list_number(line: str) -> str:
    """番号付きリストの行頭の番号（'1. ' '2) '）を落とす。"""
    return LIST_NUMBER_RE.sub(lambda m: m.group("indent"), line, count=1)


def remove_comments(text: str) -> str:
    return ANY_COMMENT_RE.sub(" ", text)


def split_cells(row: str) -> list[str]:
    s = remove_comments(row).strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [re.sub(r"[*_`]", "", c).strip() for c in s.split("|")]


def load_terms(path: Optional[Path]) -> list[str]:
    """data/terms.yaml の決まった言い回し（「36協定」など）。長い順・NFKC。ファイルがなければ空。"""
    if path is None or not Path(path).exists():
        return []
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ToolError(f"決まった言い回しの一覧を読めません（文字コードを確認）: {path}: {exc}") from exc
    items = data.get("terms", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ToolError(f"{path} の terms はリストにする")
    terms = {unicodedata.normalize("NFKC", str(t)).strip() for t in items if str(t).strip()}
    return sorted(terms, key=len, reverse=True)


TERM_MASK = "〓"
DEFAULT_TERMS_FILE = Path(__file__).resolve().parent.parent / "data" / "terms.yaml"
_default_terms_cache: Optional[list[str]] = None


def default_terms() -> list[str]:
    """data/terms.yaml の語（config を読まずに関数を直接呼んだときの既定。lint・照合は config の paths.terms_file）。"""
    global _default_terms_cache
    if _default_terms_cache is None:
        try:
            _default_terms_cache = load_terms(DEFAULT_TERMS_FILE)
        except ToolError as exc:
            LOG.warning("%s", exc)
            _default_terms_cache = []
    return _default_terms_cache


def mask_terms(text: str, terms: Iterable[str]) -> str:
    """決まった言い回しを、同じ長さの記号に置き換える（中の数字を数値として扱わない）。結果は NFKC。"""
    text = unicodedata.normalize("NFKC", text)
    for t in terms:
        if t and t in text:
            text = text.replace(t, TERM_MASK * len(t))
    return text


def find_invisible(text: str) -> list[str]:
    """見えない書式文字（Unicode の Cf 類。U+200B〜200D・U+2060・U+FEFF・U+00AD など）の一覧。"""
    return sorted({f"U+{ord(ch):04X}" for ch in text if unicodedata.category(ch) == "Cf"})


IMG_MD_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\([^)]*\)")
IMG_ALT_RE = re.compile(r"<img\b[^>]*\balt\s*=\s*(?:\"(?P<a>[^\"]*)\"|'(?P<b>[^']*)')", re.I)


def image_alts(text: str) -> list[str]:
    """画像の alt の文字（Markdown の ![alt](...) と HTML の <img alt="...">）。"""
    out = [m.group("alt") for m in IMG_MD_RE.finditer(text)]
    out += [m.group("a") if m.group("a") is not None else m.group("b") for m in IMG_ALT_RE.finditer(text)]
    return [a for a in out if a]


def strip_md_keep_code(text: str) -> str:
    """Markdown の記法を落とす。インラインコードの**中身は残す**（数値を見落とさないため）。"""
    text = CODE_SPAN_RE.sub(lambda m: " " + m.group("body") + " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)       # 画像
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)    # リンク → テキストのみ
    text = re.sub(r"^[ \t]{0,3}#{1,6}[ \t]*", "", text, flags=re.M)  # 見出し
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)  # 強調
    return text


def is_per_unit_one(token: NumberToken, end: int, normalized: str,
                    suffixes: Iterable[str] = PER_UNIT_SUFFIX_DEFAULT) -> bool:
    """「1人あたり」「1施設あたり」「1ヶ月あたり」の 1 か。

    数値の直後に、助数詞0〜2文字（単位として数値に含まれた「人」「ヶ月」などは0文字）、
    その直後が「あたり」「当たり」の場合だけ（「1社限定で毎月」は当たらない）。
    """
    if token.value != 1 or "." in token.raw:
        return False
    alt = "|".join(re.escape(s) for s in suffixes)
    if not alt:
        return False
    rest = normalized[end:end + 8]
    return bool(re.match(r"^\s*[^\s\d、。,.，．（）()ぁ-ゖ]{0,2}(?:" + alt + ")", rest))


def checkable_numbers(
    text: str,
    ignore_patterns: Iterable[str],
    skip_years: bool,
    *,
    strip_list: bool = False,
    drop_per_unit_one: bool = False,
    per_unit_suffixes: Iterable[str] = PER_UNIT_SUFFIX_DEFAULT,
    terms: Iterable[str] = (),
) -> list[NumberToken]:
    """lint で「出典が必要な数値」として数える数値を取り出す。

    terms（data/terms.yaml の決まった言い回し）の中の数字は数えない。見えない文字は除いてから数える。

    除外するもの: HTMLコメント（マーカー含む）、{{ }} のプレースホルダ、URL、HTMLタグ、
    Markdown の記法（インラインコードの記号だけ。中身は数える）、
    （strip_list=True なら）番号付きリストの番号、ignore_patterns、（skip_years=True なら）西暦、
    （drop_per_unit_one=True なら）「1人あたり」の 1。
    """
    work = remove_comments(text)
    work = PLACEHOLDER_RE.sub(" ", work)
    if terms:
        work = mask_terms(work, terms)
    work = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", work)
    work = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", work)
    work = BARE_URL_RE.sub(" ", work)
    work = HTML_TAG_RE.sub(" ", work)
    if strip_list:
        work = strip_list_number(work)
    work = strip_md_keep_code(work)
    out: list[NumberToken] = []
    for tok, _start, end, normalized in iter_number_matches(
            work, skip_year_like=skip_years, ignore_patterns=list(ignore_patterns)):
        if drop_per_unit_one and is_per_unit_one(tok, end, normalized, per_unit_suffixes):
            continue
        out.append(tok)
    return out


# --------------------------------------------------------------------------
# 漢数字・禁止表現
# --------------------------------------------------------------------------

KANJI_DIGITS = "〇零一二三四五六七八九十百千万億兆"
DEFAULT_KANJI_COUNTERS = [
    "パーセント", "ポイント", "ヶ月", "か月", "カ月", "ケ月", "箇月", "年間", "週間", "日間", "時間", "分間",
    "施設", "店舗", "拠点", "種類", "項目", "段階", "通り",
    "円", "社", "人", "名", "件", "割", "倍", "年", "月", "週", "日", "分", "秒", "回", "個", "台", "本",
    "点", "位", "歳", "部", "枚", "冊", "院", "室", "席", "軒", "つ", "選", "問", "度", "期", "桁", "%", "％",
]
DEFAULT_KANJI_EXCLUSIONS = [
    "十分", "一部", "一度", "一時", "一日中", "一日も", "一年中", "一人ひとり", "一人一人", "一つ一つ", "一つひとつ", "一つ",
    "万人", "一歩", "一面", "一回り", "一口", "一本化", "一月遅れ",
]
# 漢数字が「一」だけのときに、エラーでなく警告にとどめる助数詞以外の単位（金額・割合）。これらはエラー
KANJI_MONEY_COUNTERS = ("円", "%", "％", "割", "パーセント")


@dataclasses.dataclass(frozen=True)
class KanjiHit:
    text: str
    severity: str          # error / warning


def find_kanji_hits(text: str, counters: Iterable[str] = DEFAULT_KANJI_COUNTERS,
                    exclusions: Iterable[str] = DEFAULT_KANJI_EXCLUSIONS,
                    terms: Optional[Iterable[str]] = None,
                    per_unit_suffixes: Iterable[str] = PER_UNIT_SUFFIX_DEFAULT) -> list[KanjiHit]:
    """漢数字＋単位・助数詞を、重さつきで返す（設計書 §1-2 数値の書き方）。

    * エラー: 二以上の漢数字（「三つ」「十日」「一万二千社」）、「一」でも金額・割合（「一円」「一割」）、
      「万・千・億」を含むもの、算用数字との混在（「1万5,000円」）
    * 警告: 「一」＋助数詞（「一人」「一日」「週一回」の「一回」）。普通の言い回しに多いため
    * 対象外: exclusions（漢数字の位置から当たり、**当たった範囲が漢数字＋助数詞の全体を含む**ものだけ。
      「一時」は「一時間」に当たらない）、terms（決まった言い回し）、「一人あたり」のように直後が「あたり」
    """
    text = remove_comments(text)
    text = mask_terms(text, default_terms() if terms is None else terms)
    text = BARE_URL_RE.sub(" ", text)
    alt = "|".join(re.escape(c) for c in sorted(set(counters), key=len, reverse=True))
    # 算用数字の直後の「万」「千」（「1万社」）は照合できる1つの数値なので対象外
    rx = re.compile(r"(?<![0-9.,])(?P<d>[" + KANJI_DIGITS + r"]+)(?P<c>" + alt + ")")
    excl = [re.compile(e) for e in exclusions]
    per_unit = "|".join(re.escape(x) for x in per_unit_suffixes)
    found: list[KanjiHit] = []
    for m in rx.finditer(text):
        covered = False
        for e in excl:
            em = e.match(text, m.start())
            if em and em.end() >= m.end():
                covered = True
                break
        if covered:
            continue
        digits, counter = m.group("d"), m.group("c")
        if digits == "一" and counter not in KANJI_MONEY_COUNTERS:
            if per_unit and re.match(r"^\s*(?:" + per_unit + ")", text[m.end():m.end() + 4]):
                continue
            found.append(KanjiHit(m.group(0), "warning"))
        else:
            found.append(KanjiHit(m.group(0), "error"))
    for m in re.finditer(r"\d[\d,]*\s*[万千億兆]\s*\d", text):
        found.append(KanjiHit(m.group(0), "error"))
    return found


def find_kanji_numbers(text: str, counters: Iterable[str] = DEFAULT_KANJI_COUNTERS,
                       exclusions: Iterable[str] = DEFAULT_KANJI_EXCLUSIONS,
                       terms: Optional[Iterable[str]] = None) -> list[str]:
    """find_kanji_hits のうち、エラーになるものの文字列だけ（互換用）。"""
    return [h.text for h in find_kanji_hits(text, counters, exclusions, terms) if h.severity == "error"]


DEFAULT_BANNED_EXPRESSIONS = [
    r"No\.?\s*1(?!\d)", r"ナンバー\s*ワン", r"(?<![\d.])1\s*位", r"第\s*1\s*位", r"一位",
    r"最安", r"業界\s*唯一", r"一番\s*おすすめ", r"いちばん\s*おすすめ",
    r"実際に使ってみ", r"使ってみた", r"試してみた", r"試したところ", r"導入して感じ",
]
DEFAULT_CAUTION_EXPRESSIONS = [r"必ず", r"絶対", r"確実に", r"他社より安"]
DEFAULT_COMPARISON_EXPRESSIONS = [r"最も\s*(?:低|安|高|多|少|早|速|長|短)"]


def find_expressions(text: str, patterns: Iterable[str], terms: Optional[Iterable[str]] = None) -> list[str]:
    """NFKC で正規化した text に当たる表現（大文字小文字を区別しない）。terms（既定 data/terms.yaml）の中は見ない。"""
    norm = mask_terms(remove_comments(text), default_terms() if terms is None else terms)
    norm = BARE_URL_RE.sub(" ", norm)
    out = []
    for p in patterns:
        m = re.search(p, norm, re.I)
        if m:
            out.append(m.group(0))
    return out


# --------------------------------------------------------------------------
# 見出しと区間
# --------------------------------------------------------------------------

def list_headings(article: Article) -> list[Heading]:
    return list(article.struct.headings)


def section_range(article: Article, heading: Heading) -> tuple[int, int]:
    """見出しの区間 [見出しの次の行, 次の同格以上の見出しの手前] を行番号で返す（両端含む）。"""
    end = len(article.lines)
    for h in article.struct.headings:
        if h.line_no > heading.line_no and h.level <= heading.level:
            end = h.line_no - 1
            break
    return heading.end_line + 1, end


def child_headings(article: Article, heading: Heading) -> list[Heading]:
    start, end = section_range(article, heading)
    return [h for h in article.struct.headings
            if start <= h.line_no <= end and h.level == heading.level + 1]


def part_ranges(article: Article) -> dict[str, list[tuple[int, int]]]:
    """<!-- part: name --> 〜 <!-- /part --> の範囲（中身の行番号、両端含む）。入れ子は考えない。"""
    out: dict[str, list[tuple[int, int]]] = {}
    current: Optional[tuple[str, int]] = None
    for no, line in article.body_lines():
        m = PART_START_RE.search(line)
        if m:
            current = (m.group("name"), no)
            continue
        if PART_END_RE.search(line) and current:
            out.setdefault(current[0], []).append((current[1] + 1, no - 1))
            current = None
    return out


# --------------------------------------------------------------------------
# 日付
# --------------------------------------------------------------------------

def to_date(value: Any) -> Optional[date]:
    """YAML の日付（date 型）または 'YYYY-MM-DD' 文字列を date にする。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def date_str(value: Any) -> Optional[str]:
    d = to_date(value)
    return d.isoformat() if d else (str(value) if value not in (None, "") else None)


# --------------------------------------------------------------------------
# アフィリエイト（ASP）の台帳・リンク
# --------------------------------------------------------------------------

def load_asp_programs(path: Path) -> dict[str, dict[str, str]]:
    """data/asp-programs.csv を program_id → 行 の dict にする。"""
    import csv

    if not path.exists():
        raise ToolError(f"案件の一覧が見つかりません: {path}（data/asp-programs.csv）")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "program_id" not in reader.fieldnames:
            raise ToolError(f"{path} に program_id 列がありません")
        return {(row.get("program_id") or "").strip(): row for row in reader
                if (row.get("program_id") or "").strip()}


def split_domains(value: str) -> list[str]:
    return [d.strip() for d in re.split(r"[|\s,]+", value or "") if d.strip()]


@dataclasses.dataclass
class LinkProblem:
    href: str
    text: str
    rel: str


def host_matches(host: str, domains: Iterable[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    for d in domains:
        d = str(d).lower().strip().lstrip(".")
        if d and (host == d or host.endswith("." + d)):
            return True
    return False


def find_affiliate_links_without_rel(html: str, domains: Iterable[str]) -> list[LinkProblem]:
    """既知のアフィリエイトのドメインへのリンクで、rel に sponsored も nofollow もないもの。"""
    domains = list(domains)
    if not domains:
        return []
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError as exc:
        raise ToolError(
            "beautifulsoup4 が未インストールです。`pip install -r tools/requirements.txt` を実行してください。"
        ) from exc
    soup = BeautifulSoup(html, "html.parser")
    problems: list[LinkProblem] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if not host_matches(urlparse(href).hostname or "", domains):
            continue
        rel = a.get("rel") or []
        if isinstance(rel, str):
            rel = rel.split()
        rel_l = [r.lower() for r in rel]
        if "sponsored" not in rel_l and "nofollow" not in rel_l:
            problems.append(LinkProblem(href=href, text=a.get_text(" ", strip=True)[:60],
                                        rel=" ".join(rel)))
    return problems
