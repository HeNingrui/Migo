"""B scoring functions adapted from headphone-agent a034f97.
No catalog, model credentials, or payment authority is copied from that project.
"""
from typing import Any
FEATURE_WEIGHT=0.5
PRICE_WEIGHT=0.3
REVIEW_WEIGHT=0.2
CRITERION_FIELDS={"price_cents","battery_hours","wearing_weight_g","anc","estimated_delivery_days"}
NUMERIC_FIELDS={"price_cents","battery_hours","wearing_weight_g","estimated_delivery_days"}

def _ranking_key(
    product: dict[str, Any],
    preferences: dict[str, Any],
    score: dict[str, Any],
    eligible: list[dict[str, Any]],
) -> tuple[Any, ...]:
    """Apply policy ordering: ordered preference tiers, then composite score.

    A preference is deliberately represented before the weighted score.  This
    is the policy's "minimum relaxation" rule: a product that satisfies the
    first preference stays ahead of one that misses it, even when the latter
    happens to be cheaper or has better reviews.  Numeric criteria use the
    deterministic feature score for that dimension; missing values sort last.
    """
    preference_key: list[Any] = []
    for criterion in preferences["criteria"]:
        if criterion["attribute"] not in CRITERION_FIELDS:
            continue
        criterion_score = _criterion_score(product, criterion, eligible)
        preference_key.extend((1, 0.0) if criterion_score is None else (0, -criterion_score))
    requested_uses = preferences.get("use_cases", [])
    preference_key.extend(0 if use_case in product.get("use_cases", []) else 1 for use_case in requested_uses)
    if preferences.get("prefer_anc"):
        preference_key.append(0 if product.get("anc") is True else 1)
    preset = preferences.get("priority_preset", "best_match")
    if preset == "lower_price":
        preference_key.append(product.get("price_cents") if product.get("price_cents") is not None else float("inf"))
    elif preset == "longer_battery":
        battery = product.get("battery_hours")
        preference_key.append(-battery if battery is not None else float("inf"))
    elif preset == "lighter_weight":
        weight = product.get("wearing_weight_g")
        preference_key.append(weight if weight is not None else float("inf"))
    return (*preference_key, -score["composite"], product["product_id"])

def _score_products(
    products: list[dict[str, Any]],
    preferences: dict[str, Any],
    review_scores_by_id: dict[str, float],
) -> dict[str, dict[str, Any]]:
    prices = [product["price_cents"] for product in products if product.get("price_cents") is not None]
    low_price = min(prices) if prices else 0
    high_price = max(prices) if prices else 0
    result: dict[str, dict[str, Any]] = {}
    for product in products:
        criterion_values = [
            _criterion_score(product, criterion, products)
            for criterion in preferences.get("criteria", [])
            if criterion["attribute"] in CRITERION_FIELDS
        ]
        known_criteria = [value for value in criterion_values if value is not None]
        use_cases = preferences.get("use_cases", [])
        use_case_score = (
            sum(use_case in product.get("use_cases", []) for use_case in use_cases) / len(use_cases)
            if use_cases
            else None
        )
        feature_values = [*known_criteria]
        if use_case_score is not None:
            feature_values.append(use_case_score)
        if preferences.get("prefer_anc"):
            anc_score = 1.0 if product.get("anc") is True else 0.0 if product.get("anc") is False else None
            if anc_score is not None:
                feature_values.append(anc_score)
        feature_score = sum(feature_values) / len(feature_values) if feature_values else 1.0
        price = product.get("price_cents")
        price_score = 1.0 if price is None or high_price == low_price else (high_price - price) / (high_price - low_price)
        review_score = review_scores_by_id.get(product["product_id"])
        components = [(FEATURE_WEIGHT, feature_score), (PRICE_WEIGHT, price_score)]
        if review_score is not None:
            components.append((REVIEW_WEIGHT, review_score))
        denominator = sum(weight for weight, _value in components)
        composite = sum(weight * value for weight, value in components) / denominator if denominator else 0.0
        result[product["product_id"]] = {
            "feature": feature_score,
            "price": price_score,
            "review": review_score,
            "composite": composite,
        }
    return result

def _criterion_known(product: dict[str, Any], criterion: dict[str, Any]) -> bool:
    attribute = criterion["attribute"]
    return product.get(attribute) is not None

def _criterion_score(product: dict[str, Any], criterion: dict[str, Any], products: list[dict[str, Any]]) -> float | None:
    value = product.get(criterion["attribute"])
    if value is None:
        return None
    attribute = criterion["attribute"]
    if attribute == "anc":
        return (1.0 if value is True else 0.0) if criterion.get("direction", "higher") == "higher" else (0.0 if value is True else 1.0)
    if attribute not in NUMERIC_FIELDS:
        return 1.0
    values = [item.get(attribute) for item in products if isinstance(item.get(attribute), (int, float))]
    if not values:
        return None
    low, high = min(values), max(values)
    if low == high:
        return 1.0
    direction = criterion.get("direction", "higher")
    if direction == "higher":
        return (float(value) - low) / (high - low)
    if direction == "lower":
        return (high - float(value)) / (high - low)
    distance = abs(float(value) - float(criterion["target"]))
    max_distance = max(abs(float(candidate) - float(criterion["target"])) for candidate in values)
    return 1.0 if max_distance == 0 else 1.0 - distance / max_distance
