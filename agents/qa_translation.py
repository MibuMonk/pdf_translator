"""
qa_translation.py — Translation content quality checks.
Covers: coverage, completeness, linebreak consistency, mixed language, terminology, fragmentation.
Imported by: test_agent
"""
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from shared_utils import has_cjk  # noqa: E402
from qa_utils import _weighted_len  # noqa: E402


# ---------------------------------------------------------------------------
# Module-level regex / constant globals
# ---------------------------------------------------------------------------

_PRODUCT_NAME_RE = re.compile(r'^[A-Z][A-Z0-9._\-/]*$')

# Regex: bullet/section marker followed by English word
_UNTRANSLATED_HEADING_RE = re.compile(r'[■•]\s*[A-Za-z]{2,}')

# Regex: 3+ consecutive English words (each 2+ chars)
_ENGLISH_PHRASE_RE = re.compile(r'[A-Za-z]{2,}(?:\s+[A-Za-z]{2,}){2,}')

# Regex: text inside parentheses
_PARENS_RE = re.compile(r'\([^)]*\)')

# Regex: ALL-CAPS abbreviation (2+ uppercase letters, optionally with digits)
_ALLCAPS_ABBREV_RE = re.compile(r'^[A-Z][A-Z0-9]{1,}$')

# Regex: code identifier (contains underscore)
_CODE_IDENT_RE = re.compile(r'_')

# Known product names / technical terms that should stay English
_ENGLISH_KEEP_TERMS = {
    "momenta box", "momenta model lab", "momenta",
    "vvp runtime", "vvp run time", "runtime",
    "google maps", "open street map", "point cloud",
    "deep learning", "machine learning", "neural network",
    "open source", "pull request", "merge request",
    "good event set", "road test dashboard",
    "ict dashboard", "rct report",
}

# Product name words that should not be flagged as untranslated headings
_HEADING_KEEP_WORDS = frozenset({
    "momenta", "google", "apple", "microsoft", "amazon", "tesla", "nvidia",
    "intel", "qualcomm", "huawei", "baidu", "alibaba",
})

# Regex: CJK unified ideographs (BMP + Ext-A + CJK compat ideographs + SIP)
_CJK_RE = re.compile(r'[\u3000-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]')

# Known variant pairs: (variant_a, variant_b, description)
# These are CJK translation variants that indicate inconsistency
_VARIANT_PAIRS = [
    ("摄像头", "相机", "camera"),
    ("过滤器", "滤波器", "filter"),
    ("边界工况", "边缘场景", "edge case"),
    ("服务器", "伺服器", "server"),
    ("数据库", "资料库", "database"),
    ("接口", "界面", "interface (API vs UI)"),
    ("文件", "档案", "file/document"),
    ("激光雷达", "雷达", "LiDAR"),
    ("传感器", "感测器", "sensor"),
    ("算法", "演算法", "algorithm"),
    ("组件", "元件", "component"),
    ("模块", "模组", "module"),
    ("配置", "设定", "configuration/setting"),
    ("执行", "运行", "execute/run"),
    ("框架", "架构", "framework"),
]

# Stop words for English term extraction
_STOP_WORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "for", "with", "from", "that",
    "this", "are", "was", "were", "been", "being", "have", "has", "had",
    "will", "would", "could", "should", "may", "might", "can", "shall",
    "not", "all", "each", "every", "both", "few", "more", "most", "other",
    "some", "such", "than", "too", "very", "also", "just", "about", "into",
    "over", "after", "before", "between", "through", "during", "without",
    "again", "further", "then", "once", "here", "there", "when", "where",
    "how", "what", "which", "who", "whom", "its", "their", "our", "your",
})

_TRIVIAL_RE = re.compile(r'^[\d\s.,;:!?()\[\]/%+\-=\\\'\"\u00b1\u00d7\u00f7\u2248\u2264\u2265\u221e\u00b0\u00b5\u03b1-\u03c9\u0391-\u03a9]*$')
_ACRONYM_DEF_RE = re.compile(r'^[A-Z]{2,}[0-9A-Z]*[\s\n]*[\(:]')
# Matches strings that contain ONLY ASCII-range characters (letters, digits,
# punctuation, spaces).  No CJK, kana, hangul, or other non-ASCII scripts.
_PURE_ASCII_RE = re.compile(r'^[\x20-\x7E\t\n\r]*$')


# ---------------------------------------------------------------------------
# Helper predicates
# ---------------------------------------------------------------------------

def _is_likely_product_name(text: str) -> bool:
    """
    Return True if text looks like a product name, acronym, or identifier
    that should NOT be translated (e.g. "CDI", "Mviz", "DDOD").
    Criteria: all uppercase (with digits/punctuation), no spaces, length < 15.
    """
    stripped = text.strip()
    if len(stripped) < 15 and ' ' not in stripped and _PRODUCT_NAME_RE.match(stripped):
        return True
    return False


def _is_target_language(text: str, tgt: str) -> bool:
    """Check if text is predominantly already in the target language."""
    clean = re.sub(r'[^\w\s]', '', text, flags=re.UNICODE).strip()
    if not clean:
        return False
    if tgt == 'en':
        latin = len(re.findall(r'[a-zA-Z]', clean))
        total = len(re.findall(r'\S', clean))
        return total > 0 and latin / total > 0.5
    elif tgt == 'ja':
        return bool(re.search(r'[\u3040-\u30ff]', clean))
    elif tgt.startswith('zh'):
        has_cjk = bool(re.search(r'[\u4e00-\u9fff]', clean))
        has_kana = bool(re.search(r'[\u3040-\u30ff]', clean))
        return has_cjk and not has_kana
    return False


def _is_trivially_invariant(text: str) -> bool:
    """Return True if text is composed only of numbers/punctuation/symbols."""
    return bool(_TRIVIAL_RE.match(text))


def _is_acronym_definition(text: str) -> bool:
    """True if text looks like an acronym definition line (DDOD: Data-Driven …)."""
    return bool(_ACRONYM_DEF_RE.match(text.strip()))


def _is_pure_ascii(text: str) -> bool:
    """True if text contains only ASCII printable chars, tabs, newlines.

    Pure-ASCII blocks (product names like 'HONDA', abbreviations like 'API',
    technical terms like 'Wi-Fi') are legitimately kept unchanged during
    translation.  Flagging them as 'unchanged_translation' is a false positive.

    Returns False if the text contains ANY CJK, kana, hangul, or other
    non-ASCII characters — those blocks should still be checked.
    """
    if bool(_PURE_ASCII_RE.match(text)):
        return True
    # "Mostly ASCII": >85% of chars are ASCII printable + only non-CJK non-Latin
    # symbols make up the rest (e.g. ±×°µ in technical metrics).
    # These are effectively technical identifiers and should not be flagged.
    total = len(text)
    if total == 0:
        return True
    # Count fullwidth punctuation (（）【】「」、。…etc.) as ASCII-equivalent
    # for the purpose of this check — they are decorative wrappers, not CJK content
    _FULLWIDTH_PUNCT = frozenset('\uff08\uff09\u3010\u3011\u300c\u300d\u3001\u3002\u2026\uff0c\uff01\uff1f\uff1a\uff1b')
    ascii_equiv_count = sum(
        1 for c in text
        if ('\x20' <= c <= '\x7e' or c in '\t\n\r' or c in _FULLWIDTH_PUNCT)
    )
    if ascii_equiv_count / total >= 0.85 and not any('\u4e00' <= c <= '\u9fff' or
                                               '\u3040' <= c <= '\u30ff' or
                                               '\uac00' <= c <= '\ud7af'
                                               for c in text):
        return True
    return False


def _check_translation_block(page_num: int, block_id: str, block: dict) -> list[dict]:
    """Return a list of translation issue dicts for a single block."""
    issues = []
    text       = (block.get("text") or "").strip()
    translated = (block.get("translated") or "").strip()

    if not text:
        return issues

    if not translated:
        issues.append({
            "page":       page_num,
            "block_id":   block_id,
            "type":       "missing_translation",
            "severity":   "critical",
            "text":       text,
            "translated": translated,
        })
        return issues

    if (
        translated == text
        and not _is_trivially_invariant(text)
        and not _is_acronym_definition(text)
        and not _is_pure_ascii(text)
        and len(text) > 5
    ):
        issues.append({
            "page":       page_num,
            "block_id":   block_id,
            "type":       "unchanged_translation",
            "severity":   "warning",
            "text":       text,
            "translated": translated,
        })

    if translated.endswith("…") and not text.endswith("…"):
        issues.append({
            "page":       page_num,
            "block_id":   block_id,
            "type":       "likely_truncated",
            "severity":   "warning",
            "text":       text,
            "translated": translated,
        })

    wt_src = _weighted_len(text)
    wt_trl = _weighted_len(translated)
    if wt_src > 40 and wt_trl < wt_src * 0.25:
        issues.append({
            "page":       page_num,
            "block_id":   block_id,
            "type":       "suspiciously_short",
            "severity":   "warning",
            "text":       text,
            "translated": translated,
        })

    return issues


# ---------------------------------------------------------------------------
# Public check functions
# ---------------------------------------------------------------------------

def translation_completeness_check(translated_json_path: str) -> dict:
    """
    Check for untranslated content and low translation ratios per page.
    - untranslated_content: block where translated == text, len > 10, contains spaces (English sentence)
    - low_translation_ratio: page where < 50% of characters are translated
    """
    import json
    with open(translated_json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        pages = data.get("pages", [data])
    else:
        return {"check_result": "fail", "details": [{"error": "Unexpected JSON structure"}]}

    issues: list[dict] = []
    page_ratios: list[dict] = []

    # Read target language for _is_target_language check
    tgt_lang = data.get("target_lang", "") if isinstance(data, dict) else ""

    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        blocks = page_entry.get("blocks", [])

        total_chars = 0
        translated_chars = 0

        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            text = (block.get("text") or "").strip()
            translated = (block.get("translated") or "").strip()
            block_id = block.get("block_id", block.get("id", f"p{page_num:02d}_b{idx:03d}"))

            if not text:
                continue

            # Skip trivially-invariant content (numbers, symbols) from ratio;
            # these are correctly left unchanged and should not penalise the score.
            if _is_trivially_invariant(text):
                continue

            # Skip content already in the target language (e.g. English terms in
            # a Chinese→English document are correctly left unchanged).
            if tgt_lang and _is_target_language(text, tgt_lang):
                continue

            total_chars += len(text)

            # Check if translated differs from source
            if translated and translated != text:
                translated_chars += len(text)
            elif translated == text:
                # Same as source — check if it should have been translated
                if (
                    len(text) > 10
                    and ' ' in text
                    and not _is_trivially_invariant(text)
                    and not _is_acronym_definition(text)
                    and not _is_likely_product_name(text)
                    and not _is_pure_ascii(text)
                ):
                    issues.append({
                        "page": page_num,
                        "block_id": block_id,
                        "type": "untranslated_content",
                        "severity": "error",
                        "text": text[:100],
                    })

        # Per-page translation ratio
        if total_chars > 0:
            ratio = translated_chars / total_chars
            page_ratios.append({"page": page_num, "ratio": round(ratio, 3)})
            if ratio < 0.50:
                issues.append({
                    "page": page_num,
                    "type": "low_translation_ratio",
                    "severity": "error",
                    "ratio": round(ratio, 3),
                    "total_chars": total_chars,
                    "translated_chars": translated_chars,
                })

    has_errors = any(i.get("severity") == "error" for i in issues)
    return {
        "check_result": "fail" if has_errors else "pass",
        "details": {
            "issues": issues,
            "page_ratios": page_ratios,
            "untranslated_count": sum(1 for i in issues if i["type"] == "untranslated_content"),
            "low_ratio_pages": sum(1 for i in issues if i["type"] == "low_translation_ratio"),
        },
    }


def linebreak_consistency_check(translated_json_path: str) -> dict:
    """
    Check for line breaks lost during translation.
    - missing_bullet_break: bullet marker (■/•) not preceded by \n in translation
      but original has \n before the corresponding marker → severity "error"
    - linebreak_count_mismatch: translation lost more than half of original \n → severity "warning"
    """
    import json
    with open(translated_json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        pages = data.get("pages", [data])
    else:
        return {"check_result": "fail", "details": {"issues": [], "total_checked": 0, "blocks_with_missing_breaks": 0}}

    issues: list[dict] = []
    total_checked = 0

    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        blocks = page_entry.get("blocks", [])

        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            text = block.get("text") or ""
            translated = block.get("translated") or ""
            block_id = block.get("block_id", block.get("id", f"p{page_num:02d}_b{idx:03d}"))

            if not text or not translated:
                continue

            # Problem A: skip purely decorative PUA blocks (e.g. glyph-based bullets)
            if re.sub(r'[\ue000-\uf8ff\s]', '', text) == '':
                continue

            total_checked += 1

            # Problem B: count only *semantic* newlines in original (same definition as
            # _clean_layout_breaks in translate_agent) so layout-wrap \n that were
            # legitimately removed don't trigger false-positive mismatches.
            # A newline is semantic when the next line starts with a bullet/marker
            # OR the previous line ends with sentence-final punctuation (。！？).
            _sem_bullet = re.compile(
                r'^[\s]*(?:[•■\-–·*▶▷►▸◆◇○●→⇒★※]|【|\d+[.\)）])'
            )
            _sent_final = re.compile(r'[。！？]\s*$')
            orig_lines = text.split("\n")
            orig_breaks = 0
            for _li in range(1, len(orig_lines)):
                if _sem_bullet.match(orig_lines[_li]) or _sent_final.search(orig_lines[_li - 1]):
                    orig_breaks += 1
            trans_breaks = translated.count("\n")

            # Rule 1: missing_bullet_break
            # Check if translated text has ■ or • NOT at position 0 and NOT preceded by \n
            for marker in ("■", "•"):
                start = 0
                while True:
                    pos = translated.find(marker, start)
                    if pos == -1:
                        break
                    if pos > 0 and translated[pos - 1] != "\n":
                        # Check if original has \n before this marker type
                        orig_has_break_before_marker = False
                        ostart = 0
                        while True:
                            opos = text.find(marker, ostart)
                            if opos == -1:
                                break
                            if opos > 0 and text[opos - 1] == "\n":
                                orig_has_break_before_marker = True
                                break
                            ostart = opos + 1

                        if orig_has_break_before_marker:
                            issues.append({
                                "page": page_num,
                                "block_id": block_id,
                                "type": "missing_bullet_break",
                                "severity": "error",
                                "original_breaks": orig_breaks,
                                "translated_breaks": trans_breaks,
                                "text_preview": translated[:80],
                            })
                            break  # one issue per block per marker is enough
                    start = pos + 1

            # Rule 2: linebreak_count_mismatch
            # Skip if block already has a Rule 1 error (avoid duplicate reporting)
            _block_already_flagged = any(
                i["block_id"] == block_id and i["severity"] == "error" for i in issues
            )
            if orig_breaks > 0 and trans_breaks < orig_breaks / 2 and not _block_already_flagged:
                issues.append({
                    "page": page_num,
                    "block_id": block_id,
                    "type": "linebreak_count_mismatch",
                    "severity": "warning",
                    "original_breaks": orig_breaks,
                    "translated_breaks": trans_breaks,
                    "text_preview": translated[:80],
                })

    has_errors = any(i.get("severity") == "error" for i in issues)
    blocks_with_missing = len(set(i["block_id"] for i in issues))
    return {
        "check_result": "fail" if has_errors else "pass",
        "details": {
            "issues": issues,
            "total_checked": total_checked,
            "blocks_with_missing_breaks": blocks_with_missing,
        },
    }


def mixed_language_check(translated_json_path: str) -> dict:
    """
    Detect blocks where translated text still contains untranslated English phrases.
    - untranslated_heading: ■/• followed by English word (severity: error)
    - english_phrase_in_translation: 3+ consecutive English words in CJK text (severity: warning)

    Skipped entirely when target language is a Latin script language (en, de, fr, es, …)
    since all English in the translation is expected.
    """
    import json
    with open(translated_json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        pages = data
        target_lang = ""
    elif isinstance(data, dict):
        pages = data.get("pages", [data])
        target_lang = data.get("target_lang", "")
    else:
        return {"check_result": "fail", "details": {"issues": [], "total_checked": 0, "blocks_with_mixed_language": 0}}

    # Skip this check for Latin-script target languages — English phrases are expected
    _latin_targets = {"english", "en", "german", "french", "spanish", "portuguese",
                      "italian", "dutch", "russian", "polish", "arabic"}
    if target_lang.lower() in _latin_targets:
        return {
            "check_result": "pass",
            "details": {
                "issues": [],
                "total_checked": 0,
                "blocks_with_mixed_language": 0,
                "skipped_reason": f"target_lang={target_lang!r} is not a CJK language",
            },
        }

    issues: list[dict] = []
    total_checked = 0

    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        blocks = page_entry.get("blocks", [])

        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            text = block.get("text") or ""
            translated = block.get("translated") or ""
            block_id = block.get("block_id", block.get("id", f"p{page_num:02d}_b{idx:03d}"))

            if not translated:
                continue

            total_checked += 1

            # Rule 1: untranslated_heading — ■/• followed by English word
            for m in _UNTRANSLATED_HEADING_RE.finditer(translated):
                matched = m.group()
                # Exception: skip if original is English AND translation has no CJK
                # (entire block intentionally kept as-is, e.g. brand names)
                if not has_cjk(translated) and ' ' in text:
                    continue
                # Exception: skip ALL-CAPS abbreviations/product names after bullet
                # e.g. "• MBOX", "• VVP", "• FDR"
                eng_word = matched.lstrip("■•").strip()
                if _ALLCAPS_ABBREV_RE.match(eng_word):
                    continue
                # Exception: skip known product/brand names
                if eng_word.lower() in _HEADING_KEEP_WORDS:
                    continue
                # Exception: skip if followed by colon/CJK/English word (term used inline)
                # e.g. "• VVP Camera：...", "• Good Event Set"
                end_pos = m.end()
                if end_pos < len(translated):
                    rest = translated[end_pos:end_pos + 20].lstrip()
                    if rest and (rest[0] in '：:' or has_cjk(rest[:1])):
                        continue
                    # If followed by another English word, it's a multi-word term, not untranslated heading
                    if rest and re.match(r'[A-Za-z]', rest):
                        continue
                issues.append({
                    "page": page_num,
                    "block_id": block_id,
                    "type": "untranslated_heading",
                    "severity": "error",
                    "matched_text": matched,
                    "text_preview": translated[:100],
                })

            # Rule 2: english_phrase_in_translation — 3+ consecutive English words in CJK text
            # Gate: require actual CJK ideographs or kana (not just CJK punctuation like 【】)
            # Fullwidth brackets 【U+3010/3011】 are in has_cjk range but don't indicate CJK text
            _has_cjk_ideograph = bool(re.search(
                r'[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]', translated
            ))
            if not _has_cjk_ideograph:
                continue  # Not a CJK translation, skip phrase check

            # Remove parenthesised content before scanning
            cleaned = _PARENS_RE.sub('', translated)

            for m in _ENGLISH_PHRASE_RE.finditer(cleaned):
                phrase = m.group()
                words = phrase.split()

                # Exclude: all words are ALL-CAPS abbreviations
                if all(_ALLCAPS_ABBREV_RE.match(w) for w in words):
                    continue

                # Exclude: contains underscore (code identifier)
                if _CODE_IDENT_RE.search(phrase):
                    continue

                # Exclude: known product/technical terms
                if phrase.lower() in _ENGLISH_KEEP_TERMS:
                    continue

                # Exclude: any sub-phrase of 3 words matches known terms
                skip = False
                for term in _ENGLISH_KEEP_TERMS:
                    if term in phrase.lower():
                        skip = True
                        break
                if skip:
                    continue

                issues.append({
                    "page": page_num,
                    "block_id": block_id,
                    "type": "english_phrase_in_translation",
                    "severity": "warning",
                    "matched_text": phrase,
                    "text_preview": translated[:100],
                })

    has_errors = any(i.get("severity") == "error" for i in issues)
    blocks_with_mixed = len(set(i["block_id"] for i in issues))
    return {
        "check_result": "fail" if has_errors else "pass",
        "details": {
            "issues": issues,
            "total_checked": total_checked,
            "blocks_with_mixed_language": blocks_with_mixed,
        },
    }


def terminology_consistency_check(translated_json_path: str) -> dict:
    """
    Detect inconsistent translations of the same term across the document.
    Two strategies:
    1. Known variant pairs: check if both variants appear in translated text
    2. Dynamic: find English terms in source that appear in 3+ blocks, check if
       they map to different CJK translations across blocks
    """
    import json
    with open(translated_json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        pages = data.get("pages", [data])
    else:
        return {"check_result": "pass", "details": {"variant_pair_issues": [], "dynamic_issues": [], "total_checked": 0}}

    # Collect all translated text per page for variant-pair scanning
    all_translated_text = []  # list of (page_num, block_id, translated_text)
    total_checked = 0

    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        blocks = page_entry.get("blocks", [])

        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            translated = block.get("translated") or ""
            block_id = block.get("block_id", block.get("id", f"p{page_num:02d}_b{idx:03d}"))
            if translated:
                total_checked += 1
                all_translated_text.append((page_num, block_id, translated))

    # --- Strategy 1: Known variant pairs ---
    variant_pair_issues = []
    full_translated = "\n".join(t for _, _, t in all_translated_text)

    for variant_a, variant_b, desc in _VARIANT_PAIRS:
        count_a = full_translated.count(variant_a)
        count_b = full_translated.count(variant_b)
        if count_a > 0 and count_b > 0:
            # Both variants present — collect sample locations
            pages_a = []
            pages_b = []
            for pg, bid, txt in all_translated_text:
                if variant_a in txt and len(pages_a) < 3:
                    pages_a.append({"page": pg, "block_id": bid})
                if variant_b in txt and len(pages_b) < 3:
                    pages_b.append({"page": pg, "block_id": bid})
            variant_pair_issues.append({
                "type": "variant_pair",
                "severity": "warning",
                "term_description": desc,
                "variant_a": variant_a,
                "variant_a_count": count_a,
                "variant_b": variant_b,
                "variant_b_count": count_b,
                "sample_locations_a": pages_a,
                "sample_locations_b": pages_b,
            })

    # --- Strategy 2: Dynamic English term consistency ---
    dynamic_issues = []

    # Build: english_term -> {chinese_context -> [block_ids]}
    term_contexts = defaultdict(lambda: defaultdict(list))

    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        blocks = page_entry.get("blocks", [])

        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            text = block.get("text") or ""
            translated = block.get("translated") or ""
            block_id = block.get("block_id", block.get("id", f"p{page_num:02d}_b{idx:03d}"))

            if not text or not translated or not has_cjk(translated):
                continue

            # Extract English terms from original text
            eng_terms = set(re.findall(r'\b[A-Za-z]{3,}\b', text))
            eng_terms = {t.lower() for t in eng_terms} - _STOP_WORDS

            for term in eng_terms:
                # Find how this term's surrounding context was translated
                # Use a simple heuristic: find the term in original, get its line,
                # find the corresponding CJK segment in translation
                # For simplicity: record the first CJK phrase near the term position
                # ratio-based position mapping
                term_lower = term.lower()
                text_lower = text.lower()
                pos = text_lower.find(term_lower)
                if pos == -1:
                    continue

                # Map position ratio to translated text
                ratio = pos / max(len(text), 1)
                trans_pos = int(ratio * len(translated))

                # Extract a CJK window around the mapped position (up to 6 chars)
                window_start = max(0, trans_pos - 3)
                window_end = min(len(translated), trans_pos + 6)
                cjk_window = translated[window_start:window_end]

                # Extract only CJK characters from window
                cjk_chars = _CJK_RE.findall(cjk_window)
                if len(cjk_chars) >= 2:
                    cjk_key = "".join(cjk_chars[:4])  # first 4 CJK chars as key
                    term_contexts[term_lower][cjk_key].append((page_num, block_id))

    # Flag terms with 2+ distinct CJK translations, each appearing 2+ times
    for eng_term, translations in term_contexts.items():
        if len(translations) < 2:
            continue
        # Filter to translations that appear at least twice
        significant = {k: v for k, v in translations.items() if len(v) >= 2}
        if len(significant) < 2:
            continue
        # Sort by frequency descending
        sorted_trans = sorted(significant.items(), key=lambda x: -len(x[1]))
        dynamic_issues.append({
            "type": "inconsistent_term_translation",
            "severity": "warning",
            "english_term": eng_term,
            "translations": [
                {
                    "chinese": k,
                    "occurrences": len(v),
                    "sample_locations": [{"page": pg, "block_id": bid} for pg, bid in v[:3]],
                }
                for k, v in sorted_trans[:4]
            ],
        })

    all_issues = variant_pair_issues + dynamic_issues
    return {
        "check_result": "fail" if any(i.get("severity") == "error" for i in all_issues) else "pass",
        "details": {
            "variant_pair_issues": variant_pair_issues,
            "dynamic_issues": dynamic_issues,
            "total_checked": total_checked,
        },
    }


def fragmentation_check(translated_data) -> dict:
    """
    Detect paragraph fragmentation: ■ heading in one block and • bullets in the
    next block (same column, small y gap).  Also detects consecutive bullet blocks
    that were split apart.
    """
    import json
    if isinstance(translated_data, str):
        with open(translated_data, encoding="utf-8") as f:
            translated_data = json.load(f)

    if isinstance(translated_data, list):
        pages = translated_data
    elif isinstance(translated_data, dict):
        pages = translated_data.get("pages", [translated_data])
    else:
        return {"check_result": "pass", "details": {"issues": []}}

    issues: list[dict] = []

    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        blocks = page_entry.get("blocks", [])

        # Sort blocks by y0 (top of bbox)
        sorted_blocks = []
        for idx, blk in enumerate(blocks):
            if not isinstance(blk, dict):
                continue
            bbox = blk.get("bbox")
            translated = blk.get("translated") or ""
            if not bbox or len(bbox) < 4 or not translated.strip():
                continue
            sorted_blocks.append({
                "block": blk,
                "translated": translated,
                "bbox": bbox,
                "block_id": blk.get("block_id", blk.get("id", f"p{page_num:02d}_b{idx:03d}")),
            })
        sorted_blocks.sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))

        for i in range(len(sorted_blocks) - 1):
            a = sorted_blocks[i]
            b = sorted_blocks[i + 1]
            a_text = a["translated"]
            b_text = b["translated"]
            a_x0 = a["bbox"][0]
            b_x0 = b["bbox"][0]
            a_y1 = a["bbox"][3]
            b_y0 = b["bbox"][1]

            same_column = abs(a_x0 - b_x0) < 30

            # Rule 1: ■ heading alone + next block starts with •
            if (a_text.lstrip().startswith("■")
                    and "•" not in a_text
                    and b_text.lstrip().startswith("•")
                    and same_column):
                issues.append({
                    "type": "section_fragmentation",
                    "severity": "warning",
                    "page": page_num,
                    "block_ids": [a["block_id"], b["block_id"]],
                    "text_preview": a_text[:40],
                })

            # Rule 2: block A ends with • line, block B starts with • (split bullets)
            # Guard: skip if block A's translated text does NOT end with sentence-final
            # punctuation — truncated blocks have a trailing • that is part of ongoing
            # content, not a completed bullet boundary.
            _a_ends_sentence = bool(re.search(r'[。！？…]\s*$', a_text))
            if (a_text.rstrip().endswith("•") or a_text.rstrip().split("\n")[-1].lstrip().startswith("•")):
                if b_text.lstrip().startswith("•") and same_column and _a_ends_sentence:
                    y_gap = b_y0 - a_y1
                    if y_gap < 30:
                        # Avoid duplicate if already reported by Rule 1
                        already = any(
                            iss["block_ids"] == [a["block_id"], b["block_id"]]
                            for iss in issues
                        )
                        if not already:
                            issues.append({
                                "type": "section_fragmentation",
                                "severity": "warning",
                                "page": page_num,
                                "block_ids": [a["block_id"], b["block_id"]],
                                "text_preview": a_text[:40],
                            })

    has_errors = any(i.get("severity") == "error" for i in issues)
    return {
        "check_result": "fail" if has_errors else "pass",
        "details": {"issues": issues},
    }


def coverage_check(translated_json_path: str) -> dict:
    """
    Check translation coverage and quality from translated.json.
    Returns a result dict compatible with the issue_results framework.
    """
    import json
    with open(translated_json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        pages = data.get("pages", [data])
    else:
        return {"check_result": "fail", "details": [{"error": f"Unexpected JSON structure"}]}

    total_blocks      = 0
    translated_blocks = 0
    all_issues: list[dict] = []
    per_page_stats: list[dict] = []

    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        blocks   = page_entry.get("blocks", [])

        page_total      = 0
        page_translated = 0
        page_issues: list[dict] = []

        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            text = (block.get("text") or "").strip()
            if not text:
                continue

            block_id = block.get("block_id", block.get("id", f"p{page_num:02d}_b{idx:03d}"))
            page_total += 1
            translated = (block.get("translated") or "").strip()
            if translated:
                page_translated += 1

            page_issues.extend(_check_translation_block(page_num, block_id, block))

        total_blocks      += page_total
        translated_blocks += page_translated
        all_issues.extend(page_issues)
        per_page_stats.append({
            "page":       page_num,
            "total":      page_total,
            "translated": page_translated,
            "issues":     len(page_issues),
        })

    coverage_pct = (
        round(translated_blocks / total_blocks * 100, 1)
        if total_blocks > 0 else 0.0
    )
    passed = coverage_pct >= 95

    retry_candidates = [iss["block_id"] for iss in all_issues if iss.get("severity") == "critical"]
    confidence = 1.0 if passed else round(coverage_pct / 100, 4)

    summary = {
        "total_blocks":      total_blocks,
        "translated_blocks": translated_blocks,
        "coverage_pct":      coverage_pct,
        "issue_count":       len(all_issues),
        "pass":              passed,
        "per_page":          per_page_stats,
    }

    return {
        "check_result": "pass" if passed else "fail",
        "details": {
            "summary":          summary,
            "translation_issues": all_issues,
            "self_eval": {
                "retry_candidates": retry_candidates,
                "confidence":       confidence,
            },
        },
    }


def quality_check(translated_json_path: str) -> dict:
    """
    Quality check: flag blocks with warning-level translation issues.
    Returns a result dict compatible with the issue_results framework.
    """
    import json
    with open(translated_json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        pages = data.get("pages", [data])
    else:
        return {"check_result": "fail", "details": [{"error": "Unexpected JSON structure"}]}

    warnings: list[dict] = []

    for page_entry in pages:
        if not isinstance(page_entry, dict):
            continue
        page_num = page_entry.get("page", page_entry.get("page_num", 0))
        blocks   = page_entry.get("blocks", [])

        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            text = (block.get("text") or "").strip()
            if not text:
                continue
            block_id = block.get("block_id", block.get("id", f"p{page_num:02d}_b{idx:03d}"))
            for iss in _check_translation_block(page_num, block_id, block):
                if iss.get("severity") == "warning":
                    warnings.append(iss)

    if warnings:
        return {"check_result": "fail", "details": warnings}
    return {"check_result": "pass", "details": []}
