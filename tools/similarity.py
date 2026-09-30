#!/usr/bin/env python
"""
similarity.py — 同じ商材の型B記事同士の類似度チェック（設計書 §8 T4・§4）。

front matter の type が B で、parent（親の型A）が同じ記事同士を比べる。
比べるのは **要件トレース表・判断基準チェックリスト（P3）・FAQ** の部分だけ
（同じ商材・同じ推薦製品の記事は、全文で比べると自然に類似度が高くなるため。§4）。

  1. 部分は正本の**目印**で取り出す（見出しの書き方に左右されないように）:
         <!-- part: trace -->     … 要件トレース表
         <!-- part: checklist --> … 判断基準チェックリスト（P3）
         <!-- part: faq -->       … FAQ（P5）
       それぞれ <!-- /part --> で閉じる。名前の一覧は config の similarity.parts。
       **目印がない型Bはエラー**（lint でも型Bに3つそろっているかを確かめる）
  2. 比べる前に、data/industries.csv の業種名をすべて共通の記号（mask_token）に置き換える
  3. 指標: 文字 n-gram の Jaccard（既定）または cosine。しきい値は config.yaml
     **しきい値の既定値 0.5 は暫定**。最初の型B記事10本の結果で決め直す（§0）

使い方
  python tools/similarity.py                       # config の paths.articles_dir 配下の全記事
  python tools/similarity.py articles/ --all       # しきい値未満の組も全部出す（しきい値を決めるとき）
  python tools/similarity.py articles/ --out tools/reports/similarity.md

終了コード: 0 = しきい値を超える組も目印の欠落もない / 1 = どちらかがある / 2 = 実行エラー
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import itertools
import logging
import math
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from common import ToolError, cfg_get, expand_path, load_config, setup_logging, write_text
from article import ANY_COMMENT_RE, load_article, part_ranges

LOG = logging.getLogger("affiliate.tools.similarity")

DEFAULT_PARTS = ["trace", "checklist", "faq"]


@dataclasses.dataclass
class SimConfig:
    metric: str = "jaccard"
    ngram: int = 3
    threshold: float = 0.5
    mask_token: str = "〓業種〓"
    parts: list[str] = dataclasses.field(default_factory=lambda: list(DEFAULT_PARTS))

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> "SimConfig":
        node = cfg_get(cfg, "similarity", {}) or {}
        sc = cls(
            metric=str(node.get("metric", "jaccard")),
            ngram=int(node.get("ngram", 3)),
            threshold=float(node.get("threshold", 0.5)),
            mask_token=str(node.get("mask_token", "〓業種〓")),
            parts=list(node.get("parts", DEFAULT_PARTS)),
        )
        if sc.metric not in ("jaccard", "cosine"):
            raise ToolError(f"similarity.metric は jaccard か cosine: {sc.metric}")
        if sc.ngram < 1:
            raise ToolError("similarity.ngram は1以上")
        return sc


# --------------------------------------------------------------------------
# 業種名
# --------------------------------------------------------------------------

def load_industry_names(path: Path) -> list[str]:
    """data/industries.csv の業種名（name 列。aliases 列があればそれも）を返す。長い順。"""
    if not path.exists():
        raise ToolError(f"業種の一覧が見つかりません: {path}（data/industries.csv）")
    names: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "name" not in reader.fieldnames:
            raise ToolError(f"{path} に name 列がありません（列: id,name,parent,name_en）")
        for row in reader:
            for key in ("name", "aliases"):
                value = (row.get(key) or "").strip()
                if not value:
                    continue
                for part in re.split(r"[|｜]", value):
                    part = unicodedata.normalize("NFKC", part).strip()
                    if part:
                        names.add(part)
    return sorted(names, key=len, reverse=True)


def mask_industries(text: str, names: list[str], token: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    if not names:
        return text
    rx = re.compile("|".join(re.escape(n) for n in names))   # 長い順に並べてあるので最長一致
    return rx.sub(token, text)


def normalize_for_similarity(text: str, names: list[str], mask_token: str) -> str:
    text = ANY_COMMENT_RE.sub(" ", text)
    text = mask_industries(text, names, mask_token)
    # Markdown の記号と空白を落とす（表の罫線や箇条書きの記号で似て見えないように）
    text = re.sub(r"^\s*(?:#{1,6}|[-*+]|\d+[.)]|>)\s*", "", text, flags=re.M)
    text = re.sub(r"\|?\s*:?-{3,}:?\s*", "", text)
    text = re.sub(r"[|*_`\[\]\s]+", "", text)
    return text


def ngrams(text: str, n: int) -> Counter:
    if len(text) < n:
        return Counter([text]) if text else Counter()
    return Counter(text[i:i + n] for i in range(len(text) - n + 1))


@dataclasses.dataclass
class Grams:
    """1つの文字列の n-gram（比べるたびに作り直さないように、記事ごとに1回だけ作る）。"""
    counts: Counter
    keys: frozenset
    norm: float

    @classmethod
    def of(cls, text: str, n: int) -> "Grams":
        c = ngrams(text, n)
        return cls(c, frozenset(c), math.sqrt(sum(v * v for v in c.values())))


def grams_similarity(a: Grams, b: Grams, metric: str) -> float:
    if not a.keys or not b.keys:
        return 0.0
    if metric == "cosine":
        small, large = (a, b) if len(a.keys) <= len(b.keys) else (b, a)
        dot = sum(v * large.counts.get(k, 0) for k, v in small.counts.items())
        return dot / (a.norm * b.norm) if a.norm and b.norm else 0.0
    inter = len(a.keys & b.keys)
    return inter / (len(a.keys) + len(b.keys) - inter)


def similarity(a: str, b: str, metric: str, n: int) -> float:
    return grams_similarity(Grams.of(a, n), Grams.of(b, n), metric)


# --------------------------------------------------------------------------
# 本体
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Doc:
    path: Path
    slug: str
    parent: str
    parts: dict[str, Optional[str]]     # 正規化後のテキスト（業種名は伏せ済み）
    grams: dict[str, Optional[Grams]] = dataclasses.field(default_factory=dict)
    combined: Optional[Grams] = None


@dataclasses.dataclass
class PairResult:
    parent: str
    a: Doc
    b: Doc
    combined: float
    per_part: dict[str, Optional[float]]
    over: bool


def extract_part(art, name: str) -> Optional[str]:
    ranges = part_ranges(art).get(name)
    if not ranges:
        return None
    s, e = ranges[0]
    text = "\n".join(art.lines[i - 1] for i in range(s, e + 1) if art.zones[i - 1] == "body")
    return text if text.strip() else None


def build_docs(paths: list[Path], sc: SimConfig, names: list[str]) -> tuple[list[Doc], list[str]]:
    """型Bの記事を読み、部分を取り出す。戻り値の2つ目はエラー（目印の欠落など）。"""
    docs, errors = [], []
    for path in paths:
        try:
            art = load_article(path)
        except ToolError as exc:
            errors.append(f"{path}: 読めません: {exc}")
            continue
        if art.front_matter is None:
            continue
        if str(art.fm("type") or "") != "B":
            continue
        parent = str(art.fm("parent") or "")
        if not parent:
            errors.append(f"{path}: 型Bだが front matter に parent がないため比べられません")
            continue
        parts: dict[str, Optional[str]] = {}
        for name in sc.parts:
            raw = extract_part(art, name)
            if raw is None:
                errors.append(f"{path}: <!-- part: {name} --> 〜 <!-- /part --> がありません"
                              f"（型Bの正本に必須。比べる部分を決められない）")
                parts[name] = None
            else:
                parts[name] = normalize_for_similarity(raw, names, sc.mask_token)
        doc = Doc(path=path, slug=art.slug, parent=parent, parts=parts)
        doc.grams = {n: (Grams.of(t, sc.ngram) if t else None) for n, t in parts.items()}
        doc.combined = Grams.of("\n".join(parts.get(n) or "" for n in sc.parts), sc.ngram)
        docs.append(doc)
    return docs, errors


def compare(docs: list[Doc], sc: SimConfig) -> list[PairResult]:
    groups: dict[str, list[Doc]] = {}
    for d in docs:
        groups.setdefault(d.parent, []).append(d)
    results: list[PairResult] = []
    for parent, group in groups.items():
        for a, b in itertools.combinations(sorted(group, key=lambda d: d.slug), 2):
            per: dict[str, Optional[float]] = {}
            for n in sc.parts:
                ga, gb = a.grams.get(n), b.grams.get(n)
                per[n] = grams_similarity(ga, gb, sc.metric) if ga and gb else None
            combined = grams_similarity(a.combined, b.combined, sc.metric) if a.combined and b.combined else 0.0
            results.append(PairResult(parent, a, b, combined, per, combined >= sc.threshold))
    results.sort(key=lambda r: -r.combined)
    return results


def render_report(results: list[PairResult], errors: list[str], sc: SimConfig,
                  n_docs: int, show_all: bool) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    over = [r for r in results if r.over]
    names = list(sc.parts)
    lines = [
        "# 型B記事の類似度チェック",
        "",
        f"生成日時: {now}",
        f"型B記事: {n_docs} 本 / 比べた組: {len(results)} / "
        f"**しきい値（{sc.threshold}、暫定）を超えた組: {len(over)}** / **エラー: {len(errors)} 件**",
        f"指標: 文字{sc.ngram}-gram の {sc.metric}。比べた部分: {', '.join(names)}（目印 <!-- part: … -->）。"
        f"業種名は「{sc.mask_token}」に置換済み",
        "",
    ]
    if errors:
        lines += ["## エラー（直すまで、この記事は正しく比べられない）", ""] + [f"- {e}" for e in errors] + [""]
    shown = results if show_all else (over + [r for r in results if not r.over][:10])
    if shown:
        header = "| 判定 | 親（parent） | 記事A | 記事B | 合計 | " + " | ".join(names) + " |"
        lines += [
            "## 組ごとの類似度" + ("" if show_all else "（超えた組＋未満の上位10組）"),
            "",
            header,
            "|" + "---|" * (5 + len(names)),
        ]
        for r in shown:
            cells = [("**超過**" if r.over else "—"), r.parent, r.a.slug, r.b.slug, f"{r.combined:.3f}"]
            cells += [("—" if r.per_part[n] is None else f"{r.per_part[n]:.3f}") for n in names]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    if over:
        lines += [
            "## 対応",
            "",
            "しきい値を超えた組は、トレース表の要件が業種固有になっているか（§4 合格ライン・差し替えテスト）を",
            "別セッションで判定し直す。推薦の結論が重なる場合は、要件が2行以上異なる必要がある。",
            "",
        ]
    return "\n".join(lines)


def collect_paths(inputs: list[str]) -> list[Path]:
    out: list[Path] = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            out += sorted(f for f in p.rglob("*.md"))
        elif p.exists():
            out.append(p)
        else:
            out += sorted(Path().glob(item))
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="同じ商材の型B記事同士で、トレース表・P3・FAQ の類似度を測る",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("inputs", nargs="*", help="記事ファイルまたはディレクトリ（既定: paths.articles_dir）")
    parser.add_argument("-c", "--config", help="設定ファイル（既定: tools/config.yaml）")
    parser.add_argument("--industries", help="業種の一覧（既定: similarity.industries_file）")
    parser.add_argument("--threshold", type=float, help="しきい値を一時的に変える")
    parser.add_argument("--all", action="store_true", help="全部の組を出す（しきい値を決めるとき）")
    parser.add_argument("-o", "--out", help="Markdownレポートの出力先")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    try:
        cfg = load_config(args.config)
        sc = SimConfig.from_cfg(cfg)
        if args.threshold is not None:
            sc.threshold = args.threshold
        inputs = args.inputs or [str(expand_path(cfg_get(cfg, "paths.articles_dir", "../articles")))]
        paths = collect_paths(inputs)
        if not paths:
            raise ToolError("比べる記事（.md）が見つかりません")
        ind_path = Path(args.industries) if args.industries else expand_path(
            cfg_get(cfg, "similarity.industries_file", "../data/industries.csv"))
        names = load_industry_names(ind_path)
        docs, errors = build_docs(paths, sc, names)
        results = compare(docs, sc)
        report = render_report(results, errors, sc, len(docs), args.all)
        if args.out:
            write_text(Path(args.out), report)
        else:
            sys.stdout.write(report)
        for e in errors:
            LOG.error("%s", e)
        return 1 if (errors or any(r.over for r in results)) else 0
    except ToolError as exc:
        LOG.error("%s", exc)
        return 2
    except Exception:
        LOG.exception("想定外のエラーで停止しました")
        return 2


if __name__ == "__main__":
    sys.exit(main())
