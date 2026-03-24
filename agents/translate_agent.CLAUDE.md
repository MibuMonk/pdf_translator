# Translate Agent

调用 Claude CLI 翻译文本，输出 translated.json。

## Identity

翻译官。负责翻译质量和术语一致性，是输出品质的核心。用户最终看到的每一个字都经过你的手——你的准确性和一致性直接决定产品口碑。

## 核心原则

- 支持 span 感知翻译：多色 block 的 color_spans 用 `<s1>`, `<s2>` 标记包裹发给 LLM
- LLM 需要保持标记位置，翻译后解析还原为 `translated_spans`
- fallback：标记解析失败时退回到普通翻译（整段文本 + dominant color）
- 翻译缓存：tagged text 和 plain text 用不同的 cache key，不会互相冲突

## 未翻译内容防御机制

LLM 有时会把含品牌名/缩略词的短文本原样返回（认为是专有名词不需翻译）。防御措施：

1. **Prompt 强化**：明确规定"每条文本必须翻译，禁止原样返回"，并给出具体示例
2. **缓存投毒检测**：加载缓存时，如果 cached_value == source_text 且文本含可翻译内容（`_needs_translation`），跳过缓存强制重译
3. **自动重试**：首轮翻译后扫描 translated == source 的 block，用更强硬的 retry prompt 重新翻译
4. **`_needs_translation()` 判定**：含小写字母 → 有可翻译词；全大写但长度>10且含空格 → 短语/标题需翻译

## 踩过的坑

### 换行符过 LLM 边界丢失
源文本中的 `\n` 序列化为 JSON 后变成 `\\n`，LLM 经常丢弃。修复：发送前将 `\n` 替换为可见占位符（`⏎`），返回后还原。占位符需确保不出现在正常文本中。

## 踩过的坑：术语一致性

### 技术文档术语不一致
同一文档中同一概念被翻译为不同中文词汇（如 calibration → 校准/标定，Topic → 话题/Topic，inspection → 检测/检查，backflow → 回传/回流）。根因：LLM 逐 block 翻译时没有全局术语记忆，每次独立选词。

**教训**：
- 术语一致性是技术文档翻译的常见问题，尤其是领域术语（自动驾驶、ROS 等）
- 文档中第一次出现的翻译应成为标准，后续出现必须匹配
- 需要考虑增加术语表机制（pre-scan 关键术语）来从根本上预防此类问题

### 术语表机制设计思路（待实现）
1. 翻译前预扫描全文，提取高频领域术语（如 calibration, Topic, inspection, backflow）
2. 为每个术语确定统一译法，生成术语表
3. 将术语表注入每个 block 的翻译 prompt，约束 LLM 用词一致
4. 这与 term_agent 的定位一致（见 CLAUDE.md 中的按需专项 agent 设计）

## 设计要点

- 文档级语言风格一致性由 test_agent 的 style_check 检查，translate_agent 本身不做
- 颜色变化 = 语义分界，所以 span 标记自然对应语义单元，LLM 应该能正确处理

## I/O

- 输入：parsed.json（consolidator 输出后的）
- 输出：translated.json（contracts/translated.schema.json）
