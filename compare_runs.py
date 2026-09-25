#!/usr/bin/env python3
"""Compare two benchmark runs pair by pair.

Usage:
    python3 compare_runs.py run1.jsonl run2.jsonl
"""
import json
import sys


def load(path):
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f]
    return {r["pair_index"]: r for r in rows if "error" not in r}


a, b = load(sys.argv[1]), load(sys.argv[2])
common = sorted(set(a) & set(b))
if not common:
    sys.exit("No successfully scored pairs in common.")

flips = [i for i in common if a[i]["pred"] != b[i]["pred"]]
drift = [abs(a[i]["p_same"] - b[i]["p_same"]) for i in common]

print(f"pairs compared:              {len(common)}")
print(f"answers that changed:        {len(flips)}")
print(f"largest probability change:  {max(drift):.6f}")
print(f"average probability change:  {sum(drift) / len(drift):.6f}")
for i in flips:
    print(f"  #{i}: {a[i]['pred']} (p={a[i]['p_same']:.4f}) -> "
          f"{b[i]['pred']} (p={b[i]['p_same']:.4f})   truth={a[i]['judgement']}")
