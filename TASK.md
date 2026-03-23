# Layout Agent

## 職責
translated.json と元 PDF を受け取り、原文を redact して訳文を再レンダリングした
最終 PDF を出力する。

## 入力
- `--input`  : 元 PDF ファイルパス
- `--json`   : translated.json パス
- `--output` : 出力 PDF パス（省略時: <stem>.ja.pdf）
- `--font`   : CJK フォントファイルパス（省略時: システムから自動検出）
- `--pages`  : ページ指定（例: "1,3,5-8"、省略時: json に含まれる全ページ）

## 出力
翻訳済み PDF

## 完了基準
- 全ブロックの原文が redact されている
- 訳文が適切なフォントサイズで再挿入されている
- 完了後 commit: "feat(layout): initial layout agent"
