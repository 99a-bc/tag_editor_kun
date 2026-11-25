# Danbooru補完辞書の配置

このリポジトリには、タグ入力補完に使うTagComplete互換のCSVを次の場所に同梱しています。

```text
resources/dictionaries/danbooru.csv
```

取得元は `DominikDoom/a1111-sd-webui-tagcomplete` の `tags/danbooru.csv` です。出典とライセンスは [THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md) に記載しています。

## このアプリでの扱い

- CSVは起動時に一度だけ読みます。配置・差し替え後はアプリを再起動してください。
- アプリからネットワーク取得や自動更新はしません。
- CSVが無い場合もタグ編集は使えます。追加・置換ダイアログの補完だけ無効になります。
- タグ表記はアプリ内では空白区切りへ変換して扱います。
- aliasは候補検索に使い、候補を選んだ時だけ正規タグを入力します。

## 配布時の注意

CSVを差し替えて配布する場合は、取得元・取得した版・必要なライセンス表記を同梱の第三者ライセンス文書に記載してください。
