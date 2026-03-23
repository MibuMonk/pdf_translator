# Test Agent

QA 检查，输出 test_report.json。

## 检查项

1. **coverage_check**：翻译覆盖率 + unchanged_translation 检测
2. **quality_check**：汇总 coverage 的 issue 判 pass/fail
3. **style_check**：调用 LLM 检查文档级语言风格一致性
   - 语气一致性（敬体/常体、您/你）
   - 术语翻译一致性
   - 句尾风格统一性
4. **translation_completeness_check**：翻译完整性检测（不依赖 LLM）
   - `untranslated_content`：translated == text 且 len > 10 且含空格（英文句子），排除产品名/缩写（纯大写、无空格、<15字符），severity=error
   - `low_translation_ratio`：每页翻译比例 < 50%，severity=error
5. **readability_check**：渲染可读性检测（不依赖 LLM，需要 output.pdf）
   - `text_too_small`：渲染后 font_size < 8pt，severity=warning
   - `content_truncated`：translated 文本量远超 bbox 容量（用 bbox 面积 / font_size^2 估算）
     - bbox 面积 < 500px²：跳过检测（图表标注等极小块，截断不可避免）
     - bbox 面积 < 2000px²：始终 severity=warning（源文件固有空间限制）
     - bbox 面积 >= 2000px²：ratio > 3.0 → error，2.0–3.0 → warning
   - `inconsistent_sizing`：两页 block[0] 文本相似度 > 80% 但 font_size 差异 > 30%，severity=warning
6. **regression_check**：与 baseline 对比的回归检测

## 回归测试

### Baseline 管理

保存当前输出为回归基准：
```
python test_agent.py --testcase 成果物4 --save-baseline
```

保存内容（存放在 `testdata/{name}/baseline/`）：
- `block_summary.json`：parsed.json 的 block 摘要（每页 block 数、每个 block 的 id/bbox/color/font_size/text 前 20 字符）
- `translated_summary.json`：translated.json 的关键信息（id/translated 前 30 字符/font_size）
- `thumbnails/`：output.pdf 每页的 80 DPI 缩略图 PNG
- `metadata.json`：时间戳、源文件路径

### 回归检测项

在 `--testcase` 模式下，如果 baseline 存在则自动运行：

| 检查 | 说明 | 严重度 |
|------|------|--------|
| block_count | block 总数偏差 >10% | error |
| block_count_per_page | 各页 block 数变化 | warning |
| title_preservation | baseline 中的标题 block（font_size>=20 且页面上部）在新输出中是否存在 | error |
| color_consistency | 每个 block 的颜色与 baseline 对比 | <=5 个变化 warning, >5 个 error |
| bbox_coverage | baseline 中有文本的 block 在新输出中是否存在 | >=5 个缺失 error |
| visual_diff | 每页缩略图像素 MSE 对比（阈值 150） | warning |

结果写入 test_report.json 的 `issue_results.regression_check`。

## 已知问题

- `unchanged_translation` 误报率高：纯 ASCII 产品名/缩写保持原文是正确行为，但被标记为 warning
- quality_check 被 warning 级别的误报拖垮，需要对纯 ASCII 文本加白名单或跳过

## 运行模式

- `--json` + `--pdf`：pipeline QA 模式
- `--testcase`：testcase 回归模式（含 baseline 回归检测）
- `--testcase` + `--save-baseline`：保存 baseline

## I/O

- 输入：translated.json + output.pdf（+ baseline/ 目录，如果存在）
- 输出：test_report.json（contracts/test_report.schema.json）
