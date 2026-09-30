#!/usr/bin/env python
"""
price_diff.py — 掲載企業の料金ページを定期取得し、前回スナップショットとの差分を検知する。

STRATEGY.md Phase 0-D「料金改定の差分検知」。800本規模の記事を維持するには、
全記事を読み直すのではなく「変わった箇所だけ」を人間に渡す必要がある。

動作
--------------------------------------------------------------------------
 1. 監視対象リスト（YAML または JSON）を読む
 2. 各URLを礼儀正しく取得（robots.txt 尊重・ホスト単位のレート制限・逐次アクセス）
 3. セレクタ指定があればその範囲、なければページ全文をテキスト化
 4. 除外フィルタ（日付・セッションID・広告枠など）を適用してノイズを消す
 5. tools/snapshots/<id>.txt と比較し、変更された「箇所」を人が読める形で出力
 6. スナップショットを更新（--dry-run で更新しない）

監視対象ファイルの書式（既定: tools/watch_targets.yaml）
--------------------------------------------------------------------------
    defaults:
      ignore_patterns:
        - '\\d{4}[-/年]\\d{1,2}[-/月]\\d{1,2}日?'   # 日付
        - '(?i)sessionid=[A-Za-z0-9]+'
    targets:
      - id: example-pricing          # 必須・ファイル名になるので英数字とハイフンのみ
        name: Example 料金ページ      # 任意・レポート表示名
        url: https://example.com/pricing
        selector: "main .pricing"    # 任意・CSSセレクタ。未指定ならページ全文
        enabled: true                # 任意・既定 true
        ignore_patterns: []          # 任意・defaults に追加される
        notes: "スタンダードプランのみ記事に掲載"

使い方
--------------------------------------------------------------------------
  python tools/price_diff.py                       # 全対象をチェック
  python tools/price_diff.py --only example-pricing
  python tools/price_diff.py --init                # 初回。差分を出さずスナップショットだけ作る
  python tools/price_diff.py --out tools/reports/price_diff.md
  python tools/price_diff.py --dry-run             # スナップショットを更新しない

終了コード: 0 = 変更なし / 1 = 変更あり（要確認） / 2 = 実行エラー
"""

from __future__ import annotations

import argparse
import dataclasses
import difflib
import hashlib
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from common import (
    TOOLS_DIR,
    ToolError,
    build_fetcher,
    cfg_get,
    expand_path,
    html_to_text,
    load_config,
    load_env,
    setup_logging,
    write_text,
)

LOG = logging.getLogger("affiliate.tools.price_diff")

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclasses.dataclass
class Target:
    id: str
    url: str
    name: str = ""
    selector: Optional[str] = None
    enabled: bool = True
    ignore_patterns: list[str] = dataclasses.field(default_factory=list)
    notes: str = ""

    @property
    def label(self) -> str:
        return self.name or self.id


@dataclasses.dataclass
class CheckResult:
    target: Target
    status: str                       # unchanged / changed / new / error
    detail: str = ""
    diff_lines: list[str] = dataclasses.field(default_factory=list)
    added: list[str] = dataclasses.field(default_factory=list)
    removed: list[str] = dataclasses.field(default_factory=list)
    final_url: str = ""
    old_hash: str = ""
    new_hash: str = ""


# --------------------------------------------------------------------------
# 監視対象の読み込み
# --------------------------------------------------------------------------

def load_targets(path: Path) -> tuple[list[Target], list[str]]:
    if not path.exists():
        raise ToolError(
            f"監視対象ファイルが見つかりません: {path}\n"
            f"  tools/watch_targets.example.yaml をコピーして作成してください。"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"監視対象ファイルを読めません: {path}: {exc}") from exc

    try:
        if path.suffix.lower() in (".json",):
            data = json.loads(text)
        else:
            import yaml  # type: ignore
            data = yaml.safe_load(text) or {}
    except ImportError as exc:
        raise ToolError("PyYAML が未インストールです。`pip install -r tools/requirements.txt`") from exc
    except Exception as exc:
        raise ToolError(f"監視対象ファイルのパースに失敗しました: {path}: {exc}") from exc

    if isinstance(data, list):          # トップレベルがリストの簡易形式も許容
        data = {"targets": data}
    if not isinstance(data, dict):
        raise ToolError(f"監視対象ファイルの形式が不正です: {path}")

    default_ignores = list((data.get("defaults") or {}).get("ignore_patterns") or [])
    raw_targets = data.get("targets") or []
    if not raw_targets:
        raise ToolError(f"監視対象が0件です: {path}")

    targets: list[Target] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_targets, start=1):
        if not isinstance(raw, dict):
            raise ToolError(f"targets[{i}] がマッピングではありません")
        tid = str(raw.get("id") or "").strip()
        url = str(raw.get("url") or "").strip()
        if not tid or not url:
            raise ToolError(f"targets[{i}] に id または url がありません")
        if not SAFE_ID_RE.match(tid):
            raise ToolError(f"targets[{i}] の id '{tid}' は英数字・ハイフン・アンダースコアのみ使用できます")
        if tid in seen:
            raise ToolError(f"id が重複しています: {tid}")
        seen.add(tid)
        targets.append(Target(
            id=tid,
            url=url,
            name=str(raw.get("name") or ""),
            selector=raw.get("selector") or None,
            enabled=bool(raw.get("enabled", True)),
            ignore_patterns=list(raw.get("ignore_patterns") or []),
            notes=str(raw.get("notes") or ""),
        ))
    return targets, default_ignores


# --------------------------------------------------------------------------
# 正規化 / 除外フィルタ
# --------------------------------------------------------------------------

def compile_filters(patterns: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for p in patterns:
        try:
            compiled.append((p, re.compile(p)))
        except re.error as exc:
            raise ToolError(f"除外フィルタの正規表現が不正です: {p!r}: {exc}") from exc
    return compiled


def apply_filters(text: str, filters: list[tuple[str, re.Pattern[str]]]) -> str:
    """動的要素をプレースホルダに置換して誤検知を防ぐ。

    削除ではなく置換にするのは、行構造を保って差分を読みやすくするため。
    """
    for _pattern, regex in filters:
        text = regex.sub("<DYNAMIC>", text)
    return text


def normalize_snapshot(text: str) -> str:
    """空行を削り、行頭行末を整えた比較用テキストにする。"""
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# スナップショット
# --------------------------------------------------------------------------

def snapshot_paths(snapshot_dir: Path, target_id: str) -> tuple[Path, Path]:
    return snapshot_dir / f"{target_id}.txt", snapshot_dir / f"{target_id}.meta.json"


def read_snapshot(snapshot_dir: Path, target_id: str) -> Optional[str]:
    text_path, _ = snapshot_paths(snapshot_dir, target_id)
    if not text_path.exists():
        return None
    try:
        return text_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"スナップショットを読めません: {text_path}: {exc}") from exc


def write_snapshot(snapshot_dir: Path, target: Target, content: str, final_url: str) -> None:
    text_path, meta_path = snapshot_paths(snapshot_dir, target.id)
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(content + "\n", encoding="utf-8", newline="\n")
    meta = {
        "id": target.id,
        "name": target.label,
        "url": target.url,
        "final_url": final_url,
        "selector": target.selector,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "sha256": sha256(content),
        "lines": content.count("\n") + 1 if content else 0,
    }
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    LOG.debug("スナップショットを更新しました: %s", text_path)


# --------------------------------------------------------------------------
# 差分
# --------------------------------------------------------------------------

def build_diff(old: str, new: str, target: Target, context: int = 2) -> tuple[list[str], list[str], list[str]]:
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"{target.id} (前回)", tofile=f"{target.id} (今回)",
        lineterm="", n=context,
    ))
    added = [l[1:].strip() for l in diff if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:].strip() for l in diff if l.startswith("-") and not l.startswith("---")]
    return diff, added, removed


def summarize_change(added: list[str], removed: list[str]) -> str:
    """人が読める1行サマリ。数値の変化があればそれを優先して示す。"""
    from common import extract_numbers, format_decimal

    old_nums = {format_decimal(t.value) for line in removed for t in extract_numbers(line)}
    new_nums = {format_decimal(t.value) for line in added for t in extract_numbers(line)}
    gone = sorted(old_nums - new_nums)
    appeared = sorted(new_nums - old_nums)
    if gone or appeared:
        return (f"数値の変化: 消えた {', '.join(gone[:8]) or 'なし'} / "
                f"現れた {', '.join(appeared[:8]) or 'なし'}")
    return f"テキスト変更 {len(removed)} 行削除 / {len(added)} 行追加（数値の変化なし）"


# --------------------------------------------------------------------------
# 実行
# --------------------------------------------------------------------------

def check_target(
    target: Target,
    fetcher: Any,
    snapshot_dir: Path,
    filters: list[tuple[str, re.Pattern[str]]],
    init_mode: bool,
    dry_run: bool,
    context_lines: int,
) -> CheckResult:
    LOG.info("チェック中: %s (%s)", target.label, target.url)
    result = fetcher.fetch(target.url)
    if not result.ok:
        LOG.error("取得失敗: %s: %s", target.url, result.error)
        return CheckResult(target, "error", detail=f"取得失敗: {result.error}",
                           final_url=result.url)

    try:
        text = html_to_text(result.text, selector=target.selector)
    except ToolError as exc:
        # セレクタが外れた＝サイト側のHTML構造変更。これ自体が要確認事項
        LOG.error("%s: %s", target.label, exc)
        return CheckResult(
            target, "error",
            detail=f"{exc}。ページ構造が変わった可能性があります。selector の見直しが必要です。",
            final_url=result.url,
        )

    filtered = normalize_snapshot(apply_filters(text, filters))
    if not filtered:
        return CheckResult(target, "error",
                           detail="抽出後のテキストが空です。selector か除外フィルタが広すぎます。",
                           final_url=result.url)

    old = read_snapshot(snapshot_dir, target.id)
    new_hash = sha256(filtered)

    if old is None or init_mode:
        if not dry_run:
            write_snapshot(snapshot_dir, target, filtered, result.url)
        status = "new" if old is None else "unchanged"
        detail = ("初回取得。スナップショットを作成しました（比較対象なし）"
                  if old is None else "--init によりスナップショットを再作成しました")
        return CheckResult(target, status, detail=detail, final_url=result.url, new_hash=new_hash)

    old_norm = normalize_snapshot(old)
    old_hash = sha256(old_norm)
    if old_hash == new_hash:
        LOG.info("変更なし: %s", target.label)
        return CheckResult(target, "unchanged", final_url=result.url,
                           old_hash=old_hash, new_hash=new_hash)

    diff, added, removed = build_diff(old_norm, filtered, target, context_lines)
    LOG.warning("変更を検知しました: %s (%s 行削除 / %s 行追加)",
                target.label, len(removed), len(added))
    if not dry_run:
        write_snapshot(snapshot_dir, target, filtered, result.url)

    return CheckResult(
        target, "changed", detail=summarize_change(added, removed),
        diff_lines=diff, added=added, removed=removed,
        final_url=result.url, old_hash=old_hash, new_hash=new_hash,
    )


def render_report(results: list[CheckResult], dry_run: bool) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    changed = [r for r in results if r.status == "changed"]
    errors = [r for r in results if r.status == "error"]
    new = [r for r in results if r.status == "new"]
    unchanged = [r for r in results if r.status == "unchanged"]

    lines = [
        "# 料金改定 差分検知レポート",
        "",
        f"生成日時: {now}" + ("（--dry-run: スナップショット未更新）" if dry_run else ""),
        f"対象 {len(results)} 件 — **変更 {len(changed)} / エラー {len(errors)} / "
        f"新規 {len(new)} / 変更なし {len(unchanged)}**",
        "",
    ]

    if changed:
        lines += ["## 変更あり（要確認）", ""]
        for r in changed:
            lines += [
                f"### {r.target.label}",
                "",
                f"- URL: {r.target.url}",
            ]
            if r.final_url and r.final_url != r.target.url:
                lines.append(f"- 最終URL: {r.final_url}（リダイレクト）")
            if r.target.selector:
                lines.append(f"- 監視範囲: `{r.target.selector}`")
            if r.target.notes:
                lines.append(f"- メモ: {r.target.notes}")
            lines += [f"- 変更概要: **{r.detail}**", ""]

            if r.removed:
                lines += ["**削除された行**", "", "```"]
                lines += r.removed[:40]
                if len(r.removed) > 40:
                    lines.append(f"... 他 {len(r.removed) - 40} 行")
                lines += ["```", ""]
            if r.added:
                lines += ["**追加された行**", "", "```"]
                lines += r.added[:40]
                if len(r.added) > 40:
                    lines.append(f"... 他 {len(r.added) - 40} 行")
                lines += ["```", ""]

            lines += ["<details><summary>前後の文脈込みの差分</summary>", "", "```diff"]
            lines += r.diff_lines[:200]
            if len(r.diff_lines) > 200:
                lines.append(f"... 他 {len(r.diff_lines) - 200} 行")
            lines += ["```", "", "</details>", ""]

    if errors:
        lines += ["## エラー（取得できなかった / 構造が変わった）", ""]
        for r in errors:
            lines.append(f"- **{r.target.label}** — {r.detail}  \n  {r.target.url}")
        lines.append("")

    if new:
        lines += ["## 新規登録（初回スナップショット作成）", ""]
        for r in new:
            lines.append(f"- {r.target.label} — {r.target.url}")
        lines.append("")

    if unchanged:
        lines += ["## 変更なし", "",
                  ", ".join(r.target.label for r in unchanged), ""]

    if changed:
        lines += [
            "---",
            "",
            "### 変更を検知したときの手順",
            "",
            "1. 上の「変更概要」で料金の実変更かノイズかを判断する",
            "2. ノイズなら `watch_targets.yaml` の `ignore_patterns` に正規表現を追加して再実行",
            "3. 実変更なら該当企業を掲載している記事を特定し、リライト対象に入れる",
            "   （`grep -rl '<企業名>' articles/` で該当記事を洗い出す）",
            "4. 記事修正後に `factcheck.py` を回して出典との整合を確認する",
            "",
        ]
    return "\n".join(lines)


def notify_webhook(webhook_url: str, results: list[CheckResult]) -> None:
    """Slack互換のIncoming Webhookに要約を投げる（任意機能）。"""
    changed = [r for r in results if r.status == "changed"]
    errors = [r for r in results if r.status == "error"]
    if not changed and not errors:
        LOG.debug("通知すべき変更がないため webhook 送信をスキップします")
        return
    try:
        import requests  # type: ignore
    except ImportError:
        LOG.error("requests が未インストールのため webhook 通知を送れません")
        return

    parts = [f"*料金差分検知* 変更 {len(changed)} 件 / エラー {len(errors)} 件"]
    for r in changed[:10]:
        parts.append(f"• {r.target.label}: {r.detail}\n  {r.target.url}")
    for r in errors[:10]:
        parts.append(f"• [ERROR] {r.target.label}: {r.detail}")
    try:
        resp = requests.post(webhook_url, json={"text": "\n".join(parts)}, timeout=15)
        if resp.status_code >= 400:
            LOG.error("webhook 通知に失敗しました: HTTP %s: %s", resp.status_code, resp.text[:200])
        else:
            LOG.info("webhook 通知を送信しました")
    except Exception as exc:
        LOG.error("webhook 通知に失敗しました: %s", exc)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="料金ページの差分を検知する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-c", "--config", help="設定ファイル（既定: tools/config.yaml）")
    parser.add_argument("-t", "--targets", help="監視対象ファイル（既定: config の price_diff.targets_file）")
    parser.add_argument("--only", nargs="+", metavar="ID", help="指定IDの対象のみチェック")
    parser.add_argument("--init", action="store_true",
                        help="差分を出さずスナップショットを作り直す（初回導入時・フィルタ変更時）")
    parser.add_argument("--dry-run", action="store_true", help="スナップショットを更新しない")
    parser.add_argument("-o", "--out", help="Markdownレポートの出力先")
    parser.add_argument("--notify", action="store_true", help="webhook 通知を送る")
    parser.add_argument("--ignore-robots", action="store_true", help="robots.txt を無視する")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    try:
        load_env()
        cfg = load_config(args.config)

        targets_file = expand_path(
            args.targets or cfg_get(cfg, "price_diff.targets_file", "watch_targets.yaml")
        )
        snapshot_dir = expand_path(cfg_get(cfg, "price_diff.snapshot_dir", "snapshots"))
        context_lines = int(cfg_get(cfg, "price_diff.diff_context_lines", 2))

        targets, default_ignores = load_targets(targets_file)
        global_ignores = list(cfg_get(cfg, "price_diff.ignore_patterns", []) or [])

        selected = [t for t in targets if t.enabled]
        if args.only:
            wanted = set(args.only)
            unknown = wanted - {t.id for t in targets}
            if unknown:
                raise ToolError(f"--only に未知のIDが含まれています: {', '.join(sorted(unknown))}")
            selected = [t for t in targets if t.id in wanted]
        if not selected:
            raise ToolError("チェック対象が0件です（enabled: false ばかりになっていませんか）")

        LOG.info("監視対象 %s 件（全 %s 件中）/ スナップショット: %s",
                 len(selected), len(targets), snapshot_dir)

        fetcher = build_fetcher(cfg, ignore_robots=args.ignore_robots)
        results: list[CheckResult] = []
        for target in selected:
            filters = compile_filters(global_ignores + default_ignores + target.ignore_patterns)
            try:
                results.append(check_target(
                    target, fetcher, snapshot_dir, filters,
                    args.init, args.dry_run, context_lines,
                ))
            except ToolError as exc:
                LOG.error("%s のチェックに失敗しました: %s", target.label, exc)
                results.append(CheckResult(target, "error", detail=str(exc)))
            except Exception as exc:
                LOG.exception("%s のチェック中に想定外のエラー", target.label)
                results.append(CheckResult(target, "error", detail=f"想定外のエラー: {exc}"))

        report = render_report(results, args.dry_run)
        if args.out:
            write_text(Path(args.out), report)
        else:
            sys.stdout.write(report)

        if args.notify:
            import os
            env_name = cfg_get(cfg, "notify.webhook_url_env", "PRICE_DIFF_WEBHOOK_URL")
            url = os.environ.get(env_name)
            if not url:
                LOG.error("--notify が指定されましたが環境変数 %s が未設定です", env_name)
            else:
                notify_webhook(url, results)

        changed = sum(1 for r in results if r.status == "changed")
        errors = sum(1 for r in results if r.status == "error")
        if errors:
            LOG.error("エラー %s 件（取得失敗・セレクタ不一致など）", errors)
        if changed:
            LOG.warning("変更 %s 件を検知しました", changed)
            return 1
        LOG.info("料金変更は検知されませんでした")
        return 2 if errors else 0

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
