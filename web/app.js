"use strict";
// Web Crawler UI. Everything coming from crawled websites (log lines, the
// website profile) is inserted as text nodes only -- never as HTML.

const $ = (sel) => document.querySelector(sel);
const POST_HEADERS = { "Content-Type": "application/json", "X-Requested-With": "webcrawler-ui" };
const FORMAT_LABELS = { csv: "CSV", json: "JSON", xlsx: "Excel", all: "All formats" };
let pollTimer = null;
let logOffset = 0;

async function api(path, options = {}) {
  const res = await fetch(path, options);
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) throw new Error(data.error || `Request failed (HTTP ${res.status})`);
  return data;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

function runDate(runId) {
  const m = /^run_(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/.exec(runId || "");
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}` : runId;
}

function fmtSeconds(s) {
  const m = Math.floor(s / 60);
  return m ? `${m}m ${String(s % 60).padStart(2, "0")}s` : `${s}s`;
}

function pillClass(status, state) {
  if (state === "running") return "pill run";
  if (/^SUCCESS/.test(status) || /^FINISHED/.test(status)) return "pill ok";
  if (/^(INTERRUPTED|STOPPED)/.test(status)) return "pill warn";
  return "pill bad";
}

// ---------------------------------------------------------------- markdown
// Small renderer for the profile's markdown subset (headings, lists, quotes,
// tables, bold, italic, links). Builds DOM nodes; links must be http(s).
function inline(text) {
  const frag = document.createDocumentFragment();
  const re = /\*\*([^*]+)\*\*|\*([^*]+)\*|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g;
  let last = 0, m;
  const plain = (s) => s.replace(/\\\|/g, "|");
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) frag.append(plain(text.slice(last, m.index)));
    if (m[1] !== undefined) frag.append(el("strong", {}, plain(m[1])));
    else if (m[2] !== undefined) frag.append(el("em", {}, plain(m[2])));
    else frag.append(el("a", { href: m[4], target: "_blank", rel: "noopener noreferrer nofollow" }, plain(m[3])));
    last = re.lastIndex;
  }
  if (last < text.length) frag.append(plain(text.slice(last)));
  return frag;
}

function cells(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split(/(?<!\\)\|/).map((c) => c.trim());
}

function renderMarkdown(md, target) {
  target.replaceChildren();
  const lines = (md || "").split(/\r?\n/);
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    let m;
    if (!line.trim()) { i++; continue; }
    if ((m = /^(#{1,4})\s+(.*)$/.exec(line))) {
      target.append(el(`h${m[1].length}`, {}, inline(m[2])));
      i++;
    } else if (line.startsWith(">")) {
      const q = el("blockquote");
      while (i < lines.length && lines[i].startsWith(">")) { q.append(inline(lines[i].replace(/^>\s?/, "")), " "); i++; }
      target.append(q);
    } else if (/^\s*[-*]\s+/.test(line)) {
      const ul = el("ul");
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        ul.append(el("li", {}, inline(lines[i].replace(/^\s*[-*]\s+/, "")))); i++;
      }
      target.append(ul);
    } else if (line.trim().startsWith("|")) {
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) { rows.push(lines[i]); i++; }
      const table = el("table");
      const body = el("tbody");
      rows.forEach((r, idx) => {
        if (/^\|[\s|:-]+\|$/.test(r.trim())) return;
        const tr = el("tr");
        for (const c of cells(r)) tr.append(el(idx === 0 ? "th" : "td", {}, inline(c)));
        (idx === 0 ? table.appendChild(el("thead")) : body).append(tr);
      });
      table.append(body);
      target.append(el("div", { class: "table-wrap" }, table));
    } else {
      const parts = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|>|\s*[-*]\s|\s*\|)/.test(lines[i])) {
        parts.push(lines[i]); i++;
      }
      target.append(el("p", {}, inline(parts.join(" "))));
    }
  }
}

// ---------------------------------------------------------------- results
const STAT_LABELS = [
  ["crawled", "Pages crawled"], ["failed", "Pages failed"], ["items_extracted", "Items extracted"],
  ["emails", "Emails found"], ["phones_found", "Phone numbers"], ["services_identified", "Services identified"],
  ["products_identified", "Products identified"], ["internal_links_discovered", "Internal links"],
  ["social_profiles_found", "Social profiles"], ["robots_txt_blocked", "Blocked by robots.txt"],
  ["js_rendering_suspected_pages", "JS-rendered pages"],
];

async function showRun(runId, statusText) {
  const d = await api(`/api/runs/${encodeURIComponent(runId)}`);
  const s = d.summary || {};
  $("#results").hidden = false;
  const status = statusText || (s.close_reason ? `close reason: ${s.close_reason}` : "no summary");
  const pill = $("#r-status");
  pill.textContent = status;
  pill.className = pillClass(statusText || (s.close_reason === "finished" ? "SUCCESS" : "FAILED"), "done");
  $("#r-meta").textContent = `${runDate(d.run_id)} · ${d.url || "unknown URL"} · folder: ${d.folder}`;

  const stats = $("#r-stats");
  stats.replaceChildren();
  if (d.summary) {
    for (const [key, label] of STAT_LABELS) {
      const v = key === "emails" ? (s.emails_found || []).length : s[key];
      if (v === undefined || v === null) continue;
      stats.append(el("div", { class: "stat" }, el("div", { class: "v" }, v), el("div", { class: "k" }, label)));
    }
    $("#r-note").textContent = `Only the ${s.crawled ?? 0} crawled page(s) were analysed (URL limit ${s.max_unique_urls ?? "?"}). ` +
      "Pages beyond the limit, blocked pages and JavaScript-rendered content are not included.";
  } else {
    $("#r-note").textContent = "This run did not produce a crawl summary. See docker_run.log for details.";
  }

  const files = $("#r-files");
  files.replaceChildren();
  for (const name of d.files) {
    const href = `/api/runs/${encodeURIComponent(d.run_id)}/files/${encodeURIComponent(name)}`;
    files.append(el("li", {}, el("a", { href, download: `${d.run_id}_${name}` }, name)));
  }
  if (!d.files.length) files.append(el("li", { class: "muted" }, "No files."));

  const profile = $("#r-profile");
  if (d.profile_md) renderMarkdown(d.profile_md, profile);
  else profile.replaceChildren(el("p", { class: "muted" }, "No website profile for this run."));
  $("#results").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------------------------------------------------------------- history
async function loadRuns() {
  try {
    const { runs } = await api("/api/runs");
    const body = $("#runs-body");
    body.replaceChildren();
    $("#runs-empty").hidden = runs.length > 0;
    for (const r of runs) {
      const result = r.close_reason ? r.close_reason : "no summary";
      const btn = el("button", { type: "button", class: "secondary small-btn" }, "Open");
      btn.addEventListener("click", () => showRun(r.run_id).catch((e) => alert(e.message)));
      body.append(el("tr", {},
        el("td", {}, runDate(r.run_id)),
        el("td", { class: "url" }, r.url || "—"),
        el("td", {}, r.crawled ?? "—"),
        el("td", {}, result + (r.has_profile ? "" : " (no profile)")),
        el("td", {}, btn)));
    }
  } catch (e) {
    $("#runs-empty").hidden = false;
    $("#runs-empty").textContent = `Could not load runs: ${e.message}`;
  }
}

// ---------------------------------------------------------------- progress
function setBusy(busy) {
  $("#start-btn").disabled = busy;
  $("#stop-btn").disabled = !busy;
  for (const id of ["url", "max_urls", "format"]) $("#" + id).disabled = busy;
}

function renderStatus(s) {
  $("#progress").hidden = false;
  $("#p-run").textContent = s.run_id;
  $("#p-url").textContent = s.url;
  $("#p-pages").textContent = s.pages;
  $("#p-elapsed").textContent = fmtSeconds(s.elapsed || 0);
  const pill = $("#state-pill");
  pill.textContent = s.state === "running" ? (s.stop_requested ? "STOPPING" : "RUNNING") : s.status;
  pill.className = pillClass(s.status, s.state);
  const log = $("#log");
  const nearBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  if (s.log && s.log.length) log.append(s.log.join("\n") + "\n");
  logOffset = s.log_total;
  if (nearBottom) log.scrollTop = log.scrollHeight;
  if (s.error) log.append(`ERROR: ${s.error}\n`);
}

async function poll() {
  try {
    const s = await api(`/api/status?since=${logOffset}`);
    if (s.state === "idle") { setBusy(false); return; }
    renderStatus(s);
    if (s.state === "running") {
      pollTimer = setTimeout(poll, 1000);
    } else {
      setBusy(false);
      await loadRuns();
      await showRun(s.run_id, s.status).catch(() => {});
    }
  } catch (e) {
    $("#log").append(`(connection problem: ${e.message} — retrying)\n`);
    pollTimer = setTimeout(poll, 3000);
  }
}

function startPolling(reset) {
  clearTimeout(pollTimer);
  if (reset) { logOffset = 0; $("#log").replaceChildren(); }
  poll();
}

// ---------------------------------------------------------------- actions
$("#crawl-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const err = $("#form-error");
  err.hidden = true;
  const url = $("#url").value.trim();
  const maxUrls = $("#max_urls").value.trim();
  const format = $("#format").value;
  if (!url) { err.textContent = "Please enter a website URL."; err.hidden = false; return; }
  const ok = window.confirm(
    `Start crawl?\n\nWebsite: ${url}\nMaximum URLs: ${maxUrls || 15}\nExport format: ${FORMAT_LABELS[format]}\n\n` +
    "This will visit the live website (robots.txt obeyed).");
  if (!ok) return;
  setBusy(true);
  try {
    await api("/api/crawl", { method: "POST", headers: POST_HEADERS,
      body: JSON.stringify({ url, max_urls: maxUrls, format }) });
    $("#results").hidden = true;
    startPolling(true);
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
    setBusy(false);
  }
});

$("#stop-btn").addEventListener("click", async () => {
  if (!window.confirm("Stop the crawl? Pages already crawled are kept.")) return;
  $("#stop-btn").disabled = true;
  try { await api("/api/stop", { method: "POST", headers: POST_HEADERS, body: "{}" }); }
  catch (e) { alert(e.message); }
});

$("#refresh-btn").addEventListener("click", loadRuns);

// resume if a crawl is already running (e.g. after a page reload)
(async () => {
  setBusy(false);
  await loadRuns();
  try {
    const s = await api("/api/status?since=0");
    if (s.state === "running") { setBusy(true); startPolling(true); }
  } catch (_) { /* server not ready */ }
})();
