"""WordPress 表示テスト用の反映ファイルを、本番と同じ publish_prep.py で作る。

使い方: python build_wptest.py [CTA同期パターンのID]
中身は tools/tests/conftest.py の設計書どおりの型B記事（架空の統計を含む）。
照合の受領ファイルと checks はテスト用に作る（目的は表示の確認で、中身の正しさではない）。
"""
import re
import struct
import sys
import zlib
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
TOOLS = Path(r"C:\Users\matsu\affiliate\tools")
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / "tests"))

import conftest as C  # noqa: E402
import factcheck  # noqa: E402
import publish_prep  # noqa: E402
from article import content_hash, load_article  # noqa: E402
from common import load_config  # noqa: E402

pattern_id = int(sys.argv[1]) if len(sys.argv) > 1 else 0
DUMMY_AFF_DOMAIN = "aff.example.net"   # テスト用のダミーのアフィリエイト・計測ドメイン

work = TOOLS.parent / "build" / "wptest-work"   # 生成物（Git の管理対象外の build/ の下）
articles = work / "articles"
build = work / "build"
checks = work / "checks"
for d in (articles, build, checks, work / "sources"):
    d.mkdir(parents=True, exist_ok=True)

(work / "asp-programs.csv").write_text(
    C.ASP_PROGRAMS_CSV.replace("www17.a8.net", DUMMY_AFF_DOMAIN), encoding="utf-8")
(work / "industries.csv").write_text(C.INDUSTRIES_CSV, encoding="utf-8")
(work / "cta_patterns.yaml").write_text(
    yaml.safe_dump({"patterns": {"prog_001": {"pattern_id": pattern_id, "name": "表示テスト用CTA"}}},
                   allow_unicode=True), encoding="utf-8")

cfg = load_config(str(TOOLS / "config.example.yaml"))
cfg["paths"].update({
    "asp_programs_file": str(work / "asp-programs.csv"),
    "checks_dir": str(checks),
    "sources_dir": str(work / "sources"),
    "build_dir": str(build),
})
cfg["similarity"]["industries_file"] = str(work / "industries.csv")
cfg["factcheck"]["manual_check_allowed_checkers"] = ["実行者"]
cfg["affiliate_domains"] = list(cfg.get("affiliate_domains") or []) + [DUMMY_AFF_DOMAIN]
cfg.setdefault("publish_prep", {})["cta_patterns_file"] = str(work / "cta_patterns.yaml")
cfg_path = work / "config.yaml"
cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

C.write_related_targets(articles)

text = C.good_article_text()
text = text.replace("slug: dental-clinic-attendance-system", "slug: wp-render-test")
text = re.sub(r'^title: .*$', 'title: "【表示テスト】歯科医院の勤怠管理システムに必要な3つの要件"', text, flags=re.M)
text = re.sub(r'^description: .*$', 'description: "WordPressでの表示を確かめるためのダミー記事です。確認後に削除します。"',
              text, flags=re.M)
text = text.replace(
    "歯科医院では土曜の診療や",
    "この投稿はWordPressでの表示を確かめるためのダミーです。中の統計や料金は架空で、確認が終わりしだい削除します。\n\n"
    "歯科医院では土曜の診療や",
    1)
text = text.replace("月額は 1,980円 です。", "月額は 2,980円 です。")          # 料金表（2,980円）と揃える
text = text.replace("2026-10-01", "2026-09-26")                               # テストの日より前にする
text = text.replace("updated: 2026-09-26", "updated: 2026-09-28")             # 更新日の確認用（手順D）
text = text.replace("？\nA. ", "？\n\nA. ")                                   # FAQ: 質問と回答の間に空行（設計書 §2 P5）

path = articles / "wp-render-test.md"
path.write_text(C.stamp_checks(text, articles), encoding="utf-8")
art = load_article(path)
factcheck.write_receipt(checks, art.slug, content_hash(art), "WordPress表示テスト用（照合は行っていない）", {})

rc = publish_prep.main([str(path), "-c", str(cfg_path), "--cta-patterns", str(work / "cta_patterns.yaml")])
print("publish_prep exit code:", rc)
out = build / "wp-render-test"
if rc != 0 or not out.exists():
    sys.exit(1)

# 手順E（Markdown貼り付け）用：ブロックの区切りの行を除く（区切りがあるとエディタが Markdown として変換しない）
md = (out / "content.md").read_text(encoding="utf-8")
paste = "\n".join(ln for ln in md.split("\n") if not ln.strip().startswith("<!-- wp:"))
(out / "content-paste.md").write_text(paste, encoding="utf-8")

# 関門の要約の先頭に、テスト用であることを書く
gs = out / "gate-summary.md"
gs.write_text("> **【表示テスト用】照合は行っていない。受領ファイルと checks はテスト用に作ったもの。本番の要約ではない。**\n\n"
              + gs.read_text(encoding="utf-8"), encoding="utf-8")

# アイキャッチ用のテスト画像（1200x630・単色・撮影情報なし）
w, h = 1200, 630
row = b"\x00" + bytes([200, 200, 200]) * w
raw = zlib.compress(row * h, 9)


def chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
       + chunk(b"IDAT", raw) + chunk(b"IEND", b""))
(out / "test-eyecatch-1200x630.png").write_bytes(png)

# CTA の同期パターンに入れる計測用の画像タグ（ダミー。手順A）
(out / "cta-tracking-img.html").write_text(
    f'<img src="https://{DUMMY_AFF_DOMAIN}/px.gif" width="1" height="1" alt="">\n', encoding="utf-8")

for f in sorted(out.iterdir()):
    print("  ", f.name, f.stat().st_size, "bytes")
