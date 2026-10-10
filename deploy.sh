#!/usr/bin/env bash
# Assemble the replay page and its data into public/ and deploy.
#   /web/           <- web            the simulation page
#   /web/pipeline/  <- web/pipeline   the pipeline report page, reached as /pipeline
#   /data/          <- data
#
# The page reads its data from ../data/, so the two folders go up together, side by side,
# exactly as they sit in the repository. One thing is rewritten on the way: the pipeline page is
# baked. web/pipeline/bake.js runs the page's own counting under node and writes the result into the
# copy in public/ as text, so a reader without JavaScript sees the numbers. The page still counts
# from the files at load and marks any number that differs from what was baked.
#
# Before anything is published, every data file the page asks for must exist. The page
# fetches all of them at load and shows an error if one is missing, so a missing file
# would put a broken page in front of a reader.
set -euo pipefail
cd "$(dirname "$0")"

missing=0
for name in $(grep -ohE '"(pipeline/)?[a-z0-9_.-]+\.(csv|jsonl|json|txt)"' web/index.html web/pipeline/index.html web/pipeline/pipeline.js | tr -d '"' | sort -u); do
  if [ ! -f "data/$name" ]; then
    echo "STOP: a page loads data/$name, which does not exist." >&2
    missing=1
  fi
done
if [ "$missing" -ne 0 ]; then
  echo "      Run the scripts in sim/ and pipeline/ first. Nothing was deployed." >&2
  exit 1
fi

rm -rf public
mkdir -p public/web public/data
cp -r web/. public/web/
cp -r data/. public/data/
node web/pipeline/bake.js web/pipeline/index.html public/web/pipeline/index.html
node web/pipeline/bake.js --check public/web/pipeline/index.html
rm public/web/pipeline/bake.js
echo "assembled:"
echo "  public/web   $(find public/web -type f | wc -l) files"
echo "  public/data  $(find public/data -type f | wc -l) files, $(du -sk public/data | cut -f1) KB"
firebase deploy --only hosting --project shelf-replay
