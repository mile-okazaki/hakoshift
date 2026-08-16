# サンプルデータ

## comps-sample.csv — 相場データの書式サンプル

> 💡 CSVを手作りしなくても、`research add` に価格を打ち込む方が簡単です:
> `python -m mercari_tool research add "ナイキ エアマックス 90 27cm" --prices 9800,11500,8900`
> このCSVは、Excelでまとめて管理したい場合の書式サンプルです。

`data/comps/<検索語をハイフンでつないだ名前>.csv` として置くと
`research` / `price` / `draft` から自動で読まれます。

```bash
mkdir -p data/comps
cp examples/comps-sample.csv "data/comps/ナイキ-エアマックス-90-27cm.csv"
python -m mercari_tool research "ナイキ エアマックス 90 27cm"
```

列はすべて省略可能（`価格` のみ必須）。英語ヘッダー
（`title,price,sold,condition,sold_at,listed_at,url`）でも読めます。

**`売却` 列が空欄の行は「出品中」**として扱われ、成約価格の統計には入りません。

## comments-sample.txt — コメント一括処理の入力例

```bash
python -m mercari_tool comment NK-AM90-27 --file examples/comments-sample.txt
```

## sales-import-sample.csv — 売上の一括取り込み

```bash
python -m mercari_tool sales import examples/sales-import-sample.csv
```
