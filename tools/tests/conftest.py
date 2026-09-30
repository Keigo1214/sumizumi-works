"""tools/tests の共通部品。ネットワークには出ない（取得は FakeFetcher で置き換える）。"""

from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path
from typing import Optional

import pytest

TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from common import FetchResult, load_config  # noqa: E402

EXAMPLE_CONFIG = TOOLS_DIR / "config.example.yaml"
DATA_DIR = TOOLS_DIR.parent / "data"

# --------------------------------------------------------------------------
# 設計書（docs/article-template.md v0.3）に載っている記法の例（そのまま）
# --------------------------------------------------------------------------

# §1-2 (1) 行内マーカー
SPEC_INLINE = (
    "スタンダードプランの月額は 1,980円 です。"
    "<!-- src: https://example.com/pricing accessed=2026-09-27 -->（◯◯公式サイト、2026年9月時点）"
)

# §1-2 (2) ブロックマーカー
SPEC_BLOCK = """\
<!-- src-block: https://example.com/pricing accessed=2026-09-27 -->
| プラン | 月額 | ユーザー数上限 |
|---|---|---|
| ライト | 1,000円 | 10人 |
| スタンダード | 2,980円 | 50人 |
<!-- /src-block -->
（◯◯公式サイト、2026年9月時点・税抜）"""

# §1-2 計算した値（試算・シミュレーター）
SPEC_CALC = """\
試算の条件：10人で利用し、月払いの場合（初期費用は含まない）<!-- nofact -->

| サービス | 初期費用 | 1人あたり月額 | 最低人数 | 契約形態 |
|---|---|---|---|---|
| X社 | 0円 | 300円 | 10人 | 月払い可 |<!-- src: https://x.example.com/pricing accessed=2026-09-27 -->
| Y社 | 0円 | 400円 | なし | 月払い可 |<!-- src: https://y.example.com/price accessed=2026-09-27 -->
| Z社 | 0円 | 250円 | なし | 年契約のみ |<!-- src: https://z.example.com/plan accessed=2026-09-27 -->

計算式：1人あたり月額 × 利用人数（最低人数を下回る場合は最低人数で計算）

<!-- calc-block -->
| サービス | 月額の試算 |
|---|---|
| X社 | 3,000円 |
| Y社 | 4,000円 |
| Z社 | 試算の対象外（月払いができないため） |
<!-- /calc-block -->

上の試算の条件では、比較した中でX社の月額が最も低くなります。"""

# §2 比較表（P2）
SPEC_P2 = """\
| サービス | 初期費用 | 月額（1人あたり） | 最低人数 | 契約 |
|---|---|---|---|---|
| X社 | 0円 | 300円 | 10人 | 月払い可 |<!-- src: https://x.example.com/pricing accessed=2026-09-27 -->
| Y社 | 0円 | 400円 | なし | 年契約のみ |<!-- src: https://y.example.com/price accessed=2026-09-27 -->

（各社公式サイト、2026年9月時点・税抜）"""

# §4 要件トレース表（設計書の雛形そのまま。プレースホルダと「…」を含む）
SPEC_TRACE_TEMPLATE = """\
| 業種固有の事実（出典つき） | そこから導かれる要件 | 製品への影響 |
|---|---|---|
| {{事実1}}<!-- src: 事実1の出典URL accessed=… --> | {{要件1}} | X：対応／Y：非対応 → Yは{{条件}}の場合に不向き |
| {{事実2}}<!-- src: … --> | {{要件2}} | … |
| {{事実3}}<!-- src: … --> | {{要件3}} | … |"""

# §4 の書き方（マーカーは「事実」のセルの直後）に従って値を入れたもの
SPEC_TRACE_FILLED = """\
| 業種固有の事実（出典つき） | そこから導かれる要件 | 製品への影響 |
|---|---|---|
| 歯科診療所の約6割が土曜日も診療している<!-- src: https://stat.example.go.jp/dental accessed=2026-09-27 --> | 土曜のシフトを組める | X：対応／Y：非対応 → Yは土曜診療がある場合に不向き |
| 歯科衛生士の約3割が非常勤である<!-- src: https://stat.example.go.jp/hygienist accessed=2026-09-27 --> | 短時間勤務の打刻 | X：対応／Y：対応 |
| 分院を持つ医療法人がある<!-- src: https://law.example.go.jp/corp accessed=2026-09-27 --> | 複数拠点の管理 | X：対応／Y：非対応 → Yは分院がある場合に不向き |"""

# §1-1 の front matter（title・description は数値の検査のため値を入れた）
SPEC_FRONT_MATTER = """\
---
type: B                                   # A / B
keyword: 歯科医院 勤怠管理                  # 主キーワード（data/keywords.csv に一意登録）
slug: dental-clinic-attendance-system
title: "歯科医院の勤怠管理システムに必要な3つの要件｜月額300円からの製品"
description: "歯科医院で勤怠管理システムを選ぶときに必要な3つの要件を、出典つきで整理しました。"
parent: attendance-system-comparison      # 型Bのみ。親の型A
siblings: [veterinary-clinic-attendance-system, dental-lab-attendance-system]
industry: dental-clinic                   # data/industries.csv のID（型Bのみ）
programs: [prog_001]                      # data/asp-programs.csv のID
watch_ids: [x-pricing, y-pricing]         # tools/watch_targets.yaml のID
published: 2026-10-01
updated: 2026-10-01                       # 内容を更新した日（表示する更新日もこれに合わせる）
verified: 2026-10-01                      # 全出典を最後に確認した日（§1-5「最新」の条件に使う）
checks:                                   # 品質ゲートの証跡（§6）
  lint: {result: pass, date: 2026-10-01}
  factcheck_tool: {result: pass, date: 2026-10-01}
  factcheck_session: {result: pass, date: 2026-10-01}
  trace_table: {result: pass, date: 2026-10-01}   # 型Bのみ（§4）
  owner_review: {result: pass, date: 2026-10-01}
history:                                  # 更新・訂正履歴（P7に表示）
  - {date: 2026-10-01, note: 公開}
---"""

GOOD_BODY = f"""\
歯科医院では土曜の診療や非常勤のスタッフが多く、勤怠管理システムの選び方が一般の企業と違います。
この記事では、必要な要件と、要件を満たす製品を整理します。

{{{{CTA:prog_001}}}}

## 歯科医院で勤怠管理システム選びが難しい理由

{SPEC_INLINE}

## 歯科医院に必要な3つの要件 <!-- nofact -->

<!-- part: trace -->
{SPEC_TRACE_FILLED}

### 土曜のシフトを組める

土曜の診療がある医院では、曜日ごとにシフトを組める製品が必要です。

### 短時間勤務の打刻

非常勤のスタッフの打刻を、1人あたりの勤務時間で集計できることが必要です。

### 複数拠点の管理

分院がある場合は、拠点ごとに集計できる製品が必要です。
<!-- /part -->
<!-- part: checklist -->
判断基準チェックリスト（必須／あると便利）

- 必須：土曜のシフトを組める（土曜診療があるため）
- あると便利：拠点ごとの集計（分院を作る予定があるため）
<!-- /part -->

## 料金の比較

{SPEC_BLOCK}

{SPEC_P2}

## 試算

{SPEC_CALC}

## 歯科医院におすすめの勤怠管理システム

### 土曜のシフトを重視するなら：X社

曜日ごとのシフトと短時間勤務の打刻の両方に対応しています。

### 分院を作る予定がないなら：Y社

分院のない医院に向いています。

## 導入の流れ

1. 管理画面で打刻の方法を設定する
2. スタッフを登録する

## よくある質問

<!-- part: faq -->
Q. 非常勤のスタッフだけでも使えますか？
A. 使えます。短時間勤務の打刻に対応した製品を選んでください。

Q. 土曜だけシフトが違っても管理できますか？
A. 曜日ごとにシフトを組める製品なら管理できます。

Q. 分院ごとに集計できますか？
A. 拠点ごとの集計に対応した製品を選んでください。
<!-- /part -->

## 関連記事

<!-- part: related -->
- [勤怠管理システムの比較](/attendance-system-comparison/)
<!-- /part -->
"""

# 関連記事のリンク先（同じディレクトリに置く公開済みの正本。中身は front matter だけでよい）
RELATED_TARGETS = {
    "attendance-system-comparison": ("A", "勤怠管理システムの比較"),
    "dental-sample": ("B", "歯科医院の勤怠管理システムの選び方"),
}
RELATED_A = "\n## 業種別の選び方\n\n<!-- part: related -->\n- 歯科医院：[歯科医院の勤怠管理システムの選び方](/dental-sample/)\n<!-- /part -->\n"
RELATED_B = "\n## 関連記事\n\n<!-- part: related -->\n- [勤怠管理システムの比較](/attendance-system-comparison/)\n<!-- /part -->\n"


def write_related_targets(d: Path) -> None:
    for slug, (typ, title) in RELATED_TARGETS.items():
        p = d / f"{slug}.md"
        if not p.exists():
            p.write_text(f"---\ntype: {typ}\nslug: {slug}\ntitle: \"{title}\"\ndescription: \"説明\"\n"
                         f"published: 2026-09-01\nupdated: 2026-09-01\n---\n本文\n", encoding="utf-8")


def good_article_text() -> str:
    """設計書の例を並べた型Bの正本（checks の hash は入っていない。stamp_checks で入れる）。"""
    return SPEC_FRONT_MATTER + "\n" + GOOD_BODY


CHECKS_DIR: Optional[Path] = None      # test_config_path が決める（受領ファイルの置き場所）


def write_test_receipt(path: Path) -> None:
    """照合の合格の受領ファイルを、今の正本のハッシュで書く（テスト用）。"""
    import factcheck
    from article import content_hash, load_article

    from article import validate_slug
    assert CHECKS_DIR is not None, "test_config_path フィクスチャを先に使う"
    art = load_article(path)
    if art.front_matter is None or validate_slug(art.slug):
        return                                  # slug が不正な記事の受領ファイルは作れない（テストの意図どおり）
    factcheck.write_receipt(CHECKS_DIR, art.slug, content_hash(art), "test report", {})


def stamp_checks(text: str, tmp_dir: Path) -> str:
    """checks の各項目に、今の正本のハッシュを書き込む（checks はハッシュに含まれない）。"""
    from article import content_hash, load_article

    probe = tmp_dir / "_hash_probe.md"
    probe.write_text(text, encoding="utf-8")
    h = content_hash(load_article(probe))
    probe.unlink()
    return re.sub(r"\{result: pass, date: ([0-9-]+)\}", r"{result: pass, date: \1, hash: " + h + "}", text)


MIN_FRONT_MATTER = """\
---
type: A
keyword: テスト
slug: {slug}
title: "{title}"
description: "{description}"
updated: 2026-10-01
history:
  - {{date: 2026-10-01, note: 公開}}
---
"""

CHECKS_BLOCK = """\
checks:
  factcheck_session: {result: pass, date: 2026-10-01}
  owner_review: {result: pass, date: 2026-10-01}
"""


def make_article(tmp_path: Path, body: str, *, name: str = "test-article",
                 title: str = "テスト記事", description: str = "テストの説明",
                 front_matter: Optional[str] = None, checks: bool = False,
                 encoding: str = "utf-8", newline: str = "\n", related: bool = True) -> Path:
    """最小の front matter を付けて記事を書く。body は textwrap.dedent される。

    related=True なら、末尾に関連記事の区間（型A・型Bとも必須）を足し、リンク先の正本も同じ場所に書く。
    checks=True なら、必須の checks を今の正本のハッシュつきで入れ、照合の受領ファイルも書く
    （publish_prep の品質ゲートを通す）。
    """
    fm = front_matter if front_matter is not None else MIN_FRONT_MATTER.format(
        slug=name, title=title, description=description)
    if checks:
        fm = fm.rstrip("\n")
        assert fm.endswith("---")
        fm = fm[:-3] + CHECKS_BLOCK + "---\n"
    text = fm + textwrap.dedent(body)
    if related:
        write_related_targets(tmp_path)
        if not text.endswith("\n"):
            text += "\n"
        text += RELATED_B if re.search(r"^type:\s*B\b", fm, re.M) else RELATED_A
    if checks:
        text = stamp_checks(text, tmp_path)
    path = tmp_path / f"{name}.md"
    path.write_bytes(text.replace("\n", newline).encode(encoding))
    if checks:
        write_test_receipt(path)
    return path


ASP_PROGRAMS_CSV = """\
program_id,asp,advertiser,program_name,tracking_domains,status,note
prog_001,テストASP,X社,X社 勤怠管理,www17.a8.net,提携中,テスト用
prog_002,テストASP,Y社,Y社 勤怠管理,,提携中,計測ドメイン未記入
"""
INDUSTRIES_CSV = "id,name,parent,name_en\ndental-clinic,歯科医院,medical,dental clinic\n" \
                 "veterinary-clinic,動物病院,medical,veterinary clinic\n"


@pytest.fixture(scope="session")
def test_config_path(tmp_path_factory) -> Path:
    """テスト用の config.yaml（config.example.yaml ＋ 確認者の許可リスト・テスト用の案件一覧・業種・受領ファイルの置き場所）。"""
    import yaml

    global CHECKS_DIR
    d = tmp_path_factory.mktemp("cfg")
    asp = d / "asp-programs.csv"
    asp.write_text(ASP_PROGRAMS_CSV, encoding="utf-8")
    ind = d / "industries.csv"
    ind.write_text(INDUSTRIES_CSV, encoding="utf-8")
    CHECKS_DIR = d / "checks"
    cfg = load_config(str(EXAMPLE_CONFIG))
    cfg["paths"]["asp_programs_file"] = str(asp)
    cfg["paths"]["checks_dir"] = str(CHECKS_DIR)
    cfg["paths"]["sources_dir"] = str(d / "sources")
    cfg["similarity"]["industries_file"] = str(ind)
    cfg["factcheck"]["manual_check_allowed_checkers"] = ["実行者"]
    path = d / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture
def cfg(test_config_path):
    return load_config(str(test_config_path))


@pytest.fixture
def good_article(tmp_path: Path, test_config_path) -> Path:
    write_related_targets(tmp_path)
    path = tmp_path / "dental-clinic-attendance-system.md"
    path.write_text(stamp_checks(good_article_text(), tmp_path), encoding="utf-8")
    write_test_receipt(path)
    return path


# CTA の同期パターン（設計書 §2 P4b: 数値を書かない。中身はボタンと計測用の画像タグだけ。外側に cta クラス）
CTA_HTML = ('<div class="wp-block-group cta"><a class="btn" href="https://px.a8.net/svt/ejp?a8mat=XXXX" '
            'rel="nofollow sponsored">公式サイトで詳しく見る</a>'
            '<img src="https://www17.a8.net/0.gif?a8mat=XXXX" width="1" height="1" alt=""></div>')
AD_HTML = ('<div class="ad-disclosure" role="note"><span class="ad-disclosure__label">PR</span>'
           '<span class="ad-disclosure__text">本記事には広告（アフィリエイトリンク）が含まれます。</span></div>')


def published_page(content_html: str, ad: str = AD_HTML, head: str = "", sidebar: str = "",
                   ref: int = 123) -> str:
    """WordPress で公開したページの代わり（同期パターンは描画済みのボタンに置き換える）。"""
    body = content_html.replace('<!-- wp:block {"ref":%d} /-->' % ref, CTA_HTML)
    return (f"<html><body><header>サイト名{head}</header><article><h1>タイトル</h1>"
            f"<div class=\"entry-content cf\">{ad}{body}</div></article>"
            f"<aside>{sidebar}</aside></body></html>")


class FakeFetcher:
    """PoliteFetcher の代わり。url → FetchResult を返す。取得したURLを記録する。"""

    def __init__(self, pages: dict[str, object]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        page = self.pages.get(url)
        if isinstance(page, FetchResult):
            return page
        if page is None:
            return FetchResult(url, url, 404, "", False, error="HTTP 404")
        return FetchResult(url, url, 200, str(page), True, content_type="text/html")


def html_page(body: str) -> str:
    return f"<html><head><meta charset='utf-8'></head><body><main>{body}</main></body></html>"
