# Translate Agent

## 職責
parsed.json の各ブロックの text フィールドを翻訳し、translated フィールドを追加した
translated.json を出力する。翻訳キャッシュで API コストを節約する。

## 入力
- `--input`  : parsed.json パス
- `--output` : 出力 translated.json パス（省略時: <stem>.translated.json）
- `--cache`  : 翻訳キャッシュ .json パス（省略時: <PDF stem>.ja.transcache.json）
- `--context`: 術語・背景知識ファイルパス（省略可）
- `--batch`  : 一バッチあたりの最大ブロック数（デフォルト: 40）

## 出力 (translated.json)
parsed.json と同一スキーマ。各 block に `"translated": "..."` フィールドを追加。

## 完了基準
- 全ブロックの translated フィールドが非空
- キャッシュに新規翻訳を書き込み済み
- 完了後 commit: "feat(translate): initial translate agent"
