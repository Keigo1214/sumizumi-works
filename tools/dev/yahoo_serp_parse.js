// Yahoo! JAPAN 検索の結果ページから、自然検索の上位と広告の数を抜き出す（ブラウザの JavaScript 実行で使う）。
// 2026-09-30〜10-01 のジャンル調査で使ったもの（data/genre-research/serp-yahoo-*.json）。
//
// 使い方（アプリ内ブラウザ。ログイン不要）：
//   1. https://search.yahoo.co.jp/search?p=<URLエンコードした語>&ei=UTF-8 を開く
//   2. このファイルの本文を javascript_tool で実行する
//
// 注意：
//   - **連続取得すると 429（アクセス過多）になる**。2026-09-30 は 2.5〜3 秒間隔で約25語取ったところで止められ、1時間以上解けなかった。
//     2026-10-01 は 40 秒以上の間隔で 12 語取れた。間隔は 40 秒以上にし、1日の語数を絞る
//   - 待ちはツール側で 1 回の wait（最大10秒）を必要な回数だけ取る。wait を何回も並べると、そのたびにタブの情報が出力されて
//     コンテキストが増えるので、調べる語が多いときはサブエージェントに任せて要約だけ受け取る
//   - 自然検索は .Algo の枠。広告は rd.listing.yahoo.co.jp へのリンクの見出し
//   - 大手比較メディアの定義は docs/phase0.5-provisional-genre.md §2-2

(() => {
  const org = [...document.querySelectorAll('.Algo')].map(c => {
    const a = c.querySelector('a.sw-Card__titleInner') || c.querySelector('a[href^="http"]');
    const h = c.querySelector('h3');
    if (!a) return null;
    let host = '?';
    try { host = new URL(a.getAttribute('href')).host.replace(/^www\./, ''); } catch (e) {}
    return [host, (h ? h.textContent : a.textContent).trim().slice(0, 36)];
  }).filter(Boolean);
  return {
    q: new URL(location.href).searchParams.get('p'),
    ads: document.querySelectorAll('a[href*="rd.listing.yahoo.co.jp"] h3').length,
    org: org.slice(0, 9),
  };
})();
