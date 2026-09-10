# watch-bot

指定したユーザーが発言するたびに、キーワードに合ったGIF/画像を自動送信するDiscordボットです。

## 構成

```
.
├── bot.py              # エントリーポイント
├── watch_cog.py        # /watch コマンド本体（bot.pyと同じ階層に置くこと）
├── requirements.txt
├── Procfile            # Railway用の起動コマンド定義
├── .env.example        # 環境変数のサンプル
└── .gitignore
```

> **重要:** `bot.py` と `watch_cog.py` は必ず同じフォルダ（リポジトリ直下）に置いてください。
> サブフォルダに分けると、Railway上で `ModuleNotFoundError: No module named 'watch_cog'`
> のようなエラーになることがあります。GitHubにファイルをアップロードする際、
> 意図せずフォルダ構成が変わっていないか確認してください。

## コマンド

- `/watch add target:@ユーザー [gif_keyword:単語] [image_keyword:単語]`
  - `gif_keyword` / `image_keyword` はどちらか片方だけでもOK、両方指定すると発言のたびにランダムでどちらかを送信します。
  - クールダウンはありません。対象が発言するたびに毎回即座に反応します。
- `/watch off target:@ユーザー` — 設定は残したまま送信だけ一時停止（即時反映）
- `/watch on target:@ユーザー` — 一時停止を解除（即時反映）
- `/watch remove target:@ユーザー` — 設定そのものを削除
- `/watch list` — 現在の設定一覧（稼働中/停止中も表示）を表示
- `/erase_all` — 実行したチャンネルでBotが送信したメッセージを全て削除（Botに「メッセージの管理」権限が必要）

## ローカルでの動かし方

```bash
git clone <このリポジトリのURL>
cd watch-bot
python -m venv .venv
source .venv/bin/activate      # Windowsは .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env           # DISCORD_TOKEN と TENOR_API_KEY を記入
python bot.py
```

### Botの準備

1. [Discord Developer Portal](https://discord.com/developers/applications) でアプリケーションを作成し、Botトークンを取得 → `DISCORD_TOKEN`
2. 同ページの「Bot」タブで **SERVER MEMBERS INTENT** を必ずONにする（`@ユーザー` を選択するのに必要）
3. OAuth2 → URL Generator で `bot` と `applications.commands` にチェックを入れる。Bot Permissionsは View Channels, Send Messages, Embed Links, Read Message History に加えて、`/erase_all` を使うなら **Manage Messages（メッセージの管理）** も忘れずにチェックし、生成したURLでサーバーに招待
4. GIF機能を使うなら [KLIPY](https://partner.klipy.com) で無料APIキーを取得 → `GIF_API_KEY`
   （画像検索(Openverse)側はAPIキー不要です）

   > **注意:** GoogleはTenor APIを2026年6月30日に完全終了しました。KLIPYはTenorと同じ
   > パラメータ・レスポンス形式を持つ「Tenor互換エンドポイント」を無料で提供しているため、
   > このプロジェクトではKLIPYを使っています。`partner.klipy.com` でサインアップし、
   > 「Add Platform」からテスト用APIキー（レート制限付き・無料）を発行してください。
   > 利用量が増えたら、ダッシュボードから本番用キーを申請できます。

## GitHubへのプッシュ

```bash
git init
git add .
git commit -m "Initial commit: watch bot"
git branch -M main
git remote add origin <あなたのGitHubリポジトリURL>
git push -u origin main
```

`.env` は `.gitignore` に含めているのでコミットされません。トークン類は絶対にコミットしないよう注意してください。

## Railwayへのデプロイ

1. Railwayで「New Project」→「Deploy from GitHub repo」を選び、このリポジトリを選択
2. Nixpacksが `requirements.txt` を自動検出してPython環境を構築します
3. `Procfile` の `worker: python bot.py` がそのまま起動コマンドとして使われます
   （Railwayのダッシュボードで Deploy → Settings → Start Command を明示的に `python bot.py` に設定しても構いません）
4. 「Variables」タブで環境変数を設定:
   - `DISCORD_TOKEN`
   - `GIF_API_KEY`（KLIPYのキー）
5. デプロイ後、ログに `Logged in as ...` と `Synced N slash command(s)` が出ていれば起動成功です

### 設定の永続化について（重要）

`watch_data.json` はコンテナのローカルディスクに保存されます。Railwayは**再デプロイのたびにファイルシステムがリセットされる**ため、`/watch add` で登録した設定は再デプロイ時に消えてしまいます。

再起動をまたいで設定を残したい場合は、Railwayの **Volume**機能でプロジェクトのルートディレクトリ（または `WATCH_DATA_PATH` で指定したパス）を永続ディスクにマウントしてください。長期的にはSQLiteやRailwayのPostgresアドオンに保存先を切り替えるとより安全です。

**Volumeを `/INFO` にマウントした場合:**

Railwayの「Variables」タブに以下を追加してください（コード変更は不要です）。

```
WATCH_DATA_PATH=/INFO/watch_data.json
```

設定後、再デプロイすると `watch_data.json` が `/INFO` 配下（永続ディスク）に保存されるようになり、以後の再デプロイでも `/watch add` の設定が保持されます。

### ログの確認方法

Railwayのプロジェクト画面 → 対象デプロイ → 「Deploy Logs」（または「View Logs」）で、bot側の動作ログがリアルタイムに表示されます。特に以下のような警告行が出ている場合は、そのキーワードでのヒット数自体が少なく、同じ画像/GIFが繰り返されやすい状態です。より一般的なキーワードに変更することをおすすめします。

```
GIF keyword='...' はヒット数が1件しかなく、同じ画像が繰り返される可能性があります。
```
