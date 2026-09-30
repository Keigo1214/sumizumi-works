"""T6: postcheck.py（公開後の照合）。公開ページは publish_prep.py の出力から組み立てる。"""

from __future__ import annotations

import pytest

import postcheck
from conftest import AD_HTML, FakeFetcher, published_page
from postcheck import PostConfig, check_page
from publish_prep import prepare


@pytest.fixture
def pc(cfg):
    return PostConfig.from_cfg(cfg)


@pytest.fixture
def built(good_article, cfg):
    res = prepare(good_article, cfg, {"prog_001": 123})
    assert res.errors == []
    return res.content_html


def kinds(issues):
    return sorted(i.kind for i in issues)


def test_published_page_matches(good_article, built, pc):
    issues, info = check_page(good_article, published_page(built), pc)
    assert issues == [], [(i.kind, i.detail) for i in issues]
    assert any("並びで比べました" in x for x in info)


def test_changed_number_is_reported(good_article, built, pc):
    page = published_page(built.replace("<td>400円</td>", "<td>450円</td>", 1))
    issues, _ = check_page(good_article, page, pc)
    assert kinds(issues) == ["number_changed"]
    assert "400円" in issues[0].detail and "450円" in issues[0].detail


def test_changed_calc_value_is_reported(good_article, built, pc):
    page = published_page(built.replace("3,000円", "3,300円"))
    issues, _ = check_page(good_article, page, pc)
    assert kinds(issues) == ["number_changed"]


def test_added_number_is_reported(good_article, built, pc):
    """C8: WordPress 上で書き足した出典のない数値（ページにだけある数値）も報告する。"""
    page = published_page(built + "<p>導入企業は 50,000社 です。</p>")
    issues, _ = check_page(good_article, page, pc)
    assert kinds(issues) == ["number_extra"] and "50,000社" in issues[0].detail


def test_swapped_rows_are_reported(good_article, built, pc):
    """C8: 同じ数値の入れ替え（X社 300円 と Y社 400円）も、並びで比べるので分かる。"""
    swapped = built.replace("<td>300円</td>", "<td>@@</td>", 1).replace("<td>400円</td>", "<td>300円</td>", 1) \
        .replace("<td>@@</td>", "<td>400円</td>", 1)
    issues, _ = check_page(good_article, published_page(swapped), pc)
    assert issues and all(i.kind.startswith("number_") for i in issues)


def test_number_only_in_sidebar_is_not_counted(good_article, built, pc):
    page = published_page(built.replace("2,980円", "2,000円"), sidebar="<p>2,980円のプラン</p>")
    issues, _ = check_page(good_article, page, pc)
    assert kinds(issues) == ["number_changed"]


def test_visible_marker_and_marker_in_source(good_article, built, pc):
    page = published_page(built + "<p>&lt;!-- src: https://x.example.com/ accessed=2026-09-27 --&gt;</p>"
                          + "<!-- nofact -->")
    ks = kinds(check_page(good_article, page, pc)[0])
    assert "marker_visible" in ks and "marker_in_source" in ks


def test_placeholder_visible(good_article, built, pc):
    issues, _ = check_page(good_article, published_page(built + "<p>{{業種}}</p>"), pc)
    assert kinds(issues) == ["placeholder_visible"]


def test_missing_ad_disclosure(good_article, built, pc):
    issues, _ = check_page(good_article, published_page(built, ad=""), pc)
    assert kinds(issues) == ["ad_disclosure_missing"]


def test_ad_disclosure_without_word(good_article, built, pc):
    issues, _ = check_page(good_article, published_page(built, ad='<div class="ad-disclosure">PR</div>'), pc)
    assert kinds(issues) == ["ad_disclosure_text"]


@pytest.mark.parametrize("quote", ['"', "'"])
def test_ad_disclosure_after_first_heading(good_article, built, pc, quote):
    """C9: 属性の引用符が単引用符でも、ツリー上の順序で判定する。"""
    ad = AD_HTML.replace('class="ad-disclosure"', f"class={quote}ad-disclosure{quote}")
    page = published_page(built, ad="").replace("</h2>", "</h2>" + ad, 1)
    issues, _ = check_page(good_article, page, pc)
    assert kinds(issues) == ["ad_disclosure_position"]


def test_h2_in_header_does_not_break_position_check(good_article, built, pc):
    """C9: 本文の外（ヘッダ）の h2 と比べない。"""
    page = published_page(built, head="<h2>サイトのキャッチコピー</h2>")
    assert check_page(good_article, page, pc)[0] == []


def test_ad_in_footer_is_position_error(good_article, built, pc):
    page = published_page(built, ad="").replace("</body>", f"<footer>{AD_HTML}</footer></body>")
    assert kinds(check_page(good_article, page, pc)[0]) == ["ad_disclosure_position"]


def test_ad_position_unknown_without_content_root(good_article, built, pc):
    page = published_page(built).replace("entry-content", "post-body")
    ks = kinds(check_page(good_article, page, pc)[0])
    assert "content_selector_missing" in ks and "ad_disclosure_position_unknown" in ks


def test_affiliate_link_without_rel(good_article, built, pc):
    page = published_page(built).replace('rel="nofollow sponsored"', 'rel="noopener"')
    assert kinds(check_page(good_article, page, pc)[0]) == ["affiliate_rel_missing"]


def test_tracking_image_missing(good_article, built, pc):
    """判断9: programs ごとに、asp-programs.csv の計測ドメインの <img> が本文にあるか。"""
    page = published_page(built).replace("https://www17.a8.net/0.gif", "https://example.com/x.gif")
    # CTA の中の計測用でない画像（cta_element）と、計測用の画像タグの欠落の両方が出る
    assert kinds(check_page(good_article, page, pc)[0]) == ["cta_element", "tracking_img_missing"]


def test_main_with_html_file(good_article, built, tmp_path, test_config_path):
    f = tmp_path / "page.html"
    f.write_text(published_page(built), encoding="utf-8")
    out = tmp_path / "r.md"
    rc = postcheck.main([str(good_article), "--html-file", str(f), "-c", str(test_config_path), "-o", str(out)])
    assert rc == 0, out.read_text(encoding="utf-8")
    f.write_text(published_page(built, ad=""), encoding="utf-8")
    assert postcheck.main([str(good_article), "--html-file", str(f), "-c", str(test_config_path),
                           "-o", str(out)]) == 1


def test_main_with_url_uses_polite_fetcher(good_article, built, tmp_path, monkeypatch, test_config_path):
    url = "https://site.example.com/dental-clinic-attendance-system/"
    fake = FakeFetcher({url: published_page(built)})
    monkeypatch.setattr(postcheck, "build_fetcher", lambda cfg, ignore_robots=False: fake)
    rc = postcheck.main([str(good_article), "--url", url, "-c", str(test_config_path),
                         "-o", str(tmp_path / "r.md")])
    assert rc == 0 and fake.calls == [url]
    fake404 = FakeFetcher({})
    monkeypatch.setattr(postcheck, "build_fetcher", lambda cfg, ignore_robots=False: fake404)
    assert postcheck.main([str(good_article), "--url", url, "-c", str(test_config_path),
                           "-o", str(tmp_path / "r.md")]) == 2
