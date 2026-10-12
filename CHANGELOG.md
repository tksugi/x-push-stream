# 変更履歴

## 0.1.0 — 2026-10-13

初回リリースです。X (旧 Twitter) の新着投稿・いいね・フォローなどの通知を、Pythonでリアルタイムに受信できます。

### 追加した機能

- `XPushClient` の非同期コンテキストマネージャーと非同期イテレーター。
  ct0取得、X登録、AutoPush接続、Web Push復号、受信確認、自動再接続。
- `client_id` を指定した永続化、ID単位の排他制御、アトミックな保存、
  認証情報の不一致検出、購読情報が無効になった場合の再登録。
- `UnknownNotification` とraw保持、X由来の `event.type`、本文・URL・投稿IDの抽出。
- 合成可能なフィルター、利用者のproviderによる投稿詳細取得と失敗時の元イベント保持。
- HTTPS Webhookへの条件別転送と回数制限付き再試行。
  `listen`・`webhook`・`status`・`reset` のCLI。
- `notify_likes`・`notify_mentions`・`notify_reposts` による通知設定。
  起動時に3項目を保存し、永続再開時も指定値を適用。
- 独立パッケージ `x_client_transaction` によるPythonでのID生成。
  標準SDKのct0取得とX設定APIの各リクエストに個別のIDを付与。
- 型情報、CI設定、タグのpushでPyPIへ公開するworkflow、公開用の利用・開発文書。

### 開発版からの移行

- SDK起動時の公開資料取得にも通信ログの秘匿処理を適用。
- 公開資料とX登録・設定APIで非圧縮応答を要求し、圧縮応答は読み込み前に拒否。
  サイズ制限の判定前に圧縮データが大きく展開される問題を修正。

- `event.kind` は `event.type` に変更。JSONの `kind` も削除。
  `event_types(kinds=...)` は `event_types(types=...)` に変更し、Xの通知種別を指定。
  未分類の通知は `isinstance(event, UnknownNotification)` で判定。
- `*_setting="anyone"/"off"` は `notify_*=True/False` に変更。
  3項目とも既定値は `True`。

検証済みの範囲と配信の限界は[README](README.md#確認済みの範囲と配信の限界)を参照してください。
今後の公開APIや保存形式の変更、必要な移行手順はこのファイルに記録します。
