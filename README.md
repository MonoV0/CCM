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
  - Create Instant Invite（招待を作成）
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
   - `/invite` — 相手の名前・人物や関係性を入力して招待を申請
   - `/invite_status` — 自分の申請状態・承認済みリンクを確認

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

## 開発時の検証

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q tests
```

回帰テストは一時フォルダとDiscord APIのモックを使用し、本番のBotや保存データには接続しません。
コードの確認箇所は `AGENTS.md` にまとめています。ステージの共通操作は `operations.py`、
UIの操作対象と権限確認は `stage_context.py` に分離しています。

## 審査付きの招待（`/invite`）

1. `/invite` で **相手の名前** と **人物・申請者との関係**を入力します。両方必須です。
   User ID、開発者モード、アプリ追加、外部認証は不要です。表示名でも申請できます。
2. `ADMIN_ID` の管理者へ、申請者・相手の名前・人物説明・申請IDを記載したDMが届きます。
   管理者が **承認／却下**します。審査期限は7日で、未承認の招待は発行しません。
3. 承認後に **1回限り・24時間有効**のDiscord招待を発行し、申請者へそのまま転送できるメッセージをDMします。
   この転送メッセージには、人物説明などの審査情報は含めません。
4. 申請者が相手へメッセージを転送し、相手が参加します。
   規約同意・参加区分・学年・ロール付与・個人チャンネル作成は従来のオンボーディングを使います。

申請者がDMを受け取れない場合、`/invite_status` で直近3件、
`/invite_status request_id:<申請ID>` で特定の申請の状態と同じ招待リンクを確認できます。
当該サーバーの申請者と `ADMIN_ID` のみ閲覧でき、リンク取得では再発行しません。

### 安全性・運用

- 管理者IDと審査メッセージIDを照合し、二重承認・却下後の承認を拒否します。
- 名前は申請内容であり本人確認情報ではありません。**リンクを持つ第三者の利用は防げません**。
  招待は申請した相手にだけ渡してください。本人限定招待や相手本人への自動DMは行いません。
- 同じ申請者から同じ名前への有効な申請は重複防止しますが、名前による人物同定はできません。
- `invite_requests.json` に説明・審査者・判断日時・状態・発行コードを保存します。
  Gitへコミットせず、Botユーザーだけが読める永続ストレージでバックアップしてください。
- flock（Linux/macOS）とatomic replace・fsyncで状態を保存し、発行前に `issuing` とします。
  起動時に審査待ちのボタンを復元します。JSON破損時には空データに戻さず停止します。
- 招待発行POSTは自動再試行しません。タイムアウト・保存失敗・APIエラー時はリンクを配布せず停止します。
  コード判明済みの場合は削除を試みます。`issuing` / `failed` は再起動しても再発行しません。
- 発行結果不明時は、管理者がBotを停止し、JSONのcode・Discordの招待一覧・監査ログの申請IDを照合します。
  可能性のある招待を失効させたことを確認してから、記録を削除せず `state` を `cancelled` に変更します。
  再起動後に新しく申請・審査してください。`pending` / `ready` に手動で戻してはいけません。
- `ready` は発行済みの意味です。使用済みかはDiscordが制御し、使用済みリンクの再取得でも再参加はできません。
- 新しい環境変数はありません。既存の `CHANNEL_MANAGER_TOKEN` / `ADMIN_ID` と
  **Create Instant Invite（招待を作成）**権限を使います。招待先は作成権限のある最初のテキストチャンネルです。
  今回の招待方式にはManage Guildは不要ですが、既存のチャンネル・ロール管理権限は維持してください。

### 旧方式からの移行

User ID指定方式の未処理申請は `/invite` から再申請してください。
古い審査ボタンで一般招待を発行することはありません。旧方式で発行済みの招待と記録は変更せず、
`/invite_status` でも従来の本人限定という説明を維持します。
発行中・発行失敗の記録は上記の調査手順に従ってください。

### 手動確認

1. フォームが名前と人物・関係性の2項目だけで、空白のみは拒否されることを確認。
2. 管理者DMで説明を確認し、承認前・却下後には招待が発行されないことを確認。
3. 審査待ちでBotを再起動し、元のボタンで承認。連打しても発行が1回だけであることを確認。
4. 申請者へ転送用メッセージが届き、人物説明が含まれないことを確認。
5. 申請者がDM拒否の場合、`/invite_status` で同じリンクを取得でき、別ユーザーには見えないことを確認。
6. 相手へ転送して参加し、既存オンボーディング・個人チャンネル作成・ロール付与を確認。
7. 1回の使用または24時間経過で無効になること、通信障害・保存失敗で再発行されないことを確認。

自動テストは `python -m pytest -q tests`。本番Discordには接続しません。

## Raspberry Piでの更新（シェル／管理者専用 `/update`）

`AGENTS.md` の構成に合わせ、**独立した2つのBotを別々のsystemdユーザーサービス**として動かします。

| Bot | サービス | 作業ディレクトリ・設定 |
| --- | --- | --- |
| 個人チャンネル管理Bot | `ccm-bot.service` | `channel_manager/`、同ディレクトリの `.env` |
| ステージBot | `ccm-stage-bot.service` | `stage_bot/`、同ディレクトリの `.env` |

更新処理は別の `ccm-update.service` が担当し、**両方を停止 → mainを1回pull → 両方を起動**します。
それぞれの `bot.py`、トークン、JSON保存先、モジュール構成は独立したままです。
`/update` は `channel_manager/.env` の **`ADMIN_ID` 本人だけ**が実行できます。
Discordの管理者ロールを持っていても、IDが一致しなければ実行できません。
Discordの `/update` は個人チャンネル管理Botに登録し、**2つのBotをまとめて更新**します。

### 初回設定

Raspberry Pi OS（systemdあり）で、Botを動かす一般ユーザーとしてSSHログインしてください。
以下は **既存のリポジトリが `~/CCM` にある場合**です。別の場所にある場合は、作業用のコピーを増やさず、
下記の `cd` と3つの `.service` 内の `%h/CCM` を実際の配置先に変更してください。
`%h` はそのユーザーのホームディレクトリです。

最初の一度だけ、現在動いている両方のBotを、それぞれ元の起動方法で停止します
（ターミナル起動なら `Ctrl+C`、既存サービスならそのサービスを停止し、自動起動も解除）。
**同じBotを二重に起動しないでください。** 既存の `.env` とJSONデータは移動・削除しません。
この機能を含むPRをmainにマージした後、次を実行します。

```bash
cd ~/CCM
git switch main
git pull --ff-only origin main

# 初回のみ。既存.venvがある場合はその環境を使う
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

mkdir -p ~/.config/systemd/user ~/.local/lib/ccm
cp deploy/systemd/ccm-bot.service deploy/systemd/ccm-stage-bot.service deploy/systemd/ccm-update.service ~/.config/systemd/user/
install -m 700 scripts/update.sh ~/.local/lib/ccm/update.sh
chmod 600 channel_manager/.env stage_bot/.env

systemctl --user daemon-reload
systemctl --user enable --now ccm-bot.service ccm-stage-bot.service
# ログアウト後・Raspberry Pi再起動後もBotを動かすための初回設定
sudo loginctl enable-linger "$USER"
```

`git`、`python3-venv`、`flock`（util-linux）、`timeout`（coreutils）が必要です。
Python 3.10以上を使用してください。既存の個人チャンネル管理用 `CHANNEL_MANAGER_TOKEN` / `ADMIN_ID` と、ステージ用 `DISCORD_TOKEN` をそのまま使い、
新しい環境変数は不要です。Botにsudo権限を与えたり、Discordから任意のコマンドを渡したりしません。

前版の1Bot用systemd設定を導入済みの場合も、3つのサービス定義と更新スクリプトを再コピーし、
`daemon-reload` を実行してください。その後、既存のステージBotを元の起動方法で停止してから
`ccm-stage-bot.service` を有効化します。ステージ用サービス未登録なら、更新は両Botを停止する前に拒否します。

### 普段の更新用シェルコマンド

```bash
systemctl --user start ccm-update.service
```

このコマンドは更新終了まで待ちます。更新サービスが以下を順番に実行します。

1. mainブランチ・追跡ファイルに未保存の変更がないこと・仮想環境を確認し、排他ロックを取得。
2. `ccm-bot.service` と `ccm-stage-bot.service` を停止し、両方の停止完了を待つ。
3. `git pull --ff-only origin main`、依存パッケージのインストール・整合性確認。
4. 両サービスを起動し、5秒後に**それぞれ**の稼働状態を確認。片方だけ稼働している場合も失敗とする。

両Botサービスとは別の更新サービスが処理するので、Bot停止に巻き込まれません。
更新スクリプトはリポジトリ外にコピーして実行するため、pullによる自身の書き換えの影響も受けません。
今後スクリプトやサービス定義自体が変更された場合は、初回設定のコピーと `daemon-reload` を再実行してください。

### Discordから更新

サーバー内で `ADMIN_ID` 本人が `/update` を実行します。
Botは本人だけに受付前の案内を返し、固定の `ccm-update.service` を起動します。
60秒のクールダウンとsystemd・ファイルロックで重複更新を防ぎます。
各Botの起動DMは再起動を示しますが、更新失敗後の再起動でも届きます。更新の成否は更新ログで確認してください。
受付メッセージだけでは更新成功を意味しません。
スラッシュコマンドは起動時に同期されるため、初回導入は上記シェル手順が必要です。

### 状態・失敗時の確認

```bash
systemctl --user status ccm-bot.service ccm-stage-bot.service ccm-update.service
journalctl --user -u ccm-update.service -n 100 --no-pager
journalctl --user -u ccm-bot.service -u ccm-stage-bot.service -n 100 --no-pager
```

更新サービスはoneshotなので、正常終了後はinactiveでも正常です。終了コードとログを確認してください。
BotがDiscordへ接続・コマンド同期できたかは、起動DMとBotログで確認します。

ローカル変更や別ブランチの場合は停止前に拒否し、`git reset --hard` 等で変更を破棄しません。
pullや依存更新に失敗した場合も現在のチェックアウトから両Botの起動を試み、更新処理は失敗として記録します。
コードや依存環境の自動ロールバックは行わないので、起動に失敗した場合はログを確認して修復してください。
`.env`、JSON、個人チャンネルの記録は削除しません。導入前に既存データをバックアップしてください。

手動確認：非管理者で拒否されること、管理者で更新されること、連打して二重起動しないこと、
ネットワーク切断時に失敗ログが残り再起動を試みること、更新後に `/invite`・既存チャンネルとステージの作成・操作が使えることを確認します。

systemd参考：[サービス](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)、
[ログアウト後の継続実行](https://www.freedesktop.org/software/systemd/man/252/loginctl.html)。
