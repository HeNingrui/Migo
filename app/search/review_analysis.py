"""Explainable review screening using public text only, never evaluation labels.

Signals flag possible coordination, not proof of fraud. Raw arithmetic star
averages always retain every review. The ranking estimate is separate.
"""
from collections import Counter
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import re

EXTREME = re.compile(
    r"absolutely perfect|premium quality|zero problems|no compromises|"
    r"nothing to fault|in every respect|best (?:ever|headphones|earphones)|"
    r"perfect in every|flawless|unbeatable|life[- ]changing|"
    r"total (?:garbage|trash|junk)|complete (?:garbage|trash|junk)|"
    r"utter (?:garbage|trash)|worthless|scam|garbage|rubbish|"
    r"worst (?:ever|headphones|earphones)|absolute (?:trash|junk)|"
    r"never buy|stay away|avoid at all costs|waste of money|"
    r"disaster|superb|exceptional|outstanding|inferior",
    re.I,
)
PUSH = re.compile(
    r"buy with confidence|everyone should|don't hesitate|must[- ]buy|"
    r"buy (?:it|them|now)|grab (?:it|them)|highly recommend|"
    r"full marks|no question|don't waste|do not buy|never buy|"
    r"stay away|avoid|save your money|not worth|skip (?:it|them)|"
    r"no excuses|no debate|period|end of story",
    re.I,
)


def _grams(text):
    words = re.findall(r"[a-z]+", text.lower())
    return set(zip(words, words[1:]))


def _overlap(left, right):
    return len(left & right) / len(left | right) if left | right else 0.0


def analyze_reviews(product_id, reviews):
    if not reviews:
        return {"product_id": product_id, "review_count": 0, "average_rating": None,
                "rating_scale": 5.0, "suspicious_count": 0, "suspicious_review_ids": [],
                "review_score": None, "score_source": "no_reviews", "findings": [],
                "method": "text_coordination_v1", "limitations": "Signals are not proof of paid reviews."}
    texts = [r["title"] + ". " + r["text"] for r in reviews]
    grams = [_grams(t) for t in texts]
    token_counts = Counter(g for gs in grams for g in gs)
    findings = []
    flagged = set()
    for index, review in enumerate(reviews):
        text = texts[index]
        extreme = list(EXTREME.finditer(text))
        push = list(PUSH.finditer(text))
        peers = [j for j, other in enumerate(reviews) if j != index
                 and (other["rating"] >= 4.8 if review["rating"] >= 4.8 else other["rating"] <= 1.2)
                 and _overlap(grams[index], grams[j]) >= 0.12]
        extreme_rating = review["rating"] >= 4.8 or review["rating"] <= 1.2
        repeated = sorted(g for g in grams[index] if token_counts[g] >= 3)
        signals = []
        if extreme_rating and extreme:
            signals.append({"signal": "absolute_claim_with_extreme_rating",
                            "evidence": [m.group() for m in extreme[:4]]})
        if push:
            signals.append({"signal": "promotional_or_dismissive_call_to_action",
                            "evidence": [m.group() for m in push[:3]]})
        if len(peers) >= 2 and extreme_rating:
            signals.append({"signal": "similar_language_and_rating_cluster",
                            "peer_review_ids": [reviews[j]["review_id"] for j in peers],
                            "repeated_phrases": [" ".join(g) for g in repeated[:8]]})
        likely = extreme_rating and bool(extreme) and (bool(push) or len(peers) >= 2)
        confidence = min(0.95, 0.55 + 0.12 * len(signals)) if likely else 0.15 if signals else 0.05
        if likely:
            flagged.add(review["review_id"])
        findings.append({"review_id": review["review_id"],
                         "assessment": "potentially_coordinated" if likely else "no_strong_signal",
                         "confidence": round(confidence, 2), "signals": signals})
    total = sum(Decimal(str(r["rating"])) for r in reviews)
    mean = (total / len(reviews)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    retained = [r for r in reviews if r["review_id"] not in flagged]
    # No trustworthy remainder means no ranking estimate, rather than 0 or 0.6.
    score = sum(r["rating"] for r in retained) / len(retained) / 5 if retained else None
    return {"product_id": product_id, "review_count": len(reviews), "average_rating": float(mean),
            "rating_scale": 5.0, "suspicious_count": len(flagged),
            "suspicious_review_ids": sorted(flagged),
            "review_score": round(score, 4) if score is not None else None,
            "score_source": "text_screened_rating" if flagged else "all_review_rating",
            "findings": findings, "method": "text_coordination_v1",
            "limitations": "Text signals are not proof of paid reviews; false positives and misses are possible. All ratings remain in the displayed mean."}
