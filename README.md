# Discord管理bot一式（ステージbot ＋ 個人チャンネル管理bot）

このリポジトリでは、2つの独立したDiscord botを同時に運用します（discord.py / Python製）。
各botはそれぞれ専用のディレクトリに分かれており、内部もモジュールごとに分割されています。

| bot | ディレクトリ | 役割 |
|---|---|---|
| 一時ステージ作成bot | `stage_bot/` | ボイスチャンネルへの参加をトリガーに一時的なステージを自動作成し、パスワード/チケット/プリセットなど豊富な運用機能を提供する |
| 個人チャンネル管理bot | `channel_manager/` | オンボーディング（規約同意→参加区分→学年選択）を経て、メンバーごとの個人チャンネルを自動作成・管理する |

2つのbotは別プロセス・別トークンで動作し、互いに依存しません。同じサーバーで併用することも、
どちらか一方だけを起動することも可能です。

## ディレクトリ構成

```
CCM/
├── requirements.txt        # 依存パッケージ（両bot共通、1つの環境にまとめてインストール）
├── stage_bot/
│   ├── bot.py               # エントリーポイント（stage_bot/ディレクトリ内から実行）
│   ├── bot_core.py          # トークン読み込み・Intents・ロギング・エラー通知
│   ├── storage.py           # guild_config / temp_channels / presets のJSON永続化、テンプレート定数
│   ├── utils.py             # 補助関数
│   ├── views.py             # ボタン・モーダルなどUIコンポーネント（コントロールパネル本体）
│   ├── commands.py          # スラッシュコマンド定義
│   ├── events.py            # イベントハンドラ（on_ready・エラー捕捉など）
│   ├── .env.example         # 環境変数のサンプル
│   └── data/                # 実行時に自動生成される設定・状態・エラーログの保存先
│       ├── guild_config.json
│       ├── temp_channels.json
│       ├── presets.json
│       └── bot_errors.log
└── channel_manager/
    ├── bot.py               # エントリーポイント（channel_manager/ディレクトリ内から実行）
    ├── bot_core.py          # トークン読み込み・Intents・ロギング・管理者への通知
    ├── storage.py           # channel_data / hidden_channels / starred_channels のJSON永続化、学年カテゴリなどの定数
    ├── utils.py              # 補助関数
    ├── views.py               # ボタン・モーダルなどUIコンポーネント（オンボーディング・設定パネル）
    ├── commands.py             # スラッシュコマンド定義
    ├── events.py               # イベントハンドラ（オンボーディング・自動削除タスクなど）
    ├── .env.example            # 環境変数のサンプル
    ├── channel_data.json       # メンバー↔チャンネルの紐付け（実行時に自動生成）
    ├── hidden_channels.json    # 非表示化済みチャンネルの保持期限管理（実行時に自動生成）
    ├── starred_channels.json   # お気に入りチャンネルの登録状況（実行時に自動生成）
    └── bot_errors.log          # エラーログ（実行時に自動生成）
```

各botの`bot.py`は、同じディレクトリ内の`bot_core` / `storage` / `utils` / `views` / `events` / `commands`
をトップレベルモジュールとしてimportする作りになっているため、**必ずそのディレクトリ内から実行してください**
（詳細は後述の「4. bot起動」を参照）。

## 仕組み

### ステージbot（`stage_bot/`）

1. サーバー管理者が `/stage-setup` コマンドで「作成用」ボイスチャンネルを指定する（サーバー管理権限が必要）
2. メンバーがそのボイスチャンネルに参加する
3. botが一時的なステージチャンネルを作成し、そのメンバーを移動・スピーカーに設定する
4. 作成者はチャット欄の「コントロールパネルを開く」ボタン、または `/stage-panel` から各種設定を行える
5. ステージチャンネルの参加者が0人になったら、botが自動的にチャンネルを削除する（希望すればログをDMでダウンロード可能）

再起動した場合も `stage_bot/data/temp_channels.json` に作成済みチャンネルが記録されているため、
起動時に空の一時チャンネルがあれば自動でクリーンアップされます。

コントロールパネルから使える主な機能：

- 名前・トピック・人数制限の変更、公開/非公開の切り替え
- パスワードを設定して「招待パネル」を任意のテキストチャンネルへ送信（参加者はボタンからパスワード入力で入室）
- チケット招待：1回限りの使い捨てコードを発行し、相手は `/stage-ticket` で入室
- ブロック＆キック：メンバーを退出させ、再入室を禁止
- マイ・プリセット：ステージ名/トピック/人数制限のセットを保存・適用（ユーザーごとに複数保存可）
- サブ管理者（共同ホスト）指名：指名された相手もコントロールパネルを操作可能に
- 簡易パネル：ボタンを4つに絞ったモバイル向けシンプルモードへの切り替え
- 配信告知：YouTube/TwitchなどのURLをEmbedカードで告知
- リアクションピッカー：`/stage-reaction` からステージのチャットへ絵文字を送信

管理向けには、`/stage-botstatus`（稼働状況確認）と `/stage-recent-errors`（直近のエラーログ確認）が
用意されています（いずれもサーバー管理権限が必要）。エラー発生時はBotアプリケーションの所有者
（Discord Developer Portal上のオーナー）へ自動でDM通知されます。

### 個人チャンネル管理bot（`channel_manager/`）

1. 新規メンバーが「規約とデータの取り扱いに同意」→「参加区分（在学生・既卒生／外部参加）」→
  「学年選択」の3ステップを経てオンボーディングする
2. 学年・区分ごとのカテゴリ（`GRADE_CATEGORIES` で定義）内に、本人専用のテキストチャンネルが
  自動作成される
3. 各カテゴリには一覧channel（`📌チャンネル一覧`）が自動生成・自動更新され、カテゴリが
  Discordの上限（49チャンネル）に近づくと `-2`, `-3`... の形で自動的に細分化される
4. 一度離脱したメンバーが再参加した場合は、既存チャンネルの権限を自動復元して再利用する
5. `/setup_selfpanel` で自分のチャンネルに設定パネルを設置すると、⭐ボタンでお気に入りチャンネル
  （本人専用のブックマークカテゴリ）への登録/解除ができる
6. 管理者向けに、既存メンバーの一括紐付け・手動紐付け・紐付け状況確認・全体リセットに加えて、
  データ監査（`/audit_data`）・ゴースト権限のクリーンアップ（`/cleanup_ghost_permissions`）・
  稼働状況確認（`/botstatus`）・直近エラーログ確認（`/recent_errors`）などの運用コマンドを備える

コマンドエラーやイベント処理中の例外は自動でログ（`bot_errors.log`）に記録され、管理者
（`ADMIN_ID`）へDMで通知されます。Bot自体の起動時にも管理者へ通知が届くため、クラッシュ→再起動を
繰り返している場合に気づきやすくなっています。

## セットアップ手順

### 1. Discord Developer Portalでアプリを作成

**ステージbotと個人チャンネル管理botは別々のアプリケーション（別トークン）として作成してください。**
1つのbotアカウントを2プロセスで共有すると、スラッシュコマンドの競合や意図しない動作の原因になります。

それぞれについて以下を行います。

1. https://discord.com/developers/applications にアクセスし「New Application」
2. 左メニューの「Bot」からBotを作成し、「Reset Token」でトークンを取得
   - ※ MESSAGE CONTENT INTENT などの特権インテントは、ステージbotでは**有効化不要**です
     （スラッシュコマンドとボイス状態の変化のみを使用）
   - 個人チャンネル管理botは `intents.members` と `intents.message_content` を使用するため、
     Developer Portalの「Bot」設定で **SERVER MEMBERS INTENT** と **MESSAGE CONTENT INTENT**
     を有効化してください
   - ステージbotはエラー通知先を固定の管理者IDではなく、アプリケーションの**所有者（オーナー）**
     から自動取得します。Developer Portal上でアプリの所有者になっているアカウントに通知DMが届きます。

### 2. botをサーバーに招待

それぞれのbotについて「OAuth2 > URL Generator」で以下を選択してURLを生成し、自分のサーバーに招待してください。

#### ステージbot（`stage_bot/`）

- **SCOPES**: `bot`, `applications.commands`
- **BOT PERMISSIONS**:
  - View Channels（チャンネルを見る）
  - Manage Channels（チャンネルの管理）
  - Connect（接続）
  - Move Members（メンバーを移動）
  - Mute Members（メンバーをミュート ※スピーカー設定に使用）

  権限を手動入力する場合は、Permissions Integer に `22021136` を指定しても同じ権限になります。

#### 個人チャンネル管理bot（`channel_manager/`）

- **SCOPES**: `bot`, `applications.commands`
- **BOT PERMISSIONS**:
  - View Channels（チャンネルを見る）
  - Manage Channels（チャンネルの管理）
  - Manage Roles（ロールの管理）
  - Send Messages（メッセージを送信）
  - Manage Messages（メッセージの管理）
  - Read Message History（メッセージ履歴を読む）

### 3. ローカル環境の準備

依存パッケージはリポジトリ直下の `requirements.txt` にまとまっているので、1つの環境にまとめて
インストールできます。`.env` は**各botのディレクトリの中に別々に**用意します。

```bash
git clone https://github.com/MonoV0/CCM.git
cd CCM
pip install -r requirements.txt

# ステージbot用
cp stage_bot/.env.example stage_bot/.env

# 個人チャンネル管理bot用
cp channel_manager/.env.example channel_manager/.env
```

それぞれの `.env` を開き、次の内容を設定してください。

```env
# stage_bot/.env
DISCORD_TOKEN=ここにステージbotのトークン
```

```env
# channel_manager/.env
CHANNEL_MANAGER_TOKEN=ここに個人チャンネル管理botのトークン
ADMIN_ID=管理者のDiscordユーザーID（数字）
```

Python 3.10 以上を推奨します。

### 4. bot起動

各botの`bot.py`は同ディレクトリ内のモジュールをトップレベルでimportするため、**それぞれのディレクトリに
`cd`してから起動してください**（リポジトリ直下から`python stage_bot/bot.py`のように実行すると
モジュールが見つからずエラーになります）。

```bash
# ターミナル1：ステージbot
cd stage_bot
python bot.py

# ターミナル2：個人チャンネル管理bot
cd channel_manager
python bot.py
```

常時稼働させる場合は、`systemd` や `pm2`、Railway / Oracle Cloud などのホスティング上で
2つの別プロセス（別サービス、作業ディレクトリもそれぞれ`stage_bot/`・`channel_manager/`）として
登録することをおすすめします。

### 5. Discord上での設定

#### ステージbot

サーバーに `/stage-setup` コマンドを実行し、トリガーにしたいボイスチャンネルを選択します（実行にはサーバー管理権限が必要です）。

```
/stage-setup trigger:#作成用ボイスチャンネル category:#イベント（任意）
```

#### 個人チャンネル管理bot

初回セットアップ時、既存メンバーがいる場合は `/sync_channels`（管理者用）で一括紐付けを行います。
個別に紐付けが必要な場合は `/link_channel`、状況確認には `/check_channel` を使用してください。
セットアップ後の整合性チェックには `/audit_data`、退出済みメンバーの権限が残っている場合の
クリーンアップには `/cleanup_ghost_permissions`（まず`dry_run:True`で確認してから実行）が使えます。

## 使い方

### ステージbot（メンバー側）

1. 「作成用」として設定されたボイスチャンネルに参加する
2. 自動で `🎤・(あなたの名前) のステージ` という一時ステージチャンネルが作成され、移動する
3. ステージが自動開始され、作成者は自動的にスピーカーに設定される
   - うまく自動設定されない場合は、Discordの操作で「発言をリクエスト」→ 自分はチャンネルの
     管理権限を持っているため、そのまま「スピーカーになる」を選択できます
4. チャット欄の「コントロールパネルを開く」ボタン、または `/stage-panel` から名前変更・パスワード設定・
   プリセット適用などの各種操作ができる
5. 誰もいなくなると、そのステージチャンネルは自動的に削除される（ログのDMダウンロードを選択可能）

その他、`/stage-reaction`（リアクションピッカー）、`/stage-join`（パスワード付きステージへの参加）、
`/stage-ticket`（チケットコードでの入室）、`/stage-help`（機能・コマンド一覧）が利用できます。

### 個人チャンネル管理bot（メンバー側）

1. 案内に従って規約に同意し、参加区分・学年を選択する
2. 学年・区分に応じたカテゴリ内に、自分専用のチャンネルが自動作成される
3. 主なセルフサービスコマンド：
   - `/help` — コマンド一覧を表示
   - `/find` — 名前の一部から他メンバーの個人チャンネルを検索
   - `/rename` — 自分の個人チャンネル名を変更
   - `/setup_selfpanel` — 自分のチャンネルに設定パネル（お気に入り登録ボタンなど）を設置
   - `/mydata` — 自分の登録状況を確認
   - `/export_my_channel` — 自分のチャンネルの投稿内容をファイルで受け取る
   - `/delete_my_data` — 在籍したまま自分のチャンネルと登録データを削除
   - `/leave` — サーバー退出前にチャンネルの扱い（削除／非表示）を選択
   - `/privacy` — データの取り扱いについての説明を再表示
   - `/invite` — 招待申請フォームを開く

## カスタマイズ

### ステージbot

- `stage_bot/storage.py` 内の `DEFAULT_NAME_TEMPLATE` / `DEFAULT_TOPIC_TEMPLATE` を編集すると、
  作成されるチャンネル名・ステージトピックの文言を変更できます（`{username}` が
  参加したメンバーの表示名に置き換わります）。
- サーバーごとに作成先カテゴリを変えたい場合は `/stage-setup` の `category` 引数で指定してください。

### 個人チャンネル管理bot

- `channel_manager/storage.py` 内の `GRADE_CATEGORIES` を編集すると、学年・区分ごとのカテゴリ名を
  変更・追加できます。
- 同ファイルの `HIDDEN_RETENTION_DAYS` で、`/leave` の「非表示にする」選択後や自主退出・キック時に
  チャンネルを自動削除するまでの保持日数を調整できます（デフォルト30日）。
- 同ファイルの `MAX_CHANNELS_PER_CATEGORY` でカテゴリ自動細分化の閾値を調整できます
  （Discordの1カテゴリあたりの上限は50のため、デフォルトは49）。
- 同ファイルの `RULES_TEXT` を編集すると、オンボーディング時の規約文言を変更できます。

## 注意点

- ステージチャンネルの作成・削除、個人チャンネルの作成・管理には、それぞれのbotに
  「チャンネルの管理」権限が必要です。
- ステージbot：1つの作成用チャンネルに同時に複数人が参加すると、参加した人数分のステージ
  チャンネルが作成されます（1人1チャンネル方式）。複数人で1つのステージを共有したい場合は、
  作成されたステージチャンネルへ各自で参加してもらう運用にしてください。
- 個人チャンネル管理bot：`/reset`（管理者用）はメンバーのロールと個人チャンネルを一括で
  リセットする破壊的操作です。実行時は対象メンバーに事前・事後でDM通知が送られます。
- 個人チャンネル管理bot：`/cleanup_ghost_permissions` は退出済みメンバーなどの残留権限を削除する
  操作です。必ず `dry_run:True`（デフォルト）で対象を確認してから `dry_run:False` で実行してください。
- 2つのbotは別プロセス・別トークンで動作するため、片方を再起動・停止してももう片方には
  影響しません。障害切り分けの際は、それぞれの`bot_errors.log`（`stage_bot/data/` と
  `channel_manager/`直下）やスラッシュコマンド（`/stage-recent-errors` / `/recent_errors`）で
  個別に確認してください。