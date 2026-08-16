# ツール操作マニュアル

> ✏️ このマニュアルは標準の運用フローです。コマンドの網羅的なリファレンスは
> [../README.md](../README.md) にあります。担当分担・件数目標など
> 「うちの運用ではこう使う」の部分は適宜書き換えてください。

## 1日の流れ

**朝（10〜15分）**

1. アプリでコメント・値下げ交渉・購入通知を確認する
2. 値下げ交渉が来ていたら、返す前に判定する
   ```bash
   python -m mercari_tool price offer <SKU> <提示額>
   ```
   判定は「応じる / 逆提案する / 断る」の3択と逆提案額まで出ます。
   **判定より安く応じない**のがルールです（利益率下限を割るため）。
3. コメントには返信文を作らせ、内容を確認して送る
   ```bash
   python -m mercari_tool comment <SKU> --text "（受け取ったコメント）"
   ```
4. 前日に売れたものを記録する
   ```bash
   python -m mercari_tool sales add --sku <SKU> --price <価格> --sync
   ```

**出品作業日（仕入れが届いた日など）**

1. 仕入れ表を取り込み、仕入れを記録する
   ```bash
   python -m mercari_tool product import shiire.csv --dry-run   # 確認
   python -m mercari_tool product import shiire.csv
   python -m mercari_tool sales buy --sku <SKU> --cost <単価> --qty <数> --sync
   ```
2. 商品ごとに、メルカリで相場を見て書き写す（1点あたり1〜2分）。
   検索 →「販売状況: 売り切れ」で絞り込み → 直近の成約価格を10件ほど
   ```bash
   python -m mercari_tool research add --sku <SKU> --prices 9800,11500,8900,10200
   ```
3. 撮影して `photos/<SKU>/` に入れる（→ [撮影マニュアル](photography.md)）
4. まとめてドラフトを生成し、`⚠` の行だけ手当てして再実行
   ```bash
   python -m mercari_tool draft --all --in-stock --photos-root photos --whiten
   ```
5. `listing.txt` を貼って出品する（→ [出品手順](listing.md)）。
   進捗の管理は `data/output/drafts.csv` をスプレッドシートに貼って行う

**週次**

- 売れ行きと在庫の確認
  ```bash
  python -m mercari_tool sales report
  python -m mercari_tool sales report --by category
  ```
- 2週間動かない商品は `price suggest` を見直して値下げを検討する
  （「値下げ下限」より下げない）

**月次**

- 月次推移と仕入れ判断
  ```bash
  python -m mercari_tool sales report --mode monthly
  python -m mercari_tool sourcing rank --price <想定売値>
  ```
- 純利益と現金収支の両方を見ます。仕入れた月は現金収支がマイナスに
  なりますが、純利益がプラスなら赤字ではありません（詳細は README の
  売上管理の章）。
- メルカリの手数料・送料の改定がないかを確認し、あれば `data/fees.json` を
  更新します（`python -m mercari_tool fees` で現在の設定を表示）。

## 運用ルール（初期設定。チームに合わせて変更）

| 項目 | ルール |
|---|---|
| 相場の書き写し | 出品する本人が、出品前に必ず行う（データ無し出品は不可） |
| 値下げ判断 | `price offer` の判定に従う。判定を超える値下げは責任者に確認 |
| コメント返信 | ツールの文面を**必ず読んでから**送る。クレーム対応の文面は責任者が確認 |
| 売上記録 | 売れた当日中。`--sync` でシート反映まで行う |
| 台帳の修正 | スプレッドシートは毎回上書きされるため、修正は必ずツール側で行う |

## よく使うコマンド

```bash
# 仕入れ表からまとめて登録（--dry-run で確認してから）
python -m mercari_tool product import shiire.csv --dry-run
python -m mercari_tool product import shiire.csv

# 相場を書き写す（メルカリで検索して、売れている値段を並べる）
python -m mercari_tool research add --sku <SKU> --prices 9800,11500,8900

# 出品準備
python -m mercari_tool draft <SKU> --photos-dir photos --whiten

# 溜まったぶんをまとめて（photos/<SKU>/ に写真を置いておく）
python -m mercari_tool draft --all --in-stock --photos-root photos --whiten

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
