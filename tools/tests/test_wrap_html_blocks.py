"""publish_prep.py の本文を「カスタムHTML」ブロックで包む処理（2026-09-30 追加）。"""

import json

import publish_prep
from publish_prep import wrap_in_html_blocks


def test_wraps_plain_html_in_one_block():
    out = wrap_in_html_blocks("<p>本文</p>\n<table><tr><td>1,000円</td></tr></table>\n")
    assert out == "<!-- wp:html -->\n<p>本文</p>\n<table><tr><td>1,000円</td></tr></table>\n<!-- /wp:html -->\n"


def test_keeps_synced_pattern_reference_outside_blocks():
    html = '<p>前</p>\n<!-- wp:block {"ref":123} /-->\n<h2>後</h2>\n<p>本文</p>\n'
    out = wrap_in_html_blocks(html)
    assert out == ('<!-- wp:html -->\n<p>前</p>\n<!-- /wp:html -->\n\n'
                   '<!-- wp:block {"ref":123} /-->\n\n'
                   '<!-- wp:html -->\n<h2>後</h2>\n<p>本文</p>\n<!-- /wp:html -->\n')


def test_reference_at_start_and_end_does_not_create_empty_blocks():
    html = '<!-- wp:block {"ref":1} /-->\n<p>本文</p>\n<!-- wp:block {"ref":2} /-->\n'
    out = wrap_in_html_blocks(html)
    assert out.count("<!-- wp:html -->") == 1
    assert out.startswith('<!-- wp:block {"ref":1} /-->')
    assert out.rstrip().endswith('<!-- wp:block {"ref":2} /-->')


def test_inline_comment_that_is_not_a_whole_line_is_not_split():
    html = '<p>文中に <!-- wp:block {"ref":1} /--> がある</p>\n'
    out = wrap_in_html_blocks(html)
    assert out.count("<!-- wp:html -->") == 1


def test_publish_prep_output_is_wrapped_with_and_without_cta(good_article, test_config_path, tmp_path):
    """CTA がある記事もない記事も、本文がカスタムHTMLブロックに入る（表示の揃い）。"""
    cfg = publish_prep.load_config(str(test_config_path))
    cta_map = {"prog_001": 555}
    res = publish_prep.prepare(good_article, cfg, cta_map)
    assert not [m for m in res.messages if m.level == "error"], res.messages
    html = res.content_html
    assert html.startswith("<!-- wp:html -->")
    assert '<!-- wp:block {"ref":555} /-->' in html
    # 区切りの行以外に、ブロックの外へはみ出した本文がない
    outside = [ln for ln in html.split("\n\n")
               if not ln.startswith("<!-- wp:html -->") and not ln.startswith("<!-- wp:block")]
    assert outside == [], outside

    text = good_article.read_text(encoding="utf-8").replace("{{CTA:prog_001}}", "")
    nocta = tmp_path / "nocta" / good_article.name
    nocta.parent.mkdir()
    nocta.write_text(text, encoding="utf-8")
    res2 = publish_prep.prepare(nocta, cfg, cta_map, gate=False)
    assert res2.content_html.startswith("<!-- wp:html -->")
    assert "wp:block" not in res2.content_html


def test_wrapping_can_be_turned_off(good_article, test_config_path):
    cfg = publish_prep.load_config(str(test_config_path))
    cfg.setdefault("publish_prep", {})["wrap_html_blocks"] = False
    res = publish_prep.prepare(good_article, cfg, {"prog_001": 555})
    assert "<!-- wp:html -->" not in res.content_html
    json.dumps(res.meta, ensure_ascii=False)
