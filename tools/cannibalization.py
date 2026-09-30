#!/usr/bin/env python
"""
cannibalization.py — キーワードカニバリゼーション検出。

Search Console API から「検索クエリ × URL」を取得し、同一クエリに対して自サイトの
複数URLが表示されているケースを検出する。深刻度（クリックの分散度・順位の近さ・
検索ボリューム）でソートして出力する。

STRATEGY.md Phase 0-D / Phase 7「カニバリゼーション検出結果の処理」に対応。
業種特化ロングテール 200〜800本を積む戦略では、似たKWの記事が増えるほど
カニバリが起きやすい。放置すると本来1本で取れる順位を2本で食い合う。

初回セットアップ
--------------------------------------------------------------------------
    python tools/cannibalization.py --auth-only     # ブラウザで一度だけ同意
詳細な手順は tools/README.md「Google API のセットアップ」を参照。

使い方
--------------------------------------------------------------------------
  python tools/cannibalization.py                          # 直近28日
  python tools/cannibalization.py --days 90
  python tools/cannibalization.py --start 2026-06-01 --end 2026-06-30
  python tools/cannibalization.py --min-impressions 50 --top 50
  python tools/cannibalization.py --out tools/reports/cannibalization.md --csv out.csv

終了コード: 0 = 深刻なカニバリなし / 1 = 検出あり / 2 = 実行エラー
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import logging
import math
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from common import (
    ToolError,
    cfg_get,
    load_config,
    load_env,
    setup_logging,
    write_text,
)

LOG = logging.getLogger("affiliate.tools.cannibalization")


@dataclasses.dataclass
class PageStat:
    page: str
    clicks: float
    impressions: float
    ctr: float
    position: float


@dataclasses.dataclass
class Cannibal:
    query: str
    pages: list[PageStat]
    severity: float
    total_clicks: float
    total_impressions: float
    best_position: float
    position_gap: float
    click_dispersion: float

    @property
    def url_count(self) -> int:
        return len(self.pages)

    def verdict(self) -> str:
        """人が次に何をすべきかの目安。数値の解釈は README に記載。"""
        top = self.pages[0]
        share = (top.clicks / self.total_clicks) if self.total_clicks else 0.0
        if self.total_clicks == 0 and self.best_position > 20:
            return "様子見（まだ順位が低くクリックもない。カニバリ以前の問題）"
        if self.position_gap <= 3 and self.click_dispersion >= 0.35:
            return "統合候補（順位が近く、クリックも分散。弱い方を強い方に301 or 内部リンク集約）"
        if self.position_gap <= 5 and self.best_position <= 20:
            return "内部リンク整理（主軸URLへ内部リンクを集約し、副URLの該当KW最適化を外す）"
        if share >= 0.85:
            return "軽微（主軸URLがクリックをほぼ独占。監視のみ）"
        return "要確認（意図的な棲み分けか、事故かを判断する）"


def _resolve_dates(args: argparse.Namespace) -> tuple[str, str]:
    if args.start and args.end:
        return args.start, args.end
    if args.start or args.end:
        raise ToolError("--start と --end は両方指定してください")
    # GSC のデータ確定には2〜3日かかるため、終端を3日前に置く
    end = date.today() - timedelta(days=3)
    start = end - timedelta(days=args.days - 1)
    return start.isoformat(), end.isoformat()


def compute_severity(pages: list[PageStat]) -> tuple[float, float, float]:
    """深刻度・順位差・クリック分散度を返す。

    深刻度 = 検索ボリューム項 × クリック分散項 × 順位近接項 × URL数項
      * 検索ボリューム項: log10(1 + 表示回数) — 母数が大きいほど損失が大きい
      * クリック分散項:   1 - (最大URLのクリック占有率) — 食い合っているほど大きい
                          クリックが0のときは表示回数の占有率で代用
      * 順位近接項:       1 / (1 + 上位2URLの順位差) — 近いほど食い合っている
      * URL数項:          1 + 0.2 × (URL数 - 2) — 3本以上なら重み増し
    """
    ordered = sorted(pages, key=lambda p: (-p.clicks, p.position))
    total_clicks = sum(p.clicks for p in ordered)
    total_impr = sum(p.impressions for p in ordered)

    if total_clicks > 0:
        dispersion = 1.0 - (ordered[0].clicks / total_clicks)
    elif total_impr > 0:
        by_impr = sorted(ordered, key=lambda p: -p.impressions)
        dispersion = 1.0 - (by_impr[0].impressions / total_impr)
    else:
        dispersion = 0.0

    top_two = sorted(ordered, key=lambda p: p.position)[:2]
    gap = abs(top_two[0].position - top_two[1].position) if len(top_two) == 2 else 99.0
    closeness = 1.0 / (1.0 + gap)

    volume = math.log10(1.0 + total_impr)
    url_factor = 1.0 + 0.2 * max(0, len(ordered) - 2)

    severity = volume * dispersion * closeness * url_factor * 100.0
    return severity, gap, dispersion


def detect(
    rows: list[dict[str, Any]],
    min_impressions: float,
    min_page_impressions: float,
    max_position: float,
    exclude_paths: list[str],
) -> list[Cannibal]:
    by_query: dict[str, list[PageStat]] = {}
    skipped = 0
    for row in rows:
        keys = row.get("keys") or []
        if len(keys) < 2:
            skipped += 1
            continue
        query, page = keys[0], keys[1]
        if exclude_paths and any(x in page for x in exclude_paths):
            continue
        stat = PageStat(
            page=page,
            clicks=float(row.get("clicks", 0) or 0),
            impressions=float(row.get("impressions", 0) or 0),
            ctr=float(row.get("ctr", 0) or 0),
            position=float(row.get("position", 0) or 0),
        )
        if stat.impressions < min_page_impressions:
            continue
        by_query.setdefault(query, []).append(stat)

    if skipped:
        LOG.warning("キーが不足した行を %s 件スキップしました", skipped)

    results: list[Cannibal] = []
    for query, pages in by_query.items():
        if len(pages) < 2:
            continue
        total_impr = sum(p.impressions for p in pages)
        if total_impr < min_impressions:
            continue
        best = min(p.position for p in pages)
        if best > max_position:
            continue
        severity, gap, dispersion = compute_severity(pages)
        results.append(Cannibal(
            query=query,
            pages=sorted(pages, key=lambda p: (-p.clicks, p.position)),
            severity=severity,
            total_clicks=sum(p.clicks for p in pages),
            total_impressions=total_impr,
            best_position=best,
            position_gap=gap,
            click_dispersion=dispersion,
        ))

    results.sort(key=lambda c: -c.severity)
    return results


def _short(url: str, keep_host: bool = False) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    return f"{parsed.netloc}{path}" if keep_host else path


def render_report(
    results: list[Cannibal], site_url: str, start: str, end: str,
    total_rows: int, top: int,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# カニバリゼーション検出レポート",
        "",
        f"生成日時: {now}",
        f"対象プロパティ: `{site_url}`",
        f"集計期間: {start} 〜 {end}",
        f"取得行数: {total_rows:,} / **カニバリ検出: {len(results)} クエリ**",
        "",
    ]
    if not results:
        lines += [
            "閾値を超えるカニバリゼーションは検出されませんでした。",
            "",
            "検出が0件でも安心しないこと。`--min-impressions` を下げると小さな食い合いが見えます。",
            "",
        ]
        return "\n".join(lines)

    lines += [
        "## 深刻度上位",
        "",
        "| # | クエリ | URL数 | 表示 | クリック | 最高順位 | 順位差 | クリック分散 | 深刻度 | 対応目安 |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for i, c in enumerate(results[:top], start=1):
        lines.append(
            f"| {i} | {c.query} | {c.url_count} | {c.total_impressions:,.0f} | "
            f"{c.total_clicks:,.0f} | {c.best_position:.1f} | {c.position_gap:.1f} | "
            f"{c.click_dispersion:.2f} | **{c.severity:.1f}** | {c.verdict()} |"
        )
    lines.append("")

    lines += ["## 明細（上位のみ）", ""]
    for i, c in enumerate(results[:top], start=1):
        lines += [
            f"### {i}. 「{c.query}」 — 深刻度 {c.severity:.1f}",
            "",
            f"- 対応目安: **{c.verdict()}**",
            f"- 合計 表示 {c.total_impressions:,.0f} / クリック {c.total_clicks:,.0f}",
            "",
            "| URL | クリック | 表示 | CTR | 平均順位 |",
            "|---|---:|---:|---:|---:|",
        ]
        for p in c.pages:
            lines.append(
                f"| {_short(p.page)} | {p.clicks:,.0f} | {p.impressions:,.0f} | "
                f"{p.ctr * 100:.2f}% | {p.position:.1f} |"
            )
        lines.append("")

    lines += [
        "---",
        "",
        "## 指標の読み方",
        "",
        "- **クリック分散**: 0 に近いほど1本のURLがクリックを独占（＝実害が小さい）。",
        "  0.5 に近いほど半々に食い合っている（＝実害が大きい）",
        "- **順位差**: 上位2URLの平均掲載順位の差。3以内なら同じKWで正面衝突している",
        "- **深刻度**: 表示回数 × クリック分散 × 順位近接 × URL数 の合成スコア。相対比較用",
        "",
        "## 対応の手順",
        "",
        "1. 主軸にするURLを1本決める（順位・クリック・記事の網羅性で選ぶ）",
        "2. 副URLからそのKWの最適化を外す（タイトル・見出し・内部リンクのアンカーテキスト）",
        "3. 副URLから主軸URLへ内部リンクを張る",
        "4. 副URLが単独では価値を持たないなら、主軸へ統合して 301 リダイレクト",
        "5. Phase 7 の月次分析でこのレポートを回し、対応済みクエリが消えたか確認する",
        "",
    ]
    return "\n".join(lines)


def write_csv(path: Path, results: list[Cannibal]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "query", "severity", "url_count", "total_impressions", "total_clicks",
                "best_position", "position_gap", "click_dispersion", "verdict",
                "page", "page_clicks", "page_impressions", "page_ctr", "page_position",
            ])
            for c in results:
                for p in c.pages:
                    writer.writerow([
                        c.query, f"{c.severity:.2f}", c.url_count,
                        int(c.total_impressions), int(c.total_clicks),
                        f"{c.best_position:.2f}", f"{c.position_gap:.2f}",
                        f"{c.click_dispersion:.3f}", c.verdict(),
                        p.page, int(p.clicks), int(p.impressions),
                        f"{p.ctr:.4f}", f"{p.position:.2f}",
                    ])
    except OSError as exc:
        raise ToolError(f"CSV の書き出しに失敗しました: {path}: {exc}") from exc
    LOG.info("CSV を書き出しました: %s", path)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Search Console のデータからキーワードカニバリゼーションを検出する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-c", "--config", help="設定ファイル（既定: tools/config.yaml）")
    parser.add_argument("--site", help="GSCプロパティ（既定: 環境変数 GSC_SITE_URL）")
    parser.add_argument("--days", type=int, default=28, help="直近N日を集計（既定: 28）")
    parser.add_argument("--start", help="開始日 YYYY-MM-DD")
    parser.add_argument("--end", help="終了日 YYYY-MM-DD")
    parser.add_argument("--min-impressions", type=float,
                        help="クエリ合計の表示回数がこれ未満なら無視")
    parser.add_argument("--min-page-impressions", type=float,
                        help="個別URLの表示回数がこれ未満なら無視（ノイズ除去）")
    parser.add_argument("--max-position", type=float,
                        help="最高順位がこれより悪いクエリは無視")
    parser.add_argument("--top", type=int, default=30, help="レポートに出す上位件数")
    parser.add_argument("-o", "--out", help="Markdownレポートの出力先")
    parser.add_argument("--csv", dest="csv_out", help="CSVの出力先（全件）")
    parser.add_argument("--auth-only", action="store_true",
                        help="OAuth 同意フローだけを実行してトークンを保存する")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    try:
        load_env()
        from google_clients import build_search_console, get_credentials, gsc_query

        if args.auth_only:
            get_credentials(interactive=True)
            LOG.info("認証が完了しました。以降は無人実行できます。")
            return 0

        cfg = load_config(args.config)
        site_url = args.site or os.environ.get("GSC_SITE_URL") or cfg_get(cfg, "search_console.site_url")
        if not site_url:
            raise ToolError(
                "GSCプロパティが未設定です。環境変数 GSC_SITE_URL を設定するか --site を指定してください。\n"
                "  形式: ドメインプロパティ 'sc-domain:example.com' / "
                "URLプレフィックス 'https://example.com/'（末尾スラッシュ必須）"
            )

        min_impr = args.min_impressions if args.min_impressions is not None else \
            float(cfg_get(cfg, "cannibalization.min_impressions", 20))
        min_page_impr = args.min_page_impressions if args.min_page_impressions is not None else \
            float(cfg_get(cfg, "cannibalization.min_page_impressions", 3))
        max_pos = args.max_position if args.max_position is not None else \
            float(cfg_get(cfg, "cannibalization.max_position", 50))
        exclude_paths = list(cfg_get(cfg, "cannibalization.exclude_paths", []) or [])

        start, end = _resolve_dates(args)
        LOG.info("集計期間: %s 〜 %s / プロパティ: %s", start, end, site_url)

        creds = get_credentials(interactive=True)
        service = build_search_console(creds)
        rows = gsc_query(
            service, site_url, start, end, ["query", "page"],
            max_rows=int(cfg_get(cfg, "cannibalization.max_rows", 100000)),
        )
        if not rows:
            LOG.warning("データが0行でした。サイトが新しくインデックスされていないか、"
                        "期間・プロパティ指定が誤っている可能性があります。")

        results = detect(rows, min_impr, min_page_impr, max_pos, exclude_paths)
        LOG.info("カニバリ検出: %s クエリ（閾値: 表示>=%s, URL表示>=%s, 最高順位<=%s）",
                 len(results), min_impr, min_page_impr, max_pos)

        report = render_report(results, site_url, start, end, len(rows), args.top)
        if args.out:
            write_text(Path(args.out), report)
        else:
            sys.stdout.write(report)
        if args.csv_out:
            write_csv(Path(args.csv_out), results)

        return 1 if results else 0

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
