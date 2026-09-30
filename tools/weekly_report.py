#!/usr/bin/env python
"""
weekly_report.py — 週次レポート自動生成（GA4 + Search Console）。

STRATEGY.md Phase 0-D / Phase 6 / §14「継続性の設計」に対応。

**このスクリプトの設計上の要件**
    この事業は収益ゼロの期間が 9〜12ヶ月続く。収益だけを見ていると必ず離脱する。
    したがって本レポートは、収益より先に**先行指標**（表示回数・10位以内KW数・
    インデックス数・参照ドメイン数）を、**前週比・前月比つきで最上部に**置く。
    「収益0円だが表示回数が前月比 +180%」を可視化することが本ツールの主目的である。

出力
--------------------------------------------------------------------------
  * Markdown レポート 1枚（tools/reports/weekly-YYYY-Www.md）
  * PROGRESS.md の「週次記録」テーブルに追記できる1行（--append-progress で自動追記）

取得できない指標
--------------------------------------------------------------------------
  * 参照ドメイン数 … 無料APIがないため手入力（--referring-domains）。
                      Ahrefs / Search Console の「リンク」画面から転記する。
  * 確定収益・新規記事数・リライト数 … ASP管理画面と実作業ログから手入力。
  * インデックス数 … GSC の「ページ」レポートの正確な値はAPIで取れないため、
                      既定では「表示回数のあったユニークURL数」を代理値として出す。
                      正確な値は --indexed-pages で上書きできる。

使い方
--------------------------------------------------------------------------
  python tools/weekly_report.py
  python tools/weekly_report.py --end 2026-08-16 --new-articles 6 --rewrites 2 \\
      --referring-domains 12 --revenue 0
  python tools/weekly_report.py --append-progress
  python tools/weekly_report.py --no-ga4          # GA4未設定でも動かす

終了コード: 0 = 成功 / 2 = 実行エラー
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from common import (
    PROJECT_DIR,
    ToolError,
    cfg_get,
    expand_path,
    format_change,
    load_config,
    load_env,
    pct_change,
    setup_logging,
    write_text,
)

LOG = logging.getLogger("affiliate.tools.weekly_report")


@dataclasses.dataclass
class Period:
    label: str
    start: date
    end: date

    @property
    def start_s(self) -> str:
        return self.start.isoformat()

    @property
    def end_s(self) -> str:
        return self.end.isoformat()

    def __str__(self) -> str:
        return f"{self.start_s}〜{self.end_s}"


@dataclasses.dataclass
class Metrics:
    """1期間分の数字。取得できなかった指標は None のままにする（0 と区別する）。"""
    impressions: Optional[float] = None
    clicks: Optional[float] = None
    avg_position: Optional[float] = None
    top10_keywords: Optional[float] = None
    total_keywords: Optional[float] = None
    indexed_pages: Optional[float] = None
    sessions: Optional[float] = None
    pageviews: Optional[float] = None
    referring_domains: Optional[float] = None

    @property
    def ctr(self) -> Optional[float]:
        if self.impressions in (None, 0) or self.clicks is None:
            return None
        return self.clicks / self.impressions * 100.0


# --------------------------------------------------------------------------
# 期間の決定
# --------------------------------------------------------------------------

def build_periods(end: date, days: int) -> tuple[Period, Period, Period]:
    """今週 / 前週 / 前月（4週前の同じ長さ）の3期間を作る。"""
    current = Period("今週", end - timedelta(days=days - 1), end)
    prev_end = current.start - timedelta(days=1)
    previous = Period("前週", prev_end - timedelta(days=days - 1), prev_end)
    month_end = end - timedelta(days=28)
    month_ago = Period("前月同週", month_end - timedelta(days=days - 1), month_end)
    return current, previous, month_ago


def default_end_date(lag_days: int) -> date:
    """GSC のデータ確定には2〜3日かかるため、既定では lag_days 日前を終端にする。"""
    return date.today() - timedelta(days=lag_days)


# --------------------------------------------------------------------------
# Search Console
# --------------------------------------------------------------------------

def fetch_gsc_metrics(service: Any, site_url: str, period: Period, gsc_query: Any) -> Metrics:
    m = Metrics()

    # 全体のサマリ（ディメンションなし）
    try:
        rows = gsc_query(service, site_url, period.start_s, period.end_s, [], row_limit=1, max_rows=1)
        if rows:
            r = rows[0]
            m.impressions = float(r.get("impressions", 0) or 0)
            m.clicks = float(r.get("clicks", 0) or 0)
            m.avg_position = float(r.get("position", 0) or 0)
        else:
            LOG.warning("%s (%s) の GSC サマリが0行でした", period.label, period)
            m.impressions, m.clicks = 0.0, 0.0
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"GSC サマリの取得に失敗しました ({period}): {exc}") from exc

    # クエリ別（10位以内KW数）
    try:
        qrows = gsc_query(service, site_url, period.start_s, period.end_s, ["query"], max_rows=50000)
        m.total_keywords = float(len(qrows))
        m.top10_keywords = float(sum(1 for r in qrows if float(r.get("position", 999) or 999) <= 10.0))
    except Exception as exc:
        LOG.error("クエリ別データの取得に失敗しました (%s): %s", period, exc)

    # ページ別（インデックス数の代理値 = 表示のあったユニークURL数）
    try:
        prows = gsc_query(service, site_url, period.start_s, period.end_s, ["page"], max_rows=50000)
        m.indexed_pages = float(len(prows))
    except Exception as exc:
        LOG.error("ページ別データの取得に失敗しました (%s): %s", period, exc)

    return m


# --------------------------------------------------------------------------
# GA4
# --------------------------------------------------------------------------

def fetch_ga4_metrics(client: Any, property_id: str, period: Period, metrics: Metrics) -> Metrics:
    try:
        from google.analytics.data_v1beta.types import (  # type: ignore
            DateRange,
            Metric,
            RunReportRequest,
        )
    except ImportError as exc:
        raise ToolError(
            "google-analytics-data が未インストールです。"
            "`pip install google-analytics-data` を実行するか --no-ga4 を指定してください。"
        ) from exc

    request = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=period.start_s, end_date=period.end_s)],
        metrics=[Metric(name="sessions"), Metric(name="screenPageViews")],
    )
    try:
        response = client.run_report(request)
    except Exception as exc:
        text = str(exc)
        hint = ""
        if "403" in text or "PERMISSION" in text.upper():
            hint = ("\n  → GA4 Data API が有効化されていないか、認証アカウントに"
                    "当該プロパティの閲覧権限がありません。")
        elif "404" in text or "NOT_FOUND" in text.upper():
            hint = "\n  → GA4_PROPERTY_ID が誤っています（数字のみのプロパティIDを指定）。"
        raise ToolError(f"GA4 の取得に失敗しました ({period}): {text}{hint}") from exc

    if not response.rows:
        LOG.warning("GA4 (%s) のデータが0行でした", period)
        metrics.sessions, metrics.pageviews = 0.0, 0.0
        return metrics

    values = response.rows[0].metric_values
    try:
        metrics.sessions = float(values[0].value or 0)
        metrics.pageviews = float(values[1].value or 0)
    except (IndexError, ValueError) as exc:
        LOG.error("GA4 のレスポンス解析に失敗しました: %s", exc)
    return metrics


# --------------------------------------------------------------------------
# レポート描画
# --------------------------------------------------------------------------

def _fmt(value: Optional[float], digits: int = 0, suffix: str = "") -> str:
    if value is None:
        return "（未取得）"
    if digits == 0:
        return f"{value:,.0f}{suffix}"
    return f"{value:,.{digits}f}{suffix}"


def render_report(
    current: Period, previous: Period, month_ago: Period,
    cur: Metrics, prev: Metrics, mon: Metrics,
    site_url: str, manual: dict[str, Optional[float]], notes: str,
) -> str:
    week_label = f"{current.end.isocalendar()[0]}-W{current.end.isocalendar()[1]:02d}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        f"# 週次レポート {week_label}",
        "",
        f"生成日時: {now}",
        f"集計期間: **{current}**（{ (current.end - current.start).days + 1 } 日間）",
        f"比較対象: 前週 {previous} / 前月同週 {month_ago}",
        f"プロパティ: `{site_url}`",
        "",
        "---",
        "",
        "## 先行指標（収益より先にここを見る）",
        "",
        "> 収益ゼロの期間は 9〜12ヶ月続く前提。収益ではなく下の4指標が伸びていれば前進している。",
        "> STRATEGY.md §14「継続性の設計」に基づく必須セクション。",
        "",
        "| 指標 | 今週 | 前週比 | 前月同週比 |",
        "|---|---:|---|---|",
    ]

    leading = [
        ("**表示回数**", cur.impressions, prev.impressions, mon.impressions, 0, False),
        ("**10位以内KW数**", cur.top10_keywords, prev.top10_keywords, mon.top10_keywords, 0, False),
        ("**インデックス数（表示のあったURL数）**", cur.indexed_pages, prev.indexed_pages,
         mon.indexed_pages, 0, False),
        ("**参照ドメイン数**", cur.referring_domains, prev.referring_domains,
         mon.referring_domains, 0, False),
    ]
    for name, c, p, m, digits, invert in leading:
        lines.append(
            f"| {name} | {_fmt(c, digits)} | {format_change(c, p, invert)} | "
            f"{format_change(c, m, invert)} |"
        )
    lines.append("")

    # 一言サマリ（最も伸びた先行指標を目立たせる）
    highlights = []
    for name, c, p, m, _d, _i in leading:
        change = pct_change(c, m)
        if change is not None and change > 0:
            highlights.append((change, name.strip("*"), change))
    if highlights:
        highlights.sort(reverse=True)
        best = highlights[0]
        lines += [
            f"> **今週のハイライト: {best[1]} が前月同週比 {best[2]:+.0f}%。**",
            "> 収益が0でもこれは前進。撤退判定は STRATEGY.md Phase 10 の数字だけで行う。",
            "",
        ]
    else:
        lines += [
            "> 先行指標に前月比プラスの項目がありません。2週続く場合は KW選定（Phase 3）と",
            "> 技術SEO（Phase 5）を点検すること。ただし3ヶ月未満のサイトでは正常な範囲。",
            "",
        ]

    lines += [
        "---",
        "",
        "## 全指標",
        "",
        "| 指標 | 今週 | 前週 | 前週比 | 前月同週 | 前月同週比 |",
        "|---|---:|---:|---|---:|---|",
    ]

    rows: list[tuple[str, Optional[float], Optional[float], Optional[float], int, bool, str]] = [
        ("インデックス数（代理値）", cur.indexed_pages, prev.indexed_pages, mon.indexed_pages, 0, False, ""),
        ("表示回数", cur.impressions, prev.impressions, mon.impressions, 0, False, ""),
        ("クリック数", cur.clicks, prev.clicks, mon.clicks, 0, False, ""),
        ("CTR", cur.ctr, prev.ctr, mon.ctr, 2, False, "%"),
        ("平均掲載順位", cur.avg_position, prev.avg_position, mon.avg_position, 1, True, ""),
        ("10位以内KW数", cur.top10_keywords, prev.top10_keywords, mon.top10_keywords, 0, False, ""),
        ("検出KW総数", cur.total_keywords, prev.total_keywords, mon.total_keywords, 0, False, ""),
        ("セッション", cur.sessions, prev.sessions, mon.sessions, 0, False, ""),
        ("PV", cur.pageviews, prev.pageviews, mon.pageviews, 0, False, ""),
        ("参照ドメイン数", cur.referring_domains, prev.referring_domains, mon.referring_domains, 0, False, ""),
    ]
    for name, c, p, m, digits, invert, suffix in rows:
        lines.append(
            f"| {name} | {_fmt(c, digits, suffix)} | {_fmt(p, digits, suffix)} | "
            f"{format_change(c, p, invert)} | {_fmt(m, digits, suffix)} | "
            f"{format_change(c, m, invert)} |"
        )
    lines += [
        "",
        "※ 平均掲載順位は**小さいほど良い**ため、▲ が改善を意味する。",
        "※ インデックス数は「期間中に表示回数が1回以上あったユニークURL数」の代理値。"
        "Search Console の「ページのインデックス登録」の正確な値は API で取得できないため、"
        "厳密な値が必要な場合は `--indexed-pages` で手入力する。",
        "",
    ]

    lines += [
        "---",
        "",
        "## 手入力項目",
        "",
        "| 項目 | 値 | 取得元 |",
        "|---|---:|---|",
        f"| 新規記事数 | {_fmt(manual.get('new_articles'))} | 実作業ログ |",
        f"| リライト数 | {_fmt(manual.get('rewrites'))} | 実作業ログ |",
        f"| 確定収益 | {_fmt(manual.get('revenue'))} 円 | ASP管理画面（承認済のみ） |",
        f"| 発生件数 | {_fmt(manual.get('conversions'))} | ASP管理画面 |",
        f"| 承認率 | {_fmt(manual.get('approval_rate'), 1, '%')} | ASP管理画面 |",
        f"| 参照ドメイン数 | {_fmt(cur.referring_domains)} | Search Console「リンク」/ Ahrefs |",
        "",
    ]
    if notes:
        lines += ["## メモ", "", notes, ""]

    lines += [
        "---",
        "",
        "## PROGRESS.md 週次記録テーブルへの追記行",
        "",
        "以下を `PROGRESS.md` の「週次記録」テーブル末尾に貼り付ける",
        "（`--append-progress` を付けて実行すると自動で追記される）。",
        "",
        "```",
        progress_row(week_label, cur, manual, notes),
        "```",
        "",
        "---",
        "",
        "## 撤退・ピボット判定（STRATEGY.md Phase 10）",
        "",
        "このレポートの数字だけで判定すること。判定日が来たら先送りしない。",
        "",
        "| 時点 | 継続条件 |",
        "|---|---|",
        "| 3ヶ月 | 40記事、うち10記事が表示回数100回/月以上 |",
        "| 6ヶ月 | 月3,000PV、初成果1件以上 |",
        "| 9ヶ月 | 月10,000PV、月1万円 |",
        "| 12ヶ月 | 月3万円 |",
        "| **18ヶ月** | **月20万円（第1判断点）** |",
        "| 24ヶ月 | 月40万円 |",
        "",
    ]
    return "\n".join(lines)


def progress_row(week_label: str, cur: Metrics, manual: dict[str, Optional[float]],
                 notes: str) -> str:
    """PROGRESS.md の週次記録テーブルの列順に合わせた1行を作る。

    | 週 | 新規 | リライト | インデックス数 | 表示回数 | 10位内KW | 参照ドメイン | PV/月 | 確定収益 | メモ |
    """
    def cell(value: Optional[float], digits: int = 0) -> str:
        if value is None:
            return "?"
        return f"{value:,.{digits}f}"

    memo = (notes or "").replace("|", "/").replace("\n", " ")[:60] or "-"
    return (
        f"| {week_label} | {cell(manual.get('new_articles'))} | {cell(manual.get('rewrites'))} "
        f"| {cell(cur.indexed_pages)} | {cell(cur.impressions)} | {cell(cur.top10_keywords)} "
        f"| {cell(cur.referring_domains)} | {cell(cur.pageviews)} | {cell(manual.get('revenue'))} "
        f"| {memo} |"
    )


def append_to_progress(progress_path: Path, row: str) -> None:
    """PROGRESS.md の週次記録テーブル末尾に1行追記する。"""
    if not progress_path.exists():
        raise ToolError(f"PROGRESS.md が見つかりません: {progress_path}")
    try:
        text = progress_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"PROGRESS.md を読めません: {progress_path}: {exc}") from exc

    marker = "## 週次記録"
    if marker not in text:
        raise ToolError(
            f"PROGRESS.md に「{marker}」セクションが見つかりません。手動で追記してください。"
        )

    head, _, tail = text.partition(marker)
    lines = tail.splitlines()

    # プレースホルダ行（| - | - | ... | 未着手 |）があれば置き換える
    placeholder_idx = None
    last_table_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|"):
            last_table_idx = i
            if "未着手" in stripped and stripped.startswith("| -"):
                placeholder_idx = i

    if placeholder_idx is not None:
        LOG.info("プレースホルダ行を今週の記録で置き換えます")
        lines[placeholder_idx] = row
    elif last_table_idx is not None:
        if row in lines:
            LOG.warning("同じ内容の行が既に存在します。追記をスキップしました。")
            return
        lines.insert(last_table_idx + 1, row)
    else:
        raise ToolError("週次記録テーブルが見つかりませんでした。手動で追記してください。")

    new_text = head + marker + "\n".join(lines)
    if not new_text.endswith("\n"):
        new_text += "\n"
    try:
        progress_path.write_text(new_text, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise ToolError(f"PROGRESS.md を書き込めません: {exc}") from exc
    LOG.info("PROGRESS.md の週次記録に追記しました: %s", progress_path)


# --------------------------------------------------------------------------
# エントリポイント
# --------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="GA4 と Search Console から週次レポートを生成する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-c", "--config", help="設定ファイル（既定: tools/config.yaml）")
    parser.add_argument("--site", help="GSCプロパティ（既定: 環境変数 GSC_SITE_URL）")
    parser.add_argument("--ga4-property", help="GA4プロパティID（既定: 環境変数 GA4_PROPERTY_ID）")
    parser.add_argument("--end", help="集計終了日 YYYY-MM-DD（既定: 3日前）")
    parser.add_argument("--days", type=int, default=7, help="集計日数（既定: 7）")
    parser.add_argument("--no-ga4", action="store_true", help="GA4 を取得しない")
    parser.add_argument("-o", "--out", help="レポート出力先（既定: tools/reports/weekly-YYYY-Www.md）")
    parser.add_argument("--append-progress", action="store_true",
                        help="PROGRESS.md の週次記録テーブルに1行追記する")
    parser.add_argument("--progress-path", help="PROGRESS.md のパス（既定: プロジェクト直下）")
    # 手入力項目
    parser.add_argument("--new-articles", type=float, help="今週の新規記事数")
    parser.add_argument("--rewrites", type=float, help="今週のリライト数")
    parser.add_argument("--revenue", type=float, help="今週の確定収益（円）")
    parser.add_argument("--conversions", type=float, help="今週の発生件数")
    parser.add_argument("--approval-rate", type=float, help="承認率（%%）")
    parser.add_argument("--referring-domains", type=float, help="参照ドメイン数（今週）")
    parser.add_argument("--referring-domains-prev", type=float, help="参照ドメイン数（前週）")
    parser.add_argument("--referring-domains-month", type=float, help="参照ドメイン数（前月同週）")
    parser.add_argument("--indexed-pages", type=float,
                        help="インデックス数を手入力で上書き（GSCのページレポートの値）")
    parser.add_argument("--notes", default="", help="レポートに載せるメモ")
    parser.add_argument("--auth-only", action="store_true", help="OAuth 同意フローのみ実行")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    try:
        load_env()
        from google_clients import build_ga4, build_search_console, get_credentials, gsc_query

        if args.auth_only:
            get_credentials(interactive=True)
            LOG.info("認証が完了しました。以降は無人実行できます。")
            return 0

        cfg = load_config(args.config)
        site_url = args.site or os.environ.get("GSC_SITE_URL") or cfg_get(cfg, "search_console.site_url")
        if not site_url:
            raise ToolError(
                "GSCプロパティが未設定です。環境変数 GSC_SITE_URL を設定するか --site を指定してください。"
            )

        lag = int(cfg_get(cfg, "weekly_report.data_lag_days", 3))
        end = date.fromisoformat(args.end) if args.end else default_end_date(lag)
        current, previous, month_ago = build_periods(end, args.days)
        LOG.info("集計: %s / 比較: %s, %s", current, previous, month_ago)

        creds = get_credentials(interactive=True)
        gsc = build_search_console(creds)

        cur = fetch_gsc_metrics(gsc, site_url, current, gsc_query)
        prev = fetch_gsc_metrics(gsc, site_url, previous, gsc_query)
        mon = fetch_gsc_metrics(gsc, site_url, month_ago, gsc_query)

        ga4_property = args.ga4_property or os.environ.get("GA4_PROPERTY_ID") \
            or cfg_get(cfg, "ga4.property_id")
        if args.no_ga4:
            LOG.info("--no-ga4 のため GA4 の取得をスキップします")
        elif not ga4_property:
            LOG.warning("GA4_PROPERTY_ID が未設定のため、セッション/PVは「未取得」になります。"
                        "設定するか --no-ga4 を明示してください。")
        else:
            ga4 = build_ga4(creds)
            for period, metrics in ((current, cur), (previous, prev), (month_ago, mon)):
                try:
                    fetch_ga4_metrics(ga4, str(ga4_property), period, metrics)
                except ToolError as exc:
                    LOG.error("%s", exc)

        if args.indexed_pages is not None:
            LOG.info("インデックス数を手入力値で上書きします: %s", args.indexed_pages)
            cur.indexed_pages = args.indexed_pages
        cur.referring_domains = args.referring_domains
        prev.referring_domains = args.referring_domains_prev
        mon.referring_domains = args.referring_domains_month
        if args.referring_domains is None:
            LOG.warning("参照ドメイン数が未入力です。Search Console の「リンク」画面から転記し、"
                        "--referring-domains で渡してください（被リンク目標: 12ヶ月で30本）。")

        manual = {
            "new_articles": args.new_articles,
            "rewrites": args.rewrites,
            "revenue": args.revenue,
            "conversions": args.conversions,
            "approval_rate": args.approval_rate,
        }

        report = render_report(current, previous, month_ago, cur, prev, mon,
                               str(site_url), manual, args.notes)

        iso = current.end.isocalendar()
        default_out = expand_path(
            cfg_get(cfg, "weekly_report.output_dir", "reports")
        ) / f"weekly-{iso[0]}-W{iso[1]:02d}.md"
        out_path = Path(args.out) if args.out else default_out
        write_text(out_path, report)

        week_label = f"{iso[0]}-W{iso[1]:02d}"
        row = progress_row(week_label, cur, manual, args.notes)
        LOG.info("PROGRESS.md 追記行: %s", row)

        if args.append_progress:
            progress_path = Path(args.progress_path) if args.progress_path \
                else PROJECT_DIR / "PROGRESS.md"
            append_to_progress(progress_path, row)

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
