"""Stage 5, the language model: only the reads nothing else could name. Each open shelf spot is shown
its reads and the closest catalog rows, and the model names one row or none. Every call is logged
with its tokens and price, every answer is cached so a second run costs nothing, and the method is
recorded as 'language model' so the golden record ranks it below every deterministic answer.

    python pipeline/match_llm.py                 # the open spots after stage 4
    python pipeline/match_llm.py --model <id>    # another model; the default is MODEL below

Reads   data/pipeline/04_reads.jsonl         stage 4's output
        data/catalog_raw.jsonl, data/catalog_v2.jsonl
        data/pipeline/llm_cache.jsonl        answers already paid for, by prompt hash (read and appended)
Writes  data/pipeline/05_reads.jsonl         the same rows, same order; match_* filled in for the reads
                                             answered here, with match_llm_confidence and match_llm_why
        data/pipeline/05_calls.jsonl         one line per API call: model, tokens, price, seconds
        data/pipeline/05_manifest.json       the counts, the calls and the bill
Needs   ANTHROPIC_API_KEY in the environment, only for prompts not yet in the cache.

The model sees candidates, not the whole catalog: the ten catalog rows closest to the spot's reads by
spelling. Measured on the spots the spelling stage left open: the right row is among the ten for 692
of 696, and the four it misses are reads of one or two words ('coca-cola', 'Det r') that fit a dozen
rows equally, which no list length would settle; twenty candidates show all 696 at twice the prompt.
It is asked for one code or none, a confidence word and a short reason, and told that two candidates
fitting equally is a none. The model runs at its lowest reasoning effort: the task is a short
classification, and the thinking it does is billed as output tokens.
Which confidence words are accepted, and which model answers, were measured by pipeline/measure_llm.py
on every spot stage 3 left open, 696 spots, before the other-days tier. 'sure' was right 211 of 211
times on Haiku 5.5 and 230 of 230 on Sonnet 5.5; 'likely' was wrong 9 of 137 and 4 of 105; 'unsure'
wrong 2 of 9 and 5 of 11. Only 'sure' is accepted. Haiku is the default: no wrong answer at 'sure' on
either model, and Sonnet named 19 more spots for 13.6 times the bill (USD 1.16 against 0.09 for the
696 spots). The two measurement files are data/pipeline/llm_measurement_<model>.json.
"""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from rapidfuzz import fuzz

import normalize                                 # pipeline/normalize.py
from match_exact import catalog_text, CATALOGS   # the catalog row as the label prints it

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "data" / "pipeline"
IN = PIPELINE / "04_reads.jsonl"
OUT = PIPELINE / "05_reads.jsonl"
CALLS = PIPELINE / "05_calls.jsonl"
MANIFEST = PIPELINE / "05_manifest.json"
CACHE = PIPELINE / "llm_cache.jsonl"

MODEL = "claude-haiku-5-5"
CANDIDATES = 10          # catalog rows shown per spot
BATCH = 25               # spots per call
EFFORT = "low"           # reasoning effort; a short classification needs no more
ACCEPT = ("sure",)       # confidence words taken as an answer: the only word with no wrong answer in the measurement
# USD per million tokens, from https://platform.claude.com/docs/en/about-claude/pricing, read 2026-10-08
PRICES = {"claude-sonnet-5-5": (2.00, 10.00), "claude-haiku-5-5": (0.10, 0.50), "claude-opus-5-5": (4.00, 20.00)}

SYSTEM = (
    "You match damaged text read from shelf labels by a camera to a product catalog. "
    "Each item has one or more reads of the same shelf spot and a short list of candidate catalog rows. "
    "Reads may be cut short, missing letters, in the wrong case, or with units written differently. "
    "Name the one candidate the reads can only be. If two candidates fit the reads equally, for example the "
    "same product in two sizes when the read shows no size, answer null. Never invent a code. "
    "Reply with a JSON array only, one object per item, in the same order: "
    '{"id": "...", "code": "<candidate code or null>", "confidence": "sure" | "likely" | "unsure", '
    '"why": "<at most twelve words>"}'
)


def load_catalog():
    versions_of, label_of, text_of = {}, {}, {}
    for version, path in CATALOGS.items():
        for line in path.open(encoding="utf-8"):
            if line.strip():
                p = json.loads(line)
                versions_of.setdefault(p["code"], set()).add(version)
                label_of.setdefault(p["code"], " · ".join(x for x in ((p.get("brands") or "").strip(),
                                                                        (p.get("product_name") or "").strip(),
                                                                        (p.get("quantity") or "").strip()) if x))
                text_of.setdefault(p["code"], normalize.normalize_text(catalog_text(p)))
    return versions_of, label_of, text_of


def candidates_for(reads, text_of):
    """The CANDIDATES catalog codes closest to the spot's reads, best first."""
    best = {}
    for code, text in text_of.items():
        best[code] = max(fuzz.partial_ratio(r["text_norm"], text) for r in reads)
    return sorted(best, key=lambda c: (-best[c], c))[:CANDIDATES]


def items_for(spots, text_of, label_of):
    """One prompt item per open spot: its id, its reads as read, and its candidates."""
    return [{"id": spot, "reads": [r["ocr_text"] for r in reads],
             "candidates": [{"code": c, "label": label_of[c]} for c in candidates_for(reads, text_of)]}
            for spot, reads in spots.items()]


def prompt_key(model, items):
    """The cache key: the model and the exact prompt. Same prompt, same answer, no call."""
    return hashlib.sha256(json.dumps([model, EFFORT, SYSTEM, items], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def load_cache():
    cache = {}
    if CACHE.exists():
        for line in CACHE.open(encoding="utf-8"):
            if line.strip():
                entry = json.loads(line)
                cache[entry["key"]] = entry
    return cache


def ask(model, items, cache, calls):
    """The model's answers for a batch of items, from the cache or from one API call."""
    key = prompt_key(model, items)
    if key in cache:
        calls.append({**cache[key]["call"], "cached": True})
        return cache[key]["answers"]
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("STOP: ANTHROPIC_API_KEY is not set and this prompt is not in the cache. Nothing was written.")
    import anthropic
    client = anthropic.Anthropic()
    t0 = time.perf_counter()
    response = client.messages.create(
        model=model, max_tokens=16000, output_config={"effort": EFFORT}, system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(items, ensure_ascii=False)}])
    seconds = round(time.perf_counter() - t0, 2)
    if response.stop_reason != "end_turn":
        sys.exit(f"STOP: the model stopped with {response.stop_reason!r} on a batch of {len(items)}; nothing cached, nothing written.")
    text = "".join(block.text for block in response.content if block.type == "text")
    if "[" not in text:
        sys.exit(f"STOP: no JSON array in the reply for a batch of {len(items)}; nothing cached, nothing written.")
    answers, _ = json.JSONDecoder().raw_decode(text, text.index("["))   # the array; a fence or a remark around it is ignored
    price_in, price_out = PRICES[model]
    usage = response.usage
    call = {"model": response.model, "items": len(items), "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "usd": round((usage.input_tokens * price_in + usage.output_tokens * price_out) / 1e6, 6),
            "seconds": seconds, "cached": False}
    calls.append(call)
    with CACHE.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"key": key, "answers": answers, "call": call}, ensure_ascii=False) + "\n")
    cache[key] = {"answers": answers, "call": call}
    return answers


def answer_spots(spots, model, text_of, label_of, cache):
    """spot_id -> the model's answer, for every spot, in batches. Returns the answers and the calls."""
    items = items_for(spots, text_of, label_of)
    answers, calls = {}, []
    for i in range(0, len(items), BATCH):
        batch = items[i:i + BATCH]
        got = ask(model, batch, cache, calls)
        allowed = {it["id"]: {c["code"] for c in it["candidates"]} for it in batch}
        for a in got:
            if a.get("id") in allowed:
                code = a.get("code")
                answers[a["id"]] = {"code": code if code in allowed[a["id"]] else None,
                                    "confidence": str(a.get("confidence", "unsure")).lower(),
                                    "why": str(a.get("why", ""))[:120],
                                    "invented": bool(code) and code not in allowed[a["id"]]}
    return answers, calls


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    model = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else MODEL
    if model not in PRICES:
        sys.exit(f"STOP: no price known for {model}; add it to PRICES with its source.")
    if not IN.exists():
        sys.exit(f"STOP: {IN.relative_to(ROOT)} not found. Run pipeline/match_crossday.py first.")
    rows = [json.loads(line) for line in IN.open(encoding="utf-8") if line.strip()]
    before = [dict(r) for r in rows]
    versions_of, label_of, text_of = load_catalog()

    spots = {}
    for r in rows:
        if not r["match_code"]:
            spots.setdefault(r["spot_id"], []).append(r)
    cache = load_cache()
    answers, calls = answer_spots(spots, model, text_of, label_of, cache)

    counts = {"language model": 0, "model said none": 0, "below accepted confidence": 0, "no answer returned": 0, "invented code": 0}
    for spot, reads in spots.items():
        a = answers.get(spot)
        if a is None:
            counts["no answer returned"] += 1
            for r in reads:
                r["match_note"] += "; language model: no answer returned"
            continue
        if a["invented"]:
            counts["invented code"] += 1
        if not a["code"]:
            counts["model said none"] += 1
            for r in reads:
                r["match_note"] += f"; language model: none ({a['why']})"
            continue
        if a["confidence"] not in ACCEPT:
            counts["below accepted confidence"] += 1
            for r in reads:
                r["match_note"] += f"; language model: {a['code']} but only {a['confidence']} ({a['why']})"
            continue
        for r in reads:
            r["match_code"], r["match_method"] = a["code"], "language model"
            r["match_catalog"] = " ".join(sorted(versions_of[a["code"]]))
            r["match_llm_confidence"], r["match_llm_why"] = a["confidence"], a["why"]
            r.pop("match_note", None)
            counts["language model"] += 1

    by_method = {}
    for r in rows:
        key = r["match_method"] or "no answer"
        by_method[key] = by_method.get(key, 0) + 1
    answered = sum(1 for r in rows if r["match_code"])
    bill = {"calls": len(calls), "api_calls": sum(1 for c in calls if not c["cached"]),
            "cached_calls": sum(1 for c in calls if c["cached"]),
            "input_tokens": sum(c["input_tokens"] for c in calls), "output_tokens": sum(c["output_tokens"] for c in calls),
            "usd": round(sum(c["usd"] for c in calls), 4), "model": model}
    checks = {
        "rows in = rows out": len(before) == len(rows),
        "same ids in the same order": [b["detection_id"] for b in before] == [r["detection_id"] for r in rows],
        "every original field unchanged": all(all(r[k] == b[k] for k in b if not k.startswith("match_")) for b, r in zip(before, rows)),
        "no earlier answer changed": all(not b["match_code"] or (b["match_code"], b["match_method"]) == (r["match_code"], r["match_method"])
                                         for b, r in zip(before, rows)),
        "every answer is a catalog code": all(r["match_code"] in versions_of for r in rows if r["match_code"]),
        "every answer here is one of the candidates shown": True,   # enforced in answer_spots: an invented code is a none
        "answered + no answer = rows": answered + by_method.get("no answer", 0) == len(rows),
        "every unanswered row says why": all(r.get("match_note") for r in rows if not r["match_code"]),
    }

    print(f"STAGE 5  LANGUAGE MODEL   {IN.relative_to(ROOT)} -> {OUT.relative_to(ROOT)}")
    print(f"  rows in             {len(rows):>6}   open before this stage {sum(1 for b in before if not b['match_code'])} reads in {len(spots)} spots")
    print(f"  model               {model}   candidates per spot {CANDIDATES}, spots per call {BATCH}, accepted confidence {', '.join(ACCEPT)}")
    print(f"  calls               {bill['calls']:>6}   to the API {bill['api_calls']}, from the cache {bill['cached_calls']}")
    print(f"  tokens              {bill['input_tokens']:>6} in, {bill['output_tokens']} out   USD {bill['usd']:.4f} "
          f"({bill['usd'] / max(1, len(spots)) * 100:.3f} cents per spot asked)")
    print(f"  answered here       {counts['language model']:>6}   reads; spots: model said none {counts['model said none']} | "
          f"below accepted confidence {counts['below accepted confidence']} | no answer returned {counts['no answer returned']} | invented code {counts['invented code']}")
    print(f"  answered in all     {answered:>6}   " + " | ".join(f"{k} {v}" for k, v in sorted(by_method.items(), key=lambda kv: -kv[1])))
    print(f"  no answer           {by_method.get('no answer', 0):>6}")
    for name, ok in checks.items():
        print(f"  CHECK    {name}: {ok}")
    print("\n  one example answered here, and one the model declined")
    shown = set()
    for r in rows:
        kind = "answered" if r["match_method"] == "language model" else ("declined" if "language model" in r.get("match_note", "") else None)
        if kind and kind not in shown:
            shown.add(kind)
            tail = f"-> {r['match_code']} ({r['match_llm_confidence']}: {r['match_llm_why']})" if kind == "answered" else r["match_note"].split("; language model: ")[-1]
            print(f"  {r['detection_id']}  {r['ocr_text']!r}  {tail}")

    if not all(checks.values()):
        sys.exit("STOP: a check failed. Nothing was written.")
    OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8", newline="\n")
    CALLS.write_text("".join(json.dumps(c) + "\n" for c in calls), encoding="utf-8", newline="\n")
    manifest = {
        "stage": 5, "name": "language model",
        "input": {"path": IN.relative_to(ROOT).as_posix(), "rows": len(rows), "open_reads": sum(1 for b in before if not b["match_code"]),
                  "open_spots": len(spots)},
        "output": {"path": OUT.relative_to(ROOT).as_posix(), "rows": len(rows), "fields_added": ["match_llm_confidence", "match_llm_why"]},
        "rules": {"model": model, "candidates": CANDIDATES, "batch": BATCH, "accept": list(ACCEPT), "effort": EFFORT,
                  "prices_usd_per_million": {"input": PRICES[model][0], "output": PRICES[model][1]},
                  "prices_source": "https://platform.claude.com/docs/en/about-claude/pricing"},
        "bill": bill, "answered_here": counts["language model"], "spots_by_outcome": {k: v for k, v in counts.items() if k != "language model"},
        "answered": answered, "by_method": by_method, "checks": checks,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"\nWrote {OUT.relative_to(ROOT)} ({len(rows)} rows), {CALLS.relative_to(ROOT)} ({len(calls)} calls) and {MANIFEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
