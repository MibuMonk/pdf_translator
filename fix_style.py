#!/usr/bin/env python3
"""
修正翻译缓存中混入的 です/ます 风格，统一为プレゼン資料向けの簡体（体言止め・普通体）。
"""
import json, re, subprocess, shutil, sys, os

CLAUDE_CLI = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")

CACHE_FILES = [
    "/Users/qirui/Downloads/【成果物3】ギャップ分析及びプロポーザル.ja.transcache.json",
    "/Users/qirui/Downloads/【成果物1】ワークショップMomenta経験講習資料.ja.transcache.json",
    "/Users/qirui/Downloads/【成果物4】ワールドモデルについての補足説明.ja.transcache.json",
]

MASU_PATTERN = re.compile(r'(ます|です|ました|でした|ません|ください|ましょう|幸いです|おります)(。|\s|\Z)')

def is_masu_style(text: str) -> bool:
    return bool(MASU_PATTERN.search(text)) and len(text.strip()) > 5

def call_claude_fix(batch: list) -> dict:
    """batch: [(key, en_src, ja_current), ...]  → {key: fixed_ja}"""
    input_json = json.dumps(
        [{"id": i, "en": en, "ja": ja} for i, (_, en, ja) in enumerate(batch)],
        ensure_ascii=False,
    )
    prompt = (
        "あなたはプレゼンテーション資料の翻訳校正の専門家です。\n"
        "以下の英語原文（en）と、現在の日本語訳（ja）が与えられます。\n"
        "現在の訳にはです・ます体が混在しています。\n\n"
        "【修正ルール】\n"
        "- スライド資料に適した簡潔なスタイル（体言止め・普通体・名詞止め）に統一する\n"
        "- です・ます・ました・ません・ください・幸いです・おります 等の丁寧語を除去する\n"
        "- 意味・専門用語・改行（\\n）・箇条書き記号（•）はそのまま保持する\n"
        "- 不要な言い換えや追加はしない\n\n"
        "入力形式: JSON配列、各要素に id / en / ja フィールド\n"
        "出力形式: JSONのみ返す（コードブロック不要）。各要素に id と fixed フィールド。\n\n"
        + input_json
    )
    try:
        result = subprocess.run(
            [CLAUDE_CLI, "-p", prompt],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            print(f"  ⚠ CLI エラー: {result.stderr[:200]}")
            return {}
        raw = result.stdout.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        items = json.loads(raw)
        return {batch[item["id"]][0]: item["fixed"] for item in items}
    except subprocess.TimeoutExpired:
        print("  ⚠ タイムアウト")
        return {}
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  ⚠ JSON解析失敗: {e}")
        return {}

BATCH_SIZE = 30

for cache_path in CACHE_FILES:
    print(f"\n=== {os.path.basename(cache_path)} ===")
    with open(cache_path, encoding="utf-8") as f:
        cache = json.load(f)

    targets = [(k, v) for k, v in cache.items() if is_masu_style(v)]
    print(f"  対象件数: {len(targets)}")
    if not targets:
        print("  スキップ（対象なし）")
        continue

    fixed_count = 0
    for start in range(0, len(targets), BATCH_SIZE):
        batch_kv = targets[start:start + BATCH_SIZE]
        batch = [(k, k, v) for k, v in batch_kv]  # (key, en_src, ja_current)
        print(f"  バッチ {start+1}-{min(start+BATCH_SIZE, len(targets))}/{len(targets)} 処理中...", flush=True)
        fixes = call_claude_fix(batch)
        for key, fixed in fixes.items():
            if fixed and fixed != cache[key]:
                print(f"    修正: {cache[key][:50]!r}")
                print(f"      → {fixed[:50]!r}")
                cache[key] = fixed
                fixed_count += 1

    print(f"  修正件数: {fixed_count}/{len(targets)}")
    # バックアップ保存
    backup = cache_path + ".bak"
    if not os.path.exists(backup):
        import shutil as sh
        sh.copy2(cache_path, backup)
        print(f"  バックアップ: {os.path.basename(backup)}")
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    print(f"  保存完了: {os.path.basename(cache_path)}")

print("\n✓ 完了")
