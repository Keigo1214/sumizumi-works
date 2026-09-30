# data/checks/ — 照合の合格の受領ファイル

`tools/factcheck.py` を**公開前の関門モード**（`--mode gate`。既定）で実行し、記事が合格したときに、
ツール自身が `data/checks/{slug}.json` を書く。`tools/publish_prep.py` はこのファイルで照合の合格を確かめる
（front matter の `checks.factcheck_tool` は使わない。設計書 §8 T10）。**手で書かない・直さない。**

```json
{
  "slug": "dental-clinic-attendance-system",
  "result": "pass",
  "mode": "gate",
  "tool": "factcheck.py",
  "content_hash": "0b209972093884b7",
  "checked_at": "2026-10-01T10:00:00",
  "report_sha256": "…（その回のレポートの全文の sha256）",
  "claims": 10,
  "numbers": 19,
  "manual_suppressed": 6,
  "coincidences_suppressed": 4,
  "lint_warnings": 0,
  "report_path": "tools/reports/factcheck.md"
}
```

- `content_hash` は正本のハッシュ（`tools/README.md` §11.4）。正本を1文字でも直すと一致しなくなり、publish_prep が止まる。照合をやり直すと書き直される
- 週次の照合（`--mode weekly`）では書かない
- この仕組みは古い合格の流用と記録漏れを防ぐものであり、偽造への防御ではない。最後の保証は、実行者が関門の要約（`build/{slug}/gate-summary.md`）を読んで下書きを公開に切り替えること
