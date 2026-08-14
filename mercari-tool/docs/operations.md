# ツール操作マニュアル

> ⬜ **未記入。** ここに日々の運用手順を追記してください。
> コマンドの網羅的なリファレンスは [../README.md](../README.md) にあります。
> こちらには「うちの運用ではこう使う」を書いてください。

## 想定される項目

> ⬜ 記入してください。
>
> - 1日の流れ（朝に何をして、夕方に何をするか）
> - 相場データの集め方と更新頻度
> - 値下げ交渉の判断を誰が行うか
> - コメント返信の運用（自動送信OKのものを誰がいつ送るか）
> - 売上記録のタイミングとスプレッドシート同期の頻度
> - 担当者が複数いる場合の分担

## よく使うコマンド

```bash
# 出品準備
python -m mercari_tool draft <SKU> --photos-dir photos --whiten

# 値下げ交渉が来たとき
python -m mercari_tool price offer <SKU> <提示額>

# コメントへの返信文
python -m mercari_tool comment <SKU> --text "..."

# 売れたとき
python -m mercari_tool sales add --sku <SKU> --price <価格> --sync

# 仕入れたとき
python -m mercari_tool sales buy --sku <SKU> --cost <単価> --qty <数> --sync

# 月次の確認
python -m mercari_tool sales report --mode monthly --by category
```

## 困ったとき

| 症状 | 確認すること |
|---|---|
| 相場データが見つからない | `data/comps/` のファイル名が検索語と一致しているか。ドラフトは「ブランド+商品名+サイズ」→「ブランド+商品名」→「商品名」の順に探します |
| タイトルが商品名のままになる | 商品マスタにブランド・サイズ・キーワードが登録されているか |
| サムネイルの文字が □ になる | 日本語フォントが無い環境です。`MERCARI_FONT_PATH` にフォントのパスを設定してください |
| 背景が白くならない | 背景が均一でないか、被写体が画面端に接しています。`--tolerance` を上げるか、撮り直してください |
| スプレッドシートに反映されない | サービスアカウントのメールアドレスに編集権限を共有しているか |
