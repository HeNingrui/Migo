"""Offline evaluation only: labels are never passed to the runtime detector."""
import json
from pathlib import Path
import sys
import zipfile
from collections import defaultdict, Counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.search.review_analysis import analyze_reviews


def evaluate(predictions, truth):
    answers = {r['review_id']: r['is_manipulated'] for r in truth['labels']}
    if not answers or any(type(v) is not bool for v in answers.values()):
        raise ValueError('Ground truth must contain nonempty boolean answers')
    if not isinstance(predictions, list):
        raise ValueError('Predictions must be an array')
    by_id = {}
    for row in predictions:
        if not isinstance(row, dict) or not isinstance(row.get('review_id'), str) or type(row.get('is_manipulated')) is not bool:
            raise ValueError('Each prediction requires review_id and boolean is_manipulated')
        if row['review_id'] in by_id:
            raise ValueError('Duplicate prediction review_id')
        by_id[row['review_id']] = row['is_manipulated']
    if set(by_id) != set(answers):
        raise ValueError(f'Predictions must cover exactly all reviews: missing={len(set(answers)-set(by_id))}, unknown={len(set(by_id)-set(answers))}')
    counts = Counter('tp' if answers[k] and by_id[k] else 'fn' if answers[k] else 'fp' if by_id[k] else 'tn' for k in answers)
    tp, fp, fn, tn = (counts[k] for k in ('tp','fp','fn','tn'))
    precision = tp/(tp+fp) if tp+fp else 0.0
    recall = tp/(tp+fn) if tp+fn else 0.0
    return {'review_count':len(answers), 'true_positive':tp, 'false_positive':fp,
            'false_negative':fn, 'true_negative':tn, 'precision':round(precision,4),
            'recall':round(recall,4), 'f1':round(2*precision*recall/(precision+recall),4) if precision+recall else 0.0,
            'accuracy':round((tp+tn)/len(answers),4),
            'note':'Synthetic benchmark only. A strong opinion or low rating alone is not evidence of manipulation.'}


def evaluate_dataset():
    records = json.loads((ROOT / "data/reviews.seed.json").read_text(encoding="utf-8"))
    grouped = defaultdict(list)
    for review in records:
        grouped[review["product_id"]].append(review)
    # Make predictions BEFORE opening the offline label archive.
    analyses = [analyze_reviews(pid,reviews) for pid,reviews in sorted(grouped.items())]
    predicted = {rid for a in analyses for rid in a["suspicious_review_ids"]}
    with zipfile.ZipFile(ROOT / "data/headphone-catalog-v4-data.zip") as archive:
        truth = json.loads(archive.read("evaluation/reviews.ground_truth.json"))
    actual = {r["review_id"] for r in truth["labels"] if r["is_manipulated"]}
    actual_products = {p["product_id"] for p in truth["affected_products"]}
    predicted_products = {a["product_id"] for a in analyses if a["suspicious_count"]}
    tp,fp,fn = len(predicted&actual),len(predicted-actual),len(actual-predicted)
    precision = tp/(tp+fp) if tp+fp else 0
    recall = tp/(tp+fn) if tp+fn else 0
    return {"dataset_id": truth["dataset_id"], "review_count":len(records), "method":"text_coordination_v1",
        "true_positives":tp,"false_positives":fp,"false_negatives":fn,
        "precision":round(precision,4),"recall":round(recall,4),
        "f1":round(2*precision*recall/(precision+recall),4) if precision+recall else 0,
        "affected_products_identified":len(actual_products&predicted_products),
        "affected_products_total":len(actual_products),"false_positive_products":len(predicted_products-actual_products),
        "limitations":"One synthetic development dataset; no held-out or real-world validation. Findings are indicators, not proof.",
        "labels_used_as_detector_input":False}


if __name__ == "__main__":
    result=evaluate_dataset()
    target=ROOT / "docs/review-evaluation.json"
    target.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(result,indent=2))
