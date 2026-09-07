/* nosqlmigrate SPA — paste DDL → decisions, artifacts, trade-offs. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { result: null };

/* ---------------------------------------------------------------- bootstrap */

document.querySelectorAll(".samples button").forEach((btn) => {
  btn.addEventListener("click", () => loadSample(btn.dataset.sample));
});

$("#analyze").addEventListener("click", analyze);
for (const tab of document.querySelectorAll("#tabs button")) {
  tab.addEventListener("click", () => showTab(tab.dataset.tab));
}

if (window.mermaid) {
  mermaid.initialize({ startOnLoad: false, theme: "default", securityLevel: "loose" });
}

async function loadSample(name) {
  setStatus(`loading ${name}…`);
  const res = await fetch("/api/samples");
  const samples = await res.json();
  const sample = samples[name];
  if (!sample) return setStatus(`unknown sample ${name}`, true);
  $("#ddl").value = sample.schema;
  $("#queries").value = sample.queries;
  $("#dialect").value = "auto";
  setStatus(`loaded ${name} — hit Analyze →`);
}

async function analyze() {
  const ddl = $("#ddl").value.trim();
  if (!ddl) return setStatus("paste a schema (or load a sample) first", true);
  const btn = $("#analyze");
  btn.disabled = true;
  setStatus("analyzing…");
  try {
    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ddl,
        queries: $("#queries").value,
        dialect: $("#dialect").value,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    state.result = await res.json();
    renderAll(state.result);
    setStatus(
      `${state.result.report.summary.tables} tables · ` +
      `${state.result.report.summary.collections} collections · ` +
      `${Math.round(state.result.report.summary.single_lookup_read_fraction * 100)}% single-collection reads`,
    );
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    btn.disabled = false;
  }
}

function setStatus(msg, isError = false) {
  const el = $("#status");
  el.textContent = msg;
  el.classList.toggle("error", isError);
}

function showTab(name) {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
}

function renderAll(result) {
  renderOverview(result);
  renderDecisions(result.report.decisions);
  renderDocument(result.report.document);
  renderColumnar(result.artifacts.cassandra_cql, result.artifacts.cassandra_warnings, result.artifacts.write_amplification);
  renderGraph(result.artifacts.neo4j_cypher);
  renderReport(result.markdown);
  showTab("overview");
}

/* ---------------------------------------------------------------- overview */

function renderOverview(result) {
  const s = result.report.summary;
  const pct = Math.round(s.single_lookup_read_fraction * 100);
  $("#tab-overview").innerHTML = `
    <div class="stats">
      <div class="stat"><div class="num">${s.tables}</div><div class="lbl">tables in</div></div>
      <div class="stat"><div class="num">${s.relationships}</div><div class="lbl">relationships</div></div>
      <div class="stat"><div class="num">${s.workload.queries}</div><div class="lbl">queries</div></div>
      <div class="stat"><div class="num">${s.collections}</div><div class="lbl">collections out</div></div>
      <div class="stat"><div class="num good">${pct}%</div><div class="lbl">reads, single collection</div></div>
    </div>
    <h3 class="section">Source schema (ER)</h3>
    <div class="mermaid" id="er"></div>
    <h3 class="section">Table dispositions — nothing silently vanishes</h3>
    <table>
      <thead><tr><th>table</th><th>disposition</th><th>into</th><th>rule</th><th>reason</th></tr></thead>
      <tbody>
        ${result.report.dispositions.map((d) => `
          <tr>
            <td class="mono">${esc(d.table)}</td>
            <td><span class="badge ${d.disposition}">${d.disposition}</span></td>
            <td class="mono">${esc(d.into || "—")}</td>
            <td><span class="badge rule">${d.rule_id}</span></td>
            <td>${esc(d.reason)}</td>
          </tr>`).join("")}
      </tbody>
    </table>
    ${result.report.consistency_risks.length ? `
      <h3 class="section">Consistency risks (denormalization debt)</h3>
      <ul>${result.report.consistency_risks.map((r) =>
        `<li><b>${esc(r.kind)}</b> <span class="badge rule">${r.rule}</span> ${esc(r.detail)}</li>`).join("")}</ul>` : ""}
    ${result.report.unmigratable.length ? `
      <h3 class="section">Handle in the application layer</h3>
      <ul>${result.report.unmigratable.map((u) =>
        `<li><span class="badge rule">${u.code}</span> ${esc(u.message)}</li>`).join("")}</ul>` : ""}
  `;
  renderMermaid(result.er_mermaid);
}

async function renderMermaid(text) {
  const el = $("#er");
  if (!el) return;
  if (window.mermaid) {
    try {
      const { svg } = await mermaid.render("er-svg", text);
      el.innerHTML = svg;
      return;
    } catch (e) { /* fall through to raw */ }
  }
  el.outerHTML = `<pre class="code">${esc(text)}</pre>`;
}

/* ---------------------------------------------------------------- decisions */

function renderDecisions(decisions) {
  $("#tab-decisions").innerHTML = decisions.map((d, i) => `
    <div class="card" id="dec-${i}">
      <div class="head" onclick="document.getElementById('dec-${i}').classList.toggle('open')">
        <span class="subject">${esc(d.subject)}</span>
        <span class="badge ${d.action}">${d.action}</span>
        <span class="badge rule">${d.rule_id} ${esc(d.rule_name)}</span>
        <span class="badge conf-${d.confidence}">${d.confidence}</span>
        <span class="arrow">▸</span>
      </div>
      <div class="body">
        <div class="signals">${signalChips(d.signals)}</div>
        <div class="gain">${esc(d.gains)}</div>
        <div class="cost">${esc(d.costs)}</div>
        <div class="mit">${esc(d.mitigations)}</div>
        ${d.near_misses.length ? `
          <div class="whynot">
            <h4>why not the others?</h4>
            <ul>${d.near_misses.map((n) =>
              `<li><span class="badge rule">${n.rule_id}</span> ${esc(n.rule_name)}: ${esc(n.detail)}</li>`).join("")}</ul>
          </div>` : ""}
      </div>
    </div>`).join("");
}

function signalChips(signals) {
  const keys = ["cardinality", "co_access", "child_standalone", "write_ratio_child",
    "fanout_min", "fanout_max", "unbounded", "bounded_side", "host", "duplicated_fields"];
  return keys.filter((k) => signals[k] !== undefined)
    .map((k) => {
      let v = signals[k];
      if (typeof v === "number") v = Math.round(v * 100) / 100;
      if (Array.isArray(v)) v = v.join(", ");
      if (typeof v === "boolean") v = v ? "yes" : "no";
      return `<span class="sig">${k}=<b>${esc(String(v))}</b></span>`;
    }).join("");
}

/* ---------------------------------------------------------------- mongodb */

function renderDocument(doc) {
  $("#tab-document").innerHTML = doc.collections.map((c) => `
    <div class="coll">
      <h4>db.${esc(c.name)} <span style="color:var(--muted);font-weight:400">· sources: ${c.source_tables.join(", ")}</span></h4>
      ${c.fields.map(fieldRow).join("")}
      ${c.indexes.length ? `<h3 class="section">indexes</h3>
        ${c.indexes.map((i) => `<div class="field-row">
          <span class="fname mono">${i.keys.map((k) => `${esc(k.field)}:${k.direction < 0 ? "-1" : "1"}`).join(", ")}</span>
          <span class="ftype">${i.unique ? "unique" : ""}</span>
          <span class="fsrc">${esc(i.reason)}</span></div>`).join("")}` : ""}
      <details class="raw"><summary>validator + example document</summary>
        <pre class="code">${esc(JSON.stringify({ validator: c.validator, example: c.example }, null, 2))}</pre>
      </details>
    </div>`).join("");
}

function fieldRow(f) {
  if (f.array_of) {
    const inner = (f.array_of.object || []).map(fieldRow).join("");
    return `<div class="field-row">
      <span class="fname">${esc(f.name)}[]</span>
      <span class="ftype">array</span>
      <span class="fsrc">${esc(f.source)}${f.array_of.object ? "" : ` (${esc(String(f.array_of.bson_type))})`}</span>
    </div>${inner ? `<div style="margin-left:24px;border-left:2px solid var(--border);padding-left:8px">${inner}</div>` : ""}`;
  }
  if (f.object) {
    return `<div class="field-row"><span class="fname">${esc(f.name)}</span><span class="ftype">object</span><span class="fsrc">${esc(f.source)}</span></div>`
      + `<div style="margin-left:24px;border-left:2px solid var(--border);padding-left:8px">${f.object.map(fieldRow).join("")}</div>`;
  }
  return `<div class="field-row">
    <span class="fname">${esc(f.name)}</span>
    <span class="ftype">${esc(f.bson_type)}</span>
    <span class="fsrc">${esc(f.source)}</span>
  </div>`;
}

/* ---------------------------------------------------------------- cassandra + neo4j */

function renderColumnar(cql, warnings, amplification) {
  const highlighted = esc(cql).replace(/(-- WARNING:.*)/g, '<span class="warn">$1</span>');
  $("#tab-columnar").innerHTML = `
    ${amplification && amplification.length ? `
      <h3 class="section">Write amplification</h3>
      <ul>${amplification.map((w) => `<li class="mono" style="font-size:12px">${esc(w.table)}: ${esc(w.detail)}</li>`).join("")}</ul>` : ""}
    <h3 class="section">CQL — one table per access pattern</h3>
    <pre class="code">${highlighted}</pre>`;
}

function renderGraph(cypher) {
  $("#tab-graph").innerHTML = `<pre class="code">${esc(cypher)}</pre>`;
}

/* ---------------------------------------------------------------- report (markdown) */

function renderReport(md) {
  $("#tab-report").innerHTML = `<div class="md">${renderMarkdown(md)}</div>`;
}

function renderMarkdown(md) {
  const lines = md.split("\n");
  const out = [];
  let inList = false;
  let tableBuf = [];
  const flushList = () => { if (inList) { out.push("</ul>"); inList = false; } };
  const flushTable = () => {
    if (!tableBuf.length) return;
    const rows = tableBuf.filter((r) => !/^\|[\s:|-]+\|$/.test(r));
    out.push("<table>" + rows.map((r, i) => {
      const cells = r.split("|").slice(1, -1);
      const tag = i === 0 ? "th" : "td";
      return `<tr>${cells.map((c) => `<${tag}>${inline(c.trim())}</${tag}>`).join("")}</tr>`;
    }).join("") + "</table>");
    tableBuf = [];
  };
  for (const line of lines) {
    if (line.startsWith("|")) { flushList(); tableBuf.push(line); continue; }
    flushTable();
    if (/^####\s/.test(line)) { flushList(); out.push(`<h4>${inline(line.slice(5))}</h4>`); }
    else if (/^##\s/.test(line)) { flushList(); out.push(`<h2>${inline(line.slice(3))}</h2>`); }
    else if (/^#\s/.test(line)) { flushList(); out.push(`<h1>${inline(line.slice(2))}</h1>`); }
    else if (/^-\s/.test(line)) { if (!inList) { out.push("<ul>"); inList = true; } out.push(`<li>${inline(line.slice(2))}</li>`); }
    else if (line === "---") { flushList(); out.push("<hr>"); }
    else if (line.trim() === "") { flushList(); }
    else { flushList(); out.push(`<p>${inline(line)}</p>`); }
  }
  flushList(); flushTable();
  return out.join("\n");
}

function inline(text) {
  return esc(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
}

/* ---------------------------------------------------------------- rules */

async function renderRulesTab() {
  const res = await fetch("/api/rules");
  const rules = await res.json();
  $("#tab-rules").innerHTML = `<div class="rule-grid">${rules.map((r) => `
    <div class="rule-card">
      <h4><span class="badge rule">${r.id}</span> ${esc(r.name)}</h4>
      <p><b>${esc(r.action)}</b> · ${esc(r.target)}</p>
      <p>${esc(r.summary)}</p>
      <p>${esc(r.rationale)}</p>
    </div>`).join("")}</div>`;
}
showTab("overview");
renderRulesTab();

/* ---------------------------------------------------------------- utils */

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
