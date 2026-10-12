# 利用ガイド

[README](../README.md)のインストールと受信例を試した後、必要な項目を参照してください。
以下のPython例は、READMEの `async def main()` 内で使います。
`TOKEN` は環境変数から読み込んだ `auth_token` を表します。

## 通知設定

標準clientは起動時に、いいね・メンション・リポストの設定をXへ保存します。
3項目とも既定値は `True` で、すべてのアカウントから受け取る設定です。
不要な種類は `False` に指定します。メンションの設定には返信も含まれます。

```python
async with XPushClient(
    auth_token=TOKEN,
    notify_likes=False,
    notify_mentions=True,
    notify_reposts=False,
) as client:
    async for event in client:
        ...
```

設定の取得・保存が失敗すると起動エラーになります。途中まで保存された場合は、
次回起動時に指定した値を再適用します。同じ購読への自動再接続では保存を繰り返しません。
SDK停止時には、Xの通知設定を以前の値へ戻す処理は行いません。

この設定はXへの配信設定です。`client.stream().event_types(...)` は受信後の選別に使います。

## 永続化と削除

`client_id` を省略すると、毎回独立した購読を作り、終了時にAutoPushの購読解除を試みます。
固定のIDを指定すると、再起動後も保存済みの購読を使います。

```python
async with XPushClient(auth_token=TOKEN, client_id="notification-bot") as client:
    async for event in client:
        ...
```

| OS | 標準の保存先 |
|---|---|
| Linux | `~/.local/state/xpushstream/`。`XDG_STATE_HOME` 指定時はその配下 |
| macOS | `~/Library/Application Support/XPushStream/` |
| Windows | `%LOCALAPPDATA%\XPushStream\` |

購読の識別子、endpoint、秘密鍵・公開鍵、認証用の秘密値、X登録の完了状態を一体で保存します。
`auth_token` とct0の平文は保存しません。保存状態はPOSIXのアクセス権またはWindowsの
所有者限定DACLで保護しますが、秘密鍵を含むため公開・共有しないでください。

保存済みの購読が有効ならXへの再登録を省略し、通知設定は起動時に保存し直します。
AutoPushが別のUAID（接続の識別子）を返した場合は、新しい鍵と購読で登録し直します。
同じIDの同時利用は拒否します。異なるIDは独立して起動できますが、
同じXセッションの複数購読への配信先は未確認です。

保存状態を確認・削除するには、clientを停止してから実行します。認証情報は不要です。

```console
xpushstream status --client-id notification-bot
xpushstream reset --client-id notification-bot
```

`status` は保存状態の有無とX登録完了のフラグを表示し、Xとの疎通は確認しません。
`reset` はローカルの購読情報を削除します。X側のdevice削除やAutoPushへの購読解除は行いません。
削除後の起動では新規登録になります。使用中のIDは確認・削除できません。
OSのロック用の空ファイルは残る場合があります。

以前と異なるtokenで同じIDを使うと `PersistentClientCredentialMismatch` になります。
別のIDを使うか、停止後に古い状態を明示して削除してください。

## イベントの内容

イベントは `XPushEvent` のdataclassで、標準の正規化処理は `UnknownNotification` を返します。
この型は通知の意味をSDKで分類していないことを表し、`type` の有無とは別です。

| 項目 | 内容 |
|---|---|
| `received_at` | SDKでの受信時刻。投稿時刻ではない |
| `type` | Xの `data.type` の文字列。欠落・文字列以外は `None` |
| `title`・`body` | 取得できた通知のタイトル・本文 |
| `url`・`tweet_id` | 取得できたXのURL・投稿ID |
| `raw` | 元のJSON |
| `tweet` | providerから取得した投稿詳細。未取得なら `None` |

`event.to_dict()` はrawも含めて辞書へ変換します。
`include_raw=False` でrawを除けますが、通知本文などは含まれます。

2026年10月7日の通知観測では、次のtypeを確認しました。

| 操作・通知 | type |
|---|---|
| 新規投稿・引用・画像付き投稿 | `tweet` |
| 返信 | `reply` |
| メンション | `mention` |
| いいね | `favorite` |
| リポスト | `retweet` |
| フォロー | `follower` |

`follow_request_accepted` と `mr_infrequent_poster` も受信しましたが、操作との対応は未確認です。
未知の文字列も保持します。引用・画像付き投稿はtypeだけでは区別できません。

## フィルター

指定した順に条件を評価し、すべてに一致したイベントを返します。
種別や本文の条件を先に置くと、対象外の通知で後の条件を評価せずに済みます。

```python
stream = (
    client.stream()
    .event_types(["reply", "mention"])
    .contains("障害")
    .exclude("復旧")
    .regex(r"API|DB")
)
async for event in stream:
    ...
```

| メソッド | 条件 |
|---|---|
| `event_types(types)` | `event.type` と完全一致。複数指定はOR |
| `contains(keyword)`・`exclude(keyword)` | タイトル・本文・取得済み投稿本文で、大文字・小文字を区別した部分一致 |
| `regex(pattern, flags=0)` | 同じテキストに対する正規表現 |
| `users(user_ids)` | 投稿詳細の投稿者IDと一致 |
| `usernames(usernames)` | 投稿詳細のユーザー名と一致。指定値の先頭 `@` と大文字・小文字を無視 |
| `exclude_replies()` | typeが `reply`、または投稿詳細の `is_reply` が `True` の通知を除外 |
| `exclude_reposts()` | typeが `retweet`、または投稿詳細の `is_repost` が `True` の通知を除外 |
| `exclude_quotes()` | 投稿詳細の `is_quote` が `True` の通知を除外 |
| `has_media()` | 投稿詳細にメディアがある通知だけ選択 |
| `where(predicate)` | 自分で定義した条件 |

投稿者・メディアの選択には投稿詳細が必要です。未取得の通知は選択されません。
除外系のフラグが不明な場合は通知を残します。
フィルターは投稿詳細の取得後に評価され、providerへの通信を減らす機能ではありません。

`EventFilter` ではAND・OR・NOTを組み合わせられます。

```python
from xpushstream.filters import EventFilter

condition = EventFilter.contains("障害") | EventFilter.contains("メンテナンス")
async for event in client.stream().where(condition & ~EventFilter.contains("訓練")):
    ...
```

## 投稿詳細を追加する

`TweetEnrichmentProvider.fetch(tweet_id)` を実装し、`TweetDetails` を返すproviderを指定します。
投稿詳細の取得APIは同梱していません。次は利用者の既存APIへ接続する例で、
`my_existing_api` を実際のAPIクライアントに置き換えて使います。

```python
from xpushstream import TweetDetails


class MyProvider:
    async def fetch(self, tweet_id: str) -> TweetDetails:
        details = await my_existing_api.fetch_tweet(tweet_id)
        return TweetDetails(
            tweet_id=tweet_id,
            author_id=details.author_id,
            username=details.username,
            text=details.text,
        )


async with XPushClient(
    auth_token=TOKEN,
    enrichment_provider=MyProvider(),
) as client:
    async for event in client.stream().users(["123"]):
        ...
```

providerを渡すと取得が有効になります。`enrich_tweets=True` を指定する場合もproviderが必要です。
投稿IDがない通知では取得しません。10秒のtimeout、取得失敗、投稿IDの不一致では元のイベントを返し、
キャンセルは伝播します。providerの接続や終了処理は呼び出し側が管理します。

## Webhookへ転送する

条件に一致したすべての送信先へ、イベントのJSONをHTTPS POSTします。rawは既定で除きます。
認証情報を埋め込んだURLやfragmentは受け付けません。

```python
from xpushstream.filters import EventFilter
from xpushstream.webhook import Route, WebhookDestination, WebhookRouter

router = WebhookRouter(
    [
        Route(
            EventFilter.contains("障害"),
            WebhookDestination("https://example.com/events", retries=3),
        ),
    ]
)
async with XPushClient(auth_token=TOKEN, client_id="relay") as client:
    await router.run(client)
```

既定では10秒のtimeoutと、初回送信に加えて最大3回の再試行を使います。
通信失敗、HTTP 408・425・429・5xxを再試行し、`Retry-After` に従います。
リダイレクトを追従せず、環境変数のproxyを継承しません。
再試行の終了やそれ以外のエラー応答は `WebhookDeliveryError` になります。
同じイベントのほかの送信先への試行後、`router.run()` に例外が伝わります。
永続的な送信待ちキューはありません。

CLIの `listen` と `webhook` でも `--event-type` を複数指定できます。
指定値のいずれかに一致する通知を、`--contains` の本文条件より先に選びます。
省略時は未知のtypeも含めて全種別を受け取ります。

## 接続の設定とエラー対応

標準clientの `proxy="http://localhost:8080"` はHTTPとWebSocket通信に適用されます。
環境変数のproxyは継承しません。メモリ上のキューの上限は `queue_size=256` です。
同じプロセス内では直近4096件の購読IDと通知versionの組み合わせで重複を抑えます。

再接続の待機時間は `reconnect_base_delay=1`、上限は `reconnect_max_delay=60` 秒です。
サーバーの待機指示があれば従います。`max_reconnect_attempts=None` では回数を制限しません。
認証不備や保存状態の問題を無限に再試行する方式ではありません。

`client.status` は接続状態、キュー内の件数、受信処理件数、復号・処理失敗件数、再接続回数を返します。
CLIの `status` は前述のとおりディスク上の状態だけを確認します。

例外は `xpushstream.exceptions` からimportできます。SDKの基本例外は `XPushError` です。

| エラー | 対応 |
|---|---|
| `Ct0AcquisitionError` | セッションと通信経路を確認。HTTP 200の応答Cookieにct0が必要 |
| `RegistrationError` | Xの登録・設定保存が失敗。HTTP 429も自動再試行せず起動エラーになる |
| `PersistentClientInUseError` | 同じIDを使うプロセスを停止、または別のIDを使用 |
| `PersistentClientCredentialMismatch` | 新しいIDを使用、または停止後に古い状態を明示して削除 |
| `StateError` | 保存状態やアクセス権を確認。破損を自動で初期化しない |
| `WebhookDeliveryError` | 送信先と処理方針を確認。送信失敗した通知の自動的な永続再送はない |

ct0は `https://x.com/i/release_notes` へのGETの応答Cookieからのみ取得します。
リダイレクトを追従しません。Xの公開資料から生成したclient transaction IDを標準のX通信に付けます。
任意の追加ヘッダーをX登録へ渡す引数はありません。

公開資料の取得とX登録・設定APIでは `Accept-Encoding: identity` を送り、
圧縮応答は本文を読み込む前に拒否します。本文の上限は公開資料で4MiB、
登録・設定APIで1MiBです。SDK経由の公開資料取得も通信ログの秘匿対象です。

## 保存・認証・登録を拡張する

`xpushstream.auth.Ct0Provider` を `ct0_provider` に渡すと、ct0の取得を置き換えられます。
`acquire(auth_token)` を非同期で実装してct0を文字列で返し、通信リソースと秘密情報の扱いを管理してください。
標準の `ReleaseNotesCt0Provider` は取得時のヘッダーとproxyを設定できます。

`xpushstream.state.StateStore` を `state_store` に渡す場合は `client_id` も必要です。
`claim(client_id)` を非同期コンテキストマネージャーとして実装し、その中で
`load`・`save`・`delete` を使います。ID単位の排他、購読情報と鍵の一体保存、
アトミックな更新、アクセス制限を実装側で保証します。store自体をSDKが終了することはありません。

`xpushstream.x_adapter.XPushRegistrar(locale="ja")` を `registrar` に渡すと、
登録JSONと `client-language` のlocaleを変更できます。既定は `en` です。
独自registrarを使う際の通知設定はregistrar側に指定してください。
`transport_factory` が返す接続はSDKが終了処理を行います。

