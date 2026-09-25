#!/usr/bin/env python3
"""
Jev on the OpenSanctions Pairs benchmark (binary, zero-shot).

Paper : "OpenSanctions Pairs" (arXiv 2603.11051), Table 3
Code  : github.com/chansmi/OSINT_entity_resolution (pinned to commit a1f62b5)
Data  : data/samples/sample_{1000,10000}.json from that repo (CC-BY-NC 4.0)

Test sets. The repo reserves the first 200 pairs of each sample for prompt
development and evaluates on the rest:
  --sample 10000 -> 9,800 test pairs  (Table 3 rows on these pairs: GPT-4o, GPT-3.5 Turbo,
                                        Llama-3.1-8B, GPT-5 Nano, Claude Opus 4.5)
  --sample 1000  ->   800 test pairs  (Table 3 rows on these pairs: GPT-5.2 Pro 0-shot,
                                        Llama-3.1-8B 4-shot)
The paper states sizes only for its prompt-optimized rows; the others were checked
against its published precision / recall / accuracy. Remaining rows use other samples
or subsets of unknown size and are shown for reference only.

Same as the paper's LLM baselines:
  * entity text: the repo's format_entity (llm_zeroshot.py), copied verbatim -
    13 fields, same value caps; everything else in the record is not shown
  * conflict-first instructions ("DEFAULT is POSITIVE"), one pair per request
  * F1 against the analyst labels
Adapted, not identical:
  * the instructions are reworded as a single yes/no question
  * the paper asks for {classification, confidence, reasoning}; Jev returns a
    probability that the records are the same entity. Match = probability >= 0.5,
    fixed before running. No tuning, no examples.

Usage (Python 3.9+, standard library only):
    export TYPESAFE_API_KEY=...
    python3 jev_opensanctions_benchmark.py --limit 20             # quick check
    python3 jev_opensanctions_benchmark.py                        # 9,800 pairs
    python3 jev_opensanctions_benchmark.py --sample 1000          # 800 pairs
"""
import argparse
import json
import math
import os
import platform
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from statistics import median

REPO_COMMIT = "a1f62b56c4bae8f372c38bff6ff0a422a3e9f1a3"
DATA_URL = ("https://raw.githubusercontent.com/chansmi/OSINT_entity_resolution/"
            f"{REPO_COMMIT}/data/samples/sample_{{n}}.json")
API_URL = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai") + "/v1/systemone"
DEV_PAIRS = 200
MAX_ATTEMPTS = 6

# Input-token price in USD per million tokens (output tokens are free).
# Source: TypeSafe launch pricing for Jev 1.13. Check before quoting other models.
PRICE_PER_M_INPUT = {"jev-1.13.0": 0.042}

# Paper Table 3: (model, configuration, F1, test pairs; None = subset of unknown size)
PAPER_TABLE3 = [
    ("nomenklatura RegressionV1", "rules", 91.33, 8000),
    ("Llama-3.1-8B", "0-shot", 94.05, 9800),
    ("Llama-3.1-8B", "0-shot (opt)", 95.94, 9800),
    ("Llama-3.1-8B", "4-shot (opt)", 95.64, 800),
    ("DeepSeek-R1-Distill-Qwen-14B", "0-shot", 97.76, 1960),
    ("DeepSeek-R1-Distill-Qwen-14B", "0-shot (opt)", 98.23, 3680),
    ("GPT-3.5 Turbo", "0-shot", 94.49, 9800),
    ("GPT-4o", "0-shot", 98.95, 9800),
    ("GPT-5 Nano", "0-shot", 95.24, 9800),
    ("GPT-5.2 Pro", "0-shot", 98.53, 800),
    ("GPT-5.2 Pro", "4-shot", 98.75, None),
    ("Claude 3 Haiku", "0-shot", 92.68, None),
    ("Claude 3.7 Sonnet", "0-shot", 97.50, None),
    ("Claude Opus 4.5", "0-shot", 95.45, 9800),
]

# ---------------------------------------------------------------------------
# 1. The paper's entity text - copied from scripts/baselines/llm_zeroshot.py
# ---------------------------------------------------------------------------
FIELD_SPECS = [  # (property, label, max values shown, separator)
    ("name", "Names", 8, ", "),
    ("alias", "Aliases", 4, ", "),
    ("birthDate", "Birth Date", None, ", "),
    ("birthPlace", "Birth Place", 2, ", "),
    ("nationality", "Nationality", None, ", "),
    ("country", "Country", None, ", "),
    ("address", "Address", 3, "; "),
    ("idNumber", "ID Numbers", 3, ", "),
    ("passportNumber", "Passport", 2, ", "),
    ("gender", "Gender", None, ", "),
    ("position", "Position", 2, ", "),
    ("firstName", "First Name", None, ", "),
    ("lastName", "Last Name", None, ", "),
]


def format_entity(entity):
    props = entity.get("properties", {})
    lines = [f"Type: {entity.get('schema', 'Unknown')}"]
    for key, label, limit, sep in FIELD_SPECS:
        if key in props:
            values = props[key][:limit] if limit else props[key]
            lines.append(f"{label}: {sep.join(values)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. The question - the paper's conflict-first instructions as one yes/no.
#    (wording follows the repository's llm_zeroshot.py, which says "person or
#    organization" for every record type; the paper's appendix differs slightly.)
# ---------------------------------------------------------------------------
QUESTION = {
    "same_entity": {
        "type": "noul",
        "instructions": (
            "You are an expert entity resolution system for sanctions screening. "
            "Do `entity_a` and `entity_b` refer to the same real-world person or "
            "organization? Your primary task is to identify CONFLICTS, not "
            "similarities. Name variations (transliterations, nicknames, titles) "
            "are common. Missing fields are normal - absence of data is NOT "
            "evidence of difference. The same entity often appears across multiple "
            "sources with variations. Look for CONTRADICTORY evidence (different "
            "dates, conflicting IDs, incompatible attributes). If no contradictions "
            "are found, they are the same entity. The DEFAULT is the same entity "
            "unless you find proof of difference."
        ),
        "criteria": {
            "true": "No contradictions found: same entity.",
            "false": "Explicit contradiction found: different entities.",
        },
    }
}


# ---------------------------------------------------------------------------
# 3. Calling Jev. Never exits: returns either a result or an error record.
# ---------------------------------------------------------------------------
def ask_jev(pair, model, api_key):
    body = json.dumps({
        "model": model,
        "state": {"entity_a": format_entity(pair["left"]),
                  "entity_b": format_entity(pair["right"])},
        "questions": QUESTION,
    }, ensure_ascii=False).encode("utf-8")
    total_start = time.perf_counter()
    error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = urllib.request.Request(API_URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read())
            p = float(result["answers"]["same_entity"]["noul"])
            tokens = result["usage"]["input_tokens"]
            if not (math.isfinite(p) and 0.0 <= p <= 1.0):
                raise ValueError(f"invalid probability {p!r}")
            return {"p_same": p, "input_tokens": int(tokens), "attempts": attempt,
                    "last_attempt_ms": (time.perf_counter() - start) * 1000,
                    "total_ms": (time.perf_counter() - total_start) * 1000}
        except urllib.error.HTTPError as err:
            error = f"HTTP {err.code}: {err.read().decode(errors='replace')[:200]}"
            if err.code not in (429, 500, 502, 503, 529):
                break  # not retryable (bad request, auth, ...)
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError,
                json.JSONDecodeError) as err:
            error = f"{type(err).__name__}: {err}"
        if attempt < MAX_ATTEMPTS:
            time.sleep(0.5 * 2 ** (attempt - 1))
    return {"error": error, "attempts": attempt,
            "total_ms": (time.perf_counter() - total_start) * 1000}


# ---------------------------------------------------------------------------
# 4. Scoring
# ---------------------------------------------------------------------------
LABELS = {"positive", "negative"}


def scores(rows):
    for r in rows:
        if r["judgement"] not in LABELS or r["pred"] not in LABELS:
            raise ValueError(f"invalid label in pair {r['pair_index']}")
    tp = sum(r["pred"] == "positive" and r["judgement"] == "positive" for r in rows)
    fp = sum(r["pred"] == "positive" and r["judgement"] == "negative" for r in rows)
    fn = sum(r["pred"] == "negative" and r["judgement"] == "positive" for r in rows)
    tn = sum(r["pred"] == "negative" and r["judgement"] == "negative" for r in rows)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": precision, "recall": recall, "f1": f1}


def record_type(pair):
    a, b = pair["left"]["schema"], pair["right"]["schema"]
    if a == b == "Person":
        return "people"
    if {a, b} <= {"Company", "Organization", "LegalEntity"}:
        return "organizations"
    return "other (Occupancy, Succession, ...)"


def p95(values):  # nearest-rank definition
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def main():
    parser = argparse.ArgumentParser(description="Jev on OpenSanctions Pairs")
    parser.add_argument("--sample", type=int, choices=[1000, 10000], default=10000)
    parser.add_argument("--model", default="jev-1.13.0")
    parser.add_argument("--limit", type=int, help="only the first N test pairs (N >= 1)")
    parser.add_argument("--workers", type=int, default=12, help="parallel requests")
    parser.add_argument("--out", default="jev_results.jsonl")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    api_key = os.environ.get("TYPESAFE_API_KEY") or sys.exit("Set TYPESAFE_API_KEY first.")

    url = DATA_URL.format(n=args.sample)
    print(f"Downloading sample_{args.sample}.json (commit {REPO_COMMIT[:7]}) ...")
    with urllib.request.urlopen(url) as response:
        pairs = json.loads(response.read())["pairs"]
    test = pairs[DEV_PAIRS:]
    if args.limit:
        test = test[:args.limit]
    print(f"Sending {len(test)} test pairs to {args.model} ({args.workers} parallel) ...")

    # Run, writing each result to disk as soon as it arrives.
    rows, failures = [], []
    wall_start = time.perf_counter()
    with open(args.out, "w", encoding="utf-8") as out, \
            ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(ask_jev, p, args.model, api_key): i
                   for i, p in enumerate(test)}
        for done, future in enumerate(as_completed(futures), 1):
            i = futures[future]
            pair, ans = test[i], future.result()
            row = {"pair_index": DEV_PAIRS + i, "type": record_type(pair),
                   "left_id": pair["left"]["id"], "right_id": pair["right"]["id"],
                   "judgement": pair["judgement"], **ans}
            if "error" in ans:
                failures.append(row)
            else:
                row["pred"] = "positive" if ans["p_same"] >= 0.5 else "negative"
                rows.append(row)
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            if done % 500 == 0:
                print(f"  {done}/{len(test)}")
    wall = time.perf_counter() - wall_start

    if failures:
        print(f"\nWARNING: {len(failures)} pairs failed (see 'error' in {args.out}); "
              "scores below exclude them.")
    if not rows:
        sys.exit("No successful results to score.")
    rows.sort(key=lambda r: r["pair_index"])

    # ---- report -----------------------------------------------------------
    s = scores(rows)
    print(f"\n=== Jev ({args.model}) on {len(rows)} test pairs from sample_{args.sample} ===")
    print(f"F1 {100 * s['f1']:.2f}   precision {100 * s['precision']:.2f}   "
          f"recall {100 * s['recall']:.2f}")
    print(f"TP {s['tp']}  FP {s['fp']}  FN {s['fn']}  TN {s['tn']}")

    print("\nBy record type:")
    for kind in ("people", "organizations", "other (Occupancy, Succession, ...)"):
        subset = [r for r in rows if r["type"] == kind]
        if subset:
            print(f"  {kind:36s} n={len(subset):5d}  F1 {100 * scores(subset)['f1']:.2f}")

    tokens = sum(r["input_tokens"] for r in rows)
    price = PRICE_PER_M_INPUT.get(args.model)
    total_ms = [r["total_ms"] for r in rows]
    retried = sum(r["attempts"] > 1 for r in rows)
    print(f"\nInput tokens per pair  {tokens / len(rows):.0f}")
    if price is None:
        print(f"Cost                   no price on file for {args.model}")
    else:
        cost = tokens * price / 1e6
        print(f"Estimated cost         ${cost:.4f} for this run, "
              f"${cost / len(rows) * 1e6:.2f} per million pairs "
              f"(recorded usage x ${price}/M input tokens; failed attempts not counted)")
    print(f"Latency per pair       median {median(total_ms):.0f} ms, "
          f"p95 {p95(total_ms):.0f} ms (includes retries; {retried} pairs retried)")
    print(f"Wall time              {wall:.1f} s with {args.workers} parallel requests")

    n_test = len(pairs) - DEV_PAIRS
    full_run = len(rows) == n_test
    print("\nPaper Table 3 (published F1). '*' = same test pairs as this run:")
    for model, config, f1, n in PAPER_TABLE3:
        mark = "*" if full_run and n == n_test else " "
        print(f"  {mark} {model:30s} {config:13s} {f1:6.2f}   n={n or '?'}")
    print(f"  * {'Jev ' + args.model:30s} {'0-shot':13s} {100 * s['f1']:6.2f}   "
          f"n={len(rows)}" + ("" if full_run else "   (partial run)"))

    meta = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": args.model, "sample": args.sample, "data_url": url,
        "repo_commit": REPO_COMMIT, "test_pairs": len(test), "scored": len(rows),
        "failed": len(failures), "threshold": 0.5, "workers": args.workers,
        "question": QUESTION, "python": platform.python_version(),
        "results": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in s.items()},
        "input_tokens": tokens, "wall_seconds": round(wall, 1),
    }
    meta_path = args.out.rsplit(".", 1)[0] + "_meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"\nPer-pair results: {args.out}\nRun metadata:     {meta_path}")


if __name__ == "__main__":
    main()
