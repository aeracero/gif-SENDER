# watch-bot

指定したユーザーが発言するたびに、キーワードに合ったGIF/画像を自動送信するDiscordボットです。

## 構成

```
.
├── bot.py              # エントリーポイント
├── watch_cog.py        # /watch コマンド本体（bot.pyと同じ階層に置くこと）
├── gif_store.py        # Volume上のGIF本体・SQLite・送信履歴
├── gif_sources.py      # Commons収集とZIP取り込み
├── gif_cog.py          # /gif 管理コマンド
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
  - クールダウンはありません。対象の発言ごとに検索します。同じ対象への送信は順番に処理し、前回と同じURLしか見つからない場合は再送をスキップします。
- `/watch off target:@ユーザー` — 設定は残したまま送信だけ一時停止（即時反映）
- `/watch on target:@ユーザー` — 一時停止を解除（即時反映）
- `/watch remove target:@ユーザー` — 設定そのものを削除
- `/watch list` — 現在の設定一覧（稼働中/停止中も表示）を表示
- `/erase_all` — 実行したチャンネルでBotが送信したメッセージを全て削除（実行者とBotに「メッセージの管理」権限が必要）

## ローカルでの動かし方

```bash
git clone <このリポジトリのURL>
cd watch-bot
python -m venv .venv
source .venv/bin/activate      # Windowsは .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env           # DISCORD_TOKEN と GIF_API_KEY を記入
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

Railwayのプロジェクト画面 → 対象デプロイ → 「Deploy Logs」（または「View Logs」）で、bot側の動作ログがリアルタイムに表示されます。特に以下のような警告行が出ている場合は、そのキーワードでのヒット数自体が少なく、新しい画像/GIFを送れずスキップされやすい状態です。より一般的なキーワードに変更することをおすすめします。

```
GIF keyword='...' はヒット数が1件しかなく、前回と同じURLは再送しません。
```

## テスト

```bash
python -m unittest discover -s tests -v
```

画像の候補が1件しかない場合、初回だけ送信し、以後は別の候補が見つかるまでスキップします。GIFと画像の両方を設定していれば別ソースも試します。別URLで配信される同一画像の内容判定は行いません。

## 検索結果の関連性

- GIF: APIのランダム順を無効化し、1ページ目の先頭10候補以内から選びます。
- 静止画: 1ページ目の30件から、タイトル・タグに検索語がすべて含まれる画像を抽出し、その先頭10候補以内から選びます。英字の大小・全角半角を吸収し、英単語は単語単位、日本語は部分一致で確認します。
- 前回のURLは引き続き除外します。候補不足でも下位ページや一致しない画像に広げません。
- これは画像内容をAIで判定する仕組みではありません。同名の人物・作品や誤ったタグは区別できません。翻訳や同義語展開も行わないため、日本語で一致しない場合は英語表記なども試してください。作品名とキャラクター名を併記すると対象を絞れます。

例: `/watch add target:@ユーザー gif_keyword:作品名 キャラクター名`

検索語に近い静止画が見つからない場合は送信をスキップします。ログの「画像 … 候補数」はタイトル・タグで絞り込んだ後の件数です。

## VolumeからGIFを送る（ローカルコレクション）

GIF本体・候補の採用状況・送信履歴をVolumeに保存します。既存の `/watch add` の `gif_keyword` と同じ名前のコレクションがあれば、採用済みのローカルGIFを添付送信します。実ファイルを送るので送信時の検索API呼び出しはありません。Discordへのアップロード通信は毎回発生します。

### Railwayの設定

Volumeを `/INFO` にマウントしたサービスのVariablesを次のように設定します。

```dotenv
WATCH_DATA_PATH=/INFO/watch_data.json
GIF_LIBRARY_PATH=/INFO/gif-library
GIF_STORAGE_MB=1024
```

`GIF_LIBRARY_PATH` を省略すると、`WATCH_DATA_PATH` と同じフォルダ内の `gif-library` を使います。すでに `WATCH_DATA_PATH=/INFO/watch_data.json` なら追加の保存先設定は不要です。`GIF_STORAGE_MB` はGIF本体の保存上限（既定1024MiB）で、SQLiteやログの容量は含みません。Volumeの空き容量には余裕を持たせてください。

新しい `Pillow` 依存関係を含めて通常どおり再デプロイしてください。起動時にフォルダとSQLiteを作成します。Botには「ファイルを添付」権限も必要です。設定する管理者には「サーバー管理」権限が必要です。

SQLiteとGIFファイルはセットでバックアップしてください。この実装は **単一Botプロセス** での運用を想定しています。

### 使い始める手順

1. 次のどちらかで候補を取り込みます。
   - `/gif collect keyword:cat count:100` — Wikimedia Commonsから最大100件の新しいGIF候補を取得。
   - `/gif import keyword:cat attachment:GIFまたはZIP` — 自分で用意したGIFを取り込み。ZIPは複数回に分けて追加できます。必要なら `credit` に著作者・ライセンス等の表示文を指定します。
2. `/gif review keyword:cat` で実物を1件ずつ表示し、「採用して次へ」「除外して次へ」で選別します。ボタンは5分で期限切れになるため、その場合は同じコマンドで再開できます。
3. 内容を別途確認済みなら `/gif approve_all keyword:cat` で未確認分を一括採用できます。すでに除外したGIFは復帰させません。
4. `/watch add target:@対象ユーザー gif_keyword:cat` で送信対象を設定します。

英字の大小・全角半角・連続する空白は正規化します。日本語と英語の翻訳や別名の自動対応はしません。例えば `猫` と `cat` は別コレクションです。

### 管理コマンド

| コマンド | 用途 |
| --- | --- |
| `/gif list keyword:cat page:1` | ID・採用状況・容量を10件ずつ表示 |
| `/gif preview gif_id:12` | IDを指定して実物を表示 |
| `/gif approve gif_id:12` | 1件を採用・除外から復帰 |
| `/gif exclude gif_id:12` | 以後の送信対象から外す。ファイルは保持 |
| `/gif delete gif_id:12` | 登録を削除。他の登録でも使っていなければ実ファイルも削除 |
| `/gif mode keyword:cat local:false` | 保存済みデータを残して既存のオンライン検索に戻す |
| `/gif mode keyword:cat local:true` | ローカル優先に戻す |

コレクションの設定と採用状況はDiscordサーバーごとに分離します。同じGIFの実ファイルは内容ハッシュで共有・重複排除しますが、他サーバーの管理IDでは操作できません。

### 送信の挙動

- 採用済みの対象からランダムに選び、全候補を一巡するまでは同じものを再送しません。候補の追加・除外がない間は、各周回で全候補を1回ずつ使います。
- 対象ユーザーごとの使用回数と直前のGIFをSQLiteに記録し、再起動後も続きから選びます。
- 周回の境目でも直前と同じGIFを避けます。1件しかない場合は初回だけ送信し、別のGIFが加わるまでスキップします。
- 送信失敗では履歴を進めません。ただしDiscordへの送信成功直後にプロセスが落ち、履歴保存が間に合わなかった場合は再送される可能性があります。
- ローカルモードのキーワードは、候補が未採用・不足・欠損・添付上限超過の場合にオンライン検索へ戻りません。`image_keyword` も同時設定していてもローカルGIFを優先します。
- 採用済みのコレクションがない既存キーワードは、コレクションを作成するまでは従来のオンライン検索です。`/gif collect` は0件で終わってもローカルコレクションを作るため、必要なら `/gif mode ... local:false` で戻せます。
- 停止・除外・削除は次の送信から反映します。すでにDiscordへ送信を開始したものは取り消せません。

### 自動収集の対象と制限

自動収集元は **Wikimedia Commons** です。検索結果からGIF形式で、対応するCC BY・CC BY-SA・CC0・パブリックドメインの情報と著作者情報を取得できるものを候補として保存します。Commons由来のファイルは、送信時にも著作者・ライセンス・元ページの情報を添えます。独自アップロードに必要なクレジットがあれば `credit` に入力してください。

検索語と実際の内容が合うかは採用前に確認してください。Commonsの画像はミーム専用の品揃えではないため、アニメ・ゲームのキャラクターなどは100件揃わない場合があります。保存・再送できる手持ちのGIFはZIP取り込みで補えます。KLIPY・GIPHYの検索結果を自動的に保存する機能は含めていません。

- 1キーワード100件まで（未確認・除外済みも含む）。不要なファイルは `delete` で枠を空けます。
- GIFは1件8MiBまで。破損、過大な解像度・展開サイズ・フレーム数は除外します。添付先サーバーの上限が小さい場合は、その上限も適用します。
- 1回の取り込みは25MiBまで、ZIP内のGIFは100件まで。Discord自身の添付上限がこれより小さい場合はそちらに従います。
- 自動収集は最大300候補・約10分で打ち切り、途中までの保存結果は保持します。収集や取り込みを同時に複数実行しません。
- 同じ実ファイルの重複は除去します。見た目が同じでも再エンコードされた別ファイルは別候補になる場合があります。

参考: [Commonsの再利用ガイド](https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia)、[MediaWiki Imageinfo API](https://www.mediawiki.org/wiki/API:Imageinfo)

### キャラクター名で無関係なGIFが出た場合

CommonsはキャラクターGIFを集める用途に適した取得元ではありません。保存方法を変えても、取得元に欲しいGIFがなければ改善しません。

収集時にはファイル名・説明に検索語がすべて含まれるかを確認し、一致しない候補はダウンロードしません。これは文字の一致であり、キャラクターの視覚判定や翻訳ではありません。同名の別対象もあるため、採用前の確認は必要です。

以前に取り込んだ外れ候補は更新だけでは消えません。

1. `/watch off target:@対象` で送信を停止。
2. `/gif exclude_all keyword:キュレネ` で外れ候補を一括除外。実ファイルは残り、必要なものは `/gif approve gif_id:番号` で復帰できます。
3. 欲しいGIFを `/gif import` で取り込み、`/gif review` で実物を確認して採用。
4. `/watch on target:@対象` で再開。

除外済みも100件の保存枠に含まれるため、枠が足りない場合は `/gif delete` で不要なIDを削除してください。検索語に合うGIFがない場合は0件で終わり、別ジャンルで埋め合わせません。

採用済みだけを解除し、未確認候補をそのまま残したい場合は `/gif unapprove_all keyword:キュレネ` を使います。解除したGIFは「除外」になり、`approve_all` で意図せず再採用されません。復帰は `/gif approve gif_id:番号` で行います。
