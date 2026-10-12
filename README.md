# XPushStream

XのWeb Pushを受信・復号し、Pythonの非同期イベントとして扱うSDKです。

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square)](#インストール)
[![MIT License](https://img.shields.io/badge/license-MIT-22863A?style=flat-square)](#ライセンス)
[![Alpha](https://img.shields.io/badge/status-alpha-D97706?style=flat-square)](#確認済みの範囲と配信の限界)

[インストール](#インストール) · [Pythonの例](#pythonで受信する) · [CLI](#cliで受信転送する) · [利用ガイド](https://github.com/tksugi/x-push-stream/blob/HEAD/docs/usage.md)

<a id="インストール"></a>

## インストール

Python **3.10以上**が必要です。PyPI公開後は次のコマンドでインストールできます。

> **公開準備中：** 現在は `0.1.0` の公開候補で、PyPIには未公開です。
> 公開前はソースからインストールしてください。開発版をPyPIから取得する場合は `--pre` を付けます。

```console
python -m pip install xpushstream
```

実行時にXとMozilla AutoPushへ接続し、購読を登録します。
いいね・メンション・リポストの通知設定は、既定ですべてのアカウントから受け取る値へ保存します。
`client_id` を指定すると、鍵を含む購読情報をOSのユーザー用ディレクトリへ保存します。

<details>
<summary>ソースからのインストールとアンインストール</summary>

取得したリポジトリのルートで実行します。

```console
python -m pip install .
```

アンインストールする場合：

```console
python -m pip uninstall xpushstream
```

アンインストールでは保存済みの購読情報やX側の通知設定は削除されません。
保存先と削除方法は[利用ガイド](https://github.com/tksugi/x-push-stream/blob/HEAD/docs/usage.md#永続化と削除)を参照してください。

</details>

<a id="pythonで受信する"></a>

## Pythonで受信する

XのセッションCookieの `auth_token` を環境変数 `XPUSHSTREAM_AUTH_TOKEN` に設定して実行します。
SDKやCLIは `.env` ファイルを自動では読み込みません。

```python
import asyncio
import os

from xpushstream import XPushClient


async def main():
    async with XPushClient(
        auth_token=os.environ["XPUSHSTREAM_AUTH_TOKEN"],
    ) as client:
        async for event in client:
            print(event.to_dict(include_raw=False))


asyncio.run(main())
```

必要な認証情報は `auth_token` だけです。SDKがct0取得、鍵生成、登録、復号、再接続を処理します。
`async with` に入った時点で接続と通知設定の保存が完了し、上の例では通知内容を標準出力に表示します。

**条件を付けて受信する：** `main()` の受信ループを次のように置き換えます。
条件を続けて指定するとANDで評価します。

```python
async for event in client.stream().event_types(["reply", "mention"]).contains("障害"):
    print(event.to_dict(include_raw=False))
```

**再起動後も同じ購読を使う：** `XPushClient` に `client_id="notification-bot"` のような固定のIDを指定します。
省略時はディスクへ保存せず、終了時に購読解除を試みます。一つのclientを同時に読み出せる処理は一つです。

<a id="cliで受信転送する"></a>

## CLIで受信・転送する

CLIも `XPUSHSTREAM_AUTH_TOKEN` を使います。
`listen` はrawを除いたJSONを一行ずつ出力し、`webhook` は指定したHTTPS URLへPOSTします。

```console
xpushstream listen --client-id notification-bot --event-type reply --event-type mention --contains 障害
xpushstream webhook --client-id relay --url https://example.com/events
```

認証情報なしでコマンド一覧を確認できます。次は実際のヘルプ出力です。

```console
$ xpushstream --help
usage: xpushstream [-h] {listen,webhook,status,reset} ...

positional arguments:
  {listen,webhook,status,reset}

options:
  -h, --help            show this help message and exit
```

<a id="確認済みの範囲と配信の限界"></a>

## 確認済みの範囲と配信の限界

通知種別はXの `data.type` を `event.type` にそのまま保持します。
標準のイベント型はすべて `UnknownNotification` です。本文・URL・投稿IDは取得できる場合だけ入り、
元のJSONには `event.raw` からアクセスできます。投稿全文や投稿者情報の取得APIは付属しません。

SDKはメモリ上のキューへ通知を格納した時点で受信確認を返します。
その後のプロセス停止やWebhook送信の失敗で通知を失う場合があります。
処理完了までの永続保存、重複のない配信、全投稿の取得は保証しません。

Xの通知設定や内部APIの変更により、登録や配信が成立しない場合があります。
同じXセッションの複数購読への配信先、停止中の通知の回収、全通知種別の挙動は未確認です。
開発版のAPIは変更される可能性があります。

公開資料取得と登録・設定APIでは、メモリ使用量を制限するため圧縮応答を拒否します。
この制限の追加後はローカルで検証しており、実X通信での再確認は未実施です。

<details>
<summary>実装と検証の範囲</summary>

ローカルの模擬サービスで、受信・復号・再接続・永続化・フィルター・投稿詳細取得の
失敗時の処理・Webhook・CLIを検証しています。

2026年10月7日のWindows環境では、実サービスで登録、通知設定の保存と読み戻し、
別プロセスでの永続再開、自動再接続、障害を挿入した際の復旧を確認しました。
通知の受信・復号は新規投稿、返信、引用、画像付き投稿、メンション、いいね、
リポスト、フォローで観測しています。最新の制御経路の検証では新たな通知受信はありません。

</details>

<a id="詳しい使い方と開発"></a>

## 詳しい使い方と開発

| 文書 | 内容 |
|---|---|
| [利用ガイド](https://github.com/tksugi/x-push-stream/blob/HEAD/docs/usage.md) | 通知設定、永続化、フィルター、投稿詳細、Webhook、エラー対応 |
| [client transaction ID生成](https://github.com/tksugi/x-push-stream/blob/HEAD/docs/client-transaction.md) | 同梱の独立パッケージの使い方 |
| [変更履歴](https://github.com/tksugi/x-push-stream/blob/HEAD/CHANGELOG.md) | 追加機能と開発版からの移行 |
| [開発への参加](https://github.com/tksugi/x-push-stream/blob/HEAD/CONTRIBUTING.md) | 品質チェックと変更時のルール |

<a id="ライセンス"></a>

## ライセンス

[MIT License](https://github.com/tksugi/x-push-stream/blob/HEAD/LICENSE)の独立実装です。
実行時の依存はHTTPX、websockets、cryptography、platformdirsで、それぞれのライセンスが適用されます。
X Corp.とは関係ありません。
