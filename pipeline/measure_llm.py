"""A measurement, not a stage: the language model asked about every spot the spelling stage left
open, before the free other-days tier runs. It answers two questions for the report: how good and
how expensive the model is on the hard cases, and how much of its bill the other-days tier saves.

    python pipeline/measure_llm.py                 # the default model
    python pipeline/measure_llm.py --model <id>    # another one, for the comparison

Reads   data/pipeline/03_reads.jsonl         stage 3's output: every read still open there
        the catalogs, and data/pipeline/llm_cache.jsonl (shared with stage 5, so the spots stage 5
        asks about are paid for once)
Writes  data/pipeline/llm_measurement_<model>.json   every spot's answer, the calls and the bill

The scorer reads the truth and reports right, wrong and declined by confidence word:
    python pipeline/score.py llm --measurement
"""
import json
import sys
from pathlib import Path

import match_llm   # pipeline/match_llm.py: the same prompt, candidates, cache and prices

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "data" / "pipeline"
IN = PIPELINE / "03_reads.jsonl"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    model = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else match_llm.MODEL
    if model not in match_llm.PRICES:
        sys.exit(f"STOP: no price known for {model}; add it to match_llm.PRICES with its source.")
    rows = [json.loads(line) for line in IN.open(encoding="utf-8") if line.strip()]
    versions_of, label_of, text_of = match_llm.load_catalog()
    spots = {}
    for r in rows:
        if not r["match_code"]:
            spots.setdefault(r["spot_id"], []).append(r)
    cache = match_llm.load_cache()
    answers, calls = match_llm.answer_spots(spots, model, text_of, label_of, cache)

    by_conf = {}
    for a in answers.values():
        key = a["confidence"] if a["code"] else "none"
        by_conf[key] = by_conf.get(key, 0) + 1
    without_reply = sorted(s for s in spots if s not in answers)   # an item the model left out of its array
    bill = {"calls": len(calls), "api_calls": sum(1 for c in calls if not c["cached"]),
            "input_tokens": sum(c["input_tokens"] for c in calls), "output_tokens": sum(c["output_tokens"] for c in calls),
            "usd": round(sum(c["usd"] for c in calls), 4), "seconds": round(sum(c["seconds"] for c in calls), 1)}
    out = PIPELINE / f"llm_measurement_{model}.json"
    out.write_text(json.dumps({
        "model": model, "spots_asked": len(spots), "reads_in_them": sum(len(v) for v in spots.values()),
        "answers_by_confidence": by_conf, "spots_without_reply": without_reply, "bill": bill, "calls": calls,
        "answers": {spot: {**a, "reads": [r["detection_id"] for r in reads]} for spot, (a, reads) in
                    ((s, (answers.get(s), spots[s])) for s in spots) if a},
    }, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"LLM MEASUREMENT   {model}: {len(spots)} spots asked ({sum(len(v) for v in spots.values())} reads), "
          f"answers by confidence " + " | ".join(f"{k} {v}" for k, v in sorted(by_conf.items())) + f" | no reply {len(without_reply)}")
    print(f"  calls {bill['calls']} ({bill['api_calls']} to the API), tokens {bill['input_tokens']} in / {bill['output_tokens']} out, "
          f"USD {bill['usd']:.4f}, {bill['seconds']} s")
    print(f"  wrote {out.relative_to(ROOT)}; score it with: python pipeline/score.py llm --measurement" + (f" --model {model}" if model != match_llm.MODEL else ""))


if __name__ == "__main__":
    main()
