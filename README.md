# 製造業ニュース自動収集・メール通知ボット

国内外の製造業（自動車・電機・産業用ロボットなど）の最新の取り組み事例、および
国内外の主要コンサルティングファーム／シンクタンクが不定期に発信するインサイトを、
一次情報から収集し、**AIによる「示唆（so-what）」付き要約**にまとめて毎日メールで通知します。
**GitHub Actions（無料枠）+ Gmail（無料）+ Gemini API（無料枠）** のみで動作し、
有料の固定費は一切かかりません。

---

## 仕組み

```
GitHub Actions（毎日 JST 8:00 に自動実行）
      │
      ▼
collect_and_notify.py
      │  sources.yaml に書かれたRSS / Google Newsサイト限定検索 を巡回
      │  （製造業各社の公式発表 + コンサル/シンクタンクのインサイト記事）
      ▼
新着記事を抽出（送信済みは data/sent_urls.json で除外）
      │
      ▼
Gemini API（無料枠）に一括投入
      │  ・記事ごとに一文要約 + コンサル視点の示唆(so-what) を生成
      │  ・記事全体を俯瞰した共通トレンドを3〜5個生成
      ▼
Gmail SMTP（アプリパスワード）でメール送信
      │
      ▼
data/sent_urls.json を更新してリポジトリにコミット（重複通知防止）
```

「一次情報」の扱いについて：
- 経済産業省など官公庁・McKinseyは **公式RSSをそのまま利用**
- トヨタ・日立・Siemens・GE・BCG・Bain・Deloitte・PwC・Accenture・Roland Berger・
  野村総研・三菱UFJリサーチ・日本総研などは、公式RSSが無いため
  **Google Newsを `site:` 指定で該当ドメインに絞った検索RSS** を使い、
  各社・各機関の公式サイト上の発表のみを抽出しています。
- `sources.yaml` の末尾に、ドメインを限定しない一般的なトレンド収集枠も用意しています（ノイズが増えたら削除してください）。

### AIによる要約・示唆生成について

- Gemini API（`gemini-2.5-flash`、無料枠）を **1回の実行につき1回だけ** 呼び出し、
  その日の新着記事すべてをまとめて渡すことで、無料枠のリクエスト数制限内に収まる設計にしています。
- 生成される内容:
  - 記事タイトルの日本語訳（原文が英語等の場合も自然な日本語見出しに変換）
  - 記事ごとの一文要約と、コンサルタントが顧客提案や景況感把握に活かせる示唆
  - 記事全体を俯瞰した共通トレンド（3〜5個）をメール冒頭に表示
- `GEMINI_API_KEY` を設定しない場合は自動的に通常表示（見出し・原文抜粋・リンクのみ、未翻訳）にフォールバックします。
- AI生成の要約・示唆は参考情報です。重要な判断の際は必ず元記事を確認してください。

### リンクについて（一次情報への直接リンク）

- Google News検索経由で見つけた記事は、内部的に `news.google.com/...` のリダイレクトリンクになっています。
- 本ツールは `googlenewsdecoder` ライブラリを使い、これを **可能な限り元記事（一次情報）のURL** に自動変換してからメールに掲載します。
- Googleの仕様変更等で解決できなかった場合のみ、フォールバックとして`news.google.com`のリンクがそのまま使われます。

### 採用・求人記事の除外について

- 「採用」「求人」「hiring」「recruitment」等のキーワードを含む記事は、全ソース共通で自動的に除外されます。
- 除外キーワードは `collect_and_notify.py` 内の `GLOBAL_EXCLUDE_KEYWORDS` で管理しています。追加したいキーワードがあれば、このリストに足してください。
- 特定ソースだけさらに絞りたい場合は、`sources.yaml` の各ソースに `exclude_keywords: ["除外したい語", ...]` を追加すると、そのソース限定で追加除外できます。

---

## セットアップ手順（20分程度）

### 1. Gmailのアプリパスワードを発行する

1. 通知に使いたいGoogleアカウントで https://myaccount.google.com/security を開く
2. 「2段階認証プロセス」を有効にする（未設定の場合）
3. https://myaccount.google.com/apppasswords を開く
4. アプリ名を適当に入力（例: manufacturing-news-bot）して生成
5. 表示される **16桁のパスワード** を控える（これがアプリパスワード。通常のGoogleパスワードとは別物です）

### 2. Gemini APIキーを取得する（無料）

1. https://aistudio.google.com/apikey を開く（Googleアカウントでログイン）
2. 「Create API key」でAPIキーを新規発行
3. クレジットカード登録は不要です。無料枠のまま利用します
4. 発行されたAPIキーを控える

> 無料枠のレート制限（2026年時点）はモデルにより異なりますが、本ボットは
> **1日1回だけ**Gemini APIを呼び出す設計のため、無料枠で十分足ります。
> 万一エラーになった場合も自動でリンク+抜粋のみの通常表示にフォールバックします。

### 3. GitHubリポジトリを作成する

1. GitHubで新規リポジトリを作成（Public/Privateどちらでも可。Publicの方がActionsの実行時間が完全無料）
2. このフォルダ一式をpush

```bash
cd manufacturing-news-bot
git init
git add .
git commit -m "init: manufacturing news bot"
git branch -M main
git remote add origin https://github.com/<あなたのアカウント>/<リポジトリ名>.git
git push -u origin main
```

### 4. GitHub Secretsを設定する

リポジトリの **Settings → Secrets and variables → Actions → New repository secret** で以下を登録:

| Secret名 | 値 |
|---|---|
| `GMAIL_ADDRESS` | 送信元Gmailアドレス（例: yourname@gmail.com） |
| `GMAIL_APP_PASSWORD` | 手順1で発行した16桁のアプリパスワード |
| `MAIL_TO` | 通知を受け取りたいメールアドレス（複数可・カンマ区切り） |
| `GEMINI_API_KEY` | 手順2で発行したGemini APIキー |

### 5. 動作確認

**Actions** タブ → 「Manufacturing News Digest」→ **Run workflow** で手動実行できます。
初回はまだ「送信済み履歴」が空なので、条件に合致した記事が多めに届きます（想定内です）。

---

## ローカルでの動作確認（任意）

```bash
pip install -r requirements.txt
export GMAIL_ADDRESS=your@gmail.com
export GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
export MAIL_TO=your@gmail.com
export GEMINI_API_KEY=xxxxxxxxxxxxxxxx
export DRY_RUN=1   # メール送信せず内容をターミナルに表示するだけ
python collect_and_notify.py
```

`DRY_RUN=1` を外せば実際にメールが送信されます。

---

## カスタマイズ

- **収集対象を追加/変更**: `sources.yaml` を編集するだけです。
  - `type: rss` … 公式RSSのURLをそのまま指定
  - `type: gnews` … Google News検索式を指定（`site:` で企業/機関ドメイン限定も可能）
  - `keywords` … 官公庁フィードのように話題が広いソースは、ここでキーワード絞り込みができます
  - `category: "コンサルインサイト"` / `"シンクタンクインサイト"` のように分類しておくとメール内で見やすくグルーピングされます
- **実行頻度を変更**: `.github/workflows/manufacturing_news.yml` の `cron` を編集
  （例: 平日のみ8時 → `"0 23 * * 0-4"`）
- **何時間前までを対象にするか**: ワークフロー内の環境変数 `LOOKBACK_HOURS`（未設定時26時間）を追加して調整可能
- **要約に使うモデルを変更**: 環境変数 `GEMINI_MODEL`（既定 `gemini-2.5-flash`）。軽量版の `gemini-2.5-flash-lite` に変更するとさらに無料枠に余裕が出ます
- **要約プロンプトを調整**: `collect_and_notify.py` 内 `call_gemini_summarize()` のプロンプト文言を編集すれば、示唆の切り口（例:「投資判断の観点で」等）を変更できます

---

## 無料枠に関する注意点

- GitHub Actionsは **Publicリポジトリなら無料無制限**、Privateでも月2,000分まで無料（この用途なら十分収まります）。
- GitHubの仕様上、**60日間リポジトリへのpush等のアクティビティが無いと、scheduled workflow は自動停止**します。その場合はActionsタブから手動で「Run workflow」を1回叩けば再開します。
- Gmailは1日あたりの送信数に上限（一般アカウントで500通/日）がありますが、1日1通の本用途では全く問題ありません。
- Google News RSS検索は無料・APIキー不要ですが、Google側の仕様変更で挙動が変わる可能性があります。届かなくなった場合はまずこの部分を確認してください。
- Gemini APIは無料枠でも十分ですが、Googleの規約上、無料枠で送信したデータはモデル改善に利用される場合があります。社外秘情報を扱う場合は有料枠（Vertex AI含む）への切り替えを検討してください。

---

## ファイル構成

```
manufacturing-news-bot/
├── collect_and_notify.py          # メインスクリプト（収集・AI要約・メール送信）
├── sources.yaml                   # 収集対象ソースの設定
├── requirements.txt
├── data/
│   └── sent_urls.json             # 送信済みURL履歴（自動更新）
└── .github/workflows/
    └── manufacturing_news.yml     # 定期実行ワークフロー
```

