# 作業案内

## 構成
- 独立した2つのDiscord bot。`stage_bot/` と `channel_manager/` は別プロセス。
- 各 `bot.py` は起動入口、`bot_core.py` は環境変数・ログ・Bot生成。
- `commands.py` はスラッシュコマンド、`events.py` はイベントと定期処理。
- `views.py` はUI、`storage.py` はJSON保存、`utils.py` は補助処理。
- ステージの削除・公開切り替え・ブロック判定は `stage_bot/operations.py`。
- ステージUIの対象指定と権限チェックは `stage_bot/stage_context.py`。

## 修正時の確認
- 最初は対象機能の関数名を検索し、関連する箇所だけ読む。
- 2つのbotで同名モジュールがあるため、安易にimportを統合しない。
- UIに操作対象のステージを渡す。コマンド実行場所と対象ステージは別になり得る。
- Discord上の削除が成功、またはNotFoundの場合にだけ管理記録を消す。
- 機能と保存形式を維持し、重複処理の共通化を優先する。

## 検証
- `python -m pip install -r requirements-dev.txt`
- リポジトリ直下で `python -m pytest -q tests`。
- テストは一時コピー・ダミー環境変数・Discord APIモックを使い、本番へ接続しない。
- 2つのbotを同一プロセスで読み込むテストでは、モジュールキャッシュを分離する。
