#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fixtures 结构校验。

用途
    确认 fixtures/ 下的 JSON 结构完整、可被 B 直接使用，并检查它们与
    成员 D 的商品种子是否自洽。

    与 gen_fixtures.py 的分工：
        gen_fixtures.py --check   校验「生成逻辑是否自相矛盾」
        check_fixtures.py         校验「仓库里现有的 JSON 文件是否完好」

用法
    python scripts/check_fixtures.py
    python scripts/check_fixtures.py --catalog <种子路径>

退出码
    0 = 全部通过；1 = 存在问题（逐条打印）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]             # repository root
FIX = ROOT / "fixtures"
DEFAULT_CATALOG = ROOT / "data" / "products.seed.json"
SETTLEMENT_CURRENCY = "HKD"

PRODUCT_FIELDS = {
    "product_id", "category", "name", "brand", "model", "variant",
    "connection", "form_factor", "price_cents", "currency", "stock",
    "anc", "battery_hours", "wearing_weight_g", "use_cases",
    "source_type", "source_url", "data_note",
    "seller_description", "shipping_origin",
}

REQUIRED_CONSTRAINT_KEYS = {
    "category", "min_price_cents", "max_price_cents", "brand_allowlist",
    "connection", "form_factor", "anc_required", "min_battery_hours",
    "max_wearing_weight_g", "in_stock_only",
}

REQUIRED_PREFERENCE_KEYS = {"use_cases", "criteria", "priority_preset"}

REQUIRED_RESPONSE_KEYS = {
    "total_matches", "returned", "applied_constraints", "applied_criteria",
    "candidates", "comparison", "gaps", "relax_hints", "suggestions", "currency",
}

REQUIRED_GAP_KEYS = {
    "unsupported_criteria", "dropped_criteria", "dropped_dimensions",
    "missing_data_attributes", "over_budget_alternatives",
}

EXPECTED_FIXTURES = {
    "search.001.normal", "search.002.relax", "search.003.anc_unknown",
    "search.004.invalid", "search.005.gaps", "summarize.001.partial",
}


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        return exc


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", default=None)
    args = parser.parse_args()

    problems: list[str] = []
    notes: list[str] = []

    # ---------------- 商品种子（D 提供） ----------------
    catalog_path = Path(args.catalog or os.environ.get("HACKU_CATALOG") or DEFAULT_CATALOG)
    if not catalog_path.exists():
        print(f"[FAIL] 找不到商品种子：{catalog_path}", file=sys.stderr)
        return 1

    raw = load(catalog_path)
    if isinstance(raw, Exception):
        print(f"[FAIL] 种子不是合法 JSON：{raw}", file=sys.stderr)
        return 1
    products = raw["products"] if isinstance(raw, dict) else raw
    ids = [p.get("product_id") for p in products]

    if len(set(ids)) != len(ids):
        problems.append("种子 product_id 存在重复")
    for product in products:
        missing = PRODUCT_FIELDS - set(product)
        if missing:
            problems.append(f"{product.get('product_id')} 缺少字段 {sorted(missing)}")
        if product.get("connection") == "wired" and product.get("battery_hours") is not None:
            problems.append(f"{product['product_id']} 是有线耳机，battery_hours 应为 null")
        if product.get("currency") != SETTLEMENT_CURRENCY:
            problems.append(f"{product['product_id']} 币种不是 {SETTLEMENT_CURRENCY}")

    in_stock = [p for p in products if p.get("stock", 0) > 0]
    anc_true = sum(1 for p in in_stock if p.get("anc") is True)
    anc_false = sum(1 for p in in_stock if p.get("anc") is False)
    notes.append(f"商品种子 {len(products)} 条，在库 {len(in_stock)} 条")
    notes.append(f"在库 anc：true {anc_true} / false {anc_false} / unknown "
                 f"{len(in_stock) - anc_true - anc_false}")
    notes.append("在库佩戴形式：" + ", ".join(
        f"{key} {sum(1 for p in in_stock if p.get('form_factor') == key)}"
        for key in ("over_ear", "in_ear", "open_ear")))
    notes.append("在库连接：" + ", ".join(
        f"{key} {sum(1 for p in in_stock if p.get('connection') == key)}"
        for key in ("wired", "wireless")))

    # ---------------- 各 fixture ----------------
    found = {p.stem for p in FIX.glob("*.json")}
    for missing_name in sorted(EXPECTED_FIXTURES - found):
        problems.append(f"缺少 fixture：{missing_name}.json")
    for extra in sorted(found - EXPECTED_FIXTURES):
        notes.append(f"注意：存在预期之外的 fixture {extra}.json")

    for path in sorted(FIX.glob("*.json")):
        document = load(path)
        if isinstance(document, Exception):
            problems.append(f"{path.name}：不是合法 JSON（{document}）")
            continue

        meta = document.get("_meta")
        if not isinstance(meta, dict):
            problems.append(f"{path.name}：缺 _meta")
        elif "do_not_edit_by_hand" not in meta:
            problems.append(f"{path.name}：_meta 缺 do_not_edit_by_hand（该文件应是生成产物）")

        request = document.get("request")
        if not isinstance(request, dict):
            problems.append(f"{path.name}：缺 request")
            continue

        constraints = request.get("constraints")
        if not isinstance(constraints, dict):
            problems.append(f"{path.name}：缺 request.constraints")
            continue
        missing = REQUIRED_CONSTRAINT_KEYS - set(constraints)
        if missing:
            problems.append(f"{path.name}：constraints 缺字段 {sorted(missing)}")

        preferences = request.get("preferences")
        if not isinstance(preferences, dict):
            problems.append(f"{path.name}：缺 request.preferences")
        else:
            missing = REQUIRED_PREFERENCE_KEYS - set(preferences)
            if missing:
                problems.append(f"{path.name}：preferences 缺字段 {sorted(missing)}")

        expected = document.get("expected")
        if not isinstance(expected, dict):
            problems.append(f"{path.name}：缺 expected")
            continue

        # ---- 错误用例 ----
        if "error" in expected:
            if document.get("expected_error_code") != expected["error"].get("code"):
                problems.append(f"{path.name}：expected_error_code 与 expected.error.code 不一致")
            if expected.get("ok") is not False:
                problems.append(f"{path.name}：错误用例的 ok 应为 false")
            if expected["error"].get("details") is None:
                problems.append(f"{path.name}：error.details 不能为 null，无详情用 {{}}")
            continue

        # ---- summarize 用例 ----
        if "dimension_stats" in expected:
            if expected.get("total_matches") != len(in_stock):
                problems.append(
                    f"{path.name}：summarize total_matches={expected.get('total_matches')}，"
                    f"但种子在库数为 {len(in_stock)}")
            for name, stats in expected["dimension_stats"].items():
                if stats["known_count"] + stats["unknown_count"] != expected["total_matches"]:
                    problems.append(f"{path.name}：{name} 计数与 total_matches 不符")
            anc = expected["boolean_stats"]["anc"]
            if (anc["true_count"] + anc["false_count"] + anc["unknown_count"]
                    != expected["total_matches"]):
                problems.append(f"{path.name}：anc 三项之和不等于 total_matches")
            continue

        # ---- search 用例 ----
        missing = REQUIRED_RESPONSE_KEYS - set(expected)
        if missing:
            problems.append(f"{path.name}：expected 缺字段 {sorted(missing)}")
            continue

        if expected["currency"] != SETTLEMENT_CURRENCY:
            problems.append(f"{path.name}：currency 不是 {SETTLEMENT_CURRENCY}")
        if expected["returned"] != len(expected["candidates"]):
            problems.append(f"{path.name}：returned 与 candidates 长度不一致")
        if expected["returned"] > expected["total_matches"]:
            problems.append(f"{path.name}：returned 大于 total_matches")
        if expected["total_matches"] == 0 and expected["candidates"]:
            problems.append(f"{path.name}：total_matches=0 但 candidates 非空")
        if expected["total_matches"] == 0 and expected["relax_hints"] is None:
            problems.append(f"{path.name}：total_matches=0 但缺 relax_hints")

        gap_missing = REQUIRED_GAP_KEYS - set(expected["gaps"])
        if gap_missing:
            problems.append(f"{path.name}：gaps 缺字段 {sorted(gap_missing)}")

        known = set(ids)
        for candidate in expected["candidates"]:
            product = candidate.get("product", {})
            if product.get("product_id") not in known:
                problems.append(f"{path.name}：候选引用了种子里不存在的商品")
            if product.get("currency") != SETTLEMENT_CURRENCY:
                problems.append(f"{path.name}：候选币种不是 {SETTLEMENT_CURRENCY}")
            if "violated_fields" not in candidate:
                problems.append(f"{path.name}：候选缺 violated_fields")
            for reason in candidate.get("reasons", []):
                if not reason.get("field"):
                    problems.append(f"{path.name}：推荐理由缺 field，无法追溯到商品字段")

        for row in expected["comparison"]:
            if len(row.get("values", [])) != expected["returned"]:
                problems.append(
                    f"{path.name}：comparison[{row.get('attribute')}] 长度与 candidates 不一致")
            if (any(not v.get("known") for v in row.get("values", []))
                    and row.get("spread") is not None):
                problems.append(
                    f"{path.name}：comparison[{row.get('attribute')}] 有未知值但 spread 非 null")

        if expected["relax_hints"]:
            for hint in expected["relax_hints"].get("hints", []):
                if not hint.get("gained_count", 0) > 0:
                    problems.append(f"{path.name}：hint {hint.get('field')} 的 gained_count 不是正数")
                if hint.get("example_product_id") not in known:
                    problems.append(f"{path.name}：hint 引用了不存在的商品")
            for near in expected["relax_hints"].get("closest_candidates", []):
                if not near.get("violated_fields"):
                    problems.append(f"{path.name}：closest_candidate 的 violated_fields 为空")

        for alt in expected["gaps"]["over_budget_alternatives"]:
            if alt.get("product_id") not in known:
                problems.append(f"{path.name}：over_budget 引用了不存在的商品")

    # ---------------- 输出 ----------------
    print("fixtures 结构校验")
    print("-" * 60)
    for note in notes:
        print(f"  {note}")
    print()

    if problems:
        print(f"[FAIL] 发现 {len(problems)} 个问题：")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("[ ok ] 全部 fixture 结构完整，且与商品种子自洽")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
