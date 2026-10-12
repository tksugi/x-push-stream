# client transaction IDを生成する

`x_client_transaction` はXPushStreamと同じ配布物に含まれる独立パッケージです。
Xの公開HTML・JavaScriptから生成用の入力を取得し、Pythonで `x-client-transaction-id` を生成します。
実行時にNode.jsやブラウザーは不要です。依存はHTTPXとPython標準ライブラリです。

通常のXPushStream利用ではSDKが生成を処理するため、このパッケージを直接呼ぶ必要はありません。
別のHTTPクライアントでIDを使う場合のAPIを説明します。

## 公開資料を取得して生成する

[READMEの手順](../README.md#インストール)でインストールしてから実行します。

```python
import asyncio

from x_client_transaction import TransactionIdGenerator


async def main():
    generator = await TransactionIdGenerator.fetch()
    transaction_id = generator.generate(
        "POST", "/i/api/1.1/notifications/settings/login.json"
    )
    # 呼び出し側のHTTPリクエストのヘッダーにtransaction_idを設定する。


asyncio.run(main())
```

`fetch()` は `https://x.com/i/jf/` と、そこで指定される
`https://abs.twimg.com/responsive-web/client-web/` 配下の公開資料を取得します。
認証情報は送らず、JavaScriptは実行しません。既定では20秒のtimeoutを使い、
リダイレクトを追従せず、環境変数のproxyを継承しません。取得量は4MiBまでです。
`Accept-Encoding: identity` を送り、圧縮応答は本文を読み込む前に拒否します。

`generate()` は通信せず、呼ぶたびに現在時刻と新しいランダムbyteを使います。
HTTPメソッドは大文字、pathは実際のリクエストで書き換えが済んだパスを指定します。
URL全体、query、fragmentは受け付けません。`/i/api` の追加・除去は行いません。

## 入力とHTTPクライアントを指定する

取得済みの対応するHTMLとJavaScriptがある場合は
`TransactionIdGenerator.from_sources(html, javascript)` で準備できます。
照合用に `generate(timestamp=..., random_byte=...)` へ固定値を渡すこともできます。
`timestamp` はUnix秒です。

`fetch(client=...)` にHTTPXの非同期クライアントを渡すと、そのクライアントを使って取得します。
呼び出し側が終了処理を行ってください。認証Cookieを持たない専用クライアントを使います。

取得失敗や未対応の資料構造は `TransactionSourceError` になります。
レスポンス本文は例外文へ含めず、キャンセルは呼び出し側へ伝播します。
生成用の入力はメモリに保持し、ディスク保存や自動更新はしません。
Xの公開資料が変わった場合は `fetch()` で新しい生成器を作ります。

## 検証の範囲

2026年10月7日にXが配信した資料の観測とCSS標準に基づく独立実装です。
合成入力4組と取得した公開資料で、Xの生成処理・ChromiumのCSS計算を使った結果との一致を確認しました。
標準SDK経由では、IDを付けたX登録・通知設定の保存・永続再開も確認しています。

X側がIDを検証する条件全般、ブラウザーごとの計算差、すべての丸め境界、
将来の公開資料での一致は未確認です。通常のテストは通信、Node.js、ブラウザーを使いません。
