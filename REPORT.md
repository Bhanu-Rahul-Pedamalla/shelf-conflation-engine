# REPORT — the shelf-scan conflation pipeline, 3 stores, 3 days, 5,705 reads

Every number in this document comes from one run of `python pipeline/run.py`, which runs the nine
stages in order, scores each one before the next starts, and writes what every step printed to
`data/pipeline/run.txt`. This is a snapshot of one run; run the command and the log is rewritten.

**Run:** `2026-10-09T04:31:59Z` · Python 3.13.2 · Windows 11 · one laptop · 19 steps · 20.3 s wall clock.

Every count below is deterministic: the same input gives byte-identical output files, and the run
above rewrote every committed stage output without changing a byte. Only the timings in the log
move between runs. The language model stage answered every prompt from the committed cache, so the
run made no API call; the bill it reports is what those prompts cost when they were first sent.

```
lines in the feed                  5,927
usable reads, each once            5,705      162 re-sent frames dropped, 60 damaged rows quarantined
reads named right                  5,586      97.9 %
reads named wrong                      3      every one overruled or held open in the record
reads left open                      116      in 18 spots nothing else could name
shelf spots with a record          1,763      1,744 right on the day last seen, 0 wrong, 1 contested, 18 open
planned facings served             1,812      1,812 verdicts stand against the shelf on the last day
compliance                         96.0 %     the shelf truth says 96.5 %; the 0.5 points are accounted for below
language model                     USD 0.0195 for 159 spots in 7 calls
```

---

## 1. Purpose

A badge camera worn by store staff reads shelf labels as they walk. Each read is a bounding box, a
text, sometimes a barcode, a position and a time. The reads are damaged, doubled, re-sent and
occasionally malformed, and the product catalog they must be matched against changes between the
days. The pipeline turns that feed into one record per shelf spot per store, saying which product
stands there, how that was decided, which reads support it and since when, under which catalog
version; then serves that realogram against the planogram as a compliance table per shelf.

The question the pipeline answers is not "what did the camera see" but "what stands on this shelf,
and how sure are we". Every stage therefore says what it could not decide, and why, instead of
guessing. The scorer measures every answer against the simulation's truth, which no stage reads.

## 2. Result

The reads, by the stage that named them. Each stage only touches what the stages before it left
open, so the rows are cumulative.

| after stage | right | wrong | open | right % | answered here by |
|---|---|---|---|---|---|
| naive baseline (case-folded lookup) | 2,665 | 2 | 3,038 | 46.7 % | |
| 2 exact match | 3,068 | 2 | 2,635 | 53.8 % | barcode 821, exact text 2,249 |
| 3 close spelling | 4,982 | 3 | 720 | 87.3 % | same spot 225, close spelling 1,690 |
| 4 other days | 5,539 | 3 | 163 | 97.1 % | other day 557 |
| 5 language model | 5,586 | 3 | 116 | 97.9 % | language model 47 |

The record (stage 6) decides each spot from all its reads, by method rank then by count. Against
the shelf truth on the day the product was last seen: 1,744 right, 0 wrong, 1 contested, 18 without
an answer. The three wrong reads above are in it: one outranked by two exact-text reads of the
right product, one outvoted two to one, one tied one to one with a right read, and that spot is
the contested one. No wrong read became a wrong record.

The catalog changed between day 1 and day 2 (stage 7): 7 changes, found from the two catalog files
alone, 7 of 7 matching the changes the simulation made. Every record carries its product under both
versions; the 3 records holding the discontinued product are kept and say so.

Served against the planogram (stage 8), 1,812 planned facings:

| verdict | facings | against the shelf on the last day |
|---|---|---|
| as planned | 1,726 | 1,717 product on the shelf; 8 emptied since last seen; 1 changed size since last seen |
| as planned, new size | 11 | 11 product on the shelf; the planogram still carries the old barcode |
| as planned, catalog dropped it | 3 | 3 still on the shelf |
| different product | 4 | 4, the simulation's 4 misplaced units |
| contested | 1 | the truth is one of the two candidates |
| open | 18 | 16 hold a product, 2 are empty now |
| never read | 49 | 49 empty on every day |

| compliance | planned | realogram says | truth says |
|---|---|---|---|
| S001 | 604 | 576 (95.4 %) | 581 (96.2 %) |
| S002 | 604 | 582 (96.4 %) | 585 (96.9 %) |
| S003 | 604 | 582 (96.4 %) | 583 (96.5 %) |
| all | 1,812 | 1,740 (96.0 %) | 1,749 (96.5 %) |

The 9-facing gap: the realogram counts 8 facings whose product has run out since it was last seen,
and does not count the 16 open spots and the 1 contested spot whose product is in fact the planned
one. 1,749 − 17 + 8 = 1,740. The realogram marks the 30 records not seen on the latest walk, which
hold those 8 empty facings, 1 changed one and 21 still stocked.

## 3. Overview

Nine scripts, one per stage. Each reads only the files of the stages before it, writes one output
and a manifest with its counts and its rules, prints CHECK lines that must all be True before it
writes anything, and is scored by `pipeline/score.py` before the next stage runs. The scorer is the
only pipeline file that reads a truth file. Every rule with a number in it was measured on this feed
before it was written, and the measurement is in the script's docstring.

```
data/detections.jsonl                 the feed, 5,927 lines
  0  pipeline/ingest.py          ->  00_reads.jsonl         every usable read once, in time order; 00_quarantine.jsonl
  1  pipeline/normalize.py       ->  01_reads.jsonl         + text_norm, one text form for reads and catalog
  2  pipeline/match_exact.py     ->  02_reads.jsonl         barcode with check digit, then exact text, one row only
  3  pipeline/match_fuzzy.py     ->  03_reads.jsonl         the shelf spot (doubles, second badge), then close spelling
  4  pipeline/match_crossday.py  ->  04_reads.jsonl         the same position on other days, catalog version by date
  5  pipeline/match_llm.py       ->  05_reads.jsonl         the language model, candidates only, 'sure' only; 05_calls.jsonl
  6  pipeline/golden.py          ->  06_golden.jsonl        one record per spot: product, method, reads, since when, version
  7  pipeline/versioning.py      ->  07_golden.jsonl        + lineage v1/v2, catalog status; 07_catalog_changes.jsonl
  8  pipeline/serve.py           ->  08_realogram.jsonl     DuckDB: every planned facing's verdict; 08_compliance.csv
     pipeline/score.py <stage>       each stage against the truth
     pipeline/test_versioning.py     the record survives the catalog change
     pipeline/run.py                 all of the above, in order, into data/pipeline/run.txt
```

## 4. The data

The products are real: 300 catalog rows from Open Food Facts, Open Beauty Facts and Open Products
Facts, stored as their source returned them. Everything else is simulated by `sim/`, and
`ASSUMPTIONS.md` lists what is verified and what is assumed.

- **Stores.** 3 stores share one planogram: 7 aisles, 4 shelves each, 900 positions, 1,812 facings
  of 0.20 m. Shelf problems: 98 facings empty on day 1 and 59 on days 2 and 3 after an overnight
  restock; 4 single units misplaced in the wrong aisle on every day.
- **Walks.** 3 full walks per store, one per day, each by a different badge, plus a second badge
  on one aisle of each store on day 1: 12 walks. A stocked facing is read once per walk; an empty
  facing gives no read.
- **The feed.** 5,927 lines: 5,705 distinct reads, 162 frames sent twice byte for byte, 60 rows
  damaged on the way (a line cut, a field missing, a wrong type, another timestamp form, a value
  out of range, no text and no barcode, an id reused with other content). A label's text is damaged
  independently: a unit respelled on 30 % of reads, the text cut short on 30 %, 1 to 3 characters
  lost on 35 %, the case changed on 40 %. A barcode is read on 15 %, never misread. 5 % of reads are
  doubled in the same frame. The text damage by kind, with how many of each the first two stages
  bring back to their label:

| damage | reads | equal to the label after case-folding | after normalizing |
|---|---|---|---|
| none | 1,174 | 1,174 | 1,174 |
| case | 787 | 787 | 787 |
| drop | 611 | 0 | 5 |
| truncate | 503 | 0 | 0 |
| unit | 417 | 154 | 417 |
| drop+case | 383 | 0 | 4 |
| truncate+case | 319 | 0 | 0 |
| truncate+drop | 298 | 0 | 0 |
| unit+case | 252 | 89 | 252 |
| the other 7 combinations | 961 | 1 | 4 |
| all | 5,705 | 2,205 | 2,643 |

- **The catalog.** v1 is the 300 rows as pulled. v2 takes effect on day 2: 3 products renamed, 2
  given a size 10 % smaller and therefore a new barcode (the GS1 declared-net-content rule), 1
  discontinued with its stock left on the shelf, 1 real product added that stands on no shelf. The
  planogram is not updated. 280 of the 280 thirteen-digit codes pass the GS1 check digit; the 20
  eight-digit codes are not GS1 codes (11 of 20 fail the GTIN-8 rule) and are matched as strings.
- **Truth, for the scorer only.** `detections_truth.csv` (the product behind each read),
  `shelf_truth_day1/2/3.csv` (what stood on each facing each day), `malformed_truth.csv` (the 60
  damaged lines), `catalog_changes_truth.csv` (the 7 changes).

## 5. The stages

**0 · ingest.** A usability rule per field: type, blank, a bounding box of four integers, the one
timestamp form the badge writes, confidence in 0..1, shelf ≥ 1, x/y/z ≥ 0, barcode digits, text or
barcode present. Same id and same content is a re-sent frame and is dropped; same id and different
content is an id conflict, the first row kept, the second quarantined. Rows sorted by time then id.
5,927 lines = 5,705 rows out + 162 duplicates + 60 quarantined, by reason: unreadable 9, missing
field 9, wrong type 9, bad timestamp 9, out of range 9, empty read 8, id conflict 7. Scored: 5,705
of 5,705 real detections present once, 0 unknown rows, the 60 quarantined rows are the 60 planted
ones on the same lines, the expected reason on 60 of 60.

**1 · normalize.** One text form, applied to reads and catalog alike: NFKD, accents dropped,
case-folded, decimal comma to dot, punctuation to space except a decimal point inside a number,
number+unit to "n unit" with one spelling per unit, single spaces. Measured first: the feed's unit
spellings (oz/OZ/oz./ounces, fl oz/floz/fl. oz, ml/mL, g/grams, lb/lbs, l/liter), its punctuation,
its accents. Rules fired: case 4,518, punctuation 2,665, unit 1,340, accent 101, spacing 87.
Distinct texts 4,176 to 3,170. Reads equal to their label: 2,205 case-folded, 2,643 normalized.
Every unit-only and unit+case read recovered (417 of 417, 252 of 252); cut and dropped-letter reads
are stage 3's job. No brand alias table: measured, none was needed, and the docstring says so.

**2 · exact match.** A barcode in the catalog wins, after the check digit on 12-, 13- and 14-digit
codes. Then the normalized text against every catalog row's normalized label, over v1 and v2
together, one answer only when exactly one code fits. 821 by barcode, 2,249 by text; 2,635 open,
2,628 fitting no row and 7 fitting two: the catalog holds two rows that differ only as "24 oz" and
"24oz". Barcode and text both present on 385 reads, disagreeing on 0. Scored: 3,068 right, 2 wrong,
2,635 open. The two wrong are cut-short labels that equal a shorter, sizeless catalog row letter
for letter ("OLIPOP Strawberry Vanilla", "nature's bakery fig bar"). Exact text cannot tell a cut
label from a short one; the record settles both later.

**3 · close spelling, and the shelf spot.** First the spot: two reads are the same facing when
they are in the same frame with box overlap ≥ 0.5 (the doubled detections: 288 pairs, all the same
facing; 0.3 lets 6 neighbours in) or from different walks on the same store, day, aisle and shelf
within 0.06 m (the second badge: 174 right pairs, 1 wrong; 0.08 m gives 199 and 1, 0.10 m 213 and 4).
A plain clustering of positions at 0.10 m gave 58 spots mixing two facings and 18 wrong
propagations; the two rules give 1 mixed spot. 5,258 spots, 4,832 of one read. An answered read
names its spot's open reads: 225, 0 wrong. Then the spelling: six rapidfuzz scorers by four blockings
by thresholds 50..100 by runner-up gaps 0, 5, 10, 15, measured against the truth
(`score.py fuzzy --curve` prints the table). The gap to the runner-up is the guard, not the
threshold: with no gap every threshold gives about 190 wrong. Chosen: partial_ratio, first-word
block, score ≥ 80, gap ≥ 10. 1,690 answered here, 1 wrong: 'psi 12 foz' to Diet Pepsi 12oz, score
95, gap 11. Scored in all: 4,982 right, 3 wrong, 720 open.

**4 · other days.** An open read sits where other walks, on other days, read a product the free
checks named: the answered reads within 0.08 m on the same store, aisle and shelf on other days,
when they all name one product, when that product was in the catalog version in effect on the
read's day, and when the read's own text scores ≥ 60 against it. Measured first: the neighbours
alone are right 555 and wrong 11, every wrong a product whose barcode changed between the days;
the version guard leaves 1 wrong; the text guard 0. At 0.06 m 524 right, at 0.10 m 570 and 2 wrong.
Result: 557 answered, 0 wrong, 163 open (no neighbour 159, neighbours disagree 3, text disagrees 1).
It runs before the language model because it costs nothing.

**5 · language model.** Only what nothing else could name: 163 reads in 159 spots. Each spot is
shown its 10 closest catalog rows by partial_ratio, 25 spots per call, and must answer with one of
them or none, with a confidence word. Measured first on every spot stage 3 left open, 696, with both
candidate models (`score.py llm --measurement --model <id>` over the committed measurement files):

| model | sure | likely | unsure | none | no reply | output tokens | USD |
|---|---|---|---|---|---|---|---|
| claude-haiku-5-5 | 211 right, 0 wrong | 128 right, 9 wrong | 7 right, 2 wrong | 337 | 2 | 98,489 | 0.0853 |
| claude-sonnet-5-5 | 230 right, 0 wrong | 101 right, 4 wrong | 6 right, 5 wrong | 349 | 1 | 44,174 | 1.1624 |

Every "likely" or "unsure" wrong is a size or flavour the read never showed. Decision: accept
"sure" only, the one word with no wrong answer on either model; Haiku, because Sonnet names 19 more
spots of 696 for 13.6 times the bill. An answer must be one of the candidates shown (invented codes
0); the reply is parsed from its first `[`; a reply the model cut short is refused and not cached;
a spot the model leaves out is counted (1). The stage's own run: 7 calls, 80,688 tokens in, 22,946
out, USD 0.0195; 47 answered, 0 wrong; model said none 91, below the accepted confidence 23, no
reply 1. 5,586 right, 3 wrong, 116 open. Prompts are cached by hash in `llm_cache.jsonl`, so this
run and every rerun make no API call. The stage 4 tier saved 537 spots from being asked.

**6 · golden record.** A spot is a store, aisle, shelf and a 0.20 m slot. Measured first: slots
give 1,763 spots for the 1,763 facings ever read, 4 holding one read that jittered in from the
neighbour; clustering by gaps gives 1,637 spots at 0.10 m with 126 mixing two facings. The reads of
a spot vote by method rank: barcode, exact text, close spelling, same spot, other day, language
model; among equals the most reads; an even split is contested and the record names both. A read
whose code is not in the catalog version in effect on the spot's latest day steps aside when a
current code competes and becomes history. The rules measured against the shelf truth on the day
the product was last seen (a scratch measurement while building; the chosen rule's numbers are the
run's): "latest day wins" 1,735 right, 2 wrong; "all reads vote" 1 wrong, 22 open, because the 8
spots whose product changed barcode split their votes; "a change needs two reads or a better rank"
5 wrong, because every real barcode change was read once a day; the rule above 1,744 right, 0
wrong, 1 contested, 18 open. Decided by: barcode 697, exact text 901, close spelling 120, other day
1, language model 25. 8 history entries, 8 right on the days they cover. Against the last day:
1,735 current, 8 emptied since, 1 changed since.

**7 · versioning.** The two catalog files compared: 297 codes in both, 3 with a different name;
3 codes only in v1, 3 only in v2. A v1-only and a v2-only code are one product in a new size when
the brand is the same and the name is the same once every token with a digit is removed: 2 of the
9 pairings, scoring 91 and 95 on text similarity where the next pairing scores 52. Unpaired: 1
discontinued, 1 new. Applying the 7 changes to v1 rebuilds v2 code for code and label for label.
Each record gains its product under v1 and v2 and a catalog status: current 1,707; renamed in v2
22, now carrying the new name; new size since v2 11, the old code in their history, all 8 history
entries explained by a change; old size with the successor never read here 1; discontinued and
still on the shelf 3, kept, because dropping them would report an empty spot that is not empty.
`test_versioning.py` rebuilds the realogram from the 2,010 reads before the change with a catalog
that knows only v1 and applies the changes: 1,676 records with a product, 1,665 keep their code,
9 move to a successor, 2 keep a product v2 dropped, 0 lost; of the 9, the 8 spots read again on the
later days were read as that successor. 6 tests, 6 pass. Scored: 7 of 7 changes, old and new
values right 7 of 7; the 3 leftovers still on the shelf on the last day; the successor named for
the old-size record is what the shelf holds.

**8 · serve.** DuckDB, in memory, over the record, the reads, the planogram and both catalogs. The
planogram's 900 positions expand to 1,812 facings, one slot each; 1,763 have a record, 49 were
never read. Verdicts and compliance as in section 2; one row per store, aisle and shelf in
`08_compliance.csv`, 84 shelves. The 18 open spots are printed with every read they hold, the 3
dropped-product spots, the contested spot with both candidates, the 4 different products.
`--sql` runs any query against the same database. Scored: 1,812 of 1,812 verdicts stand.

## 6. Failures, and how they were caught

What the simulation planted or the days changed, against what the pipeline did with it. Nothing in
the pipeline reads the plant list; the scorer does.

| planted or happened | count | caught | by what |
|---|---|---|---|
| damaged rows, 7 kinds | 60 | 60, the right reason on 60 | a usability rule per field, stage 0 |
| frames sent twice | 162 | 162 dropped | same id, same content |
| an id reused with other content | 7 | 7, the first row kept | same id, different content |
| a detection doubled in its frame | 288 | 288 joined | box overlap ≥ 0.5 in the same frame |
| a second badge reading the same aisle | 197 reads | joined within 0.06 m | the spot rule, 1 wrong pair at that distance |
| a product's barcode changed between the days | 8 spots | 8 history entries, 8 right | the catalog-aware vote; a plain vote leaves 22 spots open |
| the neighbour on another day names the new code for the old pack | 11 reads | 11 refused | the code must have been in the catalog on the read's day |
| a cut label equal to a shorter product | 2 reads | 1 outvoted 2 to 1, 1 held contested | the record's vote |
| a misspelling closer to the wrong product | 1 read | outranked | exact text outranks close spelling |
| a model answer that is a guess | 9 of 128 "likely" wrong | none accepted | measured first; "sure" only |
| the model writing after its JSON, stopping early, skipping a spot | | parsed, refused, counted | `raw_decode` from the first `[`; `stop_reason`; "no reply" |
| a product discontinued, stock left on the shelf | 3 spots | kept and flagged | the catalog diff; the record is not dropped |
| the planogram still carrying an old barcode | 11 facings | "as planned, new size" | the lineage |
| a unit in the wrong aisle | 4 | 4 "different product" | the planogram join |
| a facing emptied after its product was last seen | 8 | 8 among the 30 "not seen on the latest walk" | the badge gives no read for an empty facing; the record says what it cannot know |
| a facing empty on every day | 49 | 49 "never read" | no record |

The rows worth reading are the ones where the right behaviour is to say nothing: the contested
OLIPOP spot, where one right read and one wrong read of the same rank tie and the record names both
instead of picking; the 18 open spots, where every read was a cut or a misspelling that fits
several sizes (three cut reads of Honey Nut Cheerios cannot pick between the 10.8 oz and the
other sizes; three reads of siggi's skyr fit two catalog rows that differ only as "24 oz" and
"24oz"); and the 30 records marked as not seen on the latest walk. Had the record reported those 30
as current, it would be wrong 9 times.

## 7. Bugs found while building

Found by the checks, the scorer or the runner, and fixed; each is in the commit that fixed it.

1. **The baseline let a planted row overwrite a real read.** The naive baseline took the last row
   per id; the planted id conflict replaced a real read's answer and moved the baseline from 2,665
   right to 2,664. Fixed: first row wins, the same rule stage 0 applies.
2. **The replay page parsed every line.** Once the feed carried the nine cut lines, the page's
   `JSON.parse` failed on load. Fixed: the page skips a line by the same usability rule as stage 0,
   and says how many it skipped.
3. **The catalog's effective date was a day number.** The v2 metadata said the catalog took effect
   on day 2; the pipeline sees timestamps and has no day numbers. Fixed in the simulation: it writes
   the date, and stage 4 onward read it.
4. **Eleven of the twenty eight-digit codes fail the GTIN-8 check digit.** They are not GS1 codes.
   The check is applied to 12-, 13- and 14-digit codes only; eight-digit barcodes are matched as
   strings. The check was made to fire on a known-bad code before it was trusted.
5. **Two catalog rows have the same label.** siggi's skyr appears as "24 oz" and "24oz" under two
   codes. 7 reads fit both; the pipeline leaves them open and the manifest counts them, rather
   than picking one. The three siggi's spots are among the 18 open.
6. **Four API behaviours the stage did not expect.** A non-default temperature is rejected with a
   400 on these models; the model's thinking counts toward `max_tokens` and cut a reply short; one
   reply carried a remark after its JSON array; one reply left a spot out. Fixed: effort control
   instead of temperature, a larger budget and a refusal on any stop reason but a finished turn,
   parsing from the first `[`, and a "no reply" count. One batch was paid twice before the parser
   fix, about 4 cents.
7. **The versioning test loaded the catalog labels once per record.** 13.3 s in the first full
   run, for 1,714 records. Loaded once: 0.8 s. Found by the runner's timing column.
8. **The serve scorer called a stale record wrong.** The record at S001-A04-S2-019 holds the old
   32oz size, last seen on day 1, and says it was not seen on the latest walk; the shelf holds the
   28.8oz size now. The first scorer counted that as a contradiction. It is not: the record reports
   exactly what it could know. The scorer now separates "changed since last seen" from "wrong",
   and only a record that claims to have seen its product on the latest walk can be wrong.

## 8. Limits

- **This is a simulation, and its labels are easy.** A read's text is the catalog row's brand,
  name and size with damage applied. Real label text differs from catalog rows far more than that,
  so exact text and close spelling would name fewer reads on real images, and the language model
  and an image match would carry more. The damage rates, the barcode rate and the position noise
  are assumptions, listed in `ASSUMPTIONS.md`.
- **Three reads are wrong and 116 are open.** The pipeline does not reach every read; the record
  reaches every spot but 18 and 1. The 18 open spots hold cut labels that fit several sizes and the
  twin catalog rows; they need an image, a barcode, or a catalog fix, not a better text rule.
- **The record cannot see an empty facing.** The badge gives no read where nothing stands, so a
  product that ran out after it was last seen stays in the record, marked as not seen on the
  latest walk: 30 such records, 8 of them empty now. Compliance from the record is 0.5 points
  under the truth for this reason and for the 17 unsettled spots. A production system would get
  "this facing was in frame and gave no read" from the camera; this one does not.
- **The spot is a 0.20 m slot.** 4 of 1,763 records hold one read that jittered in from the
  neighbouring facing. Slots work because the simulation's facings are all 0.20 m wide and start
  at the shelf's edge; a store's own fixture geometry would replace the constant.
- **The planogram is the simulation's and is not updated.** The verdict "as planned, new size"
  and "as planned, catalog dropped it" exist because of that; a maintained planogram would turn
  them into plain "as planned" and "not planned".
- **The language model's numbers are from one measurement of 696 spots on two models on one day.**
  Model behaviour and prices move. The cache makes this run reproducible without a key; a new feed
  needs the key and pays again. Total spent while building, including the measurement and one
  re-paid batch: about USD 1.31.
- **Two rules were measured with scratch scripts, not repo commands.** The survivorship comparison
  in stage 6 and the pairing table in stage 7 are recorded in the docstrings; the chosen rules'
  numbers are in the run, the alternatives' are not reproducible by a command here.
- **One run, one laptop, files on disk.** No stream, no concurrency, no clock skew between badges,
  no store whose walks overlap. 20.3 seconds for 5,705 reads says nothing about 5 million.

## 9. What production would swap in

- **The feed:** a stream instead of a file, with stage 0's per-field rule applied at the edge and
  its quarantine kept, and the manifests' counts emitted as metrics instead of printed.
- **The stage files:** tables in a warehouse; the stage boundaries and manifests stay. DuckDB stays
  where it is, serving and ad-hoc queries, over the warehouse's tables instead of local files.
- **The catalog:** the retailer's product master with effective dates, and its change feed; the
  diff in stage 7 becomes the check that the feed is complete, not the source.
- **The planogram:** the store's fixture geometry for the spot, instead of a 0.20 m constant.
- **The language model:** the same prompt and the same "sure only" rule through the Batch API at
  half the price, with prompt caching on the system prompt; the cache keyed the same way.
- **Before the language model:** an image match for the open reads, since the badge has the frame
  and this pipeline does not.
- **The truth:** none exists in production. The scorer becomes a sampled audit, a person checking
  a few hundred spots a week, and the record's open and contested counts become the thing watched.
- **The runner:** an orchestrator calling the same scripts in the same order, the test in CI, and a
  byte-identity check of each stage's output as the regression test it already is.

## 10. Files

```
pipeline/ingest.py           stage 0        data/pipeline/00_reads.jsonl, 00_quarantine.jsonl, 00_manifest.json
pipeline/normalize.py        stage 1        01_reads.jsonl, 01_manifest.json
pipeline/match_exact.py      stage 2        02_reads.jsonl, 02_manifest.json
pipeline/match_fuzzy.py      stage 3        03_reads.jsonl, 03_manifest.json
pipeline/match_crossday.py   stage 4        04_reads.jsonl, 04_manifest.json
pipeline/match_llm.py        stage 5        05_reads.jsonl, 05_calls.jsonl, 05_manifest.json, llm_cache.jsonl
pipeline/measure_llm.py      the 696-spot measurement        llm_measurement_<model>.json
pipeline/golden.py           stage 6        06_golden.jsonl, 06_manifest.json
pipeline/versioning.py       stage 7        07_golden.jsonl, 07_catalog_changes.jsonl, 07_manifest.json
pipeline/test_versioning.py  the test       (writes nothing)
pipeline/serve.py            stage 8        08_realogram.jsonl, 08_compliance.csv, 08_manifest.json
pipeline/score.py            the scorer, the only reader of the truth files
pipeline/baseline.py         the naive lookup the ladder starts from        data/baseline_results.csv
pipeline/run.py              every stage and scorer in order                data/pipeline/run.txt
sim/                         the store, the catalog and its v2, the walks, the damage; ASSUMPTIONS.md
web/index.html               the replay page, the feed as it arrives
```

Reproducing this, from the repository root with Python 3.13 and `pandas`, `rapidfuzz`, `duckdb`
and `anthropic` installed:

```bash
python pipeline/run.py                                        # all 19 steps, ~20 s, no API call
python pipeline/score.py fuzzy --curve                        # the spelling rule's measurement table
python pipeline/score.py llm --measurement --model claude-haiku-5-5
python pipeline/serve.py --sql "select verdict, count(*) from realogram group by 1 order by 2 desc"
```
