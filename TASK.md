# Parse Agent

## 職責
PDF を読み込み、全翻訳対象テキストブロックを構造化 JSON として出力する。

## 入力
- `--input`  : 元 PDF ファイルパス
- `--output` : 出力 parsed.json パス（省略時: <入力stem>.parsed.json）
- `--src`    : 原文言語コード（デフォルト: en）
- `--tgt`    : 翻訳先言語コード（デフォルト: ja）
- `--pages`  : ページ指定（例: "1,3,5-8"、省略時: 全ページ）

## 出力 (parsed.json)
```json
{
  "version": "1.0",
  "input_pdf": "/abs/path/to/file.pdf",
  "source_lang": "en",
  "target_lang": "ja",
  "pages": [{
    "page_num": 1,
    "width": 960.0,
    "height": 540.0,
    "blocks": [{
      "id": "p01_b000",
      "text": "original text",
      "font_size": 14.0,
      "color": [0.0, 0.0, 0.0],
      "align": 0,
      "bbox": [x0, y0, x1, y1],
      "redact_bboxes": [[x0, y0, x1, y1]],
      "stream_rank": 5
    }],
    "image_obstacles": [[x0, y0, x1, y1]]
  }]
}
```

## 完了基準
- 全ページの翻訳対象ブロックが抽出されている
- 水印・フッター・回転文字は除外済み
- 散乱ブロック（複数列）は列ごとに分割済み
- 縦隣接・同一列のブロックはマージ済み
- 完了後 commit: "feat(parse): initial parse agent"
