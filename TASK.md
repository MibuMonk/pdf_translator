# QA Agent

## 職責
translated.json と出力 PDF を検査し、翻訳漏れ・レイアウト問題を報告する。

## 入力
- `--json`   : translated.json パス
- `--pdf`    : 出力 PDF パス（レイアウト検査用）
- `--output` : QA レポート JSON パス（省略時: qa_report.json）
- `--thumbs` : サムネイル出力ディレクトリ（省略時: スキップ）

## 出力 (qa_report.json)
```json
{
  "summary": {
    "total_blocks": 120,
    "translated_blocks": 118,
    "coverage_pct": 98.3,
    "issues": 2
  },
  "issues": [
    {
      "page": 3,
      "block_id": "p03_b005",
      "type": "missing_translation",
      "text": "original text",
      "translated": ""
    }
  ]
}
```

## 完了基準
- 翻訳カバレッジ ≥ 95% であること
- issues リストに全問題が記録されていること
- 完了後 commit: "feat(qa): initial qa agent"
