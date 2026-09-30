"""
manual_checks.py — 手動確認の記録（設計書 §8 T2・T7）。

照合で報告された組を、人（またはファクトチェックのセッション）が確認した記録を
`data/sources/{slug}.md` から読み、照合の報告から外す。

書式（`data/sources/README.md` と `_template.md` に同じ説明がある）
--------------------------------------------------------------------------
見出し「## 手動確認の記録」の直後の表。列の順番は自由だが、列の名前は固定。

    ## 手動確認の記録

    | URL | 数値 | 種類 | 確認者 | 確認日 | 理由 | ページのハッシュ | メモ |
    |---|---|---|---|---|---|---|---|
    | https://x.example.com/pricing | 300円 | 不一致 | 実行者の名前 | 2026-09-27 | JavaScript描画 | | ブラウザで確認 |
    | https://y.example.com/price | 0円 | 偶然の一致 | factcheck_session | 2026-09-27 | 偶然の一致の確認 | 3f9a0c1d2e4b5a67 | 初期費用の欄 |

  * 1行 = URL と数値の組 1つ。数値は**記事に書いた表記のまま**（値と単位で照合する。記事側が変われば無効）
  * 数値のない出典の URL そのものを確認した場合は、数値に `-`
  * 必須の列: URL・数値・確認者・確認日・理由。種類・ページのハッシュ・メモは任意（種類の既定は「不一致」）

記録の種類
  * 不一致（既定）… 不一致・取得失敗（robots.txt 拒否・JavaScript描画・リダイレクト）を確認した記録。
      確認者は factcheck.manual_check_allowed_checkers（実行者の名前。既定は空）の名前だけ有効。
      有効期限は確認日から factcheck.manual_check_valid_days（既定 90日）。
      外すもの: number_mismatch、url_error / empty_page / url_redirect（その出典の全数値に記録がある場合）、偶然の一致の候補
  * 偶然の一致 … 照合で一致したが T7 の候補になった組の文脈を確認した記録。
      確認者は許可リストの名前か factcheck.manual_check_session_checkers（既定 factcheck_session。
      別セッションのファクトチェックが書く）。**「ページのハッシュ」が必須**で、
      出典ページから抜き出した本文のハッシュ（照合のレポートに出る）が変わるまで有効（日数の期限はない）。
      外すもの: 偶然の一致の候補だけ

確認者の拒否: factcheck.manual_check_denied_checkers（既定 Claude・AI・GPT など）の語を**語として**含む名前は、
許可リストにあっても無効。2文字以下の語（AI）は大文字小文字を区別する（「Ai」という名前の人は拒否しない）。
"""

from __future__ import annotations

import dataclasses
import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional

from common import NumberToken, extract_numbers, format_decimal
from article import is_table_row, read_text_file, to_date

LOG = logging.getLogger("affiliate.tools.manual_checks")

SECTION_TITLE = "手動確認の記録"
REQUIRED_COLUMNS = ("URL", "数値", "確認者", "確認日", "理由")
OPTIONAL_COLUMNS = ("種類", "ページのハッシュ", "メモ")
DEFAULT_DENIED_CHECKERS = ("Claude", "AI", "GPT", "ChatGPT", "Gemini", "Copilot", "bot")
DEFAULT_SESSION_CHECKERS = ("factcheck_session",)
KIND_MISMATCH = "不一致"
KIND_COINCIDENCE = "偶然の一致"
KIND_ALIASES = {
    "": KIND_MISMATCH, "不一致": KIND_MISMATCH, "取得失敗": KIND_MISMATCH, "不一致・取得失敗": KIND_MISMATCH,
    "偶然の一致": KIND_COINCIDENCE, "偶然一致": KIND_COINCIDENCE,
}
NO_NUMBER = "-"
DEFAULT_VALID_DAYS = 90

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<text>.*?)\s*#*\s*$")
_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def heading_level(line: str) -> int:
    m = re.match(r"^\s{0,3}(#{1,6})\s", line)
    return len(m.group(1)) if m else 0


def heading_text(line: str) -> str:
    m = _HEADING_RE.match(line)
    return m.group("text") if m else ""


def is_table_separator(line: str) -> bool:
    return bool(_SEP_RE.match(line)) and "-" in line


def _words(name: str) -> list[str]:
    """名前の中の英字の語（ClaudeCode → Claude, Code／ChatGPT → Chat, GPT）。"""
    return re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+", name)


def denied_word(name: str, denied: Iterable[str]) -> Optional[str]:
    words = _words(name)
    for d in denied:
        d = str(d).strip()
        if not d:
            continue
        for w in words:
            if (w == d) if len(d) <= 2 else (w.lower() == d.lower()):
                return d
    return None


def checker_problem(name: str, allowed, denied, kind: str = KIND_MISMATCH,
                    session_checkers: Iterable[str] = DEFAULT_SESSION_CHECKERS) -> Optional[str]:
    """確認者の名前が有効でなければ、その理由。"""
    n = name.strip()
    if not n:
        return "確認者が空です"
    allowed_names = [str(a).strip() for a in allowed]
    session_names = [str(a).strip() for a in session_checkers]
    if kind == KIND_COINCIDENCE and n in session_names:
        return None
    d = denied_word(n, denied)
    if d:
        return f"確認者 {n} は手動確認の記録に使えません（「{d}」。人が確認した記録に限る）"
    if n not in allowed_names:
        extra = ("。偶然の一致の記録なら " + "・".join(session_names) + " も書ける"
                 if kind == KIND_COINCIDENCE and session_names else "")
        return (f"確認者 {n} が config.yaml の factcheck.manual_check_allowed_checkers にありません"
                f"（既定は空。実行者が自分の名前を入れる）{extra}")
    return None


@dataclasses.dataclass
class ManualRecord:
    source_file: Path
    line_no: int
    url: str
    number_raw: str
    checker: str
    checked_on_raw: str
    checked_on: Optional[date]
    reason: str = ""
    note: str = ""
    kind: str = KIND_MISMATCH
    page_hash: str = ""
    token: Optional[NumberToken] = None
    problem: Optional[str] = None          # 書式の誤り（あれば無効）
    used: bool = False

    @property
    def no_number(self) -> bool:
        return self.number_raw.strip() in (NO_NUMBER, "－", "ー", "—")

    def status(self, today: date, valid_days: int, page_hashes: Optional[dict[str, str]] = None) -> str:
        """valid / expired / future / invalid / page_changed / page_unknown"""
        if self.problem:
            return "invalid"
        assert self.checked_on is not None
        if self.checked_on > today:
            return "future"
        if self.kind == KIND_COINCIDENCE:
            current = (page_hashes or {}).get(self.url)
            if not current:
                return "page_unknown"
            return "valid" if current == self.page_hash else "page_changed"
        if today - self.checked_on > timedelta(days=valid_days):
            return "expired"
        return "valid"

    def expires_on(self, valid_days: int) -> Optional[date]:
        return self.checked_on + timedelta(days=valid_days) if self.checked_on else None

    def matches_number(self, token: NumberToken) -> bool:
        if self.token is None:
            return False
        return (format_decimal(self.token.value) == format_decimal(token.value)
                and self.token.unit == token.unit)


def normalize_url(url: str) -> str:
    return url.strip().strip("<>").rstrip("/")


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def parse_manual_records(path: Path, allowed_checkers=(), denied_checkers=DEFAULT_DENIED_CHECKERS,
                         session_checkers=DEFAULT_SESSION_CHECKERS
                         ) -> tuple[list[ManualRecord], list[str]]:
    """data/sources/{slug}.md から手動確認の記録を読む。戻り値: (記録, 書式の警告)"""
    text = read_text_file(path)
    lines = text.splitlines()
    warnings: list[str] = []

    start = None
    for i, line in enumerate(lines):
        if heading_level(line) and heading_text(line).strip() == SECTION_TITLE:
            start = i + 1
            break
    if start is None:
        return [], []

    # 見出しの後、次の見出しまでの最初の表
    rows: list[tuple[int, str]] = []
    in_table = False
    for j in range(start, len(lines)):
        line = lines[j]
        if heading_level(line):
            break
        if is_table_row(line):
            in_table = True
            rows.append((j + 1, line))
        elif in_table:
            break
    if not rows:
        return [], []

    header = _split_row(rows[0][1])
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        warnings.append(f"{path}: 「{SECTION_TITLE}」の表に列 {', '.join(missing)} がありません。記録を読めません")
        return [], warnings
    col = {name: header.index(name) for name in header}

    records: list[ManualRecord] = []
    for line_no, row in rows[1:]:
        if is_table_separator(row):
            continue
        cells = _split_row(row)
        if not any(cells):
            continue

        def cell(name: str) -> str:
            idx = col.get(name)
            return cells[idx] if idx is not None and idx < len(cells) else ""

        kind_raw = cell("種類")
        rec = ManualRecord(
            source_file=path, line_no=line_no,
            url=normalize_url(cell("URL")), number_raw=cell("数値"),
            checker=cell("確認者"), checked_on_raw=cell("確認日"),
            checked_on=to_date(cell("確認日")),
            reason=cell("理由"), note=cell("メモ"),
            kind=KIND_ALIASES.get(kind_raw, kind_raw), page_hash=cell("ページのハッシュ").strip("` "),
        )
        problems = []
        if rec.kind not in (KIND_MISMATCH, KIND_COINCIDENCE):
            problems.append(f"種類 '{kind_raw}' は「{KIND_MISMATCH}」か「{KIND_COINCIDENCE}」にする")
        if not re.match(r"^https?://", rec.url):
            problems.append("URL が http/https ではありません")
        cp = checker_problem(rec.checker, allowed_checkers, denied_checkers, rec.kind, session_checkers)
        if cp:
            problems.append(cp)
        if not rec.reason.strip():
            problems.append("理由が空です（robots.txt 拒否 / JavaScript描画 / リダイレクト / 偶然の一致の確認 など）")
        if rec.checked_on is None:
            problems.append(f"確認日 '{rec.checked_on_raw}' が YYYY-MM-DD ではありません")
        if rec.kind == KIND_COINCIDENCE and not re.fullmatch(r"[0-9a-f]{16}", rec.page_hash):
            problems.append("偶然の一致の記録には「ページのハッシュ」（照合のレポートに出る16文字）が必要です")
        if not rec.no_number:
            toks = extract_numbers(rec.number_raw, skip_year_like=False)
            if len(toks) != 1:
                problems.append(f"数値 '{rec.number_raw}' から数値を1つだけ読み取れません"
                                f"（1行に1つの数値を、記事の表記のまま書く）")
            else:
                rec.token = toks[0]
        if problems:
            rec.problem = "; ".join(problems)
            warnings.append(f"{path}:{line_no} 手動確認の記録が無効です: {rec.problem}")
        records.append(rec)
    return records, warnings


def sources_file_for(sources_dir: Path, slug: str) -> Path:
    return sources_dir / f"{slug}.md"


@dataclasses.dataclass
class ManualCheckOutcome:
    kept: list                 # 報告に残す Issue
    suppressed: list           # 記録によって外した Issue
    suppressed_coincidences: list
    kept_coincidences: list
    unused_records: list[ManualRecord]
    record_warnings: list[str]


def apply_manual_records(
    issues: list,
    claims: list,
    coincidences: list,
    records_by_file: dict[str, list[ManualRecord]],
    today: date,
    valid_days: int = DEFAULT_VALID_DAYS,
    record_warnings: Optional[list[str]] = None,
    page_hashes: Optional[dict[str, str]] = None,
) -> ManualCheckOutcome:
    """Issue / 偶然の一致のうち、有効な手動確認の記録がある組を外す。

    issues / claims / coincidences は factcheck.py の Issue / Claim / Coincidence。
    records_by_file のキーは記事ファイルのパス文字列。page_hashes は URL（normalize_url 済み）→
    今回取得した出典ページの本文のハッシュ（偶然の一致の記録の有効性に使う）。
    """
    page_hashes = page_hashes or {}
    claim_index: dict[tuple[str, int, str], list] = {}
    for c in claims:
        claim_index.setdefault((str(c.source_file), c.line_no, c.url), []).append(c)

    def status(rec: ManualRecord) -> str:
        return rec.status(today, valid_days, page_hashes)

    def find(file_key: str, url: str, token: Optional[NumberToken], kinds: tuple[str, ...]
             ) -> tuple[Optional[ManualRecord], list[ManualRecord]]:
        """有効な記録（あれば）と、一致したが無効な記録の一覧。"""
        valid = None
        stale: list[ManualRecord] = []
        for rec in records_by_file.get(file_key, []):
            if rec.url != normalize_url(url) or rec.kind not in kinds:
                continue
            if token is None:
                if not rec.no_number:
                    continue
            elif not rec.matches_number(token):
                continue
            if status(rec) == "valid":
                valid = rec
                break
            stale.append(rec)
        return valid, stale

    def stale_note(stale: list[ManualRecord]) -> str:
        notes = []
        for rec in stale:
            st = status(rec)
            if st == "expired":
                notes.append(f"手動確認の記録（{rec.checked_on}・{rec.checker}）は期限切れ"
                             f"（{valid_days}日。期限 {rec.expires_on(valid_days)}）。再確認が必要")
            elif st == "future":
                notes.append(f"手動確認の記録の確認日 {rec.checked_on} が未来の日付のため無効")
            elif st == "page_changed":
                notes.append(f"偶然の一致の記録（{rec.checked_on}）の後に出典ページの本文が変わった"
                             f"（記録 {rec.page_hash}・今 {page_hashes.get(rec.url)}）。確認し直す")
            elif st == "page_unknown":
                notes.append("偶然の一致の記録があるが、出典ページの本文を取得できず確かめられない")
            elif st == "invalid":
                notes.append(f"手動確認の記録（{rec.source_file.name}:{rec.line_no}）は書式の誤りで無効: {rec.problem}")
        return " / ".join(notes)

    kept, suppressed = [], []
    for issue in issues:
        file_key = str(issue.source_file)
        if issue.kind == "number_mismatch" and issue.number_value:
            token = NumberToken(raw=issue.number_raw, value=_dec(issue.number_value),
                                unit=issue.number_unit or None)
            rec, stale = find(file_key, issue.url, token, (KIND_MISMATCH,))
            if rec:
                rec.used = True
                issue.detail += f"（手動確認の記録あり: {rec.checked_on} {rec.checker}）"
                suppressed.append(issue)
                continue
            if stale:
                issue.detail += f"。{stale_note(stale)}"
            kept.append(issue)
            continue

        if issue.kind in ("url_error", "empty_page", "url_redirect"):
            claim_list = claim_index.get((file_key, issue.line_no, issue.url), [])
            tokens = [t for c in claim_list for t in c.numbers]
            used: list[ManualRecord] = []
            stale_all: list[ManualRecord] = []
            covered = bool(claim_list)
            if tokens:
                for t in tokens:
                    rec, stale = find(file_key, issue.url, t, (KIND_MISMATCH,))
                    stale_all += stale
                    if rec:
                        used.append(rec)
                    else:
                        covered = False
            elif claim_list:
                rec, stale = find(file_key, issue.url, None, (KIND_MISMATCH,))
                stale_all += stale
                if rec:
                    used.append(rec)
                else:
                    covered = False
            if covered:
                for rec in used:
                    rec.used = True
                issue.detail += "（全数値に手動確認の記録あり）"
                suppressed.append(issue)
                continue
            if stale_all:
                issue.detail += f"。{stale_note(stale_all)}"
            kept.append(issue)
            continue

        kept.append(issue)

    kept_co, supp_co = [], []
    for co in coincidences:
        rec, stale = find(str(co.source_file), co.url, co.token, (KIND_COINCIDENCE, KIND_MISMATCH))
        if rec:
            rec.used = True
            supp_co.append(co)
        else:
            if stale:
                co.reason += f"。{stale_note(stale)}"
            kept_co.append(co)

    unused = [r for recs in records_by_file.values() for r in recs
              if not r.used and status(r) == "valid"]
    return ManualCheckOutcome(kept, suppressed, supp_co, kept_co, unused, list(record_warnings or []))


def _dec(text: str):
    from decimal import Decimal
    return Decimal(text)
