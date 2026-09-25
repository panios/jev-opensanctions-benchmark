# Jev on the OpenSanctions Pairs benchmark

Can a small, cheap judgement model match frontier LLMs at entity resolution, deciding
whether two records describe the same person or organization?

This repo runs [Jev](https://docs.typesafe.ai) (TypeSafe AI, `jev-1.13.0`) on the
**OpenSanctions Pairs** benchmark ([arXiv 2603.11051](https://arxiv.org/abs/2603.11051),
[code](https://github.com/chansmi/OSINT_entity_resolution)) using the paper's own test
pairs, the same record text, and the same conflict-first instructions.

The paper takes pairs of OpenSanctions records that analysts have already ruled on
(merge / don't merge) and asks whether general-purpose LLMs can reproduce those decisions
better than nomenklatura's own `RegressionV1` matcher. Each LLM sees both records as the
same 13-field text and is scored as F1 against the analyst judgements. Across roughly ten
models (Llama-3.1-8B, DeepSeek-R1-Distill, GPT-3.5/4o/5, Claude), the frontier ones beat the
rules-based baseline by a wide margin: 91.33 F1 for `RegressionV1` versus 98.95 for GPT-4o.

## Result

**Same 9,800 test pairs** (`sample_10000.json`, pairs 200–9,999), zero-shot:

| Model | Configuration | F1 |
|---|---|---|
| GPT-4o | 0-shot | 98.95 |
| **Jev 1.13** | **0-shot, no tuning** | **98.87** (95% CI 98.70–99.04) |
| Llama-3.1-8B | 0-shot, DSPy-optimized prompt | 95.94 |
| Claude Opus 4.5 | 0-shot | 95.45 |
| GPT-5 Nano | 0-shot | 95.24 |
| GPT-3.5 Turbo | 0-shot | 94.49 |
| Llama-3.1-8B | 0-shot | 94.05 |

Baselines are the published numbers from the paper's Table 3. The paper states test-set sizes
only for its prompt-optimized rows; for the rows above, the published precision, recall and
accuracy are consistent with exactly these 9,800 pairs and not with the full 10,000 (Claude
3.7 Sonnet, 97.50, fits neither and is left out). Jev's row comes from this repo. On these
pairs Jev makes 171 errors to GPT-4o's implied 159, 12 pairs apart: not distinguishable at
this sample size.

**Same 800 test pairs** (`sample_1000.json`, pairs 200–999): Jev **99.10**, GPT-5.2 Pro
0-shot 98.53 (its 4-shot 98.75 was scored on a different subset). 604 of these 800 pairs also
appear in the 9,800 set, so this is not an independent result.

| Jev, 9,800 pairs | |
|---|---|
| Precision / recall | 98.52 / 99.22 (TP 7,477 · FP 112 · FN 59 · TN 2,152) |
| F1 by record type | people 98.79 (n=3,677) · organizations 96.84 (n=2,285) · other 99.57 (n=3,838: 3,322 auto-merge relationship pairs, all correct, and 516 Position, Vessel, Security and cross-schema pairs at 96.11) |
| F1 excluding blank-prompt pairs | 97.98 (n=6,478), see caveats |
| Input tokens per pair | 643 |
| Cost | $0.26 for the run, **$27 per million pairs** (Jev list price $0.042 per million input tokens; output tokens free) |
| Latency | median 742 ms, p95 814 ms per pair (one pair per request, 12 in parallel) |
| Stability | two runs of the 800 pairs: **0 answers changed** (largest probability change 0.06) |
| Failures | 0 of 9,800 requests |

Full output: [`results/run_9800_summary.txt`](results/run_9800_summary.txt).

## What is identical to the paper, and what is not

Identical:
- **Test pairs**: the paper's samples, downloaded from its repo at a pinned commit; the first 200 pairs of each sample are the paper's development set and are not used.
- **Record text**: the paper's `format_entity` function, copied verbatim. Each record is shown as `Type:` plus up to 13 fields (names, aliases, birth date and place, nationality, country, address, ID and passport numbers, gender, position, first and last name).
- **Instructions**: the paper's conflict-first rules ("look for contradictions; missing fields are not evidence of difference; the default is the same entity"), in the wording of the repository's `llm_zeroshot.py`, which differs slightly from the paper's appendix.
- **Metric**: F1 against the OpenSanctions analyst labels.

Adapted:
- The instructions are asked as one yes/no question. Jev returns the probability that the two records are the same entity; a pair counts as a match at **≥ 0.5**, fixed before running.
- The paper's models return `{classification, reasoning}` (the repository's prompt also asks for a confidence); Jev returns a probability only.
- No prompt optimization, no examples, no tuning on the test pairs.

## Run it yourself

Python 3.9+, standard library only.

```bash
export TYPESAFE_API_KEY="your-key"
python3 jev_opensanctions_benchmark.py --limit 20                      # quick check
python3 jev_opensanctions_benchmark.py --out run_9800.jsonl            # 9,800 pairs, ~10 min, ~$0.27
python3 jev_opensanctions_benchmark.py --sample 1000 --out run_800.jsonl   # 800 pairs
python3 compare_runs.py run_a.jsonl run_b.jsonl                        # compare two runs pair by pair
```

Each run writes the answer for every pair to `<out>.jsonl` and the run settings to `<out>_meta.json`.

## Files

| File | What it is |
|---|---|
| `jev_opensanctions_benchmark.py` | The benchmark: downloads the data, queries Jev, scores, prints the comparison with the paper |
| `compare_runs.py` | Compares two runs pair by pair (changed answers, probability drift) |
| `results/run_9800.jsonl` | Jev's answer for each of the 9,800 pairs: record IDs, analyst label, probability, prediction, tokens, latency |
| `results/run_9800_meta.json` | Settings and totals for that run |
| `results/run_9800_summary.txt` | Printed output of that run, plus the additional analysis below |
| `results/run_800_a.jsonl`, `run_800_b.jsonl` | Two runs of the 800 pairs, used for the stability check (earlier version of the script, same prompt and threshold) |

## Caveats

- **Blank-prompt pairs.** 3,322 of the 9,800 pairs are relationship records (Occupancy, Succession, Ownership, ...) that the paper's text format renders as just `Type: Occupancy` on both sides. All are labelled "same", and a conflict-first prompt answers "same" by default. The paper discloses this and notes that it raises every method's F1. Without these pairs Jev scores 97.98.
- **Identical-text pairs.** Beyond the blank pairs, 767 more pairs render as identical text on both sides (502 of them are the same OpenSanctions record on both sides). 4,074 of all 4,089 identical-text pairs are labelled "same"; the 15 labelled "different" (8 crypto wallets, 6 securities, 1 other) cannot be answered from the text and are all among Jev's errors. Excluding every identical-text pair, Jev scores 97.76 (n=5,711).
- **The text format hides the strongest evidence.** Company registration numbers (INN, OGRN, registration and tax numbers), vessel IMO numbers, crypto addresses and citizenship are not shown to the models. Of Jev's 171 errors, about 55 turn on such a hidden field.
- **Where the other errors come from.** About 55 follow from the prompt's own rules: a visible conflict that analysts merged anyway, or identical names with nothing to tell them apart. About 60 are genuine misjudgements, mostly different organization names accepted as variants. These counts are heuristic.
- **No paired comparison.** The paper publishes aggregate scores only, not per-pair predictions, so we cannot test whether Jev and GPT-4o get the same pairs wrong.
- **Labels are analyst decisions**, used as ground truth. A few look debatable.
- **Pricing** is the list price at the time of the run (September 2026).

## Data and license

- Code: [MIT](LICENSE).
- The benchmark data is © OpenSanctions, licensed [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/). It is downloaded at runtime from the paper's repository and **not** redistributed here. The files in `results/` contain only OpenSanctions record IDs, the analyst labels and Jev's outputs.

## Reference

OpenSanctions Pairs benchmark: [arXiv 2603.11051](https://arxiv.org/abs/2603.11051) ·
[github.com/chansmi/OSINT_entity_resolution](https://github.com/chansmi/OSINT_entity_resolution) (commit `a1f62b5`).
