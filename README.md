# watch-bot

指定したユーザーが発言するたびに、キーワードに合ったGIF/画像を自動送信するDiscordボットです。

## 構成

```
.
├── bot.py              # エントリーポイント
├── cogs/
│   └── watch_cog.py    # /watch コマンド本体
├── requirements.txt
├── Procfile            # Railway用の起動コマンド定義
├── .env.example        # 環境変数のサンプル
└── .gitignore
```

## コマンド

- `/watch add target:@ユーザー [gif_keyword:単語] [image_keyword:単語] [cooldown:秒数]`
  - `gif_keyword` / `image_keyword` はどちらか片方だけでもOK、両方指定すると発言のたびにランダムでどちらかを送信します。
  - `cooldown` は連投を防ぐための秒数（省略時10秒）。
- `/watch remove target:@ユーザー` — 設定解除
- `/watch list` — 現在の設定一覧を表示

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
3. OAuth2 → URL Generator で `bot` と `applications.commands` にチェックを入れ、生成したURLでサーバーに招待
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
