/* The pipeline report as a page. Every number is counted from the files in ../../data when the
   page loads: the stage manifests, the stage outputs, the truth files the scorer reads, and the
   runner's log. Nothing is typed in. compute() does the counting and knows nothing about the page;
   render() writes the page from what compute() returned. Run under node, compute() can be checked
   against the scorer's output without a browser. */
(function (root) {
"use strict";

const DATA = "../../data/";
const MANIFESTS = [0, 1, 2, 3, 4, 5, 6, 7, 8].map(n => `pipeline/0${n}_manifest.json`);
const FILES = {
  reads: "pipeline/05_reads.jsonl", realogram: "pipeline/08_realogram.jsonl", changes: "pipeline/07_catalog_changes.jsonl",
  run: "pipeline/run.txt", haiku: "pipeline/llm_measurement_claude-haiku-5-5.json", sonnet: "pipeline/llm_measurement_claude-sonnet-5-5.json",
  truth: "detections_truth.csv", shelf1: "shelf_truth_day1.csv", shelf2: "shelf_truth_day2.csv", shelf3: "shelf_truth_day3.csv",
  changesTruth: "catalog_changes_truth.csv", malformed: "malformed_truth.csv", baseline: "baseline_results.csv",
  v1: "catalog_raw.jsonl", v2: "catalog_v2.jsonl",
};
const SLOT_M = 0.20;
const STAGE_OF = { barcode: 2, "exact text": 2, "same spot": 3, "close spelling": 3, "other day": 4, "language model": 5 };
const METHODS = ["barcode", "exact text", "same spot", "close spelling", "other day", "language model"];
const RANK = { barcode: 1, "exact text": 2, "close spelling": 3, "same spot": 4, "other day": 5, "language model": 6 };
const COMPLIANT = ["as planned", "as planned, new size", "as planned, catalog dropped it"];
const KIND_OF = { "new size, new barcode": "new_size", renamed: "renamed", discontinued: "discontinued", new: "new" };

/* ---------- parsing ---------- */
function parseCSV(text) {
  const rows = []; let row = [], field = "", quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) { if (c === '"') { if (text[i + 1] === '"') { field += '"'; i++; } else quoted = false; } else field += c; }
    else if (c === '"') quoted = true;
    else if (c === ",") { row.push(field); field = ""; }
    else if (c === "\n") { row.push(field); rows.push(row); row = []; field = ""; }
    else if (c !== "\r") field += c;
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  const head = rows.shift();
  return rows.map(r => Object.fromEntries(head.map((h, i) => [h, r[i]])));
}
const parseJSONL = text => text.split("\n").filter(Boolean).map(line => JSON.parse(line));
const label = p => p ? [p.brands, p.product_name, p.quantity].map(x => (x || "").trim()).filter(Boolean).join(" · ") : "unknown";
const count = (items, key) => { const c = {}; for (const x of items) { const k = key(x); if (k !== undefined) c[k] = (c[k] || 0) + 1; } return c; };
const facingKey = r => [r.store_id, r.aisle, r.shelf, r.position, r.facing].join("|");
const slotKey = (store, aisle, shelf, slot) => [store, aisle, shelf, slot].join("|");
// Python's floor division on floats, which the pipeline uses for the slot: floor(a / b) differs from it when a is an
// exact multiple of b in decimal but not in binary (1.0 // 0.2 is 4.0 in Python; Math.floor(1.0 / 0.2) is 5).
function pyFloorDiv(a, b) {
  let mod = a % b, div = (a - mod) / b;
  if (mod && (b < 0) !== (mod < 0)) { mod += b; div -= 1; }
  if (!div) return 0;
  const floor = Math.floor(div);
  return div - floor > 0.5 ? floor + 1 : floor;
}
const slotOf = x => pyFloorDiv(x, SLOT_M) + 1;

/* ---------- counting ---------- */
function compute(F) {
  const M = F.manifests;
  const truth = new Map(parseCSV(F.truth).map(t => [t.detection_id, t]));
  const reads = parseJSONL(F.reads);
  const realogram = parseJSONL(F.realogram);
  const changes = parseJSONL(F.changes);
  const changesTruth = parseCSV(F.changesTruth);
  const malformed = parseCSV(F.malformed);
  const baseline = parseCSV(F.baseline);
  const products = new Map();
  for (const p of [...parseJSONL(F.v2), ...parseJSONL(F.v1)]) products.set(p.code, p);     // v1 wins where both list a code
  const shelf = {};
  for (const day of [1, 2, 3]) shelf[day] = new Map(parseCSV(F["shelf" + day]).map(t => [facingKey(t), t]));
  const lastDay = 3;
  const dates = [...new Set(realogram.filter(r => r.last_read).map(r => r.last_read.slice(0, 10)))].sort();
  const dayOf = date => dates.indexOf(date) + 1;
  const bySlot = new Map(realogram.map(r => [slotKey(r.store_id, r.aisle, r.shelf, r.slot), r]));
  const effective = M[7].input.catalog_v2_effective_from;

  // the run log: the stamp, the steps, the wall clock
  const runLines = F.run.split("\n");
  const stamp = (runLines[0].match(/^RUN\s+(\S+)\s+python (\S+)\s+(.*)$/) || []);
  const steps = runLines.filter(l => l.startsWith("====")).map(l => {
    const m = l.match(/^====\s+(.+?)\s+python (\S+(?: \S+)*?)\s+([\d.]+) s\s+exit (\d+)/);
    return m ? { name: m[1].replace(/\s+/g, " "), cmd: m[2], secs: +m[3], exit: +m[4] } : null;
  }).filter(Boolean);
  const done = (runLines.find(l => l.startsWith("DONE")) || "").match(/DONE\s+(\d+) steps\s+([\d.]+) s/) || [];
  const run = { stamp: stamp[1] || null, python: stamp[2] || null, platform: stamp[3] || null, steps, nSteps: +done[1] || steps.length, wall: +done[2] || null,
                allExitZero: steps.every(s => s.exit === 0) };

  // the reads, by the stage that named them: later stages never change an earlier answer, so a stage's total is every read whose method is in a stage at or before it
  const outcome = r => !r.match_code ? "no answer" : r.match_code === truth.get(r.detection_id).true_code ? "right" : "wrong";
  const ladder = [];
  const base = count(baseline, b => b.outcome);
  ladder.push({ stage: "naive baseline", right: base.right || 0, wrong: base.wrong || 0, open: base["no answer"] || 0, here: null });
  for (const n of [2, 3, 4, 5]) {
    const o = count(reads, r => r.match_method && STAGE_OF[r.match_method] <= n ? outcome(r) : "no answer");
    const here = METHODS.filter(m => STAGE_OF[m] === n).map(m => {
      const rs = reads.filter(r => r.match_method === m);
      return { method: m, answered: rs.length, wrong: rs.filter(r => outcome(r) === "wrong").length };
    });
    ladder.push({ stage: n, right: o.right || 0, wrong: o.wrong || 0, open: o["no answer"] || 0, here });
  }
  const total = reads.length;

  // every wrong read, and what the record at its spot made of it
  const wrongReads = reads.filter(r => outcome(r) === "wrong").map(r => {
    const t = truth.get(r.detection_id);
    const row = bySlot.get(slotKey(r.store_id, r.aisle, r.shelf, slotOf(r.position_x)));
    let fate;
    if (!row) fate = "no record at this spot";
    else if (row.verdict === "contested") fate = `the record at ${row.spot_id} is contested: both candidates named, no product recorded`;
    else if (row.product_code === t.true_code) fate = RANK[r.match_method] > RANK[row.method]
      ? `outranked: ${row.spot_id} records ${label(products.get(row.product_code))} by ${row.method} (${row.supporting} reads)`
      : `outvoted: ${row.spot_id} records ${label(products.get(row.product_code))} by ${row.method} (${row.supporting} reads)`;
    else fate = `NOT overruled: ${row.spot_id} records ${label(products.get(row.product_code))}`;
    return { id: r.detection_id, text: r.ocr_text, method: r.match_method, damage: t.damage,
             answered: label(products.get(r.match_code)), truth: label(products.get(t.true_code)), fate, overruled: !fate.startsWith("NOT") };
  });

  // the record, against the shelf truth: on the day its product was last seen, and against the last day
  const records = realogram.filter(r => r.spot_id);
  const rec = { right: 0, wrong: 0, contested: 0, open: 0, current: 0, emptied: 0, changed: 0, notSeenLatest: 0 };
  for (const r of records) {
    if (!r.product_code) { rec[r.verdict === "contested" ? "contested" : "open"]++; continue; }
    const t = shelf[dayOf(r.last_seen.slice(0, 10))].get(facingKey(r));
    const onLastDay = shelf[lastDay].get(facingKey(r)).actual_code;
    if (t.actual_code === r.product_code) rec.right++; else rec.wrong++;
    if (onLastDay === r.product_code) rec.current++; else if (onLastDay === "") rec.emptied++; else rec.changed++;
    if (r.seen_on_latest_walk === false) rec.notSeenLatest++;
  }
  // reads of two facings in one record
  const facingsOfSlot = new Map();
  for (const r of reads) {
    const t = truth.get(r.detection_id), k = slotKey(r.store_id, r.aisle, r.shelf, slotOf(r.position_x));
    if (!facingsOfSlot.has(k)) facingsOfSlot.set(k, new Set());
    facingsOfSlot.get(k).add(facingKey(t));
  }
  rec.mixed = [...facingsOfSlot.values()].filter(s => s.size > 1).length;

  // every verdict against the shelf on the last day
  const verdictTruth = {}; const stale = {};
  const bump = (o, k) => { o[k] = (o[k] || 0) + 1; };
  for (const r of realogram) {
    const t = shelf[lastDay].get(facingKey(r)), on = t.actual_code, v = r.verdict;
    let key;
    if (COMPLIANT.includes(v)) key = on === r.product_code ? "product on the shelf" : r.seen_on_latest_walk === false
      ? (on === "" ? "changed since last seen: empty now" : "changed since last seen: another product now") : "another product";
    else if (v === "different product") key = on === r.product_code && t.status === "misplaced" ? "that product, misplaced" : "wrong";
    else if (v === "contested") key = r.candidates.some(c => c.product_code === on) ? "truth is one of the candidates" : "truth is neither";
    else if (v === "open") key = on ? "a product stands here" : "shelf empty now";
    else key = [1, 2, 3].every(d => shelf[d].get(facingKey(r)).actual_code === "") ? "empty on every day" : "stocked on some day, never read";
    if (!verdictTruth[v]) verdictTruth[v] = {};
    bump(verdictTruth[v], key);
    if (r.seen_on_latest_walk === false) bump(stale, on === "" ? "shelf empty now" : on === r.product_code ? "product still there" : "another product now");
  }
  const contradicted = Object.values(verdictTruth).reduce((a, o) => a + (o["another product"] || 0) + (o.wrong || 0) + (o["truth is neither"] || 0) + (o["stocked on some day, never read"] || 0), 0);

  // compliance: the realogram's against the truth's, per store
  const successor = Object.fromEntries(changes.filter(c => c.change === "new size, new barcode").map(c => [c.v1_code, c.v2_code]));
  const compliance = {};
  for (const r of realogram) {
    const c = compliance[r.store_id] || (compliance[r.store_id] = { planned: 0, realogram: 0, truth: 0 });
    const t = shelf[lastDay].get(facingKey(r));
    c.planned++;
    if (COMPLIANT.includes(r.verdict)) c.realogram++;
    if (t.actual_code === t.planned_code || (t.actual_code && successor[t.planned_code] === t.actual_code)) c.truth++;
  }
  const openWithProduct = (verdictTruth.open || {})["a product stands here"] || 0;
  const contestedWithProduct = (verdictTruth.contested || {})["truth is one of the candidates"] || 0;
  const emptiedCompliant = COMPLIANT.reduce((a, v) => a + ((verdictTruth[v] || {})["changed since last seen: empty now"] || 0), 0);
  const changedCompliant = COMPLIANT.reduce((a, v) => a + ((verdictTruth[v] || {})["changed since last seen: another product now"] || 0), 0);

  // the catalog changes found, against the changes made
  const keyOf = (kind, a, b) => `${kind}|${a || ""}|${b || ""}`;
  const found = new Map(changes.map(c => [keyOf(KIND_OF[c.change] || c.change, c.v1_code, c.v2_code), c]));
  const wanted = new Map(changesTruth.map(t => [keyOf(t.change, t.v1_code, t.v2_code), t]));
  const matched = [...found.keys()].filter(k => wanted.has(k));
  const valuesRight = matched.filter(k => !wanted.get(k).field || (found.get(k).old === wanted.get(k).old_value && found.get(k).new === wanted.get(k).new_value)).length;
  const changeTable = { byKind: {}, matched: matched.length, missed: [...wanted.keys()].filter(k => !found.has(k)).length,
                        extra: [...found.keys()].filter(k => !wanted.has(k)).length, valuesRight, wanted: wanted.size, found: found.size };
  for (const k of new Set([...wanted.keys(), ...found.keys()].map(k => k.split("|")[0])))
    changeTable.byKind[k] = { truth: [...wanted.keys()].filter(x => x.startsWith(k + "|")).length, found: [...found.keys()].filter(x => x.startsWith(k + "|")).length,
                              matched: matched.filter(x => x.startsWith(k + "|")).length };

  // the language model measured on every spot stage 3 left open, both models
  const measurement = {};
  for (const name of ["haiku", "sonnet"]) {
    const m = JSON.parse(F[name]);
    const words = {};
    for (const a of Object.values(m.answers)) {
      const w = a.code ? a.confidence : "none";
      if (!words[w]) words[w] = { right: 0, wrong: 0 };
      if (!a.code) { words[w].right++; continue; }
      const ok = a.reads.every(id => truth.get(id).true_code === a.code);
      words[w][ok ? "right" : "wrong"]++;
    }
    measurement[name] = { model: m.model, spots: m.spots_asked, reads: m.reads_in_them, words, noReply: m.spots_without_reply.length, bill: m.bill };
  }

  // what was planted, against what was caught
  const truthRows = [...truth.values()];
  const spotOf = new Map(reads.map(r => [r.detection_id, r.spot_id]));
  const walkOf = new Map(reads.map(r => [r.detection_id, r.walk_id]));
  const bySpot = new Map();
  for (const r of reads) { if (!bySpot.has(r.spot_id)) bySpot.set(r.spot_id, []); bySpot.get(r.spot_id).push(r.detection_id); }
  const doubles = truthRows.filter(t => t.extra === "double_in_frame");
  const doublesJoined = doubles.filter(t => spotOf.get(t.detection_id) && spotOf.get(t.detection_id) === spotOf.get(t.duplicate_of)).length;
  const second = truthRows.filter(t => t.extra === "second_badge");
  const secondJoined = second.filter(t => (bySpot.get(spotOf.get(t.detection_id)) || []).some(id => walkOf.get(id) !== t.walk_id)).length;
  const resent = truthRows.filter(t => t.times_sent === "2").length;
  const barcodeChanged = [...shelf[1].values()].filter(t => { const n = shelf[2].get(facingKey(t)); return t.actual_code && n.actual_code && n.actual_code !== t.actual_code; }).length;
  const misplacedTruth = [...shelf[lastDay].values()].filter(t => t.status === "misplaced").length;
  const emptyAllDays = [...shelf[1].keys()].filter(k => [1, 2, 3].every(d => shelf[d].get(k).actual_code === "")).length;
  const likelyWrong = Object.entries(measurement.haiku.words).filter(([w]) => w !== "sure" && w !== "none").reduce((a, [, c]) => a + c.wrong, 0);
  const likelyAll = Object.entries(measurement.haiku.words).filter(([w]) => w !== "sure" && w !== "none").reduce((a, [, c]) => a + c.right + c.wrong, 0);
  const planted = {
    damaged: { planted: malformed.length, caught: M[0].quarantine.rows, kinds: count(malformed, m => m.kind) },
    resent: { planted: resent, caught: M[0].duplicates_dropped },
    idConflict: { planted: malformed.filter(m => m.kind === "id_conflict").length, caught: M[0].quarantine.by_reason.id_conflict || 0 },
    doubles: { planted: doubles.length, caught: doublesJoined },
    second: { planted: second.length, caught: secondJoined },
    barcodeChanged, history: M[6].records_with_history, newSize: M[7].records_by_catalog_status["new size since v2"] || 0,
    oldSize: M[7].records_by_catalog_status["old size, successor not read here"] || 0,
    leftovers: M[7].leftover_records.length, misplacedTruth, differentProduct: M[8].verdicts["different product"] || 0,
    emptyAllDays, neverRead: M[8].verdicts["never read"] || 0, likelyWrong, likelyAll,
    llmWrongAccepted: ladder[4].here.find(h => h.method === "language model").wrong,
  };

  return { M, run, total, ladder, wrongReads, rec, verdictTruth, stale, contradicted, compliance, openWithProduct, contestedWithProduct, emptiedCompliant,
           changedCompliant, changeTable, measurement, planted, effective, records: records.length, facings: realogram.length, dates,
           openSpots: M[8].open_spots, leftovers: M[8].catalog_dropped_spots, contested: M[8].contested_spots, different: M[8].different_product_spots };
}

/* ---------- the page ---------- */
const fmt = n => n === null || n === undefined ? "—" : typeof n === "number" ? n.toLocaleString("en-US") : String(n);
const pct = (a, b) => (100 * a / b).toFixed(1) + " %";
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function render(N, doc) {
  const $ = id => doc.getElementById(id);
  const el = (t, c, h) => { const e = doc.createElement(t); if (c) e.className = c; if (h !== undefined) e.innerHTML = h; return e; };
  const tbl = (node, head, rows, caption, right = []) => {
    const R = new Set(right); node.innerHTML = "";
    if (head) { const th = el("thead"); th.appendChild(el("tr", null, head.map((h, i) => `<th${R.has(i) ? ' class="n"' : ""}>${h}</th>`).join(""))); node.appendChild(th); }
    const tb = el("tbody"), width = head ? head.length : Math.max(...rows.map(r => Array.isArray(r) ? r.length : 1));
    rows.forEach(r => tb.appendChild(el("tr", null, typeof r === "string" ? `<td colspan="${width}" class="group">${r}</td>`
      : r.map((c, i) => `<td${R.has(i) ? ' class="n"' : ""}>${c}</td>`).join(""))));
    node.appendChild(tb);
    if (caption) node.appendChild(el("caption", null, caption));
  };
  const M = N.M, L = N.ladder, last = L[L.length - 1], m0 = M[0], m2 = M[2], m3 = M[3], m4 = M[4], m5 = M[5], m6 = M[6], m7 = M[7], m8 = M[8];
  const V = m8.verdicts, C = m8.compliance_all, rec = N.rec, P = N.planted;
  const truthAll = Object.values(N.compliance).reduce((a, c) => a + c.truth, 0);
  const stageNames = { 0: "ingest", 1: "normalize", 2: "exact match", 3: "close spelling", 4: "other days", 5: "language model", 6: "golden record", 7: "versioning", 8: "serve" };

  $("subtitle").textContent = `${N.M[0].input.lines.toLocaleString("en-US")} lines of reads from 3 stores on 3 days, one record per shelf spot, served against the planogram. Every number on this page is counted from the data files when it loads.`;
  $("runline").innerHTML = `<b>Run:</b> <code>${esc(N.run.stamp)}</code> · Python ${esc(N.run.python)} · ${esc(N.run.platform)} · one laptop · ${N.run.nSteps} steps · ${fmt(N.run.wall)} s wall clock` +
    (N.run.allExitZero ? "" : ` · <span class="err">a step failed</span>`);

  $("s1").innerHTML =
    `A badge camera worn by store staff reads shelf labels as they walk. Each read is a bounding box, a text, sometimes a barcode, a position and a time. ` +
    `The reads are damaged, doubled, re-sent and occasionally malformed, and the product catalog they are matched against changes between the days. ` +
    `The pipeline turns that feed into one record per shelf spot per store: which product stands there, how that was decided, which reads support it and since when, ` +
    `under which catalog version. Then it serves that realogram against the planogram as a compliance table per shelf. ` +
    `The question is not "what did the camera see" but "what stands on this shelf, and how sure are we". Every stage says what it could not decide, and why, instead of guessing. ` +
    `The scorer measures every answer against the simulation's truth, which no stage reads.`;

  tbl($("resultTable"), null, [
    ["Lines in the feed", fmt(m0.input.lines)],
    ["Usable reads, each once", `${fmt(m0.output.rows)} <span class="dim">· ${fmt(m0.duplicates_dropped)} re-sent frames dropped, ${fmt(m0.quarantine.rows)} damaged rows quarantined</span>`],
    ["Reads named right", `${fmt(last.right)} <span class="dim">· ${pct(last.right, N.total)}</span>`],
    ["Reads named wrong", `${fmt(last.wrong)} <span class="dim">· ${N.wrongReads.every(w => w.overruled) ? "every one overruled or held open in the record" : "see section 2"}</span>`],
    ["Reads left open", `${fmt(last.open)} <span class="dim">· in ${fmt(V.open)} spots nothing else could name</span>`],
    ["Shelf spots with a record", `${fmt(N.records)} <span class="dim">· ${fmt(rec.right)} right on the day last seen, ${fmt(rec.wrong)} wrong, ${fmt(rec.contested)} contested, ${fmt(rec.open)} open</span>`],
    ["Planned facings served", `${fmt(N.facings)} <span class="dim">· ${fmt(N.facings - N.contradicted)} verdicts stand against the shelf on the last day</span>`],
    ["Compliance", `${pct(C.compliant, C.planned_facings)} <span class="dim">· the shelf truth says ${pct(truthAll, C.planned_facings)}</span>`],
    ["Language model", `USD ${m5.bill.usd} <span class="dim">· ${fmt(m5.input.open_spots)} spots in ${fmt(m5.bill.calls)} calls, ${m5.bill.api_calls === 0 ? "every one answered from the cache in this run" : m5.bill.api_calls + " sent to the API"}</span>`],
  ]);
  tbl($("ladderTable"), ["After stage", "Right", "Wrong", "Open", "Right %", "Answered here by"],
    L.map(l => [l.stage === "naive baseline" ? "naive baseline (case-folded lookup)" : `${l.stage} ${stageNames[l.stage]}`, fmt(l.right), fmt(l.wrong), fmt(l.open), pct(l.right, N.total),
                l.here ? l.here.map(h => `${h.method} ${fmt(h.answered)}`).join(", ") : ""]), null, [1, 2, 3, 4]);
  $("s2a").innerHTML =
    `The record decides each spot from all its reads, by method rank then by count. Against the shelf truth on the day the product was last seen: ` +
    `${fmt(rec.right)} right, ${fmt(rec.wrong)} wrong, ${fmt(rec.contested)} contested, ${fmt(rec.open)} without an answer. The ${N.wrongReads.length} wrong reads above are in it:` +
    `<ul>${N.wrongReads.map(w => `<li><code>${esc(w.text)}</code> said ${esc(w.answered)} by ${w.method}; the label was ${esc(w.truth)}. ${esc(w.fate)}.</li>`).join("")}</ul>` +
    (N.wrongReads.every(w => w.overruled) ? `No wrong read became a wrong record.` : `A wrong read became a wrong record.`);
  $("s2b").innerHTML =
    `The catalog changed between day 1 and day 2: ${fmt(m7.output.changes.rows)} changes, found from the two catalog files alone, ${fmt(N.changeTable.matched)} of ${fmt(N.changeTable.wanted)} matching the changes the simulation made. ` +
    `Every record carries its product under both versions; the ${fmt(P.leftovers)} records holding the discontinued product are kept and say so.`;
  const vt = N.verdictTruth;
  const line = v => Object.entries(vt[v] || {}).sort((a, b) => b[1] - a[1]).map(([k, n]) => `${fmt(n)} ${k}`).join("; ");
  tbl($("verdictTable"), ["Verdict", "Facings", "Against the shelf on the last day"],
    Object.keys(V).map(v => [v, fmt(V[v]), line(v)]), null, [1]);
  tbl($("complianceTable"), ["Compliance", "Planned", "Realogram says", "Truth says"],
    [...Object.entries(N.compliance).map(([s, c]) => [s, fmt(c.planned), `${fmt(c.realogram)} (${pct(c.realogram, c.planned)})`, `${fmt(c.truth)} (${pct(c.truth, c.planned)})`]),
     ["all", fmt(C.planned_facings), `${fmt(C.compliant)} (${pct(C.compliant, C.planned_facings)})`, `${fmt(truthAll)} (${pct(truthAll, C.planned_facings)})`]], null, [1, 2, 3]);
  $("s2c").innerHTML =
    `The ${fmt(truthAll - C.compliant)}-facing gap: the realogram counts ${fmt(N.emptiedCompliant)} facings whose product has run out since it was last seen, and does not count the ` +
    `${fmt(N.openWithProduct)} open spots and the ${fmt(N.contestedWithProduct)} contested spot whose product is in fact the planned one. ` +
    `${fmt(truthAll)} − ${fmt(N.openWithProduct + N.contestedWithProduct)} + ${fmt(N.emptiedCompliant)} = ${fmt(truthAll - N.openWithProduct - N.contestedWithProduct + N.emptiedCompliant)}. ` +
    `The realogram marks the ${fmt(C.not_seen_on_latest_walk)} records not seen on the latest walk, which hold those ${fmt(N.stale["shelf empty now"] || 0)} empty facings, ` +
    `${fmt(N.stale["another product now"] || 0)} changed and ${fmt(N.stale["product still there"] || 0)} still stocked.`;

  $("s3").innerHTML =
    `Nine scripts, one per stage. Each reads only the files of the stages before it, writes one output and a manifest with its counts and its rules, prints CHECK lines that must all be ` +
    `true before it writes anything, and is scored against the truth before the next stage runs. The scorer is the only pipeline file that reads a truth file. Every rule with a number ` +
    `in it was measured on this feed before it was written, and the measurement is in the script's docstring. The runner runs the ${N.run.nSteps} steps in order and keeps what they printed:`;
  tbl($("stepTable"), ["Step", "Command", "Seconds"], N.run.steps.map(s => [s.name, `<code>${esc(s.cmd)}</code>`, s.secs.toFixed(2)]), null, [2]);

  $("s4").innerHTML =
    `The products are real: ${fmt(M[2].input.catalog_codes - 3)} catalog rows from Open Food Facts, Open Beauty Facts and Open Products Facts, stored as their source returned them. ` +
    `Everything else is simulated, and the simulation page's "Verified and assumed" list says what is checked and what is made up.`;
  tbl($("dataTable"), null, [
    ["Stores, shelves", `3 stores sharing one planogram of ${fmt(N.facings / 3)} facings each: ${fmt(N.facings)} planned facings of ${SLOT_M.toFixed(2)} m`],
    ["Walks", `3 full walks per store, one per day (${N.dates.join(", ")}), plus a second badge on one aisle of each store on day 1`],
    ["The feed", `${fmt(m0.input.lines)} lines: ${fmt(m0.output.rows)} distinct reads, ${fmt(m0.duplicates_dropped)} frames sent twice byte for byte, ${fmt(m0.quarantine.rows)} rows damaged on the way`],
    ["Damage on the way", Object.entries(P.damaged.kinds).map(([k, n]) => `${k.replace(/_/g, " ")} ${n}`).join(", ")],
    ["Reads with a barcode", `${fmt(m2.by_method.barcode)} of ${fmt(m2.input.rows)}`],
    ["Doubled in the frame", `${fmt(P.doubles.planted)} reads; second badge ${fmt(P.second.planted)} reads`],
    ["The catalog", `v1 ${fmt(M[2].input.catalog_codes - 3)} rows as pulled; v2 in effect from ${N.effective}: ${Object.entries(m7.changes_by_kind).map(([k, n]) => `${k} ${n}`).join(", ")}. The planogram is not updated.`],
    ["Shelf problems", `${fmt(P.emptyAllDays)} facings empty on every day; ${fmt(P.misplacedTruth)} units misplaced; ${fmt(P.barcodeChanged)} facings whose barcode changed between day 1 and day 2`],
    ["Truth, for the scorer only", `detections_truth.csv, shelf_truth_day1/2/3.csv, malformed_truth.csv, catalog_changes_truth.csv`],
  ]);

  const here = n => L.find(l => l.stage === n).here;
  const after = n => L.find(l => l.stage === n);
  const hm = (n, m) => here(n).find(h => h.method === m);
  const stages = [
    { n: 0, title: "Ingest", file: "pipeline/ingest.py",
      does: `A usability rule per field: type, blank, a bounding box of four integers, the one timestamp form the badge writes, confidence in 0..1, shelf ≥ 1, x/y/z ≥ 0, barcode digits, text or barcode present. Same id and same content is a re-sent frame and is dropped; same id and different content is an id conflict, the first row kept, the second quarantined. Rows sorted by time then id.`,
      rule: `Every line is accounted for: a read out, a duplicate dropped, or quarantined with a reason.`,
      rows: [["Lines in", fmt(m0.input.lines)], ["Reads out", fmt(m0.output.rows)], ["Re-sent frames dropped", fmt(m0.duplicates_dropped)],
             ["Quarantined", `${fmt(m0.quarantine.rows)} <span class="dim">· ${Object.entries(m0.quarantine.by_reason).map(([k, n]) => `${k.replace(/_/g, " ")} ${n}`).join(", ")}</span>`],
             ["Planted damaged rows", `${fmt(P.damaged.planted)} <span class="dim">· quarantined ${fmt(P.damaged.caught)}</span>`]] },
    { n: 1, title: "Normalize", file: "pipeline/normalize.py",
      does: `One text form, applied to reads and catalog alike: NFKD, accents dropped, case-folded, decimal comma to dot, punctuation to space except a decimal point inside a number, number and unit to "n unit" with one spelling per unit, single spaces. Measured first: the feed's unit spellings, its punctuation, its accents. No brand alias table: measured, none was needed.`,
      rule: `The catalog is normalized by the same function as the reads, so a match is a match of forms, not of spellings.`,
      rows: [["Texts changed", `${fmt(M[1].text_changed)} <span class="dim">· beyond case alone ${fmt(M[1].text_changed_beyond_case)}</span>`],
             ["Distinct texts", `${fmt(M[1].distinct_texts.in)} → ${fmt(M[1].distinct_texts.out)}`],
             ["Rules fired", Object.entries(M[1].rules_fired).map(([k, n]) => `${k} ${fmt(n)}`).join(", ")]] },
    { n: 2, title: "Exact match", file: "pipeline/match_exact.py",
      does: `A barcode in the catalog wins, after the GS1 check digit on 12-, 13- and 14-digit codes. Then the normalized text against every catalog row's normalized label, over v1 and v2 together, one answer only when exactly one code fits.`,
      rule: `Exact text cannot tell a cut label from a short one; the record settles those later.`,
      rows: [["Answered", `${fmt(m2.answered)} <span class="dim">· barcode ${fmt(m2.by_method.barcode)}, exact text ${fmt(m2.by_method["exact text"])}</span>`],
             ["Open", `${fmt(m2.by_method["no answer"])} <span class="dim">· ${Object.entries(m2.no_answer_by_note).map(([k, n]) => `${k} ${fmt(n)}`).join("; ")}</span>`],
             ["Barcode and text both present", `${fmt(m2.barcode_and_text_both)} <span class="dim">· disagreeing ${fmt(m2.barcode_and_text_disagree)}</span>`],
             ["Against the truth", `${fmt(after(2).right)} right, ${fmt(after(2).wrong)} wrong, ${fmt(after(2).open)} open <span class="dim">· wrong by exact text ${fmt(hm(2, "exact text").wrong)}, by barcode ${fmt(hm(2, "barcode").wrong)}</span>`]] },
    { n: 3, title: "Close spelling, and the shelf spot", file: "pipeline/match_fuzzy.py",
      does: `First the spot: two reads are the same facing when they are in the same frame with box overlap ≥ ${m3.rules.box_overlap} (the doubled detections) or from different walks on the same store, day, aisle and shelf within ${m3.rules.same_spot_m} m (the second badge). An answered read names its spot's open reads. Then the spelling: ${m3.rules.scorer} against the catalog rows sharing the read's ${m3.rules.block}, score ≥ ${m3.rules.score_min} and at least ${m3.rules.gap_min} ahead of the runner-up. The curve across scorers, blockings, thresholds and gaps is in the scorer (<code>score.py fuzzy --curve</code>).`,
      rule: `The gap to the runner-up is the guard, not the threshold: with no gap every threshold gives about 190 wrong.`,
      rows: [["Spots", `${fmt(m3.spots.count)} <span class="dim">· ${Object.entries(m3.spots.sizes).map(([k, n]) => `${k} read${k > 1 ? "s" : ""} ${fmt(n)}`).join(", ")}</span>`],
             ["Answered here", `${fmt(m3.answered_here["same spot"] + m3.answered_here["close spelling"])} <span class="dim">· same spot ${fmt(m3.answered_here["same spot"])}, close spelling ${fmt(m3.answered_here["close spelling"])}</span>`],
             ["Doubled boxes joined", `${fmt(P.doubles.caught)} of ${fmt(P.doubles.planted)} <span class="dim">· second-badge reads joined to the first badge's ${fmt(P.second.caught)} of ${fmt(P.second.planted)}</span>`],
             ["Against the truth", `${fmt(after(3).right)} right, ${fmt(after(3).wrong)} wrong, ${fmt(after(3).open)} open <span class="dim">· wrong by close spelling ${fmt(hm(3, "close spelling").wrong)}, by same spot ${fmt(hm(3, "same spot").wrong)}</span>`]] },
    { n: 4, title: "Other days", file: "pipeline/match_crossday.py",
      does: `An open read sits where other walks, on other days, read a product the free checks named: the answered reads within ${m4.rules.same_position_m} m on the same store, aisle and shelf on other days, when they all name one product, when that product was in the catalog version in effect on the read's day, and when the read's own text scores ≥ ${m4.rules.text_min} against it. It runs before the language model because it costs nothing.`,
      rule: `The code must have been in the catalog on the read's day: without that guard the neighbour names the new barcode for a read of the old pack.`,
      rows: [["Answered here", fmt(m4.answered_here)],
             ["Left open", Object.entries(m4.left_open_by_reason).map(([k, n]) => `${k} ${fmt(n)}`).join(", ")],
             ["Against the truth", `${fmt(after(4).right)} right, ${fmt(after(4).wrong)} wrong, ${fmt(after(4).open)} open <span class="dim">· wrong here ${fmt(hm(4, "other day").wrong)}</span>`]] },
    { n: 5, title: "Language model", file: "pipeline/match_llm.py",
      does: `Only what nothing else could name: ${fmt(m5.input.open_reads)} reads in ${fmt(m5.input.open_spots)} spots. Each spot is shown its ${m5.rules.candidates} closest catalog rows, ${m5.rules.batch} spots per call, and must answer with one of them or none, with a confidence word. Accepted: ${m5.rules.accept.map(a => `"${a}"`).join(", ")}, the one word with no wrong answer on either model in the measurement below. Model ${m5.rules.model}, effort ${m5.rules.effort}. Prompts are cached by hash, so a rerun makes no API call.`,
      rule: `An answer must be one of the candidates shown; a reply the model cut short is refused and not cached; a spot the model leaves out is counted.`,
      rows: [["Calls", `${fmt(m5.bill.calls)} <span class="dim">· to the API ${fmt(m5.bill.api_calls)}, from the cache ${fmt(m5.bill.cached_calls)}</span>`],
             ["Tokens", `${fmt(m5.bill.input_tokens)} in, ${fmt(m5.bill.output_tokens)} out <span class="dim">· USD ${m5.bill.usd}</span>`],
             ["Answered here", `${fmt(m5.answered_here)} <span class="dim">· ${Object.entries(m5.spots_by_outcome).map(([k, n]) => `${k} ${fmt(n)}`).join(", ")}</span>`],
             ["Against the truth", `${fmt(after(5).right)} right, ${fmt(after(5).wrong)} wrong, ${fmt(after(5).open)} open <span class="dim">· wrong here ${fmt(hm(5, "language model").wrong)}</span>`]] },
    { n: 6, title: "Golden record", file: "pipeline/golden.py",
      does: `A spot is a store, aisle, shelf and a ${m6.rules.slot_m.toFixed(2)} m slot. The reads of a spot vote by method rank: ${Object.keys(m6.rules.method_rank).join(", ")}; among equals the most reads; an even split is contested and the record names both. A read whose code is not in the catalog version in effect on the spot's latest day steps aside when a current code competes and becomes history.`,
      rule: `${m6.rules.survivorship}.`,
      rows: [["Records", `${fmt(m6.output.records)} <span class="dim">· ${Object.entries(m6.records_by_status).map(([k, n]) => `${k} ${fmt(n)}`).join(", ")}</span>`],
             ["Decided by", Object.entries(m6.records_by_method).sort((a, b) => RANK[a[0]] - RANK[b[0]]).map(([k, n]) => `${k} ${fmt(n)}`).join(", ")],
             ["History", `${fmt(m6.records_with_history)} records <span class="dim">· a product the catalog has since replaced</span>`],
             ["Against the shelf truth", `${fmt(rec.right)} right on the day last seen, ${fmt(rec.wrong)} wrong, ${fmt(rec.contested)} contested, ${fmt(rec.open)} open <span class="dim">· against the last day: ${fmt(rec.current)} current, ${fmt(rec.emptied)} emptied since, ${fmt(rec.changed)} changed since</span>`],
             ["Records holding a read of a neighbouring facing", fmt(rec.mixed)]] },
    { n: 7, title: "Versioning", file: "pipeline/versioning.py",
      does: `The two catalog files compared. A code in both whose brand, name or quantity differs is renamed. ${m7.rules.new_size.charAt(0).toUpperCase() + m7.rules.new_size.slice(1)}: a new size, and the GS1 rule gives it a new barcode. ${m7.rules.unpaired.charAt(0).toUpperCase() + m7.rules.unpaired.slice(1)}. Each record gains its product under v1 and v2, a catalog status, and a label ${m7.rules.label}. A test rebuilds the realogram from the reads before the change with a catalog that knows only v1, applies the changes, and checks that no record is lost.`,
      rule: `A record whose product the catalog dropped is not dropped: the stock is still on the shelf, so the record keeps it and says the catalog no longer carries it.`,
      rows: [["Changes found", `${fmt(m7.output.changes.rows)} <span class="dim">· ${Object.entries(m7.changes_by_kind).map(([k, n]) => `${k} ${fmt(n)}`).join(", ")}</span>`],
             ["Against the changes made", `${fmt(N.changeTable.matched)} of ${fmt(N.changeTable.wanted)} <span class="dim">· missed ${fmt(N.changeTable.missed)}, extra ${fmt(N.changeTable.extra)}, old and new values right ${fmt(N.changeTable.valuesRight)}</span>`],
             ["Records by catalog status", Object.entries(m7.records_by_catalog_status).map(([k, n]) => `${k} ${fmt(n)}`).join("; ")],
             ["Relabelled to the version in effect", fmt(m7.records_relabelled)],
             ["History explained by a change", `${fmt(m7.history_explained_by_a_change)} <span class="dim">· not explained ${fmt(m7.history_not_explained)}</span>`]] },
    { n: 8, title: "Serve", file: "pipeline/serve.py",
      does: `DuckDB, in memory, over the record, the reads, the planogram and both catalogs. The planogram's positions expand to ${fmt(m8.output.realogram.rows)} facings, one slot each. Every facing gets a verdict from the record at its spot; every shelf a compliance row. The open spots, the contested spot and the dropped products are listed, not hidden. <code>--sql</code> runs any query against the same database.`,
      rule: `A record whose product was last seen on an earlier day is marked as not seen on the latest walk: the badge gives no read for an empty facing, so the record cannot tell a product that ran out from one it missed.`,
      rows: [["Planned facings", `${fmt(m8.output.realogram.rows)} <span class="dim">· with a record ${fmt(N.records)}, never read ${fmt(V["never read"])}</span>`],
             ["Verdicts", Object.entries(V).map(([k, n]) => `${k} ${fmt(n)}`).join("; ")],
             ["Compliance", `${fmt(C.compliant)} of ${fmt(C.planned_facings)}, ${pct(C.compliant, C.planned_facings)} <span class="dim">· ${Object.entries(m8.compliance_by_store).map(([s, c]) => `${s} ${c.compliance_pct} %`).join(", ")}</span>`],
             ["Against the shelf on the last day", `${fmt(N.facings - N.contradicted)} of ${fmt(N.facings)} verdicts stand`],
             ["Engine", esc(m8.engine)]] },
  ];
  const sw = $("stages"); sw.innerHTML = "";
  for (const st of stages) {
    sw.appendChild(el("h3", null, `5.${st.n} ${st.title}`));
    sw.appendChild(el("p", null, st.does));
    sw.appendChild(el("p", "rule-line", `<b>Rule:</b> ${st.rule}`));
    const wrap = el("div", "tw"), t = el("table", "small"); tbl(t, null, st.rows); wrap.appendChild(t); sw.appendChild(wrap);
    sw.appendChild(el("p", "fileref", `<code>${st.file}</code>`));
  }

  $("s6a").innerHTML = `What the simulation planted or the days changed, against what the pipeline did with it. Nothing in the pipeline reads the plant list; the counts on the left come from the truth files, the counts on the right from the stage outputs.`;
  tbl($("plantTable"), ["Planted or happened", "Count", "Caught", "By what"], [
    ["Damaged rows, 7 kinds", fmt(P.damaged.planted), fmt(P.damaged.caught), "a usability rule per field, stage 0"],
    ["Frames sent twice", fmt(P.resent.planted), fmt(P.resent.caught), "same id, same content"],
    ["An id reused with other content", fmt(P.idConflict.planted), fmt(P.idConflict.caught), "same id, different content; the first row kept"],
    ["A detection doubled in its frame", fmt(P.doubles.planted), fmt(P.doubles.caught) + " joined", `box overlap ≥ ${m3.rules.box_overlap} in the same frame`],
    ["A second badge reading the same aisle", fmt(P.second.planted) + " reads", fmt(P.second.caught) + " joined", `the same shelf within ${m3.rules.same_spot_m} m, another walk`],
    ["A product's barcode changed between the days", fmt(P.barcodeChanged) + " facings", `${fmt(P.newSize)} records hold the new code, ${fmt(P.history)} with the old code as history; ${fmt(P.oldSize)} holds the old code and names its successor`, "the catalog-aware vote and the lineage"],
    ["A cut label equal to a shorter product, a misspelling closer to the wrong one", fmt(N.wrongReads.length) + " reads", `${fmt(N.wrongReads.filter(w => w.overruled).length)} overruled or held contested`, "the record's vote by rank, then count"],
    ["A model answer that is a guess", `${fmt(P.likelyWrong)} of ${fmt(P.likelyAll)} "likely" or "unsure" wrong`, `${fmt(P.llmWrongAccepted)} wrong accepted`, `measured first; "${m5.rules.accept.join('", "')}" only`],
    ["A product discontinued, stock left on the shelf", fmt(P.leftovers) + " spots", fmt(P.leftovers) + " kept and flagged", "the catalog diff; the record is not dropped"],
    ["The planogram still carrying an old barcode", fmt(V["as planned, new size"]) + " facings", fmt(V["as planned, new size"]) + ' "as planned, new size"', "the lineage"],
    ["A unit in the wrong aisle", fmt(P.misplacedTruth), fmt(P.differentProduct) + ' "different product"', "the planogram join"],
    ["A facing emptied after its product was last seen", fmt(N.stale["shelf empty now"] || 0), `among the ${fmt(C.not_seen_on_latest_walk)} "not seen on the latest walk"`, "the record says what it cannot know"],
    ["A facing empty on every day", fmt(P.emptyAllDays), fmt(P.neverRead) + ' "never read"', "no record"],
  ], null, [1]);
  $("s6b").innerHTML = `The rows worth reading are the ones where the right behaviour is to say nothing. The contested spot, where one right read and one wrong read of the same rank tie and the record names both instead of picking:` +
    `<ul>${N.contested.map(c => `<li><code>${esc(c.spot_id)}</code> · candidates ${c.candidates.map(esc).join(" and ")}</li>`).join("")}</ul>` +
    `The ${fmt(N.openSpots.length)} open spots, where every read was a cut or a misspelling that fits several catalog rows:` +
    `<ul class="open">${N.openSpots.map(s => `<li><code>${esc(s.spot_id)}</code> · ${s.reads} read${s.reads > 1 ? "s" : ""}: ${s.texts.map(t => `<code>${esc(t)}</code>`).join(" · ")}</li>`).join("")}</ul>` +
    `The ${fmt(N.leftovers.length)} spots holding the product the catalog dropped: ${N.leftovers.map(s => `<code>${esc(s)}</code>`).join(", ")}. ` +
    `The ${fmt(N.different.length)} spots holding a different product than planned: ${N.different.map(d => `<code>${esc(d.spot_id)}</code>`).join(", ")}. ` +
    `And the ${fmt(C.not_seen_on_latest_walk)} records marked as not seen on the latest walk: had the record reported them as current, it would be wrong ${fmt((N.stale["shelf empty now"] || 0) + (N.stale["another product now"] || 0))} times.`;

  const bugs = [
    ["The baseline let a planted row overwrite a real read.", "The naive baseline took the last row per id; the planted id conflict replaced a real read's answer. Fixed: first row wins, the same rule stage 0 applies."],
    ["The replay page parsed every line.", "Once the feed carried the cut lines, the page's JSON parser failed on load. Fixed: the page skips a line by the same usability rule as stage 0, and says how many it skipped."],
    ["The catalog's effective date was a day number.", "The v2 metadata said the catalog took effect on day 2; the pipeline sees timestamps and has no day numbers. Fixed in the simulation: it writes the date, and stage 4 onward read it."],
    ["Eleven of the twenty eight-digit codes fail the GTIN-8 check digit.", "They are not GS1 codes. The check is applied to 12-, 13- and 14-digit codes only; eight-digit barcodes are matched as strings. The check was made to fire on a known-bad code before it was trusted."],
    ["Two catalog rows have the same label.", `siggi's skyr appears as "24 oz" and "24oz" under two codes. ${fmt(m2.no_answer_by_note["no barcode, text fits 2 rows"] || 0)} reads fit both; the pipeline leaves them open and the manifest counts them, rather than picking one.`],
    ["Four API behaviours the stage did not expect.", "A non-default temperature is rejected on these models; the model's thinking counts toward the token budget and cut a reply short; one reply carried a remark after its JSON array; one reply left a spot out. Fixed: effort control instead of temperature, a larger budget and a refusal on any stop reason but a finished turn, parsing from the first bracket, and a no-reply count. One batch was paid twice before the parser fix, about 4 cents."],
    ["The versioning test loaded the catalog labels once per record.", "13.3 s in the first full run, for 1,714 records. Loaded once: under a second. Found by the runner's timing column."],
    ["The serve scorer called a stale record wrong.", "One record holds the old size of a product, last seen on day 1, and says it was not seen on the latest walk; the shelf holds the new size now. The first scorer counted that as a contradiction. It is not: the record reports exactly what it could know. The scorer now separates \"changed since last seen\" from \"wrong\", and only a record that claims to have seen its product on the latest walk can be wrong."],
  ];
  $("s7").innerHTML = ""; bugs.forEach(([h, p]) => $("s7").appendChild(el("p", null, `<b>${h}</b> ${p}`)));

  $("s8").innerHTML = "";
  [
    `<b>This is a simulation, and its labels are easy.</b> A read's text is the catalog row's brand, name and size with damage applied. Real label text differs from catalog rows far more than that, so exact text and close spelling would name fewer reads on real images, and the language model and an image match would carry more.`,
    `<b>${fmt(last.wrong)} reads are wrong and ${fmt(last.open)} are open.</b> The pipeline does not reach every read; the record reaches every spot but ${fmt(rec.open)} and ${fmt(rec.contested)}. The open spots hold cut labels that fit several sizes and the twin catalog rows; they need an image, a barcode, or a catalog fix, not a better text rule.`,
    `<b>The record cannot see an empty facing.</b> The badge gives no read where nothing stands, so a product that ran out after it was last seen stays in the record, marked as not seen on the latest walk: ${fmt(C.not_seen_on_latest_walk)} such records, ${fmt(N.stale["shelf empty now"] || 0)} of them empty now. Compliance from the record is ${(100 * (truthAll - C.compliant) / C.planned_facings).toFixed(1)} points under the truth for this reason and for the ${fmt(N.openWithProduct + N.contestedWithProduct)} unsettled spots.`,
    `<b>The spot is a ${SLOT_M.toFixed(2)} m slot.</b> ${fmt(rec.mixed)} of ${fmt(N.records)} records hold one read that jittered in from the neighbouring facing. Slots work because the simulation's facings are all ${SLOT_M.toFixed(2)} m wide and start at the shelf's edge; a store's own fixture geometry would replace the constant.`,
    `<b>The planogram is the simulation's and is not updated.</b> The verdicts "as planned, new size" and "as planned, catalog dropped it" exist because of that; a maintained planogram would turn them into plain "as planned" and "not planned".`,
    `<b>The language model's numbers are from one measurement of ${fmt(N.measurement.haiku.spots)} spots on two models on one day.</b> Model behaviour and prices move. The cache makes this run reproducible without a key; a new feed needs the key and pays again.`,
    `<b>Two rules were measured with scratch scripts, not repo commands.</b> The survivorship comparison in stage 6 and the pairing table in stage 7 are recorded in the docstrings; the chosen rules' numbers are on this page, the alternatives' are not reproducible by a command.`,
    `<b>One run, one laptop, files on disk.</b> No stream, no concurrency, no clock skew between badges. ${fmt(N.run.wall)} seconds for ${fmt(N.total)} reads says nothing about 5 million.`,
  ].forEach(p => $("s8").appendChild(el("p", null, p)));

  $("s9").innerHTML = `Each substitution changes what moves or holds the data, not what is done to it.`;
  tbl($("prodTable"), ["Today", "Production", "Why the logic is unchanged"], [
    ["A file of reads", "A stream", "Stage 0's per-field rule applies at the edge; the quarantine and the counts stay, emitted as metrics instead of printed."],
    ["Stage files on disk", "Tables in a warehouse", "The stage boundaries and manifests stay. DuckDB stays for serving and ad-hoc queries, over the warehouse's tables."],
    ["Two catalog files", "The retailer's product master and its change feed", "The diff in stage 7 becomes the check that the feed is complete, not the source."],
    [`A ${SLOT_M.toFixed(2)} m slot`, "The store's fixture geometry", "The spot is still a store, aisle, shelf and position; only the constant goes."],
    ["Live calls, cached by prompt", "The Batch API with prompt caching", "Same prompt, same candidates-only rule, same accepted word, same cache key; half the price."],
    ["Text only", "An image match before the language model", "The badge has the frame; this pipeline does not."],
    ["A truth file", "A sampled audit", "No truth exists in production. The record's open and contested counts become the thing watched."],
    ["A runner script", "An orchestrator and CI", "The same scripts in the same order; the test in CI; byte-identity of each stage's output as the regression test it already is."],
  ]);

  tbl($("fileTable"), null, [
    ["<code>pipeline/ingest.py</code> … <code>pipeline/serve.py</code>", "the nine stages, one script each; their outputs and manifests in <code>data/pipeline/</code>"],
    ["<code>pipeline/score.py</code>", "the scorer, the only reader of the truth files"],
    ["<code>pipeline/test_versioning.py</code>", "the record survives the catalog change"],
    ["<code>pipeline/run.py</code>", `every stage and scorer in order → <code>data/pipeline/run.txt</code>, the log this page reads for the run line`],
    ["<code>REPORT.md</code>", "this report as a document in the repository, from the same run"],
    [`<a href="../">The simulation</a>`, "the store, the walks, the feed as it arrives"],
    [`<a href="../?view=full&map=pipeline">The verdict map</a>`, "the full store with every facing coloured by the stage that decided it"],
  ]);
}

/* ---------- loading ---------- */
async function loadAll(fetchText) {
  const names = { ...FILES };
  const manifests = await Promise.all(MANIFESTS.map(n => fetchText(DATA + n)));
  const texts = await Promise.all(Object.values(names).map(n => fetchText(DATA + n)));
  const F = Object.fromEntries(Object.keys(names).map((k, i) => [k, texts[i]]));
  F.manifests = manifests.map(t => JSON.parse(t));
  return F;
}

async function boot() {
  try {
    const F = await loadAll(async url => { const r = await fetch(url, { cache: "no-store" }); if (!r.ok) throw new Error(`${url} — HTTP ${r.status}`); return r.text(); });
    render(compute(F), document);
  } catch (e) {
    const err = document.getElementById("err"); err.hidden = false;
    err.textContent = `Could not load the run: ${e.message}. Run python pipeline/run.py, then serve the repository root over http and open /web/pipeline/.`;
  }
}

root.PipelinePage = { compute, render, loadAll, parseCSV, parseJSONL };
if (typeof document !== "undefined") boot();
})(typeof globalThis !== "undefined" ? globalThis : this);
