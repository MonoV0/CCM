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
  - Manage Guild（サーバー管理：対象者限定招待に必要）
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
   - `/invite` — username・User ID・人物説明を入力して招待を申請
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

## 審査付き・対象者限定の招待（`/invite`）

### 利用者・管理者の流れ

1. サーバー内で `/invite` を実行し、相手の **Discord username、User ID、人物・関係性の説明**を入力します。
   User IDはDiscordの設定 → 詳細設定 → 開発者モードを有効にし、相手のプロフィールから
   「ユーザーIDをコピー」で取得します。表示名やサーバー内ニックネームは使いません。
2. BotはIDからアカウントを取得し、入力usernameと一致するか確認します。
   usernameからDiscord全体を検索しません。名前の一致は誤入力の検出用であり、本人認証には使いません。
3. `ADMIN_ID` の管理者へ、申請者、入力username、取得したusername、**承認対象User ID**、人物・関係性、
   申請IDを記載したDMが届きます。管理者は説明とアカウントを確認し「承認」または「却下」します。
   審査期限は申請から7日です。承認対象IDは後から差し替えられません。
4. 承認時だけ、Discordの `target_users_file` に対象者1名のIDを指定した招待を作成します。
   **1回限り・発行から24時間**。対象制限の非同期処理完了と許可ID一覧を確認してから配布します。
5. Botが対象者本人へ招待をDMします。通常、申請者は転送不要です。
   DMできない場合だけ、同じ対象者限定リンクを申請者へDMし、転送を依頼します。
6. 申請者へのDMも届かない場合は、サーバー内の `/invite_status` で直近3件を確認できます。
   `/invite_status request_id:<申請ID>` では特定の申請を確認できます。結果は本人だけに表示され、
   当該サーバーの申請者と `ADMIN_ID` のみ閲覧可能です。リンクを取得しても再発行はしません。
7. 対象者が自身のDiscordアカウントで招待を受けると、従来の規約同意・参加区分・学年選択へ進みます。
   招待APIに `role_ids` を送らないため、既存のロール付与・個人チャンネル作成を迂回しません。

### 設定・データ・運用

- **新しい環境変数、OAuth2設定、外部Webサービスは不要**です。
  既存の `CHANNEL_MANAGER_TOKEN` と `ADMIN_ID` を使います。
- Botに **Create Instant Invite（招待を作成）** と **Manage Guild（サーバー管理）** を付与してください。
  招待先は、Botが招待作成できる最初のテキストチャンネルです。
  既存のチャンネル・ロール管理権限もそのまま必要です。
- `invite_requests.json` を既存JSONと同じ起動ディレクトリに保存します。
  申請者・対象者ID、人物説明、審査者、判断日時、状態、配送結果、発行済みコードを含みます。
  リポジトリへコミットせず、Bot実行ユーザーだけが読める永続ディスクで管理・バックアップしてください。
  一時ファイルは所有者のみ読み書き可能で作成し、fsync・atomic replaceで保存します。
- 審査はファイルロックと状態遷移で排他制御します。同じ申請への承認／却下の連打や、同じ対象への
  処理中の再申請を防ぎます。`invite_requests.lock` と同じ永続ボリュームを使ってください。
  ファイルロックはLinux/macOSの `flock` を使用します。別ディスクの複数Botでの並行運用は非対応です。
- `pending` の審査ボタンは、固定custom IDと保存したメッセージIDから再起動時に復元します。
  判断済み・期限切れの申請は操作できません。JSON破損時は空データに置き換えず停止します。
- 発行前に `issuing` を保存します。発行POSTに自動再試行はありません。
  通信切断、制限反映失敗、ストレージ障害などではリンクを配布せず停止します。
  コードが判明している場合は削除を試みます。一般招待へ切り替えるフォールバックはありません。
  429の場合も安全側で停止するため、管理者はレート制限解除後に状態を調査してください。
- `issuing` / `failed` は再起動後も自動再発行しません。管理者はBotを停止し、記録のcodeと
  Discordのサーバー設定 → 招待、監査ログの申請IDを照合してください。
  発行済みの可能性がある招待を削除し、無効化を確認してから、その記録を削除せず
  `state` を `cancelled` に変更して再起動します。コード不明時も対象となるBot発行招待を調査してください。
  その後、新しい申請・新しい審査が必要です。`pending` / `ready` に手動で戻してはいけません。
  `submitting` で審査DMが届かなかった場合も、同様に停止して `cancelled` に変更してから再申請できます。
- 使用済みかどうかはDiscordが制御します。状態表示 `ready` は「発行・制限確認済み」であり、参加済みを
  意味しません。`/invite_status` が返すリンクも、すでに使用されていれば無効です。
- 移行前の審査は永続化されていないため再申請が必要です。旧方式で発行済みの一般招待は本改修では
  自動削除しません。導入時に管理者が失効させてください。別経路の一般招待やサーバーの公開設定は管理対象外です。

### APIの根拠と制限

- [Discord公式：対象者限定招待](https://docs.discord.com/developers/tutorials/using-community-invites)
  は `target_users_file` のUser IDだけが招待を閲覧・利用できることを規定しています。
- [招待API](https://docs.discord.com/developers/resources/invite) のjob-statusが `COMPLETED=2`、
  target-usersのCSVが対象者1名と完全一致することを確認します。
- [discord.py API](https://discordpy.readthedocs.io/en/latest/api.html#discord.abc.GuildChannel.create_invite)
  の `target_user` は配信視聴用であり、参加者制限ではありません。
  `target_users_file` を扱う部分だけ、discord.pyの依存パッケージであるaiohttpで公式REST APIを呼びます。
- [User API](https://docs.discord.com/developers/resources/user) はIDからの取得とDM作成に使います。
  共通サーバー・プライバシー設定等によってDMできない場合があり、BotによるDM成功は保証できません。
- OAuth2なしでusernameだけから対象アカウントを確定することはできないため、User IDの入力が必要です。
  Botは入力IDの実在を確認し、Discordが招待利用時のアカウントを制限しますが、説明に書かれた実在人物と
  アカウントの関係までは証明できません。管理者は審査時にIDも確認してください。
- 対象者限定APIが実環境で利用できない場合、このフローは停止します。一般招待に緩和しません。
  実サーバーでのAPI利用可否・第三者の利用拒否は下記の手動確認が必要です。

### 手動テスト（検証用サーバー・アカウント）

1. Botへ必要権限を付与し、未参加の対象者Aと別アカウントB、申請者、管理者を用意します。
2. `/invite` で説明の空欄、不正ID、usernameとIDの不一致を試し、申請されないことを確認します。
3. 正しい内容で申請し、管理者DMに人物・関係性とAのIDが表示され、承認前には招待が増えないことを確認します。
4. 却下して招待が作られないこと、管理者以外が承認できないことを確認します。
5. 審査待ちでBotを再起動し、元のDMで承認します。承認を連打しても招待が1個だけであることを確認します。
6. 招待の利用回数1・期限24時間・許可対象AのみをAPI／サーバー設定で確認します。
   **Bが同じリンクを開いて参加できないことを先に確認**し、その後Aで参加します。
7. AへのDMが可能ならAへ直接届くこと、DMを拒否したケースでは申請者へ転送用リンクが届くことを確認します。
   申請者もDM拒否の場合、`/invite_status` で同じコードを取得でき、他のメンバーからは閲覧できないことを確認します。
8. Aが参加後に、従来のようこそ案内 → 規約同意 → 参加区分／学年 → ロール・個人チャンネル作成・案内削除を確認します。
   再参加者の既存チャンネル復元も確認します。招待は2回目に利用できません。
9. 未使用の別申請で24時間後の失効と、7日経過した審査の拒否を確認します。
10. 検証環境で権限不足・発行後の通信障害・発行中の再起動を試し、リンクが漏れず再発行されないことを確認します。
    停止記録の復旧は上記の手順に従ってください。

自動テストは `python -m pytest -q tests`。本番へ接続せず、対象制限API・DM・永続化障害をモックします。

## Raspberry Piでの更新（シェル／管理者専用 `/update`）

個人チャンネル管理Botを **systemdのユーザーサービス**として動かします。
`/update` は `channel_manager/.env` の **`ADMIN_ID` 本人だけ**が実行できます。
Discordの管理者ロールを持っていても、IDが一致しなければ実行できません。
ステージBotはこの更新サービスから停止・再起動しません。

### 初回設定

Raspberry Pi OS（systemdあり）で、Botを動かす一般ユーザーとしてSSHログインしてください。
以下は **既存のリポジトリが `~/CCM` にある場合**です。別の場所にある場合は、作業用のコピーを増やさず、
下記の `cd` と2つの `.service` 内の `%h/CCM` を実際の配置先に変更してください。
`%h` はそのユーザーのホームディレクトリです。

最初の一度だけ、現在動いている個人チャンネル管理Botを、元の起動方法で停止します
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
cp deploy/systemd/ccm-bot.service deploy/systemd/ccm-update.service ~/.config/systemd/user/
install -m 700 scripts/update.sh ~/.local/lib/ccm/update.sh
chmod 600 channel_manager/.env

systemctl --user daemon-reload
systemctl --user enable --now ccm-bot.service
# ログアウト後・Raspberry Pi再起動後もBotを動かすための初回設定
sudo loginctl enable-linger "$USER"
```

`git`、`python3-venv`、`flock`（util-linux）、`timeout`（coreutils）が必要です。
Python 3.10以上を使用してください。既存の `CHANNEL_MANAGER_TOKEN` / `ADMIN_ID` をそのまま使い、
新しい環境変数は不要です。Botにsudo権限を与えたり、Discordから任意のコマンドを渡したりしません。

### 普段の更新用シェルコマンド

```bash
systemctl --user start ccm-update.service
```

このコマンドは更新終了まで待ちます。更新サービスが以下を順番に実行します。

1. mainブランチ・追跡ファイルに未保存の変更がないこと・仮想環境を確認し、排他ロックを取得。
2. `ccm-bot.service` を停止。
3. `git pull --ff-only origin main`、依存パッケージのインストール・整合性確認。
4. `ccm-bot.service` を起動し、5秒後にサービス稼働状態を確認。

Botサービスとは別の更新サービスが処理するので、Bot停止に巻き込まれません。
更新スクリプトはリポジトリ外にコピーして実行するため、pullによる自身の書き換えの影響も受けません。
今後スクリプトやサービス定義自体が変更された場合は、初回設定のコピーと `daemon-reload` を再実行してください。

### Discordから更新

サーバー内で `ADMIN_ID` 本人が `/update` を実行します。
Botは本人だけに受付前の案内を返し、固定の `ccm-update.service` を起動します。
60秒のクールダウンとsystemd・ファイルロックで重複更新を防ぎます。
起動DMはBotの再起動を示しますが、更新失敗後の再起動でも届きます。更新の成否は更新ログで確認してください。
受付メッセージだけでは更新成功を意味しません。
スラッシュコマンドは起動時に同期されるため、初回導入は上記シェル手順が必要です。

### 状態・失敗時の確認

```bash
systemctl --user status ccm-bot.service ccm-update.service
journalctl --user -u ccm-update.service -n 100 --no-pager
journalctl --user -u ccm-bot.service -n 100 --no-pager
```

更新サービスはoneshotなので、正常終了後はinactiveでも正常です。終了コードとログを確認してください。
BotがDiscordへ接続・コマンド同期できたかは、起動DMとBotログで確認します。

ローカル変更や別ブランチの場合は停止前に拒否し、`git reset --hard` 等で変更を破棄しません。
pullや依存更新に失敗した場合も現在のチェックアウトからBotの起動を試み、更新処理は失敗として記録します。
コードや依存環境の自動ロールバックは行わないので、起動に失敗した場合はログを確認して修復してください。
`.env`、JSON、個人チャンネルの記録は削除しません。導入前に既存データをバックアップしてください。

手動確認：非管理者で拒否されること、管理者で更新されること、連打して二重起動しないこと、
ネットワーク切断時に失敗ログが残り再起動を試みること、更新後に `/invite` と既存チャンネルが使えることを確認します。

systemd参考：[サービス](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)、
[ログアウト後の継続実行](https://www.freedesktop.org/software/systemd/man/252/loginctl.html)。
