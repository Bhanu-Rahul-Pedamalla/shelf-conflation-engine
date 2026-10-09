# shelf-conflation-engine

A simulated store stocked with real products, walked by a simulated badge camera, and the reads that walk produces: the input to a shelf-scan matching pipeline. The replay page in `web/` shows the simulation; `ASSUMPTIONS.md` lists what in it is verified and what is assumed.

## Data sources and licence

Contains data from [Open Food Facts](https://world.openfoodfacts.org), [Open Beauty Facts](https://world.openbeautyfacts.org) and [Open Products Facts](https://world.openproductsfacts.org), available under the [Open Database License](https://opendatacommons.org/licenses/odbl/1-0/). Individual contents are available under the [Database Contents License](https://opendatacommons.org/licenses/dbcl/1-0/). The product rows in `data/catalog_raw.jsonl` and the files built from them are offered under the same Open Database License. No product image is stored, only its URL.

The products are real. The stores, shelf problems, walks, read damage, duplicates and catalog v2 changes are simulated.

## Running the pipeline

Python 3.13, with `pandas`, `rapidfuzz`, `duckdb` and `anthropic` installed:

```bash
python pipeline/run.py                      # the nine stages in order, each scored before the next; ~20 s
python pipeline/test_versioning.py          # the record survives the catalog change: 6 tests
python pipeline/score.py golden             # any one stage against the truth: ingest, normalize, exact, fuzzy, crossday, llm, golden, versioning, serve
python pipeline/serve.py --sql "select verdict, count(*) from realogram group by 1 order by 2 desc"
```

Each stage is one script in `pipeline/`, reads only the files of the stages before it, and writes one output and a manifest to `data/pipeline/`. The runner writes what every step printed to `data/pipeline/run.txt`. `REPORT.md` is built from that log: the result, the stages, what was planted against what was caught, the limits. The same report is a page at `web/pipeline/`, served at `/pipeline`.

The language model stage answers from `data/pipeline/llm_cache.jsonl`, so a run of this feed makes no API call. A new feed needs `ANTHROPIC_API_KEY` in the environment for the prompts the cache does not hold.
