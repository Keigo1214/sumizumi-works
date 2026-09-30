"""T4: similarity.py（型B記事同士の類似度）。記事は設計書 §4 の構成（部分の目印つき）で作る。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import similarity
from conftest import DATA_DIR, good_article_text
from similarity import SimConfig, build_docs, compare, load_industry_names, mask_industries

INDUSTRIES = "id,name,parent,name_en\ndental-clinic,歯科医院,medical,dental clinic\n" \
             "veterinary-clinic,動物病院,medical,veterinary clinic\nbeauty-salon,美容室,beauty,beauty salon\n"

# 設計書 §4 の構成（型B）。タイトルの型3「{{業種}}向け{{商材}}に必要な{{N}}つの要件と対応製品」の見出しを使う
TYPE_B = """\
---
type: B
slug: {slug}
title: "{industry}向け勤怠管理システムに必要な2つの要件と対応製品"
description: "説明"
parent: {parent}
industry: {slug}
updated: 2026-10-01
---
{industry}では土曜の診療が多く、勤怠管理の選び方が違います。

## {industry}で勤怠管理システム選びが難しい理由

本文。

## {industry}向け勤怠管理システムに必要な2つの要件 <!-- nofact -->

<!-- part: trace -->
| 業種固有の事実（出典つき） | そこから導かれる要件 | 製品への影響 |
|---|---|---|
| {fact1}<!-- src: https://stat.example.go.jp/a accessed=2026-09-27 --> | {req1} | X：対応／Y：非対応 |
| {fact2}<!-- src: https://stat.example.go.jp/b accessed=2026-09-27 --> | {req2} | X：対応／Y：対応 |
<!-- /part -->

<!-- part: checklist -->
- 必須：{req1}（{industry}では{fact1}ため）
- あると便利：{req2}
<!-- /part -->

## 要件で絞り込んだ比較

| サービス | 月額 |
|---|---|
| X社 | 300円 |<!-- src: https://x.example.com/ accessed=2026-09-27 -->

## よくある質問（{industry}特有）

<!-- part: faq -->
Q. {industry}で{req1}は必要ですか？
A. {fact1}ため必要です。
<!-- /part -->
"""

SAME = dict(fact1="土曜日も診療している医院が多い", fact2="非常勤のスタッフが多い",
            req1="土曜のシフトを組める", req2="短時間勤務の打刻")
DIFF = dict(fact1="予約の無断キャンセルが多い", fact2="施術ごとに歩合給を計算する",
            req1="歩合給の自動計算", req2="指名数の集計")


def write(dir_: Path, slug: str, industry: str, parent="attendance-system-comparison", **parts) -> Path:
    p = dir_ / f"{slug}.md"
    p.write_text(TYPE_B.format(slug=slug, industry=industry, parent=parent, **parts), encoding="utf-8")
    return p


@pytest.fixture
def industries(tmp_path):
    p = tmp_path / "industries.csv"
    p.write_text(INDUSTRIES, encoding="utf-8")
    return p


@pytest.fixture
def arts(tmp_path):
    d = tmp_path / "articles"
    d.mkdir()
    return d


def run(tmp_path, industries, test_config_path, *extra):
    out = tmp_path / "sim.md"
    rc = similarity.main([str(tmp_path / "articles"), "-c", str(test_config_path),
                          "--industries", str(industries), "-o", str(out), *extra])
    return rc, out.read_text(encoding="utf-8")


def test_same_content_different_industry_name_is_flagged(tmp_path, arts, industries, test_config_path):
    write(arts, "dental-clinic-attendance-system", "歯科医院", **SAME)
    write(arts, "veterinary-clinic-attendance-system", "動物病院", **SAME)
    rc, report = run(tmp_path, industries, test_config_path)
    assert rc == 1
    assert "超過" in report and "1.000" in report


def test_renamed_headings_do_not_escape(tmp_path, arts, industries, test_config_path):
    """C12（c7）: 見出しの書き方を変えても、目印で部分を取り出すので同じ中身は検出される。"""
    write(arts, "dental-clinic-attendance-system", "歯科医院", **SAME)
    b = write(arts, "veterinary-clinic-attendance-system", "動物病院", **SAME)
    b.write_text(b.read_text(encoding="utf-8").replace("に必要な2つの要件", "で押さえる2つの条件")
                 .replace("## よくある質問", "## Q&A"), encoding="utf-8")
    rc, report = run(tmp_path, industries, test_config_path)
    assert rc == 1 and "1.000" in report


def test_good_article_structure_is_compared(tmp_path, arts, industries, test_config_path):
    """設計書どおりの構成（conftest の型Bの正本）同士で、業種名だけ違えば検出される。"""
    t = good_article_text()
    (arts / "a.md").write_text(t, encoding="utf-8")
    (arts / "b.md").write_text(t.replace("dental-clinic-attendance-system", "vet-attendance-system")
                               .replace("歯科医院", "動物病院"), encoding="utf-8")
    rc, report = run(tmp_path, industries, test_config_path)
    assert rc == 1 and "| 1.000 | 1.000 | 1.000 | 1.000 |" in report


def test_masking_is_what_makes_them_identical(tmp_path, arts, industries):
    a = write(arts, "a-attendance", "歯科医院", **SAME)
    b = write(arts, "b-attendance", "動物病院", **SAME)
    sc = SimConfig()
    masked, _ = build_docs([a, b], sc, load_industry_names(industries))
    unmasked, _ = build_docs([a, b], sc, [])
    assert compare(masked, sc)[0].combined == pytest.approx(1.0)
    assert compare(unmasked, sc)[0].combined < 1.0


def test_different_requirements_are_below_threshold(tmp_path, arts, industries, test_config_path):
    write(arts, "dental-clinic-attendance-system", "歯科医院", **SAME)
    write(arts, "beauty-salon-attendance-system", "美容室", **DIFF)
    rc, report = run(tmp_path, industries, test_config_path)
    assert rc == 0, report
    assert "しきい値（0.5、暫定）を超えた組: 0" in report


def test_only_same_parent_type_b_are_compared(tmp_path, arts, industries, test_config_path):
    write(arts, "dental-clinic-attendance-system", "歯科医院", **SAME)
    write(arts, "veterinary-clinic-shift-system", "動物病院", parent="shift-system-comparison", **SAME)
    a = write(arts, "type-a", "歯科医院", **SAME)
    a.write_text(a.read_text(encoding="utf-8").replace("type: B", "type: A"), encoding="utf-8")
    rc, report = run(tmp_path, industries, test_config_path)
    assert rc == 0
    assert "比べた組: 0" in report


def test_only_parts_are_compared(tmp_path, arts, industries):
    """トレース表・P3・FAQ 以外（比較表・導入文）は比べない。業種名は伏せてある。"""
    a = write(arts, "a-attendance", "歯科医院", **SAME)
    b = write(arts, "b-attendance", "美容室", **DIFF)
    sc = SimConfig()
    docs, _ = build_docs([a, b], sc, load_industry_names(industries))
    for d in docs:
        joined = " ".join(v or "" for v in d.parts.values())
        assert "X社" not in joined and "難しい理由" not in joined
        assert "〓業種〓" in joined


def test_missing_part_marker_is_error(tmp_path, arts, industries, test_config_path):
    """C12: 目印がない型Bはエラー（終了コード1）。"""
    a = write(arts, "a-attendance", "歯科医院", **SAME)
    a.write_text(a.read_text(encoding="utf-8").replace("<!-- part: faq -->\n", ""), encoding="utf-8")
    write(arts, "b-attendance", "美容室", **DIFF)
    rc, report = run(tmp_path, industries, test_config_path)
    assert rc == 1
    assert "<!-- part: faq -->" in report and "エラー: 1 件" in report


def test_type_b_without_parent_is_error(tmp_path, arts, industries, test_config_path):
    write(arts, "a-attendance", "歯科医院", parent="", **SAME)
    rc, report = run(tmp_path, industries, test_config_path)
    assert rc == 1 and "parent がない" in report


def test_threshold_and_metric_configurable(tmp_path, arts, industries, test_config_path):
    write(arts, "dental-clinic-attendance-system", "歯科医院", **SAME)
    write(arts, "beauty-salon-attendance-system", "美容室", **DIFF)
    rc, _ = run(tmp_path, industries, test_config_path, "--threshold", "0.01")
    assert rc == 1
    assert similarity.similarity("あいうえお", "あいうえお", "cosine", 3) == pytest.approx(1.0)
    assert 0 < similarity.similarity("あいうえおか", "あいうえおき", "cosine", 3) < 1


def test_mask_industries_longest_first():
    names = ["歯科", "歯科医院"]
    names.sort(key=len, reverse=True)
    assert mask_industries("歯科医院と歯科", names, "〓") == "〓と〓"


def test_data_template_industries_loads():
    assert load_industry_names(DATA_DIR / "industries.csv") == ["(例)歯科医院"]   # NFKC で全角括弧は半角になる


def test_performance_400_docs(tmp_path, industries):
    """C25: 同じ parent の型B 400本（79,800組）を、以前（68秒）より大幅に短く比べられる。"""
    import random
    random.seed(1)
    words = "土曜 シフト 非常勤 打刻 拠点 集計 残業 深夜 有給 申請 承認 医院 スタッフ 管理 勤怠 休憩 月末 締め".split()
    paths = []
    for i in range(400):
        body = " ".join(random.choice(words) for _ in range(200))
        paths.append(write(tmp_path, f"b{i}", "歯科医院", **dict(SAME, fact2=body)))
    sc = SimConfig()
    t0 = time.time()
    docs, errors = build_docs(paths, sc, load_industry_names(industries))
    res = compare(docs, sc)
    assert errors == [] and len(res) == 79800
    assert time.time() - t0 < 40
