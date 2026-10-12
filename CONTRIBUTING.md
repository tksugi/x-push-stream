# 開発への参加

Python SDKが本体です。CLI、フィルター、WebhookはSDKを利用する機能として実装します。
利用方法は[README](README.md)、利用者に影響する変更は[変更履歴](CHANGELOG.md)を参照してください。

## ローカルで確認する

Python 3.10以上とuvを使い、リポジトリのルートで実行します。

```console
uv sync --extra dev
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
uv run --extra dev mypy src
uv run --extra dev pytest
uv build
```

通常のテストは模擬HTTP・WebSocket、合成データ、匿名化した通知を使います。
実際のX認証情報は不要です。`uv build` は `dist/` にsdistとwheelを生成します。

CIの設定はLinux・Windows・macOSのPython 3.10と3.14、およびLinuxの3.11〜3.13を対象とします。
設定の存在とGitHub上での実行成功は別です。
手動のrelease workflowは配布物をartifactとして保存し、GitHub/PyPIへの公開は行いません。

## 変更時に守ること

- RFC、Mozilla AutoPushの公開資料、Xの公開クライアントや実通信の観測に基づき独立して実装します。
  既存のX Web Push OSSをコピー・移植・ラップしません。
- 通信仕様の要件、観測した挙動、このSDKで決めた仕様を区別し、根拠を変更の説明に残します。
  一例の観測から全アカウントや将来の挙動を断定しません。
- 通常利用は `auth_token` だけで起動できる形を保ちます。
  X固有のURL・ヘッダー・登録・通知解釈はX adapterに置きます。
  AutoPushとWeb Push暗号処理にはX固有の処理を入れません。
- 未知の通知はrawを保持します。投稿URLから通知の原因や引用・画像の有無を推測しません。
- 永続化は明示した `client_id` 単位です。購読情報と鍵を一体で保存し、使用中の所有権を保護します。
  認証情報の不一致や破損した状態を自動で上書きしません。
- ライブラリは標準の `logging` を使い、`basicConfig` を呼びません。
  認証情報、秘密鍵、購読識別子、通知本文をログ・例外・reprへ出しません。
- 変更に応じて、プロトコル、暗号の公開vector、未知の通知、再開、購読の無効化、
  認証情報の不一致、排他、再接続、秘密情報を含まない診断を確認します。
  フィルターや投稿詳細取得を変えた場合は、それぞれの条件と失敗時の動作も確認します。
- 利用方法や保証範囲が変わった場合はREADME・利用ガイド・変更履歴を更新します。

## 実通知の調査と不具合報告

実サービスの検証は通常のテストやCIから切り離し、検証者が明示して実行します。
受信データの調査には `examples/capture_notifications.py` が使えます。
引数は `python examples/capture_notifications.py --help` で確認してください。

このスクリプトは実際にXへ登録・通知設定を保存し、既定の `client_id="live-observation"` で
購読を永続化します。受信内容は既定でGit管理外の `private/captures/` に保存し、
画面には件数と接続状況を表示します。終了後も購読情報は残ります。
保存先のアクセス権を制限し、公開用fixtureでは個人情報・認証情報・購読識別子を置き換えます。

不具合報告にはOS、PythonとSDKのversion、再現手順、秘密情報を除いたエラーを記載してください。
token、ct0、Cookie、鍵、購読情報、HTTPの生データ、未加工の通知を添付しないでください。

## 文書と配布の範囲

公開文書はREADME、利用ガイド、client transaction IDの利用案内、変更履歴、この開発案内、LICENSEです。
設計選定、解析の詳細、作業の進捗、個別検証記録、公開作業の手順は公開文書に含めません。
sdistには公開文書、テスト、例を含めます。wheelにはPythonパッケージ、型情報、LICENSEを含めます。
