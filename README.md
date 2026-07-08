# 一時ステージチャンネル作成bot

「作成用」ボイスチャンネルに参加すると一時的なステージチャンネルが自動作成され、
参加者が0人になると自動的に削除されるDiscord botです（discord.py / Python製）。

## 仕組み

1. サーバー管理者が `/stage-setup` コマンドで「作成用」ボイスチャンネルを指定する
2. メンバーがそのボイスチャンネルに参加する
3. bot が一時的なステージチャンネルを作成し、そのメンバーを移動・スピーカーに設定する
4. ステージチャンネルの参加者が0人になったら、botが自動的にチャンネルを削除する

再起動した場合も `data/temp_channels.json` に作成済みチャンネルが記録されているため、
起動時に空の一時チャンネルがあれば自動でクリーンアップされます。

## セットアップ手順

### 1. Discord Developer Portalでアプリを作成

1. https://discord.com/developers/applications にアクセスし「New Application」
2. 左メニューの「Bot」からBotを作成し、「Reset Token」でトークンを取得
   - ※ MESSAGE CONTENT INTENT などの特権インテントは**有効化不要**です
     （このbotはスラッシュコマンドとボイス状態の変化のみを使用します）

### 2. botをサーバーに招待

「OAuth2 > URL Generator」で以下を選択してURLを生成し、自分のサーバーに招待してください。

- **SCOPES**: `bot`, `applications.commands`
- **BOT PERMISSIONS**:
  - View Channels（チャンネルを見る）
  - Manage Channels（チャンネルの管理）
  - Connect（接続）
  - Move Members（メンバーを移動）
  - Mute Members（メンバーをミュート ※スピーカー設定に使用）

  権限を手動入力する場合は、Permissions Integer に `22021136` を指定しても同じ権限になります。

### 3. ローカル環境の準備

```bash
cd discord-stage-bot
pip install -r requirements.txt
cp .env.example .env
# .env を開いて DISCORD_TOKEN に取得したトークンを設定
```

Python 3.10 以上を推奨します。

### 4. bot起動

```bash
python bot.py
```

### 5. Discord上での設定

サーバーに `/stage-setup` コマンドを実行し、トリガーにしたいボイスチャンネルを選択します。

```
/stage-setup trigger:#作成用ボイスチャンネル category:#イベント（任意）
```

設定を解除したい場合は `/stage-disable` を実行してください。

## 使い方（メンバー側）

1. 「作成用」として設定されたボイスチャンネルに参加する
2. 自動で `🎤・(あなたの名前) のステージ` という一時ステージチャンネルが作成され、移動する
3. ステージが自動開始され、作成者は自動的にスピーカーに設定される
   - うまく自動設定されない場合は、Discordの操作で「発言をリクエスト」→ 自分はチャンネルの
     管理権限を持っているため、そのまま「スピーカーになる」を選択できます
4. 誰もいなくなると、そのステージチャンネルは自動的に削除される

## ファイル構成

```
discord-stage-bot/
├── bot.py              # bot本体
├── requirements.txt    # 依存パッケージ
├── .env.example         # 環境変数のサンプル
└── data/                # 実行時に自動生成される設定・状態の保存先
    ├── guild_config.json
    └── temp_channels.json
```

## カスタマイズ

- `bot.py` 内の `DEFAULT_NAME_TEMPLATE` / `DEFAULT_TOPIC_TEMPLATE` を編集すると、
  作成されるチャンネル名・ステージトピックの文言を変更できます（`{username}` が
  参加したメンバーの表示名に置き換わります）。
- サーバーごとに作成先カテゴリを変えたい場合は `/stage-setup` の `category` 引数で指定してください。

## 注意点

- ステージチャンネルの作成・削除には bot に「チャンネルの管理」権限が必要です。
- 1つの作成用チャンネルに同時に複数人が参加すると、参加した人数分のステージチャンネルが
  作成されます（1人1チャンネル方式）。複数人で1つのステージを共有したい場合は、作成された
  ステージチャンネルへ各自で参加してもらう運用にしてください。
