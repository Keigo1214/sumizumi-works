"""T3（config.example.yaml）とデータファイルの雛形、既存スクリプトの互換性。"""

from __future__ import annotations

import csv
import importlib
from decimal import Decimal

import pytest

from common import extract_numbers, iter_number_matches
from conftest import DATA_DIR


def test_example_config_has_t3_pattern(cfg):
    assert r"^\d{1,2}月$" in cfg["factcheck"]["ignore_patterns"]
    toks = extract_numbers("2026年9月時点・月額1,980円・12月", ignore_patterns=cfg["factcheck"]["ignore_patterns"])
    assert [t.raw for t in toks] == ["1,980円"]


def example_cfg():
    from common import load_config
    from conftest import EXAMPLE_CONFIG
    return load_config(str(EXAMPLE_CONFIG))


def test_example_config_new_sections():
    cfg = example_cfg()
    assert cfg["factcheck"]["manual_check_valid_days"] == 90
    assert cfg["factcheck"]["manual_check_allowed_checkers"] == []          # 既定は空（C13）
    assert "Claude" in cfg["factcheck"]["manual_check_denied_checkers"]
    assert cfg["factcheck"]["manual_check_session_checkers"] == ["factcheck_session"]
    assert cfg["paths"]["terms_file"] == "../data/terms.yaml" and cfg["paths"]["checks_dir"] == "../data/checks"
    assert cfg["factcheck"]["lint"]["per_unit_suffixes"] == ["あたり", "当たり"]   # C16: 毎を外した
    assert cfg["factcheck"]["lint"]["nofact_line_prefixes"] == ["試算の条件：", "計算式："]
    assert cfg["factcheck"]["lint"]["parts"] == cfg["similarity"]["parts"] == ["trace", "checklist", "faq"]
    assert cfg["publish_prep"]["reference_link_mode"] == "text"                  # 判断7
    assert cfg["publish_prep"]["required_checks"] == ["factcheck_session", "owner_review"]      # C35
    assert cfg["paths"]["asp_programs_file"] == "../data/asp-programs.csv"
    assert cfg["similarity"]["threshold"] == 0.5
    assert cfg["publish_prep"]["cta_template"].format(pattern_id=7) == '<!-- wp:block {"ref":7} /-->'
    assert "a8.net" in cfg["affiliate_domains"]
    assert cfg["postcheck"]["ad_disclosure_selector"] == ".ad-disclosure"


def test_extract_numbers_unchanged_by_refactor():
    """iter_number_matches を足しても extract_numbers の結果は同じ。"""
    text = "月額１，９８０円、1.5万円、3万、12k、2026年、50%"
    toks = extract_numbers(text)
    assert [(t.raw, t.value, t.unit) for t in toks] == [
        ("1,980円", Decimal("1980"), "JPY"),
        ("1.5万円", Decimal("15000.0"), "JPY"),
        ("3万", Decimal("30000"), None),
        ("12k", Decimal("12000"), None),
        ("50%", Decimal("50"), "PCT"),
    ]
    assert [m[0] for m in iter_number_matches(text)] == toks


@pytest.mark.parametrize("name,header", [
    ("keywords.csv", ["keyword", "slug", "type", "registered", "note"]),
    ("industries.csv", ["id", "name", "parent", "name_en"]),
    ("slugs.csv", ["ja", "en", "kind", "note"]),
    ("asp-programs.csv", ["program_id", "asp", "advertiser", "program_name", "tracking_domains", "status", "note"]),
])
def test_csv_templates(name, header):
    with (DATA_DIR / name).open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == header
    assert len(rows) == 2                      # 見出し＋記入例1行
    assert "例" in ",".join(rows[1]) or "example" in ",".join(rows[1])


def test_program_rules_has_section_1_8_columns():
    text = (DATA_DIR / "program-rules.md").read_text(encoding="utf-8")
    for col in ("直接リンクの可否", "rel 付与", "計測用の画像タグ", "順位付け", "他社の併記", "表現の禁止事項"):
        assert col in text


def test_sources_readme_exists():
    text = (DATA_DIR / "sources" / "README.md").read_text(encoding="utf-8")
    assert "## 手動確認の記録" in text and "90日" in text


@pytest.mark.parametrize("module", ["factcheck", "price_diff", "publish_prep", "similarity", "postcheck",
                                    "article_lint", "manual_checks"])
def test_modules_import(module):
    importlib.import_module(module)


def test_price_diff_filters_still_work():
    import price_diff
    filters = price_diff.compile_filters([r"\d{1,2}:\d{2}"])
    assert price_diff.apply_filters("更新 12:34 料金 1,980円", filters).count("<DYNAMIC>") == 1


def test_example_config_lists_match_code_defaults():
    """config.example.yaml に書いた一覧と、コードの既定値が同じ（片方だけ直して食い違わないように）。"""
    import article
    import article_lint
    lint = example_cfg()["factcheck"]["lint"]
    assert lint["banned_expressions"] == article.DEFAULT_BANNED_EXPRESSIONS
    assert lint["caution_expressions"] == article.DEFAULT_CAUTION_EXPRESSIONS
    assert lint["comparison_expressions"] == article.DEFAULT_COMPARISON_EXPRESSIONS
    assert lint["enumeration_patterns"] == article_lint.DEFAULT_ENUMERATION_PATTERNS
    assert lint["heading_ordinal_patterns"] == article_lint.DEFAULT_HEADING_ORDINAL_PATTERNS


def test_root_gitignore_excludes_build():
    """C26: build/ は生成物なので Git の対象外。"""
    text = (DATA_DIR.parent / ".gitignore").read_text(encoding="utf-8")
    assert "build/" in text.splitlines()


def test_asp_programs_template_loads():
    from article import load_asp_programs, split_domains
    rows = load_asp_programs(DATA_DIR / "asp-programs.csv")
    assert list(rows) == ["prog_example_001"]
    assert split_domains(rows["prog_example_001"]["tracking_domains"]) == ["www17.a8.net"]
