#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A -> B fixtures 生成器 —— 期望值的唯一事实来源。

为什么需要它
    fixtures/*.json 的期望输出如果靠手算，就无法被验证：一旦 B 的实现
    与样例不一致，双方都无法判断是规范有歧义、样例算错，还是实现有 bug。
    本脚本依据 docs/A2B 规范从**成员 D 的真实商品种子**复算全部期望值，
    任何人在本机重跑都能复现。

数据来源
    默认读取 D 的种子：
        data/products.seed.json
    可用 --catalog 指定，或设 HACKU_CATALOG 环境变量。

    fixtures 不再维护自己的测试商品集。两套商品数据必然漂移，而 D 的真实
    种子本来就覆盖了需要的边界：价格边界（hp_0007 正好 HK$300）、缺货
    （hp_0005/0011/0022/0033）、ANC 未知、参数缺失、有线耳机。

用法
    python scripts/gen_fixtures.py            重新生成全部 fixture
    python scripts/gen_fixtures.py --check     只校验生成逻辑
    python scripts/gen_fixtures.py --report    只打印推导

退出码
    0 = 通过；1 = 自检发现问题
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]             # repository root
DEFAULT_CATALOG = ROOT / "data" / "products.seed.json"
OUTDIR = ROOT / "fixtures"
SPEC = "docs/A2B_搜索推荐对比接口规范_v1.1.md"
SETTLEMENT_CURRENCY = "HKD"


def _repo_relative(path: Path) -> str:
    """Write a path the way the repository sees it, not the way this machine does.

    ``_meta.catalog`` records which seed produced the expected values. An
    absolute path reads ``D:\\HackU\\...`` here and something else on every other
    machine, which would undercut the point of a generated fixture: that anyone
    can regenerate it and compare. Falls back to the absolute path only when the
    seed genuinely sits outside the repository.
    """
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)

# ---------------------------------------------------------------------------
# 字段元数据 —— 与 app/contracts/search.py 保持一致
# ---------------------------------------------------------------------------

NUMERIC_ATTRS = ("price_cents", "battery_hours", "wearing_weight_g")
KNOWN_ATTRS = {"price_cents", "battery_hours", "wearing_weight_g", "anc"}

NATURAL_DIR = {
    "price_cents": "lower",
    "wearing_weight_g": "lower",
    "battery_hours": "higher",
    "anc": "higher",
}

#: 约束字段 -> 可对比的商品字段。两个命名空间不同，必须显式映射：
#: 调用方限制的是 max_price_cents，而可对比的字段是 price_cents。
CONSTRAINT_TO_ATTR = {
    "max_price_cents": "price_cents",
    "min_price_cents": "price_cents",
    "max_wearing_weight_g": "wearing_weight_g",
    "min_battery_hours": "battery_hours",
    "anc_required": "anc",
    "connection": "connection",
    "form_factor": "form_factor",
    "brand_allowlist": "brand",
}

#: 可放宽维度白名单。放宽其他字段等于换一个需求，不是让步。
RELAXABLE = ["max_price_cents", "min_price_cents",
             "max_wearing_weight_g", "min_battery_hours", "anc_required"]

USE_CASE_CN = {"commute": "通勤", "study": "学习", "gaming": "游戏",
               "sports": "运动", "calls": "通话", "music": "音乐"}


def fmt(value):
    return int(value) if float(value).is_integer() else value


# ---------------------------------------------------------------------------
# 规范 §5.1 严格过滤（与 D 的 list_candidates 语义一致）
# ---------------------------------------------------------------------------

def passes(product: dict, c: dict) -> bool:
    if c.get("category") and product.get("category") != c["category"]:
        return False
    if c.get("in_stock_only", True) and not (product.get("stock", 0) > 0):
        return False
    if c.get("min_price_cents") is not None:
        value = product.get("price_cents")
        if value is None or value < c["min_price_cents"]:
            return False
    if c.get("max_price_cents") is not None:
        value = product.get("price_cents")
        if value is None or value > c["max_price_cents"]:
            return False
    brands = c.get("brand_allowlist") or []
    if brands and product.get("brand") not in brands:
        return False
    if c.get("connection") is not None and product.get("connection") != c["connection"]:
        return False
    if c.get("form_factor") is not None and product.get("form_factor") != c["form_factor"]:
        return False
    if c.get("anc_required") is True and product.get("anc") is not True:
        return False                     # 未知（null）与 false 都不满足
    if c.get("min_battery_hours") is not None:
        value = product.get("battery_hours")
        if value is None or value < c["min_battery_hours"]:
            return False
    if c.get("max_wearing_weight_g") is not None:
        value = product.get("wearing_weight_g")
        if value is None or value > c["max_wearing_weight_g"]:
            return False
    return True


def strict(products, c):
    return [p for p in products if passes(p, c)]


# ---------------------------------------------------------------------------
# 规范 §5.2 匹配分 / §5.3 排序
# ---------------------------------------------------------------------------

def match_score(product: dict, prefs: dict) -> int:
    hits = len(set(prefs.get("use_cases") or []) & set(product.get("use_cases") or []))
    score = 10 * hits
    if prefs.get("prefer_anc") and product.get("anc") is True:
        score += 5
    return score


def known_key(value, how):
    """未知值恒排在已知值之后。"""
    if value is None:
        return (1, 0)
    if isinstance(value, bool):
        return (0, -int(value)) if how == "desc" else (0, int(value))
    return (0, -value) if how == "desc" else (0, value)


def sort_key(product, prefs, index):
    parts = [-match_score(product, prefs)]

    preset = prefs.get("priority_preset", "best_match")
    if preset == "lower_price":
        parts.append(known_key(product.get("price_cents"), "asc"))
    elif preset == "longer_battery":
        parts.append(known_key(product.get("battery_hours"), "desc"))
    elif preset == "lighter_weight":
        parts.append(known_key(product.get("wearing_weight_g"), "asc"))

    for criterion in prefs.get("criteria") or []:
        attr = criterion["attribute"]
        value = product.get(attr)
        direction = criterion.get("direction", "higher")
        if direction == "closer_to" and criterion.get("target") is not None:
            distance = None if value is None else abs(value - criterion["target"])
            parts.append(known_key(distance, "asc"))
        elif direction == "lower":
            parts.append(known_key(value, "asc"))
        else:
            parts.append(known_key(value, "desc"))

    parts.append(index)                 # 第 4 级：product_id 升序 == 种子顺序
    return tuple(parts)


# ---------------------------------------------------------------------------
# 规范 §6 对比矩阵
# ---------------------------------------------------------------------------

def build_comparison(corpus, dimensions, criteria, sample_fields):
    rows, dropped = [], []
    crit_attrs = [x["attribute"] for x in criteria]
    crit_dir = {x["attribute"]: x.get("direction", "higher") for x in criteria}

    for dim in dimensions:
        if dim not in sample_fields:
            dropped.append(dim)
            continue
        cells = [{"value": p.get(dim), "known": p.get(dim) is not None} for p in corpus]
        known = [cell["value"] for cell in cells if cell["known"]]
        spread = None
        if dim in NUMERIC_ATTRS and cells and all(cell["known"] for cell in cells):
            spread = fmt(max(known) - min(known))
        rows.append({
            "attribute": dim,
            "direction": crit_dir.get(dim, NATURAL_DIR.get(dim, "higher")),
            "values": cells,
            "spread": spread,
            "is_distinguishing": len(set(known)) >= 2,
            "is_criterion": dim in crit_attrs,
        })

    def rank(row):
        if row["is_criterion"] and row["is_distinguishing"]:
            return 0
        if not row["is_criterion"] and row["is_distinguishing"]:
            return 1
        return 2

    order = {dim: i for i, dim in enumerate(dimensions)}
    rows.sort(key=lambda r: (rank(r), order[r["attribute"]]))
    return rows, dropped


# ---------------------------------------------------------------------------
# 候选组装
# ---------------------------------------------------------------------------

def reasons_for(product, prefs):
    """结构化理由：每条都能追溯到 Product 的某个字段。

    返回 MatchReason 形状而不是纯字符串：``field`` 与 ``observed`` 是可追溯性的
    保证——说不出来源的理由不是理由。
    """
    out = []
    used = sorted(set(prefs.get("use_cases") or []) & set(product.get("use_cases") or []))
    if used:
        out.append({"field": "use_cases",
                    "observed": list(product.get("use_cases") or []),
                    "text": "命中用途：" + "、".join(USE_CASE_CN[u] for u in used),
                    "kind": "preference"})
    if product.get("anc") is True:
        out.append({"field": "anc", "observed": True,
                    "text": "支持主动降噪", "kind": "constraint"})
    elif product.get("anc") is False:
        out.append({"field": "anc", "observed": False,
                    "text": "不支持主动降噪", "kind": "spec"})
    if product.get("connection") == "wireless":
        out.append({"field": "connection", "observed": "wireless",
                    "text": "无线连接", "kind": "constraint"})
    elif product.get("connection") == "wired":
        out.append({"field": "connection", "observed": "wired",
                    "text": "有线连接", "kind": "constraint"})
    if product.get("battery_hours") is not None:
        out.append({"field": "battery_hours", "observed": fmt(product["battery_hours"]),
                    "text": f"标称续航 {fmt(product['battery_hours'])} 小时", "kind": "spec"})
    if product.get("wearing_weight_g") is not None:
        out.append({"field": "wearing_weight_g", "observed": fmt(product["wearing_weight_g"]),
                    "text": f"佩戴重量 {fmt(product['wearing_weight_g'])} 克", "kind": "spec"})
    if product.get("price_cents") is not None:
        out.append({"field": "price_cents", "observed": product["price_cents"],
                    "text": f"价格 HK${product['price_cents'] / 100:.2f}", "kind": "constraint"})
    return out


def candidate(product, prefs, violated=None):
    return {
        "product": copy.deepcopy(product),
        "match_score": match_score(product, prefs),
        "reasons": reasons_for(product, prefs),
        "preference_misses": [],
        "violated_fields": violated or [],
    }


# ---------------------------------------------------------------------------
# 规范 §7 放宽提示
# ---------------------------------------------------------------------------

def violations(product, c):
    """返回 (违反的硬约束字段, 数值差距之和)。"""
    found, delta = [], 0.0
    if c.get("category") and product.get("category") != c["category"]:
        found.append("category")
    if c.get("in_stock_only", True) and not (product.get("stock", 0) > 0):
        found.append("in_stock_only")
    for field, attr, broke_high in (
        ("min_price_cents", "price_cents", False),
        ("max_price_cents", "price_cents", True),
        ("min_battery_hours", "battery_hours", False),
        ("max_wearing_weight_g", "wearing_weight_g", True),
    ):
        limit = c.get(field)
        if limit is None:
            continue
        value = product.get(attr)
        broke = value is None or (value > limit if broke_high else value < limit)
        if broke:
            found.append(field)
            if value is not None:
                delta += (value - limit) if broke_high else (limit - value)
    if c.get("connection") is not None and product.get("connection") != c["connection"]:
        found.append("connection")
    if c.get("form_factor") is not None and product.get("form_factor") != c["form_factor"]:
        found.append("form_factor")
    if c.get("anc_required") is True and product.get("anc") is not True:
        found.append("anc_required")
    return found, delta


def relax_hint(products, c, field):
    """单独放宽 field（其余冻结），返回 (to_value, gained, example_id)。"""
    base_ids = {p["product_id"] for p in strict(products, c)}

    def gained_with(new_constraints):
        merged = copy.deepcopy(c)
        merged.update(new_constraints)
        hits = strict(products, merged)
        new = [p for p in hits if p["product_id"] not in base_ids]
        return len(new), (new[0]["product_id"] if new else None)

    if field == "anc_required":
        if c.get(field) is not True:
            return None, 0, None
        count, example = gained_with({"anc_required": False})
        return (False, count, example) if count else (None, 0, None)

    current = c.get(field)
    if current is None:
        return None, 0, None

    ascending = field in ("max_price_cents", "max_wearing_weight_g")
    source_attr = {"max_price_cents": "price_cents",
                   "min_price_cents": "price_cents",
                   "max_wearing_weight_g": "wearing_weight_g",
                   "min_battery_hours": "battery_hours"}[field]

    values = sorted({p[source_attr] for p in products if p.get(source_attr) is not None},
                    reverse=not ascending)
    for value in [v for v in values if (v > current if ascending else v < current)]:
        count, example = gained_with({field: value})
        if count:
            return value, count, example
    return None, 0, None


def build_relax(products, c, prefs, limit):
    hints = []
    for field in RELAXABLE:
        if c.get(field) is None:
            continue
        to_value, gained, example = relax_hint(products, c, field)
        if to_value is None:
            continue
        hints.append({"field": field, "from_value": c[field], "to_value": to_value,
                      "gained_count": gained, "example_product_id": example})
    hints.sort(key=lambda h: (-h["gained_count"], h["field"]))

    scored = []
    for index, product in enumerate(products):
        if passes(product, c):
            continue
        found, delta = violations(product, c)
        if not found:
            continue
        scored.append((len(found), delta, -match_score(product, prefs),
                       product.get("price_cents") or 0, index, product, found))
    scored.sort(key=lambda t: t[:5])

    return {
        "hints": hints,
        "closest_candidates": [candidate(p, prefs, v) for *_, p, v in scored[:limit]],
    }


def over_budget(products, c):
    cap = c.get("max_price_cents")
    if cap is None:
        return []
    relaxed = copy.deepcopy(c)
    relaxed["max_price_cents"] = None
    alts = [{"product_id": p["product_id"], "price_cents": p["price_cents"],
             "delta_cents": p["price_cents"] - cap}
            for p in strict(products, relaxed)
            if p.get("price_cents") is not None and p["price_cents"] > cap]
    alts.sort(key=lambda a: (a["delta_cents"], a["product_id"]))
    return alts


# ---------------------------------------------------------------------------
# 规范 §8 summarize
# ---------------------------------------------------------------------------

def summarize(products, c):
    hits = strict(products, c)
    dimensions = {}
    for attr in NUMERIC_ATTRS:
        values = [p[attr] for p in hits if p.get(attr) is not None]
        dimensions[attr] = {
            "min": fmt(min(values)) if values else None,
            "max": fmt(max(values)) if values else None,
            "known_count": len(values),
            "unknown_count": len(hits) - len(values),
        }
    anc_true = sum(1 for p in hits if p.get("anc") is True)
    anc_false = sum(1 for p in hits if p.get("anc") is False)
    connection, form_factor = {}, {}
    for product in hits:
        connection[product["connection"]] = connection.get(product["connection"], 0) + 1
        form_factor[product["form_factor"]] = form_factor.get(product["form_factor"], 0) + 1
    return {
        "total_matches": len(hits),
        "dimension_stats": dimensions,
        "boolean_stats": {
            "anc": {"true_count": anc_true, "false_count": anc_false,
                    "unknown_count": len(hits) - anc_true - anc_false},
            "connection": connection,
            "form_factor": form_factor,
        },
    }


# ---------------------------------------------------------------------------
# 用例定义
# ---------------------------------------------------------------------------

BASE_CONSTRAINTS = {
    "category": "headphones", "min_price_cents": None, "max_price_cents": 30000,
    "brand_allowlist": [], "connection": "wireless", "form_factor": None,
    "anc_required": True, "min_battery_hours": None, "max_wearing_weight_g": None,
    "in_stock_only": True,
}

CRIT_BATTERY = [{"attribute": "battery_hours", "direction": "higher", "target": None,
                 "priority": "high", "source": "explicit",
                 "evidence_quote": "续航要长一点"}]


def cases():
    out = []

    out.append(dict(
        fid="search.001.normal",
        title="正常路径：完整硬条件 + 续航偏好 + 对比矩阵",
        constraints=copy.deepcopy(BASE_CONSTRAINTS),
        preferences={"use_cases": ["commute"], "criteria": copy.deepcopy(CRIT_BATTERY),
                     "priority_preset": "best_match", "prefer_anc": False},
        comparison={"dimensions": ["price_cents", "battery_hours"], "max_columns": 3},
        limit=3,
        note="全部候选 match_score 相同是正确结果：分数只表达用途命中程度，胜负由 criteria 决定。",
    ))

    # The lightest wireless ANC option at or under HK$300 weighs 9 g, so a 5 g
    # ceiling is genuinely unsatisfiable and the relaxation path is exercised.
    c = copy.deepcopy(BASE_CONSTRAINTS)
    c["max_wearing_weight_g"] = 5
    out.append(dict(
        fid="search.002.relax",
        title="无完美解：严格条件 0 款，产出放宽提示与最接近候选",
        constraints=c,
        preferences={"use_cases": ["commute"],
                     "criteria": [{"attribute": "wearing_weight_g", "direction": "lower",
                                   "target": None, "priority": "medium",
                                   "source": "explicit",
                                   "evidence_quote": "最好 5 克以内"}],
                     "priority_preset": "best_match", "prefer_anc": False},
        comparison={"dimensions": ["price_cents", "wearing_weight_g"], "max_columns": 3},
        limit=3,
        note="核心断言：candidates 与 comparison 为空；放宽结果只出现在 relax_hints。",
    ))

    c = copy.deepcopy(BASE_CONSTRAINTS)
    c["max_price_cents"] = None
    c["connection"] = None
    c["anc_required"] = False
    out.append(dict(
        fid="summarize.001.partial",
        title="部分指定：只给了 category，返回分布统计供 A 生成带数字的追问",
        constraints=c,
        preferences={"use_cases": [], "criteria": [], "priority_preset": "best_match",
                     "prefer_anc": False},
        comparison=None, limit=None, mode="summarize",
        note="summarize 与 search 必须共用同一套过滤逻辑，total_matches 必须一致。",
    ))

    c = copy.deepcopy(BASE_CONSTRAINTS)
    c["max_price_cents"] = None
    c["connection"] = None
    out.append(dict(
        fid="search.003.anc_unknown",
        title="边界：anc=null 不得满足 anc_required=true；lower_price 预设排序",
        constraints=c,
        preferences={"use_cases": ["commute"], "criteria": [],
                     "priority_preset": "lower_price", "prefer_anc": False},
        comparison={"dimensions": ["price_cents"], "max_columns": 5},
        limit=5,
        note="若 anc 为 null 的商品出现在结果里，说明未知被当成了满足。",
    ))

    c = copy.deepcopy(BASE_CONSTRAINTS)
    c["min_price_cents"] = 30000
    c["max_price_cents"] = 20000
    out.append(dict(
        fid="search.004.invalid",
        title="错误：min_price_cents > max_price_cents，必须返回 INVALID_CONSTRAINTS",
        constraints=c,
        preferences={"use_cases": ["commute"], "criteria": copy.deepcopy(CRIT_BATTERY),
                     "priority_preset": "best_match", "prefer_anc": False},
        comparison=None, limit=3, expect_error="INVALID_CONSTRAINTS",
        note="这是错误，不是空结果；不得返回 HTTP 200 + total_matches=0。",
    ))

    out.append(dict(
        fid="search.005.gaps",
        title="缺口：无法评估的判据必须被报告，不得静默忽略",
        constraints=copy.deepcopy(BASE_CONSTRAINTS),
        preferences={"use_cases": ["commute"],
                     "criteria": [{"attribute": "audio_quality", "direction": "higher",
                                   "target": None, "priority": "high",
                                   "source": "explicit",
                                   "evidence_quote": "音质要好的"}],
                     "priority_preset": "best_match", "prefer_anc": False},
        comparison={"dimensions": ["price_cents", "audio_quality"], "max_columns": 3},
        limit=3,
        note="audio_quality 同时进入 unsupported_criteria、dropped_criteria 与 dropped_dimensions。",
    ))

    return out


# ---------------------------------------------------------------------------
# 组装与自检
# ---------------------------------------------------------------------------

def build(case, products, sample_fields):
    c = case["constraints"]
    prefs = case["preferences"]

    if (c.get("min_price_cents") is not None and c.get("max_price_cents") is not None
            and c["min_price_cents"] > c["max_price_cents"]):
        return {
            "ok": False, "data": None,
            "error": {"code": "INVALID_CONSTRAINTS",
                      "message": "min_price_cents 大于 max_price_cents",
                      "details": {"min_price_cents": c["min_price_cents"],
                                  "max_price_cents": c["max_price_cents"]},
                      "retryable": False},
            "request_id": "<由实现填充>",
        }

    if case.get("mode") == "summarize":
        return summarize(products, c)

    hits = [p for p in strict(products, c)]
    hits.sort(key=lambda p: sort_key(p, prefs, products.index(p)))
    limit = case["limit"]
    corpus = hits[:limit]

    crit_attrs = [x["attribute"] for x in prefs.get("criteria") or []]
    applied = [a for a in crit_attrs if a in KNOWN_ATTRS]
    unsupported = [a for a in crit_attrs if a not in KNOWN_ATTRS]

    rows, dropped_dims = ([], [])
    if case.get("comparison") and len(corpus) >= 2:
        rows, dropped_dims = build_comparison(corpus, case["comparison"]["dimensions"],
                                              prefs.get("criteria") or [], sample_fields)

    relax = build_relax(products, c, prefs, limit) if not hits else None

    missing = sorted({row["attribute"] for row in rows
                      if any(not cell["known"] for cell in row["values"])})

    return {
        "total_matches": len(hits),
        "returned": len(corpus),
        "applied_constraints": copy.deepcopy(c),
        "applied_criteria": applied,
        "candidates": [candidate(p, prefs) for p in corpus],
        "comparison": rows,
        "gaps": {
            "unsupported_criteria": unsupported,
            "dropped_criteria": unsupported,
            "dropped_dimensions": sorted(set(dropped_dims)),
            "missing_data_attributes": missing,
            "over_budget_alternatives": over_budget(products, c),
        },
        "relax_hints": relax,
        "suggestions": [f"放宽 {h['field']} 到 {h['to_value']} 可多 {h['gained_count']} 款"
                        for h in (relax["hints"] if relax else [])],
        "currency": SETTLEMENT_CURRENCY,
    }


def self_check(fixture, known_ids):
    """结构性自检，拦下自相矛盾的输出。"""
    problems = []
    expected = fixture["expected"]

    if "dimension_stats" in expected:
        anc = expected["boolean_stats"]["anc"]
        total = anc["true_count"] + anc["false_count"] + anc["unknown_count"]
        if total != expected["total_matches"]:
            problems.append(f"summarize: anc 三项之和 {total} != total_matches")
        for name, stats in expected["dimension_stats"].items():
            if stats["known_count"] + stats["unknown_count"] != expected["total_matches"]:
                problems.append(f"summarize: {name} 计数与 total_matches 不符")
        return problems

    if "total_matches" not in expected:
        return problems

    if expected["returned"] != len(expected["candidates"]):
        problems.append("returned 与 candidates 长度不一致")
    if expected["returned"] > expected["total_matches"]:
        problems.append("returned 大于 total_matches")
    if expected["total_matches"] == 0 and expected["candidates"]:
        problems.append("total_matches=0 但 candidates 非空")
    if expected["total_matches"] == 0 and expected["relax_hints"] is None:
        problems.append("total_matches=0 但缺 relax_hints")
    for row in expected["comparison"]:
        if len(row["values"]) != expected["returned"]:
            problems.append(f"comparison[{row['attribute']}] 与 candidates 长度不一致")
        if any(not v["known"] for v in row["values"]) and row["spread"] is not None:
            problems.append(f"comparison[{row['attribute']}] 有未知值但 spread 非 null")
    for cand in (expected["relax_hints"] or {}).get("closest_candidates", []):
        if not cand["violated_fields"]:
            problems.append("closest_candidate 的 violated_fields 为空")
    for hint in (expected["relax_hints"] or {}).get("hints", []):
        if hint["gained_count"] <= 0:
            problems.append(f"hint {hint['field']} 的 gained_count 不是正数")
    for alt in expected["gaps"]["over_budget_alternatives"]:
        if alt["product_id"] not in known_ids:
            problems.append(f"over_budget 引用了不存在的商品 {alt['product_id']}")
    for cand in expected["candidates"]:
        if cand["product"]["product_id"] not in known_ids:
            problems.append("候选引用了不存在的商品")
        if cand["product"]["currency"] != SETTLEMENT_CURRENCY:
            problems.append("候选币种不是结算币种")
        for reason in cand.get("reasons", []):
            if not reason.get("field"):
                problems.append("推荐理由缺 field，无法追溯到商品字段")
            if "observed" not in reason:
                problems.append(f"推荐理由 {reason.get('field')} 缺 observed")
    return problems


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    parser.add_argument("--report", action="store_true", help="只打印推导，不写文件")
    parser.add_argument("--catalog", default=None, help="商品种子路径")
    args = parser.parse_args()

    catalog_path = Path(args.catalog or os.environ.get("HACKU_CATALOG") or DEFAULT_CATALOG)
    if not catalog_path.exists():
        print(f"[FAIL] 找不到商品种子：{catalog_path}", file=sys.stderr)
        print("       用 --catalog 指定，或设 HACKU_CATALOG", file=sys.stderr)
        return 1

    raw = json.loads(catalog_path.read_text(encoding="utf-8-sig"))
    products = raw["products"] if isinstance(raw, dict) else raw
    known_ids = {p["product_id"] for p in products}
    sample_fields = set(products[0].keys())

    if len(known_ids) != len(products):
        print("[FAIL] 商品 ID 重复", file=sys.stderr)
        return 1
    for product in products:
        if product.get("currency") != SETTLEMENT_CURRENCY:
            print(f"[FAIL] {product['product_id']} 币种不是 {SETTLEMENT_CURRENCY}",
                  file=sys.stderr)
            return 1

    failed = False
    lines = [f"商品种子：{catalog_path}", f"商品数：{len(products)}", ""]

    for case in cases():
        expected = build(case, products, sample_fields)
        fixture = {
            "_meta": {
                "fixture_id": case["fid"],
                "title": case["title"],
                "catalog": _repo_relative(catalog_path),
                "spec": SPEC,
                "generated_by": "scripts/gen_fixtures.py",
                "do_not_edit_by_hand": "期望输出由生成器复算。要改期望值请改种子或规范，然后重新运行生成器。",
                "note": case["note"],
            },
            "request": {"constraints": case["constraints"],
                        "preferences": case["preferences"]},
            "expected": expected,
        }
        if case.get("comparison"):
            fixture["request"]["comparison"] = case["comparison"]
        if case.get("limit") is not None:
            fixture["request"]["limit"] = case["limit"]
        if case.get("mode"):
            fixture["endpoint"] = "POST /api/v1/products/summarize"
        if case.get("expect_error"):
            fixture["expected_error_code"] = case["expect_error"]

        problems = self_check(fixture, known_ids)
        if problems:
            failed = True
            lines.append(f"[FAIL] {case['fid']}")
            lines.extend(f"        - {p}" for p in problems)
        else:
            lines.append(f"[ ok ] {case['fid']}")

        if case.get("expect_error"):
            lines.append(f"        expected_error_code = {case['expect_error']}")
        elif case.get("mode") == "summarize":
            lines.append(f"        total_matches = {expected['total_matches']}")
            lines.append("        anc = "
                         + json.dumps(expected["boolean_stats"]["anc"], ensure_ascii=False))
        else:
            lines.append(f"        total_matches = {expected['total_matches']}, "
                         f"returned = {expected['returned']}")
            if expected["candidates"]:
                lines.append("        顺序 = " + ", ".join(
                    c["product"]["product_id"] for c in expected["candidates"]))
            for hint in (expected["relax_hints"] or {}).get("hints", []):
                lines.append(f"        hint {hint['field']}: {hint['from_value']} -> "
                             f"{hint['to_value']} (+{hint['gained_count']})")
            for near in (expected["relax_hints"] or {}).get("closest_candidates", []):
                lines.append(f"        closest {near['product']['product_id']} "
                             f"violated={near['violated_fields']}")
            if expected["gaps"]["unsupported_criteria"]:
                lines.append(f"        unsupported = {expected['gaps']['unsupported_criteria']}")
        lines.append("")

        if not args.check and not args.report:
            (OUTDIR / f"{case['fid']}.json").write_text(
                json.dumps(fixture, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                # Without this, open() translates \n to os.linesep and Windows
                # writes CRLF while macOS writes LF. .gitattributes pins the
                # repository to LF, so a CRLF working copy would show up as a
                # modification the moment anyone regenerated the fixtures.
                newline="\n",
            )

    print("\n".join(lines))
    if args.check or args.report:
        print("（--check/--report：未写入文件）")
    else:
        print(f"已写入 {OUTDIR} 下 {len(cases())} 个 fixture。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
