"""Score classifier predictions against the manual labels."""
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
CODE = {"V": "vegetation", "O": "open_land", "B": "built_up", "W": "water"}
labels = {}
for f in sorted(HERE.glob("labels_sheet*.txt")):
    for line in f.read_text().splitlines():
        if line and not line.startswith("#"):
            i, c = line.split()
            labels[int(i)] = c
preds = {p["id"]: p for p in json.loads((HERE / "landcover_predictions.json").read_text())}
classes = ["vegetation", "open_land", "built_up", "water"]
conf = Counter()
per_site = Counter(); per_site_ok = Counter()
excluded = 0
for i, lab in labels.items():
    if lab == "M":
        excluded += 1
        continue
    t, p = CODE[lab], preds[i]["predicted"]
    conf[(t, p)] += 1
    per_site[preds[i]["site"]] += 1
    per_site_ok[preds[i]["site"]] += t == p
n = sum(conf.values()); ok = sum(v for (t, p), v in conf.items() if t == p)
# Cohen's kappa
pe = sum(sum(conf[(c, q)] for q in classes) * sum(conf[(q, c)] for q in classes) for c in classes) / n ** 2
kappa = (ok / n - pe) / (1 - pe)
res = {"labelled": len(labels), "mixed_excluded": excluded, "evaluated": n,
       "overall_accuracy": round(ok / n, 3), "cohen_kappa": round(kappa, 3),
       "per_site_accuracy": {s: f"{per_site_ok[s]}/{per_site[s]}" for s in per_site},
       "confusion_true_rows_pred_cols": {t: {p: conf[(t, p)] for p in classes} for t in classes},
       "producer_accuracy": {c: (round(conf[(c, c)] / s, 3) if (s := sum(conf[(c, q)] for q in classes)) else None) for c in classes},
       "user_accuracy": {c: (round(conf[(c, c)] / s, 3) if (s := sum(conf[(q, c)] for q in classes)) else None) for c in classes},
       "errors": [{"id": i, "true": CODE[l], "pred": preds[i]["predicted"], "fractions": preds[i]["fractions"]}
                  for i, l in labels.items() if l != "M" and CODE[l] != preds[i]["predicted"]]}
(HERE / "landcover_results.json").write_text(json.dumps(res, indent=1))
print(json.dumps(res, indent=1))
