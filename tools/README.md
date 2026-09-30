# tools/ — Phase 0-D 運用自動化

STRATEGY.md「Phase 0-D 運用自動化」の実装。**これは 800〜1,200本計画の前提条件**であって、
あれば便利なツールではない。手動リライトのままでは週6時間で維持できる記事数は 156本で頭打ちになる
（STRATEGY.md §2 の表）。

| スクリプト | 役割 | 実行頻度の目安 |
|---|---|---|
| `factcheck.py` | 記事内の数値と出典URLを機械照合。不一致だけを報告。`--lint` で書き方の検査（T1） | 記事公開前（毎回）＋ 週次で全記事 |
| `publish_prep.py` | 反映前の関門（T10）を確かめ、正本から WordPress に反映する成果物（下書き）と関門の要約を作る（T5・T8） | 反映のたび |
| `postcheck.py` | 正本と公開ページを突き合わせる（T6） | 公開後24時間以内 |
| `similarity.py` | 同じ商材の型B記事同士の類似度（T4） | 型B記事の公開前 |
| `price_diff.py` | 掲載企業の料金ページの差分検知 | 週1（できれば毎日） |
| `cannibalization.py` | Search Console からカニバリゼーション検出 | 月1（Phase 7 の月次分析） |
| `weekly_report.py` | GA4 + Search Console の週次レポート生成 | 週1（Phase 6 の週次計測） |

補助モジュール: `common.py`（HTTP取得・数値正規化・設定）、`google_clients.py`（OAuth）、
`article.py`（正本の読み込み・マーカーの定義）、`article_lint.py`（lint の本体）、
`manual_checks.py`（手動確認の記録）。

T1〜T8 は `docs/article-template.md` §8「公開開始前に必須の整備」の番号。
品質ゲート（設計書 §6）のどの項目をどのコマンドで確かめるかは **§14** の対応表。

---

## 1. セットアップ

### 1.1 Python 環境

Python 3.10 以上。

```powershell
cd C:\Users\matsu\affiliate
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r tools\requirements.txt
```

> PowerShell で `Activate.ps1` が実行できない場合:
> `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`

### 1.2 設定ファイル

```powershell
Copy-Item tools\config.example.yaml       tools\config.yaml
Copy-Item tools\watch_targets.example.yaml tools\watch_targets.yaml
Copy-Item tools\.env.example              tools\.env
```

- `tools/config.yaml` … 挙動の設定。**認証情報は書かない**
- `tools/.env` … 認証情報とサイト固有の値。**Git管理外**（`tools/.gitignore` で除外済み）

最低限、`config.yaml` の `fetch.user_agent` を自分のサイト名と連絡先に書き換えること。
他社サーバーにアクセスする以上、匿名でのクロールはしない。

---

## 2. Google API のセットアップ（実行者の作業）

CLAUDE.md ルール8のとおり、API の初回認証は実行者が行う。以下は一度だけの作業。

### 2.1 Google Cloud プロジェクトを作る

1. https://console.cloud.google.com/ にアクセス
2. 上部のプロジェクト選択 → 「新しいプロジェクト」
3. プロジェクト名（例: `affiliate-ops`）→ 作成

### 2.2 API を有効化する

「APIとサービス」→「ライブラリ」で以下2つを検索し、それぞれ「有効にする」。

| API 名 | 使うスクリプト |
|---|---|
| **Google Search Console API** | `cannibalization.py`, `weekly_report.py` |
| **Google Analytics Data API** | `weekly_report.py` |

### 2.3 OAuth 同意画面を設定する

1. 「APIとサービス」→「OAuth 同意画面」
2. User Type: **外部** を選択 →「作成」
   （Google Workspace アカウントなら「内部」でよい。個人 Gmail は「外部」しか選べない）
3. アプリ名（例: `affiliate-ops`）、ユーザーサポートメール、デベロッパー連絡先を入力
4. スコープ: ここでは追加しなくてよい（スクリプト側で要求する）
5. **テストユーザー**: 自分の Google アカウントを追加する ← **これを忘れると認証が通らない**
6. 公開ステータスは「テスト」のままでよい

> 「テスト」状態だとリフレッシュトークンが **7日で失効する**。
> 7日ごとに再認証したくない場合は、同意画面を「本番環境に公開」する
> （個人利用・機密スコープなしなら審査は不要で、ボタンを押すだけ）。

### 2.4 認証情報（クライアントシークレット）を作る

1. 「APIとサービス」→「認証情報」→「認証情報を作成」→「OAuth クライアント ID」
2. アプリケーションの種類: **デスクトップ アプリ**
3. 名前は任意 →「作成」
4. **JSON をダウンロード**（`client_secret_xxxx.json`）

ダウンロードしたJSONは**リポジトリの外**に置く。例:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.config\affiliate"
Move-Item "$env:USERPROFILE\Downloads\client_secret_*.json" "$env:USERPROFILE\.config\affiliate\client_secret.json"
```

`tools/.env` に絶対パスを書く:

```
GOOGLE_OAUTH_CLIENT_SECRETS=C:\Users\matsu\.config\affiliate\client_secret.json
```

### 2.5 プロパティIDを .env に入れる

| 変数 | 取得場所 | 形式 |
|---|---|---|
| `GSC_SITE_URL` | Search Console のプロパティ | ドメインプロパティ → `sc-domain:example.com`<br>URLプレフィックス → `https://example.com/`（**末尾スラッシュ必須**） |
| `GA4_PROPERTY_ID` | GA4 → 管理 → プロパティ設定 →「プロパティID」 | `123456789`（数字のみ。測定ID `G-XXXX` ではない） |

### 2.6 初回認証を実行する

```powershell
python tools\cannibalization.py --auth-only
```

ブラウザが開くので、2.3 でテストユーザーに登録したアカウントで許可する。
「このアプリは確認されていません」と出たら「詳細」→「（アプリ名）に移動」で進む。
成功すると `tools/.gsc_token.json` が作られ、以降は無人実行できる。

> トークンファイルは**パスワードと同等**。Gitにも他人にも渡さないこと。
> 失効・破損したら削除して `--auth-only` をやり直せば再発行される。

### 2.7 権限の確認

認証したGoogleアカウントに、以下の権限が必要。

- Search Console: 対象プロパティの「フル」または「制限付き」ユーザー
- GA4: 対象プロパティの「閲覧者」以上

（サービスアカウントで無人運用する場合は、サービスアカウントのメールアドレスを
上記2箇所に追加し、`.env` の `GOOGLE_APPLICATION_CREDENTIALS` に鍵JSONのパスを設定する。
OAuth より優先される。）

---

## 3. factcheck.py — ファクトチェック機械照合

記事の数値と出典URLを照合し、**一致しなかったものだけ**を報告する。
人間は不一致アラートだけを見ればよい（STRATEGY.md §13 でファクトチェック 6h → 3h を想定）。

### 3.1 出典マーカーの書式（記事側のルール）

**書式の仕様は記事テンプレート（`docs/article-template.md` §1-2）。本ツールはそれに合わせて実装している。**
出典が書かれていない数値は照合されない — だから `--lint`（§3.6）で「マーカーのない数値」をゼロにする。

使ってよい記法は (1) 行内マーカー と (2) ブロックマーカー だけ。計算した値は (3) calc-block、
事実の主張ではない数は (4) nofact。**脚注（`[^a]`）は記事テンプレートで禁止**（照合モードは古い記事との互換のため読むが、lint はエラーにする）。

#### (1) 行内マーカー

行頭（または直前のマーカー）から、このマーカーまでの数値が対象になる。`accessed=YYYY-MM-DD` は**必須**。
読者向けの出典表記はマーカーの**後ろ**に書く（前に書くと「9月」が照合範囲に入る）。

```markdown
スタンダードプランの月額は 1,980円 です。<!-- src: https://example.com/pricing accessed=2026-09-27 -->（◯◯公式サイト、2026年9月時点）
```

1文を1行にし、マーカーは文末に置く。1行に複数の文を書くと、前の文の数値まで同じURLで照合される（lint が警告する）。

#### (2) ブロックマーカー（出典が1つだけの表）

開始から終了までの**全行**が対象。中には表だけを書く（注記・URL・行内マーカーは書かない）。注記はブロックの外に。

```markdown
<!-- src-block: https://example.com/pricing accessed=2026-09-27 -->
| プラン | 月額 | ユーザー数上限 |
|---|---|---|
| ライト | 1,000円 | 10人 |
| スタンダード | 2,980円 | 50人 |
<!-- /src-block -->
（◯◯公式サイト、2026年9月時点・税抜）
```

#### (3) 計算した値（calc-block）

入力値の表（各行に出典マーカー）→「計算式：」の行 →試算結果の表を calc-block で囲む。中の行は照合しない
（`src-block` の中に入れても照合しない。ただし入れ子は lint のエラー）。中は**表の行だけ**、1列目は入力値の表の1列目にある名前だけ。

```markdown
| サービス | 初期費用 | 1人あたり月額 | 最低人数 | 契約形態 |
|---|---|---|---|---|
| X社 | 0円 | 300円 | 10人 | 月払い可 |<!-- src: https://x.example.com/pricing accessed=2026-09-27 -->

計算式：1人あたり月額 × 利用人数

<!-- calc-block -->
| サービス | 月額の試算 |
|---|---|
| X社 | 3,000円 |
<!-- /calc-block -->
```

#### (4) 事実の主張ではない数（nofact）

`<!-- nofact -->` を行末に書くと、その行は照合・lint の対象外になる。**置けるのは次の行だけ**（lint が強制する）。

- 「試算の条件：」で始まる専用の行。書ける数値は**人数・期間・回数**（`nofact_condition_units`。既定 人・名・か月・ヶ月・カ月・月・回・年・日・時間）だけ。円・社・%・割・単位のない数はエラー
- 「計算式：」で始まる専用の行。書ける数値は**単位のない定数**（× 12 など）だけ
- 見出しの行。ただし見出しの数値が**すべて数え上げ**（「3つの要件」「5選」）か**順番**（「ポイント1：」「ポイント2」行末、「ポイント3（…）」）の場合だけ。「ポイント1,000円分」「最大ポイント10倍」のように番号の後ろに単位や数字が続くものは順番とみなさない

```markdown
試算の条件：10人で利用し、月払いの場合（初期費用は含まない）<!-- nofact -->

## 歯科医院に必要な3つの要件 <!-- nofact -->
```

相場観など、出典のない事実の数値の逃げ道には使えない（「月額はおおよそ 2,000円 前後が相場です。<!-- nofact -->」「試算の条件：月額 2,000円 の場合 <!-- nofact -->」は lint のエラー）。

#### (5) 部分の目印（part）と関連記事の区間

型Bの正本には、類似度チェック（§12）で比べる部分に目印を付ける。**型Bでは trace・checklist・faq の3つが必須**。
**型A・型Bとも、関連記事の区間 `related` が必須**。

```markdown
## 歯科医院に必要な3つの要件 <!-- nofact -->

<!-- part: trace -->
| 業種固有の事実（出典つき） | そこから導かれる要件 | 製品への影響 |
|---|---|---|
| …<!-- src: https://… accessed=2026-09-27 --> | … | … |

### 土曜のシフトを組める
…（要件の説明。trace の区間の中）
<!-- /part -->
<!-- part: checklist -->
判断基準チェックリスト（必須／あると便利）

- 必須：…
- あると便利：…
<!-- /part -->

## よくある質問

<!-- part: faq -->
Q. …
A. …
（3問以上）
<!-- /part -->

## 関連記事

<!-- part: related -->
- [リンク先の記事の title と同じ文字列](/slug/)
<!-- /part -->
```

- **目印は、見出しの直後から次の同じ（以上の）階層の見出しの直前まで、区間の全体を囲む**（区間の中に目印の外の本文を残さない）。
  続けて置いた part（上の trace の直後の checklist）は、合わせて1つの区間を覆えばよい。区間の中の下の階層の見出しはよい
- `trace` の中の表は3行以上で、各行に出典マーカーが要る。`faq` は質問（「Q.」で始まる行か、？で終わる見出し）が3問以上、
  `checklist` はリストの項目が2つ以上（§4）
- **`related`（関連記事の区間）の中に書けるのは、リスト項目1つにつき内部リンク1つの行だけ**:
  - 型B: `- [リンク先の title](/slug/)`。型A（業種別の選び方）: `- 業種名：[リンク先の title](/slug/)`（業種名は `data/industries.csv` の name か aliases）
  - リンクの文字列はリンク先の正本（**同じディレクトリの** `{slug}.md`）の `title` と完全に一致すること。リンク先の `published` が今日以前であること
  - リンク先は `/` で始まる相対パスか、`config.yaml` の `site_domains` のドメインだけ。アフィリエイトのドメイン・`rel` 属性（HTML のリンク）・画像・見出し・段落・表・`{{CTA}}`・出典マーカー・nofact・calc-block・入れ子の part・URL だけの行（Cocoon のブログカードになる）は書けない
  - `related` は1記事に1つ。**この区間の中の行は正本のハッシュから外す**（§11.4）ので、新しい記事を公開したときに関連記事のリンクを足しても、他の記事の品質ゲートの記録をやり直さなくてよい（設計書 §7 工程11）
- 目印は publish_prep.py が除去する

**補足**
- 1マーカーに書けるURLは1つ。複数出典なら文を分ける
- 西暦らしき4桁（1900〜2100、単位なし）は既定で照合対象外（`--no-skip-years` で有効化）
- 行の役割（表・見出し・リスト・コードブロック）は **markdown-it のトークン**で判定する（publish_prep.py が WordPress に渡す HTML を作るときと同じ解釈）。行は `\n` だけで分ける（U+2028 などでは分けない）。BOM・CRLF は吸収する
- `.md` では front matter と、番号付きリストの行頭の番号（本当にリスト項目の行だけ）を照合しない。インラインコード（`` `…` ``）の中の数値は照合する（lint はエラー）
- **見えない書式文字**（ゼロ幅スペース U+200B・ソフトハイフン U+00AD など Unicode の Cf 類）は、数値を読む前に除く（lint はエラー。publish_prep の出力からも除く）
- **決まった言い回し**（`data/terms.yaml`。「36協定」「二段階認証」「三交代制」など）の中の数字は、lint・照合とも数値として扱わない。登録してよいのは名前として固まった語だけ（事実の数値は登録しない）。置き場所は `config.yaml` の `paths.terms_file`

### 3.2 表記ゆれの正規化

記事側とページ側の数値は、比較前に以下を揃える。

| 種類 | 例 | 正規化後 |
|---|---|---|
| 全角数字 | `１，９８０円` | `1980` + 単位 JPY |
| 桁区切り | `1,000` / `1000` | `1000` |
| 通貨記号 | `円` / `￥` / `¥` | JPY |
| パーセント | `%` / `％` | PCT |
| 算用数字＋桁の漢字 | `1,234億円` / `5千円` / `3万` | `123400000000` / `5000` / `30000` |
| 英語の桁 | `12k` / `3M` | `12000` / `3000000` |
| 小数 | `1.5万円` | `15000` |

漢数字だけの数（「一万二千社」「九割」）は数値として読めないので、lint のエラーにする（§3.6）。
単位は意味カテゴリに寄せて比較する（`円`=`￥`=`¥`、`ヶ月`=`か月`=`カ月`）。
ページ側は表のセル分割で単位が数値から離れることが多いため、既定では
「ページ側の単位が取れていない数値」も一致候補にする（その場合は §3.8 の偶然の一致の候補に出る）。厳しくするなら `--strict-units`。

### 3.3 実行例

```powershell
# 1ファイル。標準出力にレポート
python tools\factcheck.py articles\dental-clinic-attendance-system.md

# ディレクトリを再帰、レポートをファイルに
python tools\factcheck.py articles\ --recursive --out tools\reports\factcheck.md

# JSONも出す
python tools\factcheck.py articles\ -r --json tools\reports\factcheck.json -v
```

終了コード: `0` 問題なし / `1` 要確認あり（不一致・lint のエラー・偶然の一致の候補） / `2` 実行エラー

レポートの冒頭には、モード（公開前の関門／週次の照合）・要確認の件数・偶然の一致の候補の件数・lint のエラーの件数・
**手動確認の記録で報告から外した件数**が出る。関門モード（既定）で合格した記事については、**正本のハッシュ**と、
受領ファイル `data/checks/{slug}.json` を書いたことが出る（§11.2。不合格の記事のハッシュは出さない）。

```powershell
python tools\factcheck.py articles\foo.md                 # 公開前の関門（既定。偶然の一致の候補があれば 1、合格なら受領ファイル）
python tools\factcheck.py articles\ -r --mode weekly      # 週次の全記事の照合（候補は報告だけ・受領ファイルは書かない）
```

### 3.4 レポートに出るもの

一致した数値は**出さない**。出るのは以下だけ。

| 種別 | 意味 | 対応 |
|---|---|---|
| 数値が参照先に見つからない | 記事の数値が出典ページに存在しない | 記事を直すか、出典を差し替える |
| 出典URLが取得できない | 404 / 接続失敗 / robots.txt 拒否 | リンク切れなら出典を探し直す。robots.txt 拒否は人が確認して §3.7 に記録 |
| 出典URLがリダイレクトしている | 移転・正規化 | 最終URLに書き換える |
| 参照先の本文を抽出できない | JSで描画されるページ | 人が確認して §3.7 に記録 |
| 脚注の定義がない / src-block が閉じていない | 記事側の記法ミス | 記事を直す |
| 偶然の一致の可能性 | §3.8 | 文脈を確かめ、確かめた組は §3.7 に記録 |
| lint の結果 | `.md` には照合モードでも lint を実行する（§3.6） | 記事を直す |

### 3.5 誤検知が多いとき

1. 記事側が丸めている（公式1,980円 → 記事「約2,000円」）→ **記事側を実数に直す**のが正しい
2. 型番・電話番号など照合不要 → `config.yaml` の `factcheck.ignore_patterns` に正規表現を追加
3. 参照先がJS描画 → `empty_page` として出る。人が確認して §3.7 の手動確認の記録に書く

### 3.6 lint モード（`--lint`。設計書 §8 T1）

出典URLを**取得せず**、正本の書き方だけを検査する。ネットワークに出ないので、書いた直後に何度でも回せる。
**照合モード（`--lint` なし）でも `.md` には lint を実行**し、結果をレポートに併記する（lint のエラーがあれば終了コード1で、
「すべての数値が出典先で確認できました」とは出さない）。publish_prep.py も書き出す前に lint を実行する。

```powershell
python tools\factcheck.py --lint articles\dental-clinic-attendance-system.md
python tools\factcheck.py --lint articles\ -r --out tools\reports\lint.md --json tools\reports\lint.json
```

終了コード: `0` 合格（エラー0件。警告は人が確認） / `1` エラーあり / `2` 実行エラー。
対象は `.md`（正本）だけ。ディレクトリの中に `.html` があればエラーにする（正本は `.md` だけ。公開ページの照合は postcheck.py）。

レポートには記事ごとに、判定・（合格したときだけ）**正本のハッシュ**・出典マーカーの件数・**`nofact` の件数と行番号**・**`calc-block` の件数と行番号**を必ず出す
（`nofact` が許された行にしか置けなくても、置いた行は人が見て確かめる）。

**報告するもの**（エラーは終了コード1。警告は終了コードを変えない）

| 種別（レポートの表記） | 重さ | 内容 | 設計書 |
|---|---|---|---|
| マーカーのない行の数値 | エラー | マーカーも `nofact` もない行の数値。見出し・表の見出し行・「計算式：」の行も含む | T1・§1-2 |
| 最後のマーカーより後ろの数値 | エラー | 行の中で最後のマーカーより後ろの数値（どの照合範囲にも入らない） | 制約4 |
| accessed= の欠落／書式の誤り | エラー | `src`・`src-block` の全マーカー。未来の日付は警告 | 制約6 |
| 出典マーカーが0件 | エラー | issue として扱い終了コード1 | T1 |
| calc-block の直前に入力値の表・計算式がない | エラー | 下の「calc-block の規則」 | §1-2 計算した値 |
| calc-block の中の表以外の行／試算表の行が入力値の表にない | エラー | calc-block の中は表の行だけ。1列目の名前は入力値の表の1列目にあるものだけ | §1-2 |
| calc-block・src-block・part の入れ子 | エラー | どちら向きも | 制約7 |
| 脚注記法（禁止） | エラー | `[^x]` の参照と定義行。脚注はマーカーの件数に数えない | §1-2 |
| title・description の数が本文と一致しない | エラー | 下の「title・description の数」 | §1-2 |
| 漢数字の数値 | エラー | 二以上の漢数字＋単位・助数詞（「三つ」「十日」「一万二千社」「千九百八十円」「九割」）、「一」でも金額・割合（「一円」「一割」）、「1万5,000円」のような混在。本文・title・description・note・alt。`data/terms.yaml` の語と `kanji_exclusions`（漢数字＋助数詞の全体を含むときだけ）は対象外 | §1-2 数値の書き方 |
| インラインコードの中の数値 | エラー | `` `9,999円` ``（照合の対象から外れやすいため書かない） | — |
| 本文のコードブロック／閉じていないコードブロック | エラー | 比較記事には不要。閉じていないと以降の本文と P7 がすべてコードになる | — |
| HTMLの表 | エラー | `<table>` などは使わない（表の行の規則を確かめられない）。Markdown の表にする | — |
| 表の行として扱われる注記 | エラー | 表の直後に空行なしで書いた行は、GFM では表の行になる。表の後に空行を入れる | — |
| 禁止表現（§1-6） | エラー | 「No.1」「1位」「最安」「業界唯一」「一番おすすめ」「使ってみた」など。本文・title・description（一覧は `banned_expressions`） | §1-6 |
| nofact を置けない行／見出しの nofact に数え上げ・順番以外の数値／計算式の行の単位付きの数値 | エラー | §3.1 (4) | §1-2 |
| nofact と出典マーカーが同じ行／表の行の nofact／src-block の中の nofact | エラー | | §1-2 |
| src-block の中の行内マーカー／URL／calc-block の中の出典マーカー | エラー | 表以外の行（注記）は警告 | 制約3・5・7 |
| ブロックマーカーが単独の行にない／閉じていない／開始のない終了タグ | エラー | src-block・calc-block・part | — |
| 型Bの部分の目印がない／部分の目印の誤り／要件トレース表の行数・出典 | エラー | §3.1 (5) | §4・§8 T4 |
| 型Bの推薦が3つ以上 | エラー | 「おすすめ」を含む見出しの直下の見出しが3つ以上（`recommend_heading_patterns`・`max_recommendations`） | §1-10 |
| 部分の目印が区間の全体を囲んでいない／FAQ の質問・チェックリストの項目が少ない | エラー | §3.1 (5)。全部の part（related を含む） | §4 |
| 関連記事の区間の書き方／関連記事の区間がない | エラー | §3.1 (5)。型A・型Bとも `related` が必須 | §4・§7 工程11 |
| 試算の条件の行の単位／試算表の見出し行の数値／試算表のセルの形 | エラー | §3.1 (4)・§3.6「calc-block の規則」 | §1-2 |
| 見えない文字 | エラー | U+200B・U+00AD など Unicode の Cf 類。本文・title・description・history の note | §1-2 |
| 画像の alt の数値 | エラー | Markdown の `![alt](...)` と HTML の `<img alt>`。禁止表現・漢数字も alt を含めて検査する | — |
| history の note | エラー | note に数値を書かない（P7 にそのまま出るが照合を通らない）。禁止表現・漢数字も検査 | §1-1 |
| 「一」＋助数詞の漢数字 | 警告 | 「一人」「一日」「一回」など。事実の数なら算用数字＋出典、決まった言い回しなら `data/terms.yaml` | §1-2 |
| front matter の programs が案件の一覧にない | エラー | `data/asp-programs.csv` の `program_id` にない | 判断9 |
| front matter がない・読めない／slug が不正／title・description がない | エラー | slug は英小文字・数字・ハイフンだけ（行末の改行も不可）、Windows の予約名（con・nul・aux・prn・com1〜9・lpt1〜9）も不可 | §1-1・§1-4 |
| 文字コード | エラー／警告 | UTF-8 でも cp932 でも読めない → エラー。cp932 で読めた → 警告（UTF-8 で保存し直す） | — |
| 注意する表現 | 警告 | 「必ず」「絶対」「確実に」「他社より安」、条件を同じ行に書いていない「最も低い」など | §1-6 |
| 1行に複数の文 | 警告 | 前の文の数値まで同じURLで照合される | 制約1 |
| マーカー以外のHTMLコメント | 警告 | `<!-- scr: -->` のような綴りの誤りを拾う | — |
| `{{ }}` の残り（`{{CTA:…}}` 以外） | 警告 | 本文。title・description はエラー。publish_prep.py はエラー | §6 |

**対象外にするもの**: front matter／番号付きリストの行頭の番号（リスト項目の最初の行だけ）／
calc-block の中／`ignore_patterns` に当たる数値／`nofact` の付いた行（許された行だけ）／西暦らしき4桁／HTMLコメントの中／
URL（Markdown リンクの URL を含む）／`{{ }}` の中／「1人あたり」「1施設あたり」「1ヶ月あたり」の 1
（数値の直後に助数詞0〜2文字、その直後が「あたり」「当たり」の場合だけ。`drop_per_unit_one`・`per_unit_suffixes`。
「1社限定で毎月」の 1 は対象外にしない）。

**calc-block の規則**

1. calc-block の直前（空行を除く）の行が「計算式：」（または「計算式:」）で始まる
2. その前が表。表と「計算式：」の間には、空行と、**数値を含まない**注記行を最大2行まで挟める（`calc_basis_max_note_lines`）
3. 表のデータ行が**すべて**行末に行内マーカーを持つか、表全体が `src-block` で囲まれている
4. calc-block の中は表の行だけ。試算表の1列目の名前は、入力値の表の1列目にあるものだけ
5. calc-block が2つ続く場合は、それぞれに入力値の表と計算式が要る

**title・description の数**（設計書 §1-2「タイトル・メタディスクリプションの数値」）

front matter の `title`・`description` の数を2種類に分ける。区別は `config.yaml` の
`factcheck.lint.enumeration_patterns`（正規表現。名前付きグループ `n` に数、`noun` に名詞）で設定する。同じ位置では上のパターンが優先。

| 種類 | 既定の書き方 | 検査 |
|---|---|---|
| 数え上げの数 | `Nつの＋名詞`（名詞も比べる）、`Nつ`、`N選`、`N項目`／`Nステップ`／`N問`／`Nか条`、`N社を比較`／`N社の比較`／`N社比較` | 同じ書き方・同じ数（名詞があれば同じ名詞）の**見出し**が本文にあること。その見出しの下の項目の数が N と一致すること。数え方は ①直下の見出し ②区間の最初の表のデータ行 ③区間の最上位のリスト項目 の順。どれもなければ「数えられない」エラー |
| 事実の数値 | 上に当たらない数すべて | 本文に**出典つき**で書いた数値（行内マーカー・src-block の範囲。calc-block と nofact の行、「1人あたり」の 1 は含まない）に、**同じ値・同じ単位**があること。単位のない数値は title に書けない。「1万社」と「10,000社」は同じ値 |

西暦（「【2026年最新】」）と `ignore_patterns`（「9月」）は数えない。禁止表現（「No.1」「1位」）は別にエラーになる。

### 3.7 手動確認の記録（設計書 §8 T2）

照合で報告された組を確認した記録を、`data/sources/{slug}.md` の「## 手動確認の記録」の表に **URLと数値の組ごと**に書く。
書式は `data/sources/README.md`、雛形は `data/sources/_template.md`。**記録には種類がある**（設計書 §6・§8 T2）。

```markdown
## 手動確認の記録

| URL | 数値 | 種類 | 確認者 | 確認日 | 理由 | ページのハッシュ | メモ |
|---|---|---|---|---|---|---|---|
| https://x.example.com/pricing | 300円 | 不一致 | 実行者の名前 | 2026-09-27 | JavaScript描画 | | 料金表で確認 |
| https://y.example.com/price | 0円 | 偶然の一致 | factcheck_session | 2026-09-27 | 偶然の一致の確認 | 3f9a0c1d2e4b5a67 | 初期費用の欄 |
```

| 種類 | 書ける確認者 | 有効な間 | 外すもの |
|---|---|---|---|
| `不一致`（既定） | `factcheck.manual_check_allowed_checkers` の名前だけ（**実行者**。既定は空） | 確認日から90日（`manual_check_valid_days`） | 数値が参照先に見つからない（その組だけ）、取得できない・本文を抽出できない・リダイレクト（**そのマーカーの全数値に有効な記録があるときだけ**）、偶然の一致の候補 |
| `偶然の一致` | 上の名前か `factcheck.manual_check_session_checkers`（既定 `factcheck_session`。**別セッションのファクトチェック**） | **出典ページから抜き出した本文のハッシュ（「ページのハッシュ」の列）が変わるまで**。日数の期限はない | 偶然の一致の候補だけ（数値の不一致は消せない） |

- **理由は必須**。「Claude」「AI」などを**語として**含む確認者は無効（`manual_check_denied_checkers`。2文字以下の語は大文字小文字を区別するので「Ai」という名前の人は拒否しない）
- `{slug}` は front matter の `slug`（なければファイル名）。**英小文字・数字・ハイフンだけ**（`../` などを含む slug では記録を読まない）。置き場所は `paths.sources_dir` か `--sources-dir`（実行したディレクトリから見た位置）
- 数値は**記事の表記のまま**書く。値と単位で照合するので、**記事側の数値が変わると記録は効かなくなり、報告に戻る**（効かなくなった行はレポートの「使われなかった手動確認の記録」に出る）
- 「不一致」の期限: 確認日＋90日の日までは有効、その翌日から期限切れとして報告に戻る。判定の日付は `--today YYYY-MM-DD` で変えられる
- 「偶然の一致」の「ページのハッシュ」は、照合のレポートの「偶然の一致の可能性」の各行に出る値を写す。出典ページの本文が変われば無効になり、候補に戻る（「本文が変わった」と出る）
- 外した件数はレポートの**冒頭**に、外したものの一覧はレポートの「手動確認の記録で報告から外したもの」に出る。公開前には関門の要約（§11.3）に記録の一覧が出るので、実行者が中身を確かめる。`--no-manual-records` で記録を使わずに全件を出せる

### 3.8 偶然の一致への対策（設計書 §8 T7）

照合で**一致した**数値のうち、次のものは、出典ページ側の前後の文脈と**ページのハッシュ**をレポートの「偶然の一致の可能性」に出す。

- 値が0か一桁の数値（0〜9。`coincidence.max_small_value`）
- 単位のない数値（`coincidence.unitless`）
- 記事側に単位があるのに、一致した出典側の数値がすべて単位なし（型番・電話番号などとの一致）
- 修飾語の食い違い: 記事と出典で「約・最大・最低・最小・以上・以下・未満・超・〜」が一致する箇所がない（「から」と「~」は「〜」と同じとみなす。例: 記事「1,980円」・出典「1,980円〜」）
- ただし「1施設あたり」「1人あたり」の 1 は出さない（lint と同じ規則）

**モードで扱いを分ける**（`factcheck.py --mode`）:

| モード | 使う場面 | 偶然の一致の候補 | 受領ファイル（§11.2） |
|---|---|---|---|
| `gate`（既定） | 公開前の関門（lint＋照合を publish_prep の前に行う） | あれば**終了コード1** | 合格した記事について書く |
| `weekly` | 週次の全記事の照合 | 報告だけ（終了コードは変えない） | 書かない |

`--no-fail-on-coincidence` は `--mode weekly` と同じ（互換のため残している）。
工程7（ファクトチェックのセッション）でこの節を読み、文脈が記事の主張と同じ意味かを確かめ、
**確かめた組は §3.7 の記録に種類「偶然の一致」・ページのハッシュつきで書く**（確認者 `factcheck_session`）。記録が工程7で読んだ証跡になる。

### 3.9 照合モードの変更点（最初の版からの違い）

- **calc-block の中は照合しない**（`src-block` の中にあっても）。HTML の記事でも同じ
- `.md` では front matter・コードブロックを読み飛ばし、番号付きリストの行頭の番号を照合しない（判定は markdown-it のトークン）
- インラインコードの中の数値も照合する。見えない文字は除いてから、決まった言い回しは伏せてから数値を読む
- 読み込みは BOM・CRLF を吸収し、行は `\n` だけで分ける。cp932 で読み直したら警告する
- `.md` には lint も実行する。モード（gate／weekly）で偶然の一致の扱いを分け、gate で合格した記事の受領ファイルを書く。正本のハッシュは合格した記事についてだけ出す
- HTML の記事で `src-block` / `calc-block` のマーカーがタグと同じ行にあっても、マーカーを独立した行として扱う
- それ以外（脚注の読み込み、1行に複数マーカー、URLの取得とキャッシュ、レポートの種別）は変えていない

---

## 4. price_diff.py — 料金改定の差分検知

### 4.1 監視対象の登録

`tools/watch_targets.yaml` に記入する（書式は `watch_targets.example.yaml` を参照）。

**記事に料金を載せた企業は必ず登録する。** 載せた瞬間に鮮度維持の義務が発生する。
`selector` は可能な限り指定する（ページ全文監視は誤検知が桁違いに増える）。

セレクタの調べ方: ブラウザで料金表を右クリック →「検証」→ 該当要素で右クリック →
Copy → Copy selector。取得した長いセレクタは、`main .pricing` 程度まで手で短くする方が
サイト側の小変更に強い。

### 4.2 実行例

```powershell
# 初回。差分は出さずスナップショットだけ作る
python tools\price_diff.py --init

# 通常実行
python tools\price_diff.py --out tools\reports\price_diff.md

# 特定の対象だけ
python tools\price_diff.py --only example-saas-pricing -v

# スナップショットを更新せず様子を見る（フィルタ調整中に使う）
python tools\price_diff.py --dry-run

# Slack等に通知（.env の PRICE_DIFF_WEBHOOK_URL が必要）
python tools\price_diff.py --notify
```

終了コード: `0` 変更なし / `1` 変更あり / `2` エラー

### 4.3 スナップショット

`tools/snapshots/<id>.txt`（正規化後のテキスト）と `<id>.meta.json`（URL・取得日時・ハッシュ）。
**Git管理する**こと。料金の変遷そのものが Phase 5 の被リンク施策
「独自調査データの公開（主要企業の料金推移レポート）」の一次データになる。

```powershell
git add tools\snapshots
git commit -m "snapshot: 2026-08-16 の料金ページ"
```

### 4.4 誤検知（動的要素）の対策

除外フィルタは2箇所に書ける。マッチした部分は `<DYNAMIC>` に置換される。

- `config.yaml` の `price_diff.ignore_patterns` … 全対象共通（日付・時刻・セッションID等）
- `watch_targets.yaml` の `defaults.ignore_patterns` / 各 target の `ignore_patterns` … 個別

フィルタを追加・変更したら、**一度 `--init` でスナップショットを作り直す**
（古いスナップショットにはフィルタが当たっていないため差分が出続ける）。

### 4.5 変更を検知したときの手順

1. レポートの「変更概要」で料金の実変更かノイズかを判断
2. ノイズなら `ignore_patterns` を足して `--init`
3. 実変更なら該当企業を掲載している記事を洗い出す
   ```powershell
   Select-String -Path articles\*.md -Pattern "企業名" -List | Select-Object Path
   ```
4. 記事を修正し、`factcheck.py` を回して出典との整合を確認

---

## 5. cannibalization.py — カニバリゼーション検出

同一クエリに自サイトの複数URLが表示されているケースを、深刻度順に出す。
業種特化ロングテールを200〜800本積む戦略では、似たKWの記事が増えるほど起きやすい。

### 5.1 実行例

```powershell
python tools\cannibalization.py                          # 直近28日
python tools\cannibalization.py --days 90 --top 50
python tools\cannibalization.py --start 2026-06-01 --end 2026-06-30
python tools\cannibalization.py --out tools\reports\cannibalization.md --csv tools\reports\cannibalization.csv
python tools\cannibalization.py --min-impressions 50     # 閾値を上げてノイズを減らす
```

終了コード: `0` 検出なし / `1` 検出あり / `2` エラー

### 5.2 深刻度の定義

```
深刻度 = log10(1 + 表示回数) × クリック分散 × 順位近接 × URL数係数 × 100
```

- **クリック分散** = `1 - 最大URLのクリック占有率`。0に近いほど1本が独占（実害小）、0.5に近いほど食い合い
- **順位近接** = `1 / (1 + 上位2URLの順位差)`。順位差3以内なら正面衝突
- **URL数係数** = 3本以上で重みが増す

絶対値に意味はない。相対比較で上から潰す。

### 5.3 対応の手順

1. 主軸URLを1本決める（順位・クリック・網羅性で選ぶ）
2. 副URLからそのKWの最適化を外す（タイトル・見出し・アンカーテキスト）
3. 副URLから主軸URLへ内部リンクを張る
4. 副URLが単独で価値を持たないなら統合して 301 リダイレクト
5. 翌月に再実行し、該当クエリが消えたか確認する

---

## 6. weekly_report.py — 週次レポート

GA4 と Search Console から数字を取り、Markdown 1枚を生成する。

**このスクリプトの設計上の要件**: 収益ゼロの期間が 9〜12ヶ月続くため、
先行指標（表示回数・10位以内KW数・インデックス数・参照ドメイン数）の
**前週比・前月比をレポート最上部に置く**。これは離脱防止の設計であり、
表示位置を下げないこと（STRATEGY.md §14）。

### 6.1 実行例

```powershell
# 基本（既定で3日前を終端とする直近7日）
python tools\weekly_report.py

# 手入力項目つき
python tools\weekly_report.py --new-articles 6 --rewrites 2 --referring-domains 12 --revenue 0 `
    --notes "コアアップデート観測中"

# PROGRESS.md の週次記録テーブルに自動追記
python tools\weekly_report.py --append-progress --new-articles 6 --rewrites 2

# GA4 未設定でも回す
python tools\weekly_report.py --no-ga4
```

出力先の既定は `tools/reports/weekly-YYYY-Www.md`。

### 6.2 出力される指標

| 指標 | 取得元 |
|---|---|
| インデックス数 | GSC（表示のあったユニークURL数を代理値として使用。`--indexed-pages` で上書き可） |
| 表示回数 / クリック数 / CTR / 平均掲載順位 | GSC |
| 10位以内KW数 | GSC（クエリ別の平均掲載順位 ≤ 10 の件数） |
| セッション / PV | GA4 Data API |
| 参照ドメイン数 | **手入力**（`--referring-domains`）— 無料APIがない |
| 新規記事数 / リライト数 / 確定収益 / 発生件数 / 承認率 | **手入力** |

> **インデックス数について**: Search Console の「ページのインデックス登録」の正確な値は
> API で一括取得できない（URL検査APIは1URLずつ・日次クォータ2,000）。そのため
> 「期間中に表示回数が1回以上あったユニークURL数」を代理値として出す。
> 正確な値が必要な週は GSC 管理画面から転記して `--indexed-pages` で渡す。

### 6.3 比較期間

- **前週** = 直前の同じ長さの期間
- **前月同週** = 28日前を終端とする同じ長さの期間

Search Console のデータ確定に2〜3日かかるため、既定の集計終端は「3日前」
（`config.yaml` の `weekly_report.data_lag_days` で変更可）。

### 6.4 PROGRESS.md への追記

レポート末尾に PROGRESS.md 週次記録テーブルの列順に合わせた1行が出る。
`--append-progress` を付けると自動で追記する（プレースホルダ行 `| - | - | ... | 未着手 |`
があれば置き換える）。手入力項目を渡していない列は `?` になる。

---

## 7. 定期実行

### 7.1 Windows タスクスケジューラ（推奨）

まずバッチファイルを作る。`tools\run_weekly.bat`:

```bat
@echo off
cd /d C:\Users\matsu\affiliate
call .venv\Scripts\activate.bat
python tools\weekly_report.py --append-progress >> tools\reports\weekly.log 2>&1
python tools\cannibalization.py --out tools\reports\cannibalization.md >> tools\reports\weekly.log 2>&1
```

`tools\run_price_diff.bat`:

```bat
@echo off
cd /d C:\Users\matsu\affiliate
call .venv\Scripts\activate.bat
python tools\price_diff.py --out tools\reports\price_diff.md --notify >> tools\reports\price_diff.log 2>&1
```

登録（管理者PowerShell）:

```powershell
# 毎週月曜 9:00 に週次レポート
schtasks /Create /TN "affiliate\weekly-report" /TR "C:\Users\matsu\affiliate\tools\run_weekly.bat" `
  /SC WEEKLY /D MON /ST 09:00 /F

# 毎日 7:00 に料金差分チェック
schtasks /Create /TN "affiliate\price-diff" /TR "C:\Users\matsu\affiliate\tools\run_price_diff.bat" `
  /SC DAILY /ST 07:00 /F
```

確認・手動実行・削除:

```powershell
schtasks /Query /TN "affiliate\weekly-report" /V /FO LIST
schtasks /Run   /TN "affiliate\weekly-report"
schtasks /Delete /TN "affiliate\weekly-report" /F
```

> **注意**: 「ユーザーがログオンしている時のみ実行」にしておくこと。
> OAuth のトークン更新はユーザープロファイル配下のファイルを読むため、
> 別ユーザーコンテキストで実行すると認証に失敗する。

### 7.2 cron（Linuxサーバーに移す場合）

```cron
# 毎日 7:00 料金差分
0 7 * * * cd /srv/affiliate && .venv/bin/python tools/price_diff.py --out tools/reports/price_diff.md --notify >> tools/reports/price_diff.log 2>&1

# 毎週月曜 9:00 週次レポート
0 9 * * 1 cd /srv/affiliate && .venv/bin/python tools/weekly_report.py --append-progress >> tools/reports/weekly.log 2>&1

# 毎月1日 10:00 カニバリ検出
0 10 1 * * cd /srv/affiliate && .venv/bin/python tools/cannibalization.py --out tools/reports/cannibalization.md >> tools/reports/cannibalization.log 2>&1
```

無人サーバーではブラウザ同意ができないため、サービスアカウント方式
（`.env` の `GOOGLE_APPLICATION_CREDENTIALS`）を使うか、
ローカルで作った `.gsc_token.json` を転送する。

### 7.3 終了コードの使い分け

| コード | factcheck | price_diff | cannibalization | weekly_report |
|---|---|---|---|---|
| 0 | 問題なし | 変更なし | 検出なし | 成功 |
| 1 | 要確認あり（不一致・lint のエラー・関門モードでは偶然の一致の候補も） | 変更あり | 検出あり | — |
| 2 | 実行エラー | 実行エラー | 実行エラー | 実行エラー |

| コード | factcheck --lint | publish_prep | postcheck | similarity |
|---|---|---|---|---|
| 0 | 合格 | 成果物を書き出した | 問題なし | しきい値超えなし・エラーなし |
| 1 | エラーあり | エラーのある記事がある（その記事は書き出さない） | 要確認あり | しきい値超えあり、または目印の欠落などのエラー |
| 2 | 実行エラー | 実行エラー | 実行エラー（公開ページを取得できない等） | 実行エラー |

cron のメール通知や監視ツールでは「1 = 人間の確認が必要」「2 = ツールが壊れている」
として扱い分けること。**2 を無視しない** — ツールが黙って死ぬのが最悪の失敗パターン。

---

## 8. クロール先への礼儀（`factcheck.py` / `price_diff.py` / `postcheck.py`）

（`factcheck.py --lint`・`publish_prep.py`・`similarity.py` はネットワークに出ない。）

他社サーバーにアクセスするため、以下を必ず守る。実装上も強制している。

- **User-Agent を明示** — `config.yaml` の `fetch.user_agent`。連絡先URLを含める
- **robots.txt を尊重** — 拒否されたURLは取得しない。`Crawl-delay` があれば従う
- **リクエスト間隔を空ける** — 同一ホストへ既定3秒以上。`robots.txt` の指定が長ければそちらを採用
- **同一ホストへの並列アクセスをしない** — 全スクリプトが同期・逐次実行。並列化しないこと
- **同一URLは1回だけ取得** — factcheck は同じ出典URLをキャッシュして再取得しない

`--ignore-robots` は自社サイトなど正当な理由がある場合にのみ使う。

---

## 9. トラブルシューティング

| 症状 | 原因 / 対処 |
|---|---|
| `設定ファイルが見つかりません` | `config.example.yaml` を `config.yaml` にコピーしていない |
| `環境変数 GOOGLE_OAUTH_CLIENT_SECRETS が設定されていません` | `.env` にパスを書く（§2.4） |
| `403` エラー | Google Cloud で API が未有効化、またはアカウントにプロパティ権限がない（§2.2 / §2.7） |
| `404` エラー（GSC） | `GSC_SITE_URL` の形式ミス。ドメインプロパティは `sc-domain:` 接頭辞、URLプレフィックスは末尾スラッシュ必須 |
| 7日ごとに再認証を求められる | OAuth 同意画面が「テスト」状態。「本番環境に公開」する（§2.3） |
| `このアプリは確認されていません` | 自作アプリなので正常。「詳細」→「（アプリ名）に移動」 |
| price_diff で毎回差分が出る | 動的要素がフィルタで潰せていない。`ignore_patterns` を追加して `--init` |
| `セレクタ ... に一致する要素が見つかりません` | サイト側のHTML構造変更。これ自体が要確認事項。`selector` を修正 |
| factcheck が `empty_page` を出す | 参照先がJS描画。手動確認に回す |
| 手動確認の記録を書いたのに消えない | 確認者が書けない人（不一致は `manual_check_allowed_checkers` の名前だけ。既定は空）、理由が空、数値の表記が記事と違う、期限切れ、偶然の一致なのにページのハッシュがない・ページの本文が変わった。レポートの「書式の警告」と各項目の注記を見る |
| publish_prep が `照合の受領ファイル … がありません` を出す | `factcheck.py` を関門モード（既定）で実行して合格させる（週次モードでは書かない） |
| `--hash` が `合格していないのでハッシュを出しません` を出す | lint を合格させ、照合を関門モードで合格させてから実行する |
| publish_prep が `hash が今の正本のハッシュと違います` を出す | 確かめた後に正本が変わった。lint・照合・確認をやり直し、`--hash` の値を書き直す（§11.4） |
| `UTF-8 として読めなかったため cp932 として読みました` | 正本を UTF-8 で保存し直す。UTF-8 でも cp932 でも読めなければ lint のエラー |
| `robots.txt により取得を見送りました` が出るが、そのページは公開されている | Python標準の `urllib.robotparser` は複雑な `Allow`/`Disallow` の組み合わせ（ワイルドカードや `Disallow: /?` など）を保守的に解釈することがある。**安全側の誤りなので既定では従う**。自社サイトなど正当な理由がある場合のみ `--ignore-robots` を使う |
| GSCのデータが0行 | 期間が新しすぎる（確定に2〜3日）、またはサイトが未インデックス |

すべてのスクリプトは `-v` で DEBUG ログが出る。エラーは握りつぶさず、
何が失敗したかを標準エラーに出す設計になっている。

---

## 10. ファイル一覧

```
tools/
├─ README.md                     … このファイル
├─ requirements.txt              … 依存パッケージ
├─ config.example.yaml           … 設定のテンプレート（実値なし）
├─ config.yaml                   … 実設定（Git管理外）
├─ .env.example                  … 認証情報のテンプレート（実値なし）
├─ .env                          … 実認証情報（Git管理外）
├─ .gitignore                    … 認証情報・生成物の除外
├─ common.py                     … 共通基盤（HTTP・数値正規化・設定）
├─ google_clients.py             … Google OAuth とAPIクライアント
├─ article.py                    … 正本の読み込み・マーカーの定義・rel 検査（共通）
├─ article_lint.py               … lint の本体（factcheck.py --lint）
├─ manual_checks.py              … 手動確認の記録（T2）
├─ factcheck.py                  … ファクトチェック機械照合（--lint を含む）
├─ publish_prep.py               … WordPress への反映の成果物を作る（T5・T8）
├─ postcheck.py                  … 公開後の照合（T6）
├─ similarity.py                 … 型B記事の類似度（T4）
├─ price_diff.py                 … 料金改定の差分検知
├─ cannibalization.py            … カニバリゼーション検出
├─ weekly_report.py              … 週次レポート生成
├─ watch_targets.example.yaml    … 監視対象のテンプレート
├─ watch_targets.yaml            … 実監視対象（Git管理外）
├─ tests/                        … pytest のテスト（ネットワークに出ない）
├─ snapshots/                    … 料金ページのスナップショット（Git管理する）
└─ reports/                      … 生成レポート（Git管理外）

data/                            … （tools の外）
├─ keywords.csv                  … 主キーワード → slug（一意。重複防止）
├─ industries.csv                … 業種の分類（id,name,parent,name_en）。similarity.py が業種名を伏せるのに使う
├─ slugs.csv                     … 日本語 → 英語表記（スラッグの統一）
├─ program-rules.md              … 案件別 掲載ルール台帳
├─ cta_patterns.yaml             … 案件ID → CTAの同期パターンID（publish_prep.py）
├─ asp-programs.csv              … 案件の一覧（program_id・ASP・広告主・計測ドメイン・状態）。lint が programs を、postcheck が計測タグを確かめる
├─ terms.yaml                    … 決まった言い回し（「36協定」など。中の数字を数値として扱わない）
├─ checks/                       … 照合の合格の受領ファイル（factcheck.py が書き、publish_prep.py が読む。README.md）
└─ sources/                      … 出典の一覧と手動確認の記録（README.md・_template.md）

build/{slug}/                    … publish_prep.py の出力（content.html / content.md / meta.json）。生成物（ルートの .gitignore で除外）
```

データファイルの雛形は、見出しと記入例1行（「（例）」「example-」「prog_example_001」で例と分かる値）だけ。
実データを入れたら記入例の行は消す。`industries.csv` には任意で `aliases` 列（`|` 区切りの別名。例「歯医者」）を足せる。
足すと similarity.py がその別名も伏せる。

---

## 11. publish_prep.py — WordPress への反映の成果物（T5・T8・T10）

### 11.1 使い方

```powershell
python tools\publish_prep.py articles\dental-clinic-attendance-system.md
python tools\publish_prep.py articles\*.md --out-dir build
python tools\publish_prep.py --hash articles\dental-clinic-attendance-system.md   # checks に書くハッシュ（合格時だけ表示）
```

出力は `build/{slug}/`（`config.yaml` の `paths.build_dir`。`build/` はルートの `.gitignore` で Git の対象外）:

| ファイル | 使い道 |
|---|---|
| `content.html` | 本文のHTML。REST API 方式なら `content` に渡す。貼り付け方式ならブロックエディタの「コードエディター」に貼る |
| `content.md` | 同じ内容のMarkdown（マーカー除去・CTA置換・P7追加・改行をつないだ後）。差分の確認用・Markdown貼り付けの検証用 |
| `meta.json` | `title`・`slug`・`description`・`updated`（**表示する更新日**は `display_updated`）・`post_type: post`・**`status: draft`（常に下書き）**・CTA・参考資料の一覧・`content_hash` |
| `gate-summary.md` | **関門の要約**。実行者が公開の前に読む（§11.3） |

**エラーが1つでもある記事は書き出さない**（古い `build/{slug}/` もそのまま残る。`meta.json` の `content_hash` と `built_at` で、どの正本から作ったかを確かめる）。

### 11.2 書き出す前の関門（設計書 §8 T10）

| 確かめること | エラーになるもの |
|---|---|
| lint（publish_prep が**自分で実行**する） | lint のエラーが1件でもある |
| 照合の合格の**受領ファイル** `data/checks/{slug}.json` | ない／`result: pass`・`mode: gate`・`tool: factcheck.py` でない／slug が違う／**`content_hash` が今の正本のハッシュと違う** |
| front matter の `checks`（**判定**の証跡） | 必須の項目（`required_checks`: factcheck_session・owner_review。型Bは `required_checks_type_b`: trace_table も）がない／`result` が `pass` でない／`date` がない／**`hash` が今の正本のハッシュと違う** |
| front matter | `type`・`slug`・`title`・`description`・`updated` がない、slug が不正、title・description に `{{` |

- 受領ファイルは、`factcheck.py`（照合）を**関門モード**（`--mode gate`。既定）で実行し、その記事が合格したとき（不一致・lint のエラー・偶然の一致の候補がすべてゼロ）に、**ツール自身が書く**（`paths.checks_dir`。形式は `data/checks/README.md`）。週次の照合（`--mode weekly`）では書かない
- `checks.lint`・`checks.factcheck_tool` は必須ではない（lint は publish_prep が実行し、照合は受領ファイルで確かめるため）。書いてあって pass でなければ警告
- **ハッシュ（checks に書く値）は、合格したときだけ表示する**: `--lint` のレポートは lint が合格した記事だけ、照合のレポートは合格して受領ファイルを書いた記事だけ、`publish_prep.py --hash` は lint が合格し受領ファイルが今の正本と一致するときだけ
- 判定の順序: 執筆 → `factcheck.py --lint` → `factcheck.py`（関門モード。受領ファイル）→ 別セッションの判定（`--hash` の値を `checks.factcheck_session`・`trace_table` に書く）→ 実行者の確認（`checks.owner_review`）→ `publish_prep.py`

> **この仕組みは、古い記録の流用と記録漏れを防ぐものであり、偽造への防御ではない。**
> Claude はツールを実行でき、受領ファイルや checks のファイルも編集できるので、偽造を機械で防ぐことは原理的にできない。
> **最後の保証は、実行者が関門の要約を読み、WordPress で下書きを公開に切り替えることである**（設計書 §7 工程10b）。

### 11.3 処理の内容と関門の要約

| 処理 | 内容 | エラーになるもの |
|---|---|---|
| マーカーの除去 | `src`・`src-block`・`calc-block`・`nofact`・`part` と、その他のHTMLコメントをすべて除去。**単独の行のマーカーは、前後がどちらも空行でなく、リスト項目の間でもないときだけ空行にし、それ以外は行ごと消す** | 閉じていないコメント |
| 見えない文字 | Unicode の Cf 類（U+200B など）を出力から除く（lint もエラーにする） | — |
| CTA | 単独の行・単独の段落の `{{CTA:案件ID}}` を `data/cta_patterns.yaml` の同期パターンの参照 `<!-- wp:block {"ref":ID} /-->` に置き換える（`publish_prep.cta_template`） | 対応がない案件ID、行の途中や段落の途中の `{{CTA:…}}` |
| `{{` の残り | 置き換えた後の本文と title・description | 残っていればエラー |
| P7 | マーカーの URL と `accessed=` から「参考資料」を作る（出現順、**重複URLは1つ**、参照日は新しい方）。**URLは既定で文字として出す**（front matter に `p7_links: link` と書いた記事だけリンクにする）。`updated` から「最終更新日」、`history` から「更新・訂正履歴」（新しい順）。note と URL は HTML エスケープする | 正本に P7 の見出し（`参考資料`・`参考資料・更新履歴`・`更新・訂正履歴` で**始まる**見出し）がある、`accessed=` がない、`history` がない |
| 改行をつなぐ（T8） | markdown-it の **paragraph の範囲の中だけ**、文と文の間の改行をつなぐ（リスト項目・引用の中の段落を含む）。境目が日本語なら空白を入れない。ハード改行・Setext 見出し・表・HTMLブロックには触れない | — |
| rel の検査 | `affiliate_domains` のドメインへのリンクで `rel` に `sponsored` も `nofollow` もないもの | 見つかればエラー |

**関門の要約**（`build/{slug}/gate-summary.md`）の中身: 正本とハッシュ／「下書きとして入れる・公開の切り替えは実行者だけ・偽造への防御ではない」の注意／
lint の結果（警告の一覧、nofact の行、calc-block）／受領ファイルの中身（日時・モード・ハッシュ・レポートのハッシュ・照合した数・記録で外した件数）／
checks の表（結果・日付・ハッシュの一致）／手動確認の記録の件数（種類ごと・無効な行）と全行の一覧／偶然の一致（T7）の扱い／実行者が確かめること。

Markdown → HTML の変換には **markdown-it-py** を使う（新しい依存。`requirements.txt`）。
理由: GFM の表を変換できる、CommonMark 準拠で変換結果が予測しやすい、純Pythonで Windows でも入れやすい。
lint も同じ markdown-it のトークンで行の役割を決めるので、lint が見る構造と出力の構造が一致する。

### 11.4 正本のハッシュ（受領ファイル・checks に記録する値）

```yaml
checks:
  factcheck_session: {result: pass, date: 2026-10-01, hash: 3f9a0c1d2e4b5a67}
  owner_review: {result: pass, date: 2026-10-01, hash: 3f9a0c1d2e4b5a67}
```

- **定義**（`article.content_hash`）: `sha256( front matter から checks を除いたものを JSON（キー順・UTF-8）にした文字列 + "\n---\n" + 本文 )` の16進の先頭16文字。
  本文は front matter の後の全行を `\n` でつなぎ、末尾の改行を除いたもの。ただし**関連記事の区間（`<!-- part: related -->` 〜 `<!-- /part -->`）の、開始と終了の目印の間の行だけ**を除く（目印の行そのものと位置は含める）。
  区間は `article.related_range` で求める（lint も同じ関数を使う）。related が重複・閉じていない・入れ子のときは何も除かない（lint もエラーにする）
- `checks` 自体は含めないので、checks に書き込んでも値は変わらない（循環しない）。BOM・CRLF の違いも値を変えない。**それ以外は front matter（`updated`・`history` など）を含めて1文字でも変われば値が変わる**
- 関連記事のリンクだけを足す作業（設計書 §7 工程11）は related の区間の中だけで行えば、ハッシュは変わらない（品質ゲートの記録をやり直さない。lint と postcheck は行う）
- **注意: 正本の外のファイル（`data/cta_patterns.yaml`・`config.yaml`・`data/terms.yaml`・`data/industries.csv` など）を判定の後に変えても、ハッシュは変わらない。**
  これらを変えたときは、影響する記事の lint・照合をやり直す（CTA の同期パターンの中身は postcheck が確かめる）

### 11.5 WordPress での解釈の確認手順（**未確認**。反映方式を決める前に実行者が行う）

> **2026-09-30 更新**：実行者向けの詳しい手順は `docs/wp-verification.md`（WordPress 表示テスト手順書）に移した。
> 反映方式の案の名前は設計書 §8 に合わせる（**案1a**＝コードエディターに `content.html` を貼る／**案1b**＝Markdown をビジュアルエディターに貼る／**案2**＝REST API で下書き投稿）。
> 本文は既定で「カスタムHTML」ブロック（`<!-- wp:html -->`）に包んで出力する（`publish_prep.wrap_html_blocks`）。以下は当初のメモで、案の番号は旧定義のまま。

出力が WordPress で実際にどう解釈されるかは、まだ確かめていない（設計書 §11-1）。
次の手順で確かめ、結果を設計書 §11 に反映してから反映方式（§8 案1／案2）を決める。

1. テスト用の記事を1本用意する（表・CTA・P7・関連記事を含むもの。`tests/conftest.py` の `good_article_text()` を `articles/` に書き出す）。lint・照合（関門モード）を通し、`--hash` の値を checks に入れてから publish_prep に通す
2. **同期パターンを1つ作る**: 管理画面「外観 → エディター → パターン」（または `/wp-admin/edit.php?post_type=wp_block`）でボタンのパターンを「同期」で作り、URL の `post=数字` を `data/cta_patterns.yaml` の `pattern_id` に書く。**パターンには数値を書かない**（ボタンの文言は「公式サイトで詳しく見る」など）。中身はボタンと計測用の画像タグだけにし、外側のグループブロックに `cta` クラスを付ける（設計書 §2 P4b）
3. **貼り付け方式（案1）の確認**: 新規「投稿」→ 右上のメニュー →「コードエディター」→ `content.html` を貼る →「ビジュアルエディター」に戻す
   - 表が「テーブル」ブロック（または「クラシック」ブロック）として崩れずに表示されるか
   - `<!-- wp:block {"ref":ID} /-->` が同期パターンとして表示されるか（「クラシック」に吸収されていないか）
   - 下書きのプレビューで、表・CTAボタン・関連記事・参考資料・更新履歴が表示されるか
4. **Markdown の貼り付け**（参考）: `content.md` をビジュアルエディターに貼ったとき、表と見出しがブロックに変換されるか
5. **REST API 方式（案2）の確認**（初回の認証設定は実行者。CLAUDE.md ルール8）: `POST /wp-json/wp/v2/posts` に `title`・`slug`・`status: draft`・`content`（`content.html` の中身）を送り、3 と同じ点を確かめる。**`status` は必ず `draft`**（公開の切り替えは実行者）。`description` をどのプラグインの項目（SEO SIMPLE PACK か Cocoon か）で管理しているかと、REST API で書けるかは未確認（2026-09-30 追記：反映方式の比較と推奨は `HANDOFF.md` §4-4）
6. **表示する更新日**: `meta.json` の `display_updated`（= front matter の `updated`）を、どの項目に入れれば記事に表示されるかを確かめる（WordPress の `modified` は保存のたびに自動で更新され、REST API では直接書けない。Cocoon 側の更新日の設定を使えるかは未確認）
7. 公開後、`postcheck.py` を通す（§13）。本文のクラス（`.entry-content`）と CTA のクラス（`.cta`）が想定どおりかもここで確かめる

---

## 12. similarity.py — 型B記事の類似度（T4）

```powershell
python tools\similarity.py                                   # paths.articles_dir の全記事
python tools\similarity.py articles\ --all --out tools\reports\similarity.md   # 全部の組（しきい値を決めるとき）
python tools\similarity.py articles\ --threshold 0.4         # しきい値を一時的に変える
```

- front matter の `type: B` で、`parent` が同じ記事同士だけを比べる
- 比べる部分は、正本の**目印**（§3.1 (5)）で取り出す: `<!-- part: trace -->`（要件トレース表）・`<!-- part: checklist -->`（P3）・`<!-- part: faq -->`（FAQ）。
  見出しの書き方（タイトルの型3「必要なNつの要件」など）に左右されない。名前の一覧は `similarity.parts`（lint の `parts` と揃える）。関連記事の区間（related）は比べない
- **目印がない型B・parent がない型Bはエラー**（レポートの「エラー」に出て、終了コード1）
- 比べる前に、`data/industries.csv` の `name`（と任意の `aliases`）をすべて `〓業種〓` に置き換える（長い名前から順に。NFKC で正規化してから）。Markdown の記号・空白・マーカーも落とす
- 指標は文字 n-gram（既定 3）の **Jaccard**（`metric: cosine` も可）。3つの部分をつないだ文字列で「合計」を測り、部分ごとの値も表に出す。n-gram は記事ごとに1回だけ作る（同じ parent の400本・79,800組で約7秒。以前は68秒）
- **しきい値の既定値 0.5 は暫定**。設計書 §0 のとおり、最初の型B記事10本で `--all` の結果を見て決め直し、`config.yaml` に書く
- 終了コード `1` はしきい値を超えた組かエラーがあるとき。超えた組は、設計書 §4 の合格ライン（差し替えテスト・要件が2行以上異なるか）を別セッションで判定し直す

---

## 13. postcheck.py — 公開後の照合（T6）

```powershell
python tools\postcheck.py articles\dental-clinic-attendance-system.md --url https://example.com/dental-clinic-attendance-system/
python tools\postcheck.py articles\foo.md --html-file saved.html        # 保存したHTMLで（ネットワークを使わない）
```

- 取得は `PoliteFetcher`（§8。robots.txt 尊重・UA明示・間隔）。自社サイトの robots.txt が拒む場合に限り `--ignore-robots`
- 本文の範囲は `postcheck.content_selector`（既定 `.entry-content`。Cocoon の本文のクラスという想定で**未確認**。違えば `--selector` か config で直す）。見つからなければページ全体で照合し、要確認として報告する

| 確かめること | 種別 |
|---|---|
| 正本を publish_prep と同じ方法で描いた本文（マーカー除去・P7付き。CTA は除く）の**全数値の並び**と、公開ページの本文（`exclude_selectors` の広告表示・目次・CTA を除く）の全数値の並びを比べる。書き換え・足りない数値・ページにだけある数値（WordPress 上での書き足し）を、前後の文脈つきで出す。並びで比べるので、行の入れ替えも分かる | `number_changed` / `number_missing` / `number_extra` |
| 本文に `<!--` `-->` `accessed=` `src-block` `calc-block` `nofact` `part:` が見えている／HTMLソースに出典マーカーのコメントが残っている | `marker_visible` / `marker_in_source` |
| 本文に `{{` が見えている | `placeholder_visible` |
| 広告表示（`.ad-disclosure`。`docs/legal/ad-disclosure.md` §4）がない／「広告」の文字がない／**本文の中の**最初の見出し・表より後ろにある（HTMLのツリー上の順序で比べる）／位置を判定できない | `ad_disclosure_*` |
| アフィリエイトのドメインへのリンクで rel に sponsored も nofollow もない（ページ全体。CTAの同期パターンの中のリンクもここで確かめられる） | `affiliate_rel_missing` |
| **CTA の同期パターン（`postcheck.cta_selector`。既定 `.cta`）の中の数値はすべてエラー**。中の要素はボタン（リンク）と計測用の画像タグ（`data/asp-programs.csv` の `tracking_domains` の `<img>`）だけ。リンクの外の文字・`<p>` などの要素・計測用でない画像はエラー（同期パターンは正本の外にあり、出典の照合を通らないため。設計書 §2 P4b） | `cta_number` / `cta_element` |
| front matter の `programs` ごとに、`data/asp-programs.csv` の `tracking_domains` のドメインの `<img>`（計測用の画像タグ）が本文にあるか（計測ドメインが未記入の案件は確かめない） | `tracking_img_missing` / `tracking_img_unknown` |

- `.cta` のクラスを付けていないボタン（Cocoon のボタンブロックなど）の中の数値は、`number_extra`（ページにだけある数値）として出る。CTA は必ず `cta` クラスのグループに入れる
- スマホのファーストビューに広告表示が入るか、スマホで表が読めるか、クリックが計測されるかは確かめられない（レポートの末尾に人の作業として出す）

---

## 14. 品質ゲート（設計書 §6）とコマンドの対応

| §6 の項目 | 確かめるコマンド | 補足 |
|---|---|---|
| lint が通過（マーカーのない数値ゼロ、accessed= の欠落ゼロ、マーカー0件でない、title・description の数値が一致） | `factcheck.py --lint articles\{slug}.md` → 終了コード 0。publish_prep も自分で実行する | §3.6 |
| 計算した値はすべて calc-block の中にあり、入力値の表と計算式が示されている | 同上（`calc_block_no_basis`・`calc_block_non_table`・`calc_row_unknown`・`calc_header_number`・`calc_cell_format`・`nested_block`・`src_in_calc_block`）＋ レポートの calc-block の行番号を人が見る | 計算が正しいかは工程7で再計算（ツールでは確かめない） |
| factcheck.py の照合が通過（偶然の一致の候補も含む） | `factcheck.py articles\{slug}.md`（関門モード）→ 終了コード 0 で、受領ファイル `data/checks/{slug}.json` が書かれる | 候補は、文脈を確かめて種類「偶然の一致」で記録すると消える（§3.8） |
| 未解決の要確認がゼロ（記録できる人を種類で分ける） | 同上。記録の書式・種類・確認者・期限は §3.7。レポートの冒頭に記録で外した件数、関門の要約に記録の全行が出る | 不一致・取得失敗は実行者だけ、偶然の一致は別セッションも |
| 型B：要件トレース表の合格ライン | `factcheck.py --lint`（表の行数・各行の出典・部分の目印・FAQの数・チェックリストの数・推薦の数）＋ `similarity.py`（類似度）＋ 別セッションの判定（`checks.trace_table`） | 合格ラインそのもの（選定に効くか・差し替えテスト）は人が判定 |
| 表現ルール（§1-6） | `factcheck.py --lint`（禁止表現はエラー、断定・比較優位の表現は警告。本文・title・description・history の note・画像の alt） | 一覧にない表現・文脈は人が判定 |
| アフィリエイトリンクに rel="sponsored"（または nofollow） | `publish_prep.py`（正本の中のリンク）・`postcheck.py`（公開ページ。同期パターンの中も） | |
| 計測用の画像タグが残っている | `postcheck.py`（`programs` ごとの計測ドメインの `<img>`）＋ lint（`programs` の ID が `asp-programs.csv` にあるか） | |
| スラッグが英語 | `factcheck.py --lint`（`slug_invalid`）・`publish_prep.py` | |
| `{{` の置き換え漏れがない | `publish_prep.py`（エラー）・`postcheck.py`（公開ページ） | lint は本文では警告、title・description ではエラー |
| 内部リンク（親の型A・近い業種の型B）がつながっている | `factcheck.py --lint`（`part: related` が必須。リンク先の正本の存在・公開済み・title の一致） | 関連記事を足すのは related の区間の中だけ（ハッシュの対象外） |
| マーカーが除去された状態で反映されている | `publish_prep.py`（出力にマーカーが残ればエラー）・`postcheck.py`（`marker_*`） | |
| 「投稿」として作成した | `meta.json` の `post_type: post`／`postcheck.py` の広告表示の有無（固定ページだと出ない） | 間接的な確認 |
| 品質ゲートの証跡 | `publish_prep.py`（受領ファイル・必須の checks・pass・今の正本のハッシュ）。`gate-summary.md` を実行者が読む | §11.2。**偽造への防御ではない** |
| 実行者の最終確認 | 関門の要約を読み、WordPress で**下書きを公開に切り替える**（実行者だけ） | 設計書 §7 工程10b |
| 公開後：公開ページの数値が正本と一致 | `postcheck.py 正本 --url 公開URL` | |
| 公開後：広告表示が読める | `postcheck.py`（有無・文言・位置）＋ スマホ実機で人が確認 | |
| 結論が冒頭3行以内・付加価値・推薦ルール（非提携の最適製品を外していない）・弱み・掲載ルール台帳・P6・スマホで表・クリック計測 | ツールなし（人とファクトチェックのセッション） | |

---

## 15. テスト

```powershell
pip install -r tools\requirements.txt      # pytest を含む
python -m pytest tools\tests -q
```

- ネットワークには出ない（取得は `FakeFetcher` で置き換える）。記事はテストの中で一時ディレクトリに書く
- 設計書 §1-1（front matter）・§1-2（行内マーカー・ブロックマーカー・計算した値）・§2（比較表）・§4（要件トレース表）の記法の例を、そのままテストケースに入れている（`tests/conftest.py`）。conftest の型Bの正本は、区間の全体を覆う部分の目印・関連記事の区間・推薦2つ・checks のハッシュを入れた、関門を通る形。`make_article` は関連記事の区間とリンク先の正本を自動で足し、`checks=True` なら受領ファイルも書く
- 独立レビューの再現ケースは、c1〜c12 を `tests/test_review_regressions.py`、c13〜c18 を `tests/test_review_regressions_2.py` にそのまま入れている。最終回の修正の機能ごとのテストは `tests/test_final_round.py`
- テストは `config.example.yaml` をもとにしたテスト用の設定（確認者の許可リストに「実行者」、テスト用の `asp-programs.csv`・`industries.csv`・受領ファイルの置き場所）で動く
- `.pytest_cache/` は `tools/.gitignore` とルートの `.gitignore` で除外済み

---

## 16. 実装で判断したこと（設計書があいまいな点）

設計書（`docs/article-template.md` v0.5）は編集していない。あいまいな点は次のとおり決めた。

1. 「1人あたり」の 1 は lint でも数えない。範囲は「数値の直後に助数詞0〜2文字、その直後が『あたり』『当たり』」だけ。漢数字の「一人あたり」も同じ
2. nofact を置ける行: 見出し（数え上げか順番だけ）、「試算の条件：」（人数・期間・回数だけ）、「計算式：」（単位のない定数だけ）。順番は「ポイントN」で、N の後ろが区切り（：．、（ ）」や行末）のものだけ
3. calc-block の中は表の行だけ。見出し行に数値を書かない（「1人あたり」の1を除く）。データのセルは「数値＋単位（`calc_cell_units`）」か「試算の対象外（理由）」だけ（「約3,000円」「3,000円（税込）」は不可）。1列目は入力値の表の1列目にある名前だけ
4. title・description の数え上げは、同じ書き方・同じ数・同じ名詞の見出し＋項目の数（直下の見出し → 表のデータ行 → リスト項目）の一致。事実の数値は単位まで一致
5. 偶然の一致（T7）の候補は、関門モードでは終了コード1、週次モードでは報告だけ
6. 手動確認の記録は種類で分ける。不一致・取得失敗は実行者（許可リスト。既定は空）だけ・90日。偶然の一致は別セッション（`factcheck_session`）も書け、出典ページの本文のハッシュが変わるまで有効
7. P7 の参考資料のURLは既定で文字。`p7_links: link` の記事だけリンク
8. publish_prep は、正本のマーカー以外のHTMLコメントと見えない文字も除去する
9. 型Bの比べる部分は目印で取り出す。目印は区間の全体を囲む（続けて置いた part はまとめて1区間）。FAQ は3問以上（「Q.」で始まる行か？で終わる見出し）、チェックリストは2項目以上
10. CTA の置き換え先は `<!-- wp:block {"ref":ID} /-->`。同期パターンの中の数値・余分な要素は postcheck がエラーにする
11. 正本のハッシュの定義は §11.4。related の区間の中の行だけを外す。正本の外のファイルの変更はハッシュに影響しない
12. 漢数字: 二以上・金額・割合・万千億を伴うものはエラー、「一」＋助数詞は警告。除外（`kanji_exclusions`）は漢数字＋助数詞の全体を含むときだけ効く（「一時」は「一時間」に当たらない）。決まった言い回しは `data/terms.yaml`（関数を直接呼んだときの既定もこのファイル）
13. 禁止表現（エラー）: No.1・ナンバーワン・1位・第1位・一位・最安・業界唯一・一番おすすめ・いちばんおすすめ・実際に使ってみ・使ってみた・試してみた・試したところ・導入して感じ。注意（警告）: 必ず・絶対・確実に・他社より安、同じ行に「条件」がない「最も低い」など。「最安のプラン」のような言い方もエラーになる（§1-6 の一覧どおり）
14. 本文のコードブロック・インラインコードの中の数値・HTMLの表・表の直後に空行のない注記・見えない文字・画像の alt の数値・history の note の数値は lint のエラー
15. 関連記事の区間: 型B は `- [title](/slug/)`、型A は `- 業種名：[title](/slug/)`。リンク先は**同じディレクトリの** `{slug}.md` で、`published` が今日以前、title が完全一致。絶対URLは `site_domains` のドメインだけ
16. 受領ファイルは照合の関門モードで合格した記事についてだけ書く。publish_prep は `checks.lint`・`checks.factcheck_tool` を必須にしない。ハッシュは合格のときだけ表示する（`--hash` も lint と受領ファイルが合格のときだけ）
17. 「割」を単位として扱う（「約6割」の 6 は単位つきの数値。偶然の一致の理由は「一桁」だけになる）
