#!/usr/bin/env node
/* Bake the pipeline page: run the same counting pipeline.js does at load, under node, and write
   the result into the HTML as static text, so a reader without JavaScript sees the numbers.
   The page's script still counts from the files at load and compares; a difference is marked.

     node web/pipeline/bake.js web/pipeline/index.html public/web/pipeline/index.html
     node web/pipeline/bake.js --check public/web/pipeline/index.html

   The first form fills every element render() writes, from the empty template. The second form
   counts again and compares each baked element to the fresh text, and exits 1 on any difference. */
"use strict";
const fs = require("fs"), path = require("path");
require("./pipeline.js");
const { compute, render, loadAll } = globalThis.PipelinePage;

const escText = s => String(s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const unescText = s => s.replace(/&(amp|lt|gt|quot|#39|nbsp);/g, (_, e) => ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'", nbsp: " " }[e]));

// the least of a DOM that render() uses: elements with a class, inner HTML and children, serialised in order
class El {
  constructor(tag) { this.tag = tag; this.className = ""; this.html = ""; this.children = []; }
  set innerHTML(h) { this.html = h; this.children = []; }
  get innerHTML() { return this.html + this.children.map(c => c.outerHTML).join(""); }
  set textContent(t) { this.html = escText(t); this.children = []; }
  appendChild(c) { this.children.push(c); return c; }
  get outerHTML() { return `<${this.tag}${this.className ? ` class="${this.className}"` : ""}>${this.innerHTML}</${this.tag}>`; }
}
function renderToElements(N) {
  const byId = new Map();
  render(N, { getElementById: id => { if (!byId.has(id)) byId.set(id, new El("div")); return byId.get(id); }, createElement: t => new El(t) });
  return byId;
}
// the element with this id: its open tag, its content, its close tag, found by counting nested tags of the same name
function elementAt(html, id) {
  const open = html.match(new RegExp(`<(\\w+)\\b[^>]*\\bid="${id}"[^>]*>`));
  if (!open) return null;
  const tag = open[1], start = open.index + open[0].length, re = new RegExp(`<(/?)${tag}\\b[^>]*>`, "g");
  re.lastIndex = start;
  for (let depth = 1, m; (m = re.exec(html));) {
    depth += m[1] ? -1 : 1;
    if (!depth) return { tag, open: open[0], start, end: m.index, close: m[0], content: html.slice(start, m.index) };
  }
  return null;
}
const textOf = html => unescText(html.replace(/<[^>]*>/g, ""));

async function count(dir) {
  const F = await loadAll(url => fs.promises.readFile(path.join(dir, url), "utf8"));
  return compute(F);
}

async function main(argv) {
  const check = argv[0] === "--check";
  if (check) argv = argv.slice(1);
  const [src, dst] = argv;
  if (!src || (!check && !dst)) { console.error("usage: bake.js SRC.html DST.html | bake.js --check BAKED.html"); return 2; }
  const html = fs.readFileSync(src, "utf8");
  const byId = renderToElements(await count(__dirname));

  if (check) {
    let bad = 0, n = 0;
    for (const [id, e] of byId) {
      const m = elementAt(html, id);
      if (!m) { console.error(`DRIFT: #${id} is not in ${src}`); bad++; continue; }
      const baked = textOf(m.content), fresh = textOf(e.innerHTML);
      const bn = baked.match(/\d[\d,.]*\d|\d/g) || [], fn = fresh.match(/\d[\d,.]*\d|\d/g) || [];
      n += fn.length;
      if (baked === fresh) continue;
      bad++;
      const k = fn.findIndex((x, i) => x !== bn[i]);
      console.error(`DRIFT: #${id}: ${bn.length} numbers baked, ${fn.length} counted` + (k >= 0 ? `; first difference at number ${k + 1}: baked ${bn[k]}, counted ${fn[k]}` : "; the text differs"));
    }
    console.log(`checked ${byId.size} elements, ${n} numbers, ${bad} differ`);
    return bad ? 1 : 0;
  }

  let out = html, filled = 0;
  for (const [id, e] of byId) {
    const m = elementAt(out, id);
    if (!m) throw new Error(`#${id}: no element with this id in ${src}`);
    if (m.content.trim() !== "") throw new Error(`#${id} is not empty in ${src}; bake from the template, not a baked page`);
    out = out.slice(0, m.start) + e.innerHTML + out.slice(m.end);
    filled++;
  }
  fs.writeFileSync(dst, out);
  console.log(`baked ${filled} elements into ${dst}`);
  return 0;
}

main(process.argv.slice(2)).then(code => process.exit(code), e => { console.error(e.stack || e); process.exit(1); });
