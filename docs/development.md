# PDF Translator — Development Log

## Goal
Translate 成果物1 (45-page ja→zh slide deck) with high quality, page-by-page tuning.

## Acceptance Criteria
- All test_agent checks pass or failures are justified (e.g., brand names)
- No visible text truncation in output PDF
- Consistent terminology across all pages
- All section headings translated to Chinese

## Current State (2026-03-23)
- 7 rounds完成：翻译修正(5轮) + 换行修复(1轮) + 测试自动化(1轮)
- test_agent 新增 3 个确定性检查（linebreak、mixed_language、terminology_consistency）
- 自动化检查全部 PASS，但用户目视 output.pdf 发现仍有较多问题待修
- **Next:** 用户下次提供目视反馈，逐页修复剩余排版/翻译问题

## Open Issues
- readability_check: 4 个 content_truncated（P8/P28/P40），layout_agent auto-sizing 是否视觉可接受待确认
- quality_check / translation_completeness_check FAIL 项均为品牌名误报，考虑加白名单
- style_check (LLM-based) 每次跑出不同结果，不收敛——确定性检查已部分替代
- 用户目视发现"很多问题"，具体待下次沟通

## Change Log

### Round 1: Core translation fixes
- Translated English section headings on P17/P19/P20 (Configuration→配置, Scenarios→场景, etc.)
- Fixed P31 "滤波器"→"过滤器"
- Translated P40 English (Evaluation Tools→评估工具, Release Dashboard→发布看板, etc.)
- Simplified P34 over-translations (CCB, code identifiers)
- Shortened P04/P05 "DFDI" expansion
- **Result:** style PASS, readability 13→7

### Round 2: Readability overflow fixes
- Shortened/reformatted p06_b009, p07_b008/010, p08_b008, p28_b005, p40_b003
- **Result:** readability 7→4, resolved p06/p07 completely

### Round 3: Terminology consistency
- Standardized LiDAR (not 激光雷达), 摄像头 (not 相机), SoC (not SOC), Momenta Box (not Momenta 盒子)
- Fixed P11 tone "烦请"→"请"
- **Result:** 13 blocks fixed, style found 6 new issues

### Round 4: Second terminology pass
- Standardized 边界工况 (not 边缘场景), 侧摄像头 (not 侧置摄像头)
- Removed DFDI/CFDI Chinese expansions (keep abbreviations)
- Standardized conjunctions "与" (not "和")
- **Result:** style found 4 new issues

### Round 5: Final style convergence
- Fixed FDI "车辆"→"车队", 档位→挡位, VVP Loc→VVP 定位
- **Result:** style_check PASS

### Round 6: Line break restoration
- Fixed 10 blocks (P16-P20) where ■/• markers lost preceding \n
- Root cause: translate_agent LLM dropped JSON-escaped newlines (known issue, placeholder fix exists but cache had stale entries)

### Round 7: Test automation — 3 new deterministic checks
- `linebreak_consistency_check`: detects \n loss before ■/• markers
- `mixed_language_check`: detects untranslated English headings/phrases in Chinese text
- `terminology_consistency_check`: detects same English term translated differently (variant pairs + dynamic detection)
- All 3 checks PASS on current translated.json, confirming fixes are effective
- Schema updated in contracts/test_report.schema.json
