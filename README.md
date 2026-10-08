# shelf-conflation-engine

A simulated store stocked with real products, walked by a simulated badge camera, and the reads that walk produces: the input to a shelf-scan matching pipeline. The replay page in `web/` shows the simulation; `ASSUMPTIONS.md` lists what in it is verified and what is assumed.

## Data sources and licence

Contains data from [Open Food Facts](https://world.openfoodfacts.org), [Open Beauty Facts](https://world.openbeautyfacts.org) and [Open Products Facts](https://world.openproductsfacts.org), available under the [Open Database License](https://opendatacommons.org/licenses/odbl/1-0/). Individual contents are available under the [Database Contents License](https://opendatacommons.org/licenses/dbcl/1-0/). The product rows in `data/catalog_raw.jsonl` and the files built from them are offered under the same Open Database License. No product image is stored, only its URL.

The products are real. The stores, shelf problems, walks, read damage, duplicates and catalog v2 changes are simulated.
