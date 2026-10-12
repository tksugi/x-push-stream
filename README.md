<h1 align="center">XPushStream</h1>

<p align="center">
  <strong>X (旧 Twitter) の新着投稿・いいね・フォロー通知を、Pythonでリアルタイムに受信。</strong><br>
  Python 3.10+ ｜ asyncio / async for ｜ CLI・Webhook対応
</p>

<p align="center">
  <a href="#インストール"><img src="https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-22863A?style=flat-square" alt="MIT License"></a>
  <a href="#確認済みの範囲と配信の限界"><img src="https://img.shields.io/badge/status-alpha-D97706?style=flat-square" alt="Alpha"></a>
</p>

<p align="center">
  <strong>日本語</strong> · <a href="README.en.md">English</a>
</p>

<p align="center">
  <a href="#クイックスタート">クイックスタート</a> ·
  <a href="#使い方">使い方</a> ·
  <a href="https://github.com/tksugi/x-push-stream/blob/HEAD/docs/usage.md">利用ガイド</a> ·
  <a href="CONTRIBUTING.md">開発への参加</a>
</p>

---

## 概要

XPushStreamは、X (旧 Twitter) で通知をオンにしているアカウントの新着投稿や、自分へのいいね・フォローなどの通知をリアルタイムで受信するPython SDKです。
XのWeb Push通知を受信・復号し、Pythonの非同期イベントストリームとして届けます。
通知を `async for` で読み出し、自分のアプリケーションで処理したり、Webhookへ転送したりできます。
認証にはXのセッションCookieの `auth_token` を使います。接続・鍵生成・登録・復号・再接続はSDKが処理します。

## 主な機能

- **非同期で通知を受信**：`async with` と `async for` で、届いたイベントを順に処理。
- **条件で絞り込み**：通知種別・本文・正規表現・投稿者・独自条件でイベントを選別。
- **購読の再利用**：`client_id` を指定すると、購読情報と鍵を保存して再起動後も利用。
- **CLI・Webhook**：通知をJSONで出力したり、HTTPSの送信先へ転送。
- **元の通知を保持**：未知の種別も受け取り、`event.raw` から元のJSONを参照。

---

<a id="クイックスタート"></a>

## クイックスタート

### 1. インストール

<a id="インストール"></a>

Python **3.10以上**が必要です。PyPIからインストールします。

```console
python -m pip install xpushstream
```

<a id="pythonで受信する"></a>

### 2. Pythonで受信する

XのセッションCookieの `auth_token` を `YOUR_AUTH_TOKEN` に入れ、次のコードを `receive.py` として保存します。

> [!IMPORTANT]
> 標準設定では、起動時にいいね・メンション・リポストの通知設定をXへ保存し、すべてのアカウントから受け取る値に変更します。
> 不要な種類は `notify_likes=False`・`notify_mentions=False`・`notify_reposts=False` で無効にできます。終了時に以前の設定へ戻す処理はありません。

```python
import asyncio

from xpushstream import XPushClient


async def main():
    async with XPushClient(auth_token="YOUR_AUTH_TOKEN") as client:
        async for event in client:
            print(event.to_dict(include_raw=False))


asyncio.run(main())
```

同じターミナルで実行します。

```console
python receive.py
```

`async with` に入ると接続と通知設定の保存が完了し、届いた通知を順に読み出せます。
この例は元のJSONを除いた通知内容を標準出力に表示します。本文などの個人情報は含まれます。

---

<a id="使い方"></a>

## 使い方

### 条件に合う通知だけを読む

上の受信ループを置き換えると、返信・メンションのうち「障害」を含む通知だけを読み出せます。
種別のリストはOR、続けて指定した条件はANDで評価します。

```python
async for event in client.stream().event_types(["reply", "mention"]).contains("障害"):
    print(event.to_dict(include_raw=False))
```

フィルターは受信後の選別です。X側の通知設定は変更しません。
正規表現や投稿者による選別、独自条件は[利用ガイド](https://github.com/tksugi/x-push-stream/blob/HEAD/docs/usage.md#フィルター)を参照してください。

### 再起動後も同じ購読を使う

`XPushClient` に `client_id="notification-bot"` のような固定のIDを指定すると、購読情報と鍵をOSのユーザー用ディレクトリへ保存し、次回の起動で再利用します。
省略時は毎回独立した購読を作り、ディスクへ保存せず、終了時に購読解除を試みます。

同じIDの同時利用は拒否します。別の `auth_token` で保存済みのIDを使うと、`PersistentClientCredentialMismatch` になります。
`auth_token` とct0の平文は保存しません。保存先と削除手順は[永続化と削除](https://github.com/tksugi/x-push-stream/blob/HEAD/docs/usage.md#永続化と削除)を参照してください。
一つのclientを同時に読み出せる処理は一つです。

<a id="cliで受信転送する"></a>

## CLIで受信・転送する

CLIを使う場合は、環境変数 `XPUSHSTREAM_AUTH_TOKEN` にXの `auth_token` を設定します。
CLIは `.env` ファイルを自動では読み込みません。
`listen` は元のJSONを除いたイベントを一行ずつ出力し、`webhook` は指定したHTTPS URLへPOSTします。
WebhookのURLは自分の送信先に置き換えてください。

```console
xpushstream listen --client-id notification-bot --event-type reply --event-type mention --contains 障害
xpushstream webhook --client-id relay --url https://example.com/events
```

認証情報なしでも、コマンドの確認とローカルの保存状態の管理ができます。
`status` と `reset` は対象のclientを停止してから実行します。

```console
xpushstream --help
xpushstream status --client-id notification-bot
xpushstream reset --client-id notification-bot
```

`reset` はローカルの購読情報を削除します。X側のdevice削除やAutoPushへの購読解除は行いません。
Pythonからの転送、条件別の送信先、再試行の仕様は[Webhookの利用ガイド](https://github.com/tksugi/x-push-stream/blob/HEAD/docs/usage.md#webhookへ転送する)に記載しています。

## イベントには元の通知を保持する

標準のイベント型は `UnknownNotification` です。Xの `data.type` を `event.type` にそのまま保持し、SDK独自の通知種別は推測しません。
本文・URL・投稿IDは取得できる場合だけ入り、元のJSONには `event.raw` からアクセスできます。

| 観測した操作・通知 | `event.type` |
|---|---|
| 新規投稿・引用・画像付き投稿 | `tweet` |
| 返信 / メンション | `reply` / `mention` |
| いいね / リポスト | `favorite` / `retweet` |
| フォロー | `follower` |

未知の種別も保持します。引用や画像の有無は `type` だけでは区別できません。
投稿全文や投稿者情報の取得APIは同梱していません。自分の取得処理を `TweetEnrichmentProvider` として接続すると、イベントに投稿詳細を追加できます。取得に失敗した場合は元のイベントを返します。

<a id="確認済みの範囲と配信の限界"></a>

## 確認済みの範囲と配信の限界

SDKはメモリ上のキューへ通知を格納した時点で受信確認を返します。
**処理完了までの永続保存や、重複のない配信は保証しません。** その後のプロセス停止やWebhook送信の失敗で通知を失う場合があります。

Xの通知設定や内部APIの変更により、登録や配信が成立しない場合があります。
全投稿の取得、停止中の通知の回収、同じXセッションの複数購読への配信先、全通知種別の挙動は保証・確認していません。
開発版のAPIは変更される可能性があります。

<details>
<summary>ローカル検証と実サービスでの観測</summary>

ローカルの模擬サービスで、受信・復号・再接続・永続化・フィルター・投稿詳細取得の失敗時の処理・Webhook・CLIを検証しています。

2026年10月7日のWindows環境では、実サービスで登録、通知設定の保存と読み戻し、別プロセスでの永続再開、自動再接続、障害を挿入した際の復旧を確認しました。
通知の受信・復号は新規投稿、返信、引用、画像付き投稿、メンション、いいね、リポスト、フォローで観測しています。最新の制御経路の検証では新たな通知受信はありません。

2026年10月13日に、公開資料取得とX登録・設定APIの圧縮応答を拒否する制限を追加しました。
この変更後はローカルで検証しており、実X通信での再確認は未実施です。Docker実行も未検証です。

</details>

<a id="詳しい使い方と開発"></a>

## 詳しい使い方と開発

| 文書 | 内容 |
|---|---|
| [利用ガイド](https://github.com/tksugi/x-push-stream/blob/HEAD/docs/usage.md) | 通知設定、永続化、フィルター、投稿詳細、Webhook、エラー対応 |
| [client transaction ID生成](https://github.com/tksugi/x-push-stream/blob/HEAD/docs/client-transaction.md) | 同梱の独立パッケージの使い方 |
| [変更履歴](CHANGELOG.md) | 追加機能と開発版からの移行 |
| [開発への参加](CONTRIBUTING.md) | 開発環境、品質チェック、独立実装のルール、不具合報告 |

不具合報告や改善提案は[Issues](https://github.com/tksugi/x-push-stream/issues)へお願いします。
OS、PythonとSDKのバージョン、再現手順を添えてください。認証情報・鍵・購読情報・未加工の通知は添付しないでください。

<details>
<summary>アンインストール</summary>

```console
python -m pip uninstall xpushstream
```

アンインストールでは、保存済みの購読情報やX側の通知設定は削除されません。
ローカルの購読情報も削除する場合は、アンインストール前にclientを停止して `xpushstream reset --client-id <ID>` を実行してください。

</details>

<a id="ライセンス"></a>

## ライセンス

[MIT License](LICENSE)。RFC、Mozilla AutoPushの公開資料、Xの公開クライアントと通信の観測に基づく独立実装です。
実行時の依存はHTTPX、websockets、cryptography、platformdirsで、それぞれのライセンスが適用されます。
X Corp.とは関係ありません。
