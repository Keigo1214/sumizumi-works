// A8.net メディア管理画面（https://media-console.a8.net/）の検索結果から、案件を抜き出す（ブラウザの JavaScript 実行で使う）。
// 2026-10-01 のジャンル調査で使ったもの（docs/phase0.5-provisional-genre.md §2-3・§7）。
//
// 使い方（Claude in Chrome / Edge のログイン中のタブで）：
//   1. 検索結果のページを開く
//      - キーワード：https://media-console.a8.net/program/search/keyword?keywords=<URLエンコードした語>&pageNo=1&pageSize=100
//      - カテゴリ  ：https://media-console.a8.net/program/search/category?categories=<コードをカンマ区切り>&pageNo=1&pageSize=100
//        大分類のコード（2026-10-01）：不動産・引越 0094–0102／仕事情報 0103–0112／学び・資格 0113–0122／
//        暮らし 0123–0154／Webサービス 0155–0168／インターネット接続 0169–0174
//      - プログラム詳細：https://media-console.a8.net/program/detail-not-partnered?programId=<ID>
//   2. 読み込みを 6〜7 秒待つ（**待ちはツール側の wait で取る**。タブが裏にあるとページ内の setTimeout が遅れて、45秒の上限を超える）
//   3. このファイルの本文を javascript_tool で実行する（MIN_YEN・MIN_RATE を変えれば線を変えられる）
//
// 注意：
//   - 会員向けの画面の情報なので、報酬額・確定率を記事やサイトに転載しない
//   - 読むだけ。「提携申請をする」ボタンは押さない
//   - ログインは数時間で切れる（再認証の画面になる）。パスワードの入力は Claude はしない → 実行者に再ログインしてもらう
//   - 並び順が安定せず、ページをまたいで同じ案件が重なって出ることがある（ID で重複を除く）
//   - 判定の線（STRATEGY Phase 2）：報酬額 5,000円以上（成果地点が複数なら、すべて 5,000円以上）かつ確定率の表示 55%以上。
//     ％の報酬（購入の◯％）は対象外。確定率の表示なしは不合格

(() => {
  const MIN_YEN = 5000, MIN_RATE = 55;
  const txt = (document.querySelector('main') || document.body).innerText.replace(/\t/g, '');
  const total = (txt.match(/該当件数\s*([\d,]+)件/) || [])[1];
  const ids = [...txt.matchAll(/プログラムID\s*(s\d+)/g)];
  const seen = new Set(), all = [], pass = [];
  for (let k = 0; k < ids.length; k++) {
    if (seen.has(ids[k][1])) continue;
    seen.add(ids[k][1]);
    const idPos = ids[k].index;
    const prevEnd = k === 0 ? txt.lastIndexOf('一括提携する', idPos) : ids[k - 1].index;
    const head = txt.slice(prevEnd, idPos).split('\n').map(s => s.trim()).filter(Boolean);
    const st = head.findIndex(l => /^(未提携|提携中|申請中|否認|提携解除)$/.test(l));
    const title = (st >= 2 ? head[st - 2] : head[0]) || '';
    const adv = st >= 1 ? head[st - 1] : '';
    const body = txt.slice(idPos, k + 1 < ids.length ? ids[k + 1].index : txt.length);
    const rf = (body.match(/成果報酬\n([\s\S]*?)\n\s*EPC/) || [])[1] || '';
    const reward = rf.split(/Wチャンス|キャンペーン|【|★/)[0];
    const pct = /[％%]/.test(reward);
    const yens = [...reward.matchAll(/([\d,]+)円/g)].map(m => +m[1].replace(/,/g, ''));
    const rate = parseFloat((body.match(/確定率\n\s*([\d.]+)%/) || [])[1]);
    const cat = ((body.match(/カテゴリ\s*(.+)/) || [])[1] || '').trim();
    const rec = [ids[k][1], adv, title.slice(0, 34), reward.replace(/\s*\n\s*/g, ' / ').trim().slice(0, 80), isNaN(rate) ? '-' : rate + '%', cat].join(' | ');
    all.push(rec);
    if (yens.length > 0 && !pct && yens.every(y => y >= MIN_YEN) && rate >= MIN_RATE) pass.push(rec);
  }
  // 出力を小さくするため、通常は pass と件数だけを返す。全件が要るときは all も返す
  return { total, uniq: seen.size, pass };
})();
