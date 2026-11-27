/* RAG MCP — administrative interface (vanilla JS, no external dependencies) */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const STATUS_LABEL = {
  staged: "Awaiting configuration", pending: "Queued", extracting: "Extracting",
  chunking: "Chunking", embedding: "Embedding", waiting: "Waiting for LM Studio",
  ready: "Indexed", error: "Error", cancelling: "Cancelling", cancelled: "Cancelled",
};
const badge = (s) => `<span class="badge ${esc(s)}">${esc(STATUS_LABEL[s] || s)}</span>`;

let state = {
  collections: [], categories: [], sources: [], events: [], models: [], settings: {}, effective: null,
  mode: "manual", suggestion: null, docsOffset: 0, docsLimit: 50,
  pollTick: 0, lastActive: 0, uploading: false,
  selectedDocs: new Set(), currentDocIds: [], docsHasPendingAI: false, activeTab: "dashboard",
  docCollectionFilters: new Map(), bulkBusy:false, docFilterEpoch:0,
  health: null, ingestStatus: null, searchSummary: null,
};

/* ---------------- infra ---------------- */
function token() { return localStorage.getItem("rag_token") || ""; }
async function api(path, opts = {}) {
  const headers = opts.headers || {};
  if (token()) headers["Authorization"] = "Bearer " + token();
  if (opts.body && typeof opts.body === "object" && !(opts.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, { ...opts, headers });
  if (resp.status === 401) { showAuthDialog(); throw new Error("API token required"); }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
  return data;
}
let toastTimer = null;
function toast(msg, isErr = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), isErr ? 8000 : 4000);
}
function showAuthDialog() { const d=$("authDialog"); if (!d.open) d.showModal(); }
function saveToken() { localStorage.setItem("rag_token", $("tokenInput").value.trim()); $("authDialog").close(); location.reload(); }
$("authForm").addEventListener("submit", e => { e.preventDefault(); saveToken(); });
function askAction(title, message, action="Continue", danger=false, input=null, options=null) {
  const d=$("actionDialog");
  if (d.open) return Promise.resolve(null);
  $("actionTitle").textContent=title;
  $("actionMessage").textContent=message;
  $("actionSubmit").textContent=action;
  $("actionSubmit").className=danger?"btn danger":"btn primary";
  $("actionInputWrap").classList.toggle("hidden",input===null);
  $("actionInput").value=input??"";
  $("actionSelectWrap").classList.toggle("hidden",options===null);
  $("actionSelect").replaceChildren(...(options||[]).map(c=>new Option(c.name,c.id)));
  d.returnValue="cancel";
  return new Promise(resolve=>{
    d.addEventListener("close",()=>resolve(d.returnValue==="accept"?(input!==null?$("actionInput").value:options!==null?$("actionSelect").value:true):null),{once:true});
    d.showModal();
    if(input!==null) $("actionInput").focus();
  });
}
function saveTokenFromField() { localStorage.setItem("rag_token", $("apiTokenField").value.trim()); toast("Token saved in this browser"); }
const fmtBytes = (n) => { if (!n && n !== 0) return "—"; const u = ["B", "KB", "MB", "GB"]; let i = 0; while (n >= 1024 && i < 3) { n /= 1024; i++; } return n.toFixed(i ? 1 : 0) + " " + u[i]; };
const fmtDate = (s) => s ? new Date(s).toLocaleString("en-US", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";
const fmtTime = (s) => s ? new Date(s).toLocaleTimeString("en-US") : "";

const TAB_META = {
  dashboard: ["Dashboard", "RAG MCP overview, system health, and retrieval pipeline at a glance."],
  import: ["Import", "Add documents and route them into the knowledge base."],
  documents: ["Documents", "Inspect, filter, classify, and manage indexed documents."],
  search: ["Search", "Run semantic retrieval across the selected knowledge collections."],
  settings: ["Settings", "Configure models, ingestion defaults, and runtime behavior."],
};

function showTab(name, { persist = true } = {}) {
  const valid = ["dashboard", "import", "documents", "search", "settings"];
  if (!valid.includes(name)) name = "dashboard";
  state.activeTab = name;
  if (persist) localStorage.setItem("rag_active_tab", name);
  const meta = TAB_META[name] || TAB_META.dashboard;
  if ($("pageTitle")) $("pageTitle").textContent = meta[0];
  if ($("pageSubtitle")) $("pageSubtitle").textContent = meta[1];
  const headerIcon = $("pageHeaderIcon");
  const navIcon = document.querySelector(`#nav button[data-tab="${name}"] svg`);
  if (headerIcon && navIcon) headerIcon.innerHTML = navIcon.outerHTML;
  document.querySelectorAll("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  if (name === "documents") loadDocuments();
  if (name === "search") { renderSearchCollections(); loadSearchMetrics(); }
  if (name === "import") refreshStaged();
  if (name === "settings") loadSettingsTab();
}
document.querySelectorAll("#nav button").forEach((b) => b.onclick = () => showTab(b.dataset.tab));

/* ---------------- collections ---------------- */
async function loadCollections() {
  const data = await api("/api/collections");
  state.collections = data.collections;
  renderCollections();
  fillCollectionSelects();
  await loadCollectionSources();
}
function fillCollectionSelects() {
  const opts = state.collections.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
  const imp = $("importCollection");
  const prev = imp.value;
  const autoOption = state.settings.embedding_model
    ? `<option value="__auto__">✨ Automatic classification</option>`
    : `<option value="__auto__" disabled>✨ Automatic classification — configure embedding model first</option>`;
  imp.innerHTML = autoOption + (opts || `<option value="">— create a collection —</option>`);
  if (prev === "__auto__" && state.settings.embedding_model) imp.value = prev;
  else if (prev && state.collections.some((c) => c.id === prev)) imp.value = prev;
  renderDocCollectionFilter();
}
function renderCollections() {
  const box = $("collectionBehaviorTable");
  if (box) {
    box.innerHTML = state.collections.length ? `
      <table class="settings-table">
        <thead><tr><th>Collection</th><th>Documents</th><th>Default Search</th></tr></thead>
        <tbody>${state.collections.map((c) => `
          <tr>
            <td><span class="collection-name-cell">◇ <b>${esc(c.name)}</b></span></td>
            <td>${Number(c.doc_count || 0).toLocaleString()}</td>
            <td><button class="toggle-pill ${c.default_search ? "on" : ""}" onclick="toggleDefaultSearch('${c.id}', ${!c.default_search})">${c.default_search ? "On" : "Off"}</button></td>
          </tr>`).join("")}</tbody>
      </table>` : `<p class="muted small">No collections.</p>`;
  }
  renderDashboardOverview(state.health);
}

async function createCollection() {
  toast("Top-level collections are fixed. Create book categories in Settings.", true);
}

async function renameCollection(id, currentName) {
  const next = await askAction("Rename collection","Enter a new name.","Rename",false,currentName);
  if (next === null) return;
  const name = next.trim();
  if (!name || name === currentName) return;
  try {
    await api(`/api/collections/${id}`, { method: "PATCH", body: { name } });
    await loadCollections();
    toast(`Collection renamed to '${name}'`);
  } catch (e) { toast(e.message, true); }
}

async function deleteCollection(id, name) {
  if (!(await askAction("Delete collection", `Delete collection '${name}' and ALL of its documents/vectors?`, "Delete", true))) return;
  try { await api(`/api/collections/${id}`, { method: "DELETE" }); await loadCollections(); toast(`Collection '${name}' deleted`); }
  catch (e) { toast(e.message, true); }
}

async function toggleDefaultSearch(id, enabled) {
  try {
    await api(`/api/collections/${id}`, { method: "PATCH", body: { default_search: enabled } });
    await loadCollections();
    toast(`Default search ${enabled ? 'ON' : 'OFF'}`);
  } catch (e) { toast(e.message, true); }
}
function reindexCollectionModal(id) {
  const c = state.collections.find((x) => x.id === id);
  const cfg = { ...(state.effective?.ingest || {}), ...(c.ingest_config || {}) };
  const modelOpts = state.models.map((m) => `<option value="${esc(m.id)}" ${m.id === (c.embedding_model || state.settings.embedding_model) ? "selected" : ""}>${esc(m.id)}</option>`).join("");
  openModal(`Reindex collection ${esc(c.name)}`, `
    <p class="small muted">Reprocesses every document. Changing the embedding model removes current vectors and rebuilds them from scratch.</p>
    <div class="form-grid">
      <label>Embedding model<select id="rcModel">${modelOpts}</select></label>
      <label>chunk_size<input type="number" id="rcSize" value="${cfg.chunk_size || 1500}"></label>
      <label>chunk_overlap<input type="number" id="rcOverlap" value="${cfg.chunk_overlap ?? 200}"></label>
      <label>strategy<select id="rcStrategy">${["recursive", "fixed", "markdown", "pages"].map((s) => `<option ${s === cfg.strategy ? "selected" : ""}>${s}</option>`).join("")}</select></label>
    </div>
    <button class="btn primary" onclick="reindexCollection('${id}')">Reindex ${c.doc_count} document(s)</button>`);
}
async function reindexCollection(id) {
  try {
    const body = {
      embedding_model: $("rcModel").value || null,
      config: { chunk_size: +$("rcSize").value, chunk_overlap: +$("rcOverlap").value, strategy: $("rcStrategy").value },
    };
    const r = await api(`/api/collections/${id}/reindex`, { method: "POST", body });
    closeModal(); toast(`Reindexing ${r.reindexing} document(s)`); showTab("dashboard");
  } catch (e) { toast(e.message, true); }
}

/* ---------------- upload ---------------- */
const dz = $("dropzone");
dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
dz.addEventListener("drop", async (e) => {
  e.preventDefault(); dz.classList.remove("drag");
  const entries = [];
  if (e.dataTransfer.items) {
    for (const item of e.dataTransfer.items) {
      const entry = item.webkitGetAsEntry && item.webkitGetAsEntry();
      if (entry) entries.push(entry);
    }
  }
  let out = [];
  if (entries.length) {
    for (const en of entries) await traverseEntry(en, "", out);
  } else {
    out = [...e.dataTransfer.files].map((f) => ({ file: f, path: f.name }));
  }
  uploadFiles(out);
});
async function traverseEntry(entry, path, out) {
  if (entry.isFile) {
    const file = await new Promise((res, rej) => entry.file(res, rej));
    out.push({ file, path: path + entry.name });
  } else if (entry.isDirectory) {
    const reader = entry.createReader();
    let batch;
    do {
      batch = await new Promise((res, rej) => reader.readEntries(res, rej));
      for (const en of batch) await traverseEntry(en, path + entry.name + "/", out);
    } while (batch.length > 0);
  }
}
$("fileInput").onchange = (e) => uploadFiles([...e.target.files].map((f) => ({ file: f, path: f.name })));
$("dirInput").onchange = (e) => uploadFiles([...e.target.files].map((f) => ({ file: f, path: f.webkitRelativePath || f.name })));

function uploadFiles(items) {
  const colId = $("importCollection").value;
  if (!colId) return toast("Create or select a collection first", true);
  if (!items.length) return;
  if (state.uploading) return toast("Wait for the current upload to finish", true);
  state.uploading = true;
  const fd = new FormData();
  fd.append("collection_id", colId);
  for (const it of items) { fd.append("files", it.file, it.file.name); fd.append("paths", it.path); }
  const box = $("uploadResults");
  box.innerHTML = `<div class="row"><span class="spinner"></span> <span id="upProgressText">Uploading ${items.length} file(s)…</span></div>
    <div class="progress-wrap"><div class="progress-bar" id="upBar"></div></div>`;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/documents/upload");
  if (token()) xhr.setRequestHeader("Authorization", "Bearer " + token());
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const pct = Math.round((e.loaded / e.total) * 100);
      $("upBar").style.width = pct + "%";
      $("upProgressText").textContent = `Uploading ${items.length} file(s)… ${pct}% (${fmtBytes(e.loaded)} / ${fmtBytes(e.total)})`;
    }
  };
  xhr.onload = async () => {
    state.uploading = false;
    if (xhr.status === 401) { showAuthDialog(); box.innerHTML = ""; return; }
    let data;
    try { data = JSON.parse(xhr.responseText); } catch { data = { detail: xhr.responseText }; }
    if (xhr.status >= 400) { box.innerHTML = ""; toast(data.detail || "Upload failed", true); return; }
    renderUploadResults(data);
    await refreshStaged(); await loadCollections();
    const parts = [];
    if (data.queued && data.route_mode === "manual_destination") parts.push(`${data.queued} queued for ingestion in ${data.destination_collection} (manual destination)`);
    if (data.queued && data.route_mode === "automatic_classification") parts.push(`${data.queued} queued for ingestion; embedding classification follows indexing`);
    if (data.auto_classifying) parts.push(`${data.auto_classifying} automatic classification job(s)`);
    else if (data.staged) parts.push(`${data.staged} awaiting configuration`);
    if (data.duplicates) parts.push(`${data.duplicates} duplicate(s) skipped`);
    if (data.ignored_empty) parts.push(`${data.ignored_empty} empty file(s) skipped`);
    toast(parts.join(", ") || "Upload complete");
  };
  xhr.onerror = () => { state.uploading = false; box.innerHTML = ""; toast("Upload network error", true); };
  xhr.send(fd);
  $("fileInput").value = ""; $("dirInput").value = "";
}
function renderUploadResults(data) {
  const icon = { pending: "⚙️", staged: "📄", duplicate: "♻️", ignored_empty: "⚪", unsupported: "🚫", too_large: "🐘", error: "❌" };
  const label = { pending: "imported and queued", staged: "imported; awaiting configuration", duplicate: "duplicate (skipped)", ignored_empty: "empty (skipped)", unsupported: "unsupported", too_large: "too large", error: "error" };
  const rejected = data.results.filter((r) => ["unsupported", "too_large", "error"].includes(r.status));
  const unsupported = rejected.filter((r) => r.status === "unsupported").length;
  const warning = rejected.length ? `<div class="upload-warning" role="alert">
    <strong>${rejected.length} file(s) not imported.</strong>
    ${unsupported ? `${unsupported} unsupported extension(s). ` : ""}See the rejected file(s) below.
  </div>` : "";
  $("uploadResults").innerHTML = warning + `<div class="upload-list">` + data.results.map((r) => `
    <div class="upload-item ${rejected.includes(r) ? "upload-item-rejected" : ""}">
      <span>${icon[r.status] || ""} <span class="docpath">${esc(r.source_path)}</span></span>
      <span class="muted">${label[r.status] || r.status}${r.error ? " — " + esc(r.error) : ""}</span>
    </div>`).join("") + `</div>`;
}

/* ---------------- ingest config ---------------- */
function onImportCollectionChange() {
  const automatic = $("importCollection").value === "__auto__";
  $("ingestConfigCard").classList.toggle("hidden", automatic);
  buildManualForm();
  refreshStaged();
}
async function refreshStaged() {
  let colId = $("importCollection").value;
  if (!colId) { $("stagedCount").textContent = ""; return; }
  if (colId === "__auto__") {
    const books = state.collections.find((c) => c.name === "Computer Books");
    if (!books) return;
    colId = books.id;
  }
  try {
    const data = await api(`/api/documents?collection_id=${colId}&status=staged&limit=1`);
    $("stagedCount").textContent = `${data.total} document(s) awaiting configuration`;
    $("btnStart").disabled = data.total === 0 || $("importCollection").value === "__auto__";
  } catch { /* ignore */ }
}
function buildManualForm() {
  const col = state.collections.find((c) => c.id === $("importCollection").value);
  const eff = state.effective?.ingest || { chunk_size: 1500, chunk_overlap: 200, min_chunk_size: 120, strategy: "recursive", embed_batch_size: 16 };
  const cfg = { ...eff, ...(col?.ingest_config || {}) };
  const model = state.settings.embedding_model || "";
  const embOpts = state.models
    .filter((m) => m.type !== "llm")
    .map((m) => `<option value="${esc(m.id)}" ${m.id === model ? "selected" : ""}>${esc(m.id)}${m.type === "unknown" ? " (unknown type)" : ""}</option>`).join("");
  $("manualForm").innerHTML = `
    <label>Embedding model<select id="mModel">${embOpts || `<option value="">— configure in Settings —</option>`}</select></label>
    <label>chunk_size (characters)<input type="number" id="mSize" value="${cfg.chunk_size}" min="200" max="8000"></label>
    <label>chunk_overlap (characters)<input type="number" id="mOverlap" value="${cfg.chunk_overlap}" min="0" max="1000"></label>
    <label>min_chunk_size<input type="number" id="mMin" value="${cfg.min_chunk_size}" min="0" max="2000"></label>
    <label>strategy<select id="mStrategy">${["recursive", "fixed", "markdown", "pages"].map((s) => `<option ${s === cfg.strategy ? "selected" : ""}>${s}</option>`).join("")}</select></label>
    <label>embed_batch_size<input type="number" id="mBatch" value="${cfg.embed_batch_size || 16}" min="1" max="128"></label>`;
}
function setMode(mode) {
  state.mode = mode;
  $("modeManualBtn").classList.toggle("active", mode === "manual");
  $("modeAutoBtn").classList.toggle("active", mode === "auto");
  $("modeManual").classList.toggle("hidden", mode !== "manual");
  $("modeAuto").classList.toggle("hidden", mode !== "auto");
}
async function suggestConfig() {
  const colId = $("importCollection").value;
  if (!colId) return toast("Select a collection", true);
  const model = $("autoModel").value;
  if (!model) return toast("Select an LLM model in Settings", true);
  const btn = $("btnSuggest");
  btn.disabled = true; btn.innerHTML = `<span class="spinner"></span> Analyzing…`;
  $("suggestionBox").innerHTML = `<p class="muted small">Extracting a sample and querying the LLM — this may take a few seconds…</p>`;
  try {
    const s = await api("/api/ingest/suggest", { method: "POST", body: { collection_id: colId, model } });
    state.suggestion = s;
    renderSuggestion(s);
  } catch (e) {
    $("suggestionBox").innerHTML = "";
    toast(e.message, true);
  } finally { btn.disabled = false; btn.textContent = "Analyze documents"; }
}
function renderSuggestion(s) {
  const c = s.config;
  const srcLabel = s.source === "llm" ? `🤖 suggested by ${esc(s.model)}` : "⚙️ heuristic (LLM unavailable/invalid)";
  $("suggestionBox").innerHTML = `
    <div class="suggestion">
      <h3>Suggested parameters — ${srcLabel}</h3>
      ${s.warning ? `<p class="small" style="color:var(--yellow)">⚠️ ${esc(s.warning)}</p>` : ""}
      <p class="rationale">${esc(s.rationale)}</p>
      <div class="form-grid">
        <label>chunk_size<input type="number" id="sSize" value="${c.chunk_size}"></label>
        <label>chunk_overlap<input type="number" id="sOverlap" value="${c.chunk_overlap}"></label>
        <label>min_chunk_size<input type="number" id="sMin" value="${c.min_chunk_size}"></label>
        <label>strategy<select id="sStrategy">${["recursive", "fixed", "markdown", "pages"].map((x) => `<option ${x === c.strategy ? "selected" : ""}>${x}</option>`).join("")}</select></label>
      </div>
      <p class="muted small">You can edit the values above before starting, or
        <a href="#" onclick="setMode('manual');return false" style="color:var(--accent)">return to manual mode</a>.</p>
    </div>`;
}
function currentConfig() {
  if (state.mode === "auto" && state.suggestion && $("sSize")) {
    return {
      config: {
        chunk_size: +$("sSize").value, chunk_overlap: +$("sOverlap").value,
        min_chunk_size: +$("sMin").value, strategy: $("sStrategy").value,
      },
      embedding_model: state.settings.embedding_model || null,
      suggestion: state.suggestion,
    };
  }
  return {
    config: {
      chunk_size: +$("mSize").value, chunk_overlap: +$("mOverlap").value,
      min_chunk_size: +$("mMin").value, strategy: $("mStrategy").value,
      embed_batch_size: +$("mBatch").value,
    },
    embedding_model: $("mModel").value || null,
    suggestion: null,
  };
}
async function startIngest() {
  const colId = $("importCollection").value;
  if (!colId) return toast("Select a collection", true);
  const { config, embedding_model, suggestion } = currentConfig();
  if (!embedding_model) return toast("Select an embedding model in Settings or manual mode", true);
  try {
    const r = await api("/api/ingest/start", {
      method: "POST",
      body: {
        collection_id: colId, config, embedding_model, mode: state.mode,
        suggestion, save_as_collection_default: $("saveAsDefault").checked,
      },
    });
    toast(`Ingestion started: ${r.started} document(s)`);
    state.suggestion = null; $("suggestionBox").innerHTML = "";
    await refreshStaged(); await pollStatus();
  } catch (e) { toast(e.message, true); }
}
async function pauseIngest() { try { await api("/api/ingest/pause", { method: "POST" }); toast("Ingestion paused — progress preserved"); } catch (e) { toast(e.message, true); } }
async function resumeIngest() { try { await api("/api/ingest/resume", { method: "POST" }); toast("Ingestion resumed"); } catch (e) { toast(e.message, true); } }

/* ---------------- dashboard ---------------- */
async function pollStatus() {
  try {
    const s = await api("/api/ingest/status");
    state.lastActive = s.active_documents;
    renderIngest(s);
    if (state.pollTick % 4 === 0) { await loadCollections(); await pollEvents(); }
    if (s.active_documents > 0 && $("tab-documents").classList.contains("active") && state.pollTick % 2 === 0) loadDocuments();
    if (s.active_documents > 0 && $("tab-import").classList.contains("active") && state.pollTick % 4 === 0) refreshStaged();
    state.pollTick++;
  } catch { /* health polling reports server availability */ }
}
function renderIngest(s) {
  state.ingestStatus = s;
  const pct = s.active_documents > 0 && s.chunks_total > 0 ? Math.round((s.chunks_embedded / s.chunks_total) * 100) : 0;
  ["ingestBar", "importIngestBar"].forEach((id) => { const el = $(id); if (el) el.style.width = pct + "%"; });
  ["btnPause", "importBtnPause"].forEach((id) => { const el = $(id); if (el) el.classList.toggle("hidden", s.paused); });
  ["btnResume", "importBtnResume"].forEach((id) => { const el = $(id); if (el) el.classList.toggle("hidden", !s.paused); });
  const summary = s.paused
    ? `⏸️ <b>Paused</b> — progress preserved; click Resume to continue.`
    : s.active_documents > 0
      ? `Processing <b>${s.active_documents}</b> document(s) — <b>${s.chunks_embedded}</b> of <b>${s.chunks_total}</b> chunks embedded (${pct}%); <b>${s.chunks_remaining}</b> remaining.`
      : `No active ingestion. ${s.counts.ready || 0} document(s) indexed.`;
  ["ingestSummary", "importIngestSummary"].forEach((id) => { const el = $(id); if (el) el.innerHTML = summary; });
  const order = ["staged", "pending", "extracting", "chunking", "embedding", "waiting", "ready", "error"];
  const countsHtml = order
    .filter((k) => s.counts[k])
    .map((k) => `<span class="chip">${badge(k)} <b>${s.counts[k]}</b></span>`).join("")
    || `<span class="muted small">No documents yet — import files from the Import tab.</span>`;
  ["ingestCounts", "importIngestCounts"].forEach((id) => { const el = $(id); if (el) el.innerHTML = countsHtml; });
  const currentHtml = s.current_document
    ? `Now: <b>${esc(s.current_document.filename)}</b> — ${badge(s.current_document.status)} ${s.current_document.chunks_embedded}/${s.current_document.chunks_total} chunks`
    : "";
  ["currentDoc", "importCurrentDoc"].forEach((id) => { const el = $(id); if (el) el.innerHTML = currentHtml; });
  const problemsBox = $("problems");
  if (problemsBox) problemsBox.innerHTML = (s.problems || []).map((p) => `
    <div class="problem ${p.status === "waiting" ? "waiting" : ""}">
      <b>${esc(p.filename)}</b> ${badge(p.status)}
      ${p.status === "error" ? `<button class="btn mini" onclick="retryDoc('${p.id}')">Retry</button>` : ""}
      <div class="err-msg">${esc(p.error || "")}</div>
    </div>`).join("");
  renderDashboardOverview(state.health);
}

function dashboardTotals() {
  const collections = state.collections || [];
  const documents = collections.reduce((n, c) => n + Number(c.doc_count || 0), 0);
  const ready = collections.reduce((n, c) => n + Number(c.ready_count || 0), 0);
  const errors = collections.reduce((n, c) => n + Number(c.error_count || 0), 0);
  const chunks = collections.reduce((n, c) => n + Number(c.chunk_count || 0), 0);
  const counts = state.ingestStatus?.counts || {};
  const queue = ["staged","pending","extracting","chunking","embedding","waiting"]
    .reduce((n, key) => n + Number(counts[key] || 0), 0);
  return { collections: collections.length, documents, ready, errors, chunks, queue };
}

function serviceIcon(kind) {
  const icons = {
    postgres: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5.5C8.2 3.8 10 3 12 3c3.9 0 6.8 2.7 6.8 6.2 0 2.1-.7 4.2-2.1 5.4-.8.7-1.8.8-2.8.2l-1.2-.7-.5 4.7-2.1 1.2.5-5.9-1.7.8c-1 .5-2 .2-2.7-.7C4.7 12.4 4.8 8 7 5.5Z"/><path d="M10.8 7.3c.5-.6 1.3-.6 1.8 0M14.4 7.2h.1M8.6 7.2h.1M12.5 10.2c1.1.8 2.3 1.2 3.7 1.1"/></svg>`,
    layers: refIcon("layers"),
    gear: `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6 7 7M17 17l1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4"/><circle cx="12" cy="12" r="7"/></svg>`,
  };
  return icons[kind] || icons.layers;
}

function statusRow(label, ok, detail = "", kind = "layers", statusText = "") {
  const status = statusText || (ok ? "✓ Healthy" : "● Attention");
  return `<div class="status-row">
    <span class="status-icon">${serviceIcon(kind)}</span><span class="status-service">${esc(label)}</span>
    <span class="status-value ${ok ? "ok" : "bad"}">${esc(status)}</span>
    ${detail ? `<span class="status-detail">${esc(detail)}</span>` : ""}
  </div>`;
}

function refIcon(kind, extra = "") {
  const cls = `ref-svg ${extra}`.trim();
  const icons = {
    layers: `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 8 4-8 4-8-4 8-4Z"/><path d="m4 12 8 4 8-4M4 17l8 4 8-4"/></svg>`,
    cube: `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 7.5 4.2v9.6L12 21l-7.5-4.2V7.2L12 3Z"/><path d="m4.5 7.2 7.5 4.3 7.5-4.3M12 11.5V21"/></svg>`,
    document: `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3.5h9l3 3v14H6z"/><path d="M15 3.5v4h4M9 11h6M9 14.5h6M9 18h4.5"/></svg>`,
    check: `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="m8.5 12.2 2.3 2.4 4.9-5.2"/></svg>`,
    queue: `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 8 4-8 4-8-4 8-4Z"/><path d="m4 12 8 4 8-4M4 17l8 4 8-4"/></svg>`,
    search: `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.3"/><path d="m15.1 15.1 5 5"/></svg>`,
    chunks: `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8" stroke-dasharray="1.2 3.1" stroke-width="2.2"/></svg>`,
    runtime: `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="3"/><circle cx="9" cy="9" r="1.1"/><circle cx="15" cy="9" r="1.1"/><circle cx="9" cy="15" r="1.1"/><circle cx="15" cy="15" r="1.1"/><path d="M2.5 8h2.5M2.5 16h2.5M19 8h2.5M19 16h2.5M8 2.5V5M16 2.5V5M8 19v2.5M16 19v2.5"/></svg>`,
    embedding: `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 8 4-8 4-8-4 8-4Z"/><path d="m4 12 8 4 8-4M4 17l8 4 8-4"/></svg>`,
    rerank: `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 8 4-8 4-8-4 8-4Z"/><path d="m4 12 8 4 8-4M4 17l8 4 8-4"/></svg>`,
    classify: `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.5c.9 4.6 3.2 7 7.5 8-4.3 1-6.6 3.4-7.5 8-.9-4.6-3.2-7-7.5-8 4.3-1 6.6-3.4 7.5-8Z"/></svg>`,
    resident: `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 7.5 4.2v9.6L12 21l-7.5-4.2V7.2L12 3Z"/><path d="m4.5 7.2 7.5 4.3 7.5-4.3M12 11.5V21"/></svg>`,
  };
  return icons[kind] || icons.cube;
}

function renderDashboardOverview(h = state.health) {
  const totals = dashboardTotals();
  const stats = $("dashboardStats");
  if (stats) {
    const queries = Number(state.searchSummary?.queries || 0);
    const items = [
      ["layers", totals.collections, "Collections"],
      ["document", totals.documents.toLocaleString(), "Documents"],
      ["check", totals.ready.toLocaleString(), "Ready"],
      ["queue", totals.queue.toLocaleString(), "Queue"],
      ["search", queries.toLocaleString(), "Search Requests", "24h"],
      ["chunks", totals.chunks.toLocaleString(), "Chunks"],
    ];
    stats.innerHTML = items.map(([icon, value, label, sub]) => `
      <div class="card kpi-card">
        <span class="kpi-icon">${refIcon(icon)}</span>
        <div><div class="kpi-value">${value}</div><div class="kpi-label">${label}${sub ? `<span>${sub}</span>` : ""}</div></div>
      </div>`).join("");
  }

  const health = $("runtimeHealth");
  if (health) {
    health.innerHTML = h ? [
      statusRow("PostgreSQL", Boolean(h.database), "", "postgres"),
      statusRow("LM Studio", Boolean(h.lmstudio?.reachable), "", "layers"),
      statusRow("Worker", Boolean(h.worker?.running) && !h.worker?.paused, "", "gear"),
      statusRow("Classification Queue", Boolean(h.classification_worker?.running), "", "layers", h.classification_worker?.running ? "▷ Running" : "● Attention"),
    ].join("") : `<p class="muted small">Server unavailable.</p>`;
  }

  const collectionStatus = $("collectionStatus");
  if (collectionStatus) {
    const queued = Math.max(0, totals.documents - totals.ready - totals.errors);
    const total = Math.max(1, totals.documents);
    const readyPct = (totals.ready / total) * 100;
    const queuePct = (queued / total) * 100;
    const readyEnd = readyPct;
    const queueEnd = readyPct + queuePct;
    collectionStatus.innerHTML = `
      <div class="donut-wrap">
        <div class="donut" style="--ready:${readyEnd}%;--queue:${queueEnd}%"><div><b>${totals.documents.toLocaleString()}</b><span>Documents</span></div></div>
        <div class="donut-legend">
          <div><i class="legend-dot ready"></i><span>Ready</span><b>${totals.ready.toLocaleString()}</b><small>${totals.documents ? ((totals.ready/totals.documents)*100).toFixed(1) : "0.0"}%</small></div>
          <div><i class="legend-dot queue"></i><span>Queue</span><b>${queued.toLocaleString()}</b><small>${totals.documents ? ((queued/totals.documents)*100).toFixed(1) : "0.0"}%</small></div>
          <div><i class="legend-dot error"></i><span>Error</span><b>${totals.errors.toLocaleString()}</b><small>${totals.documents ? ((totals.errors/totals.documents)*100).toFixed(1) : "0.0"}%</small></div>
        </div>
      </div>`;
  }

  const activity = $("dashboardActivity");
  if (activity) {
    const events = (state.events || []).slice(0, 5);
    const activityIcon = (e) => {
      const msg = String(e.message || "").toLowerCase();
      if (msg.includes("search")) return `<svg viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6"/><path d="m15 15 4.5 4.5"/></svg>`;
      if (msg.includes("started") || msg.includes("start")) return `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 16V7m0 0-3 3m3-3 3 3"/></svg>`;
      return `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="m8.5 12.2 2.2 2.3 4.8-5"/></svg>`;
    };
    const activityTone = (e) => {
      const msg = String(e.message || "").toLowerCase();
      if (msg.includes("started") || msg.includes("start")) return "start";
      if (msg.includes("search")) return "search";
      if (String(e.level || "").toLowerCase() === "warning") return "warning";
      if (String(e.level || "").toLowerCase() === "error") return "error";
      return "success";
    };
    activity.innerHTML = events.length ? events.map((e) => `
      <div class="activity-row ${activityTone(e)}">
        <span class="activity-icon">${activityIcon(e)}</span>
        <div class="activity-copy"><b>${esc(e.message)}</b><span>${esc(e.detail || "")}</span></div>
        <time>${fmtTime(e.ts)}</time>
      </div>`).join("") : `<p class="muted small">No recent activity.</p>`;
  }

  const runtime = $("modelRuntimeGrid");
  if (runtime) {
    const loaded = new Set(h?.model_runtime?.current_models || []);
    const embedding = state.settings.embedding_model || "Not configured";
    const reranker = state.settings.reranker_model || state.effective?.rerank?.model || "Not configured";
    const cards = [
      ["embedding","Embedding Model",embedding,loaded.has(embedding) ? "✓ In Use" : "Configured","Local (LM Studio)","cyan"],
      ["rerank","Reranker (4B)",reranker,loaded.has(reranker) ? "✓ In Use" : "Configured","Local (LM Studio)","muted"],
      ["classify","Classification","Embedding + Reranker","Existing categories only","Existing-category routing","muted"],
      ["resident","Resident-Model Behavior","Exact resident profile","∞ Always On","Models remain loaded for low-latency inference","muted"],
    ];
    runtime.innerHTML = cards.map(([icon,label,value,status,detail,tone]) => `
      <div class="model-card"><div class="model-card-head"><span class="model-icon ${tone}">${refIcon(icon)}</span><b>${esc(label)}</b><em>${esc(status)}</em></div><strong>${esc(value)}</strong><small>${esc(detail)}</small></div>`).join("");
  }

  const system = $("systemServerGrid");
  if (system) {
    const version = h?.build?.version || "dev";
    const models = h?.model_runtime?.current_models || [];
    system.innerHTML = `
      <div class="system-card"><b>PostgreSQL</b><span>${h?.database ? "✓ Healthy" : "● Offline"}</span><small>Database / pgvector</small></div>
      <div class="system-card"><b>LM Studio</b><span>${h?.lmstudio?.reachable ? "✓ Healthy" : "● Offline"}</span><small>${esc(h?.lmstudio?.url || "Not configured")} · ${models.length} resident</small></div>
      <div class="system-card"><b>Worker</b><span>${h?.worker?.running ? "✓ Running" : "● Stopped"}</span><small>${h?.worker?.current_document ? "processing document" : "idle"}</small></div>
      <div class="system-card"><b>Version / Build</b><span>${esc(version)}</span><small>${esc(h?.build?.commit || "")}</small></div>`;
  }
}

async function pollHealth() {
  let h = null;
  try { h = await api("/api/health"); } catch { /* offline */ }
  state.health = h;
  const version = h?.build?.version || "dev";
  if ($("versionText")) $("versionText").textContent = version;
  if ($("sidebarVersion")) $("sidebarVersion").classList.toggle("offline", !h);
  const status = document.querySelector(".app-header-status");
  if (status) status.classList.toggle("offline", !h || h.status !== "ok");
  renderDashboardOverview(h);
}

function renderEvents() {
  const all = state.events || [];
  const level = $("eventLevel")?.value || "";
  const query = ($("eventSearch")?.value || "").trim().toLowerCase();
  const count = (name) => all.filter((e) => (e.level || "info") === name).length;
  if ($("eventStats")) {
    $("eventStats").innerHTML = `
      <div><b>${all.length}</b><span>Total</span></div>
      <div><b>${count("info")}</b><span>Info</span></div>
      <div class="warn"><b>${count("warning")}</b><span>Warnings</span></div>
      <div class="bad"><b>${count("error")}</b><span>Errors</span></div>`;
  }
  const events = all.filter((e) => (!level || e.level === level) && (!query || String(e.message || "").toLowerCase().includes(query)));
  const log = $("eventsLog");
  if (log) log.innerHTML = events.map((e) => `
    <div class="event ${esc(e.level)}">
      <span class="ts">${fmtDate(e.ts)}</span>
      <span class="event-level">${esc((e.level || "info").toUpperCase())}</span>
      <span class="msg">${esc(e.message)}</span>
    </div>`).join("") || `<p class="muted small event-empty">No events match these filters.</p>`;
  renderDashboardOverview(state.health);
}
async function pollEvents() {
  try {
    const data = await api("/api/events?limit=100");
    state.events = data.events || [];
    renderEvents();
  } catch { /* ignore */ }
}

/* ---------------- directory sources ---------------- */
function formatInterval(seconds) {
  if (seconds % 86400 === 0) return `${seconds / 86400}d`;
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  return `${Math.round(seconds / 60)}m`;
}
function sourceIntervalOptions(selected) {
  const values = state.effective?.sources?.interval_options_seconds || [];
  return values.map((v) => `<option value="${v}" ${Number(selected) === Number(v) ? "selected" : ""}>${formatInterval(Number(v))}</option>`).join("");
}
async function loadCollectionSources() {
  const box = $("sourcesList");
  if (!box) return;
  try {
    const data = await api("/api/collection-sources");
    state.sources = data.sources || [];
    box.innerHTML = state.sources.length ? `
      <table class="source-table">
        <thead><tr><th>Source</th><th>Directory Path</th><th>Cadence</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>${state.sources.map((s) => {
          const stats = s.last_stats || {};
          const summary = s.last_sync_at
            ? `last ${fmtDate(s.last_sync_at)} · +${stats.new || 0} ~${stats.changed || 0} -${stats.deleted || 0}`
            : "not synced yet";
          return `<tr>
            <td><b>${esc(s.collection)}</b>${s.category ? `<span class="source-sync-meta">${esc(s.category)}</span>` : ""}</td>
            <td><div class="source-path"><code>${esc(s.path)}</code></div><span class="source-sync-meta">${summary}${s.last_error ? ` · error: ${esc(s.last_error)}` : ""}</span></td>
            <td>${formatInterval(Number(s.interval_seconds))}</td>
            <td><span class="toggle-pill ${s.enabled ? "on" : ""}">${s.enabled ? "On" : "Off"}</span></td>
            <td><div class="source-actions">
              <button class="btn mini" onclick="syncSource('${s.id}')">Sync</button>
              <button class="btn mini" onclick="editSource('${s.id}')">Edit</button>
              <button class="btn mini" onclick="cloneSource('${s.id}')">Clone</button>
              <button class="btn mini danger" onclick="deleteSource('${s.id}')">Delete</button>
            </div></td>
          </tr>`;
        }).join("")}</tbody>
      </table>` : `<p class="muted small">No directory sources configured.</p>`;
  } catch (e) { box.textContent = e.message; }
}
async function ensureSourceCategories() {
  const data = await api("/api/categories");
  state.categories = data.categories || [];
}

function sourceCollectionOptions(selectedId) {
  return state.collections.map((c) =>
    `<option value="${esc(c.id)}" ${c.id === selectedId ? "selected" : ""}>${esc(c.name)}</option>`
  ).join("");
}

function sourceCategoryOptions(collectionId, selectedId) {
  const categories = state.categories.filter((c) => c.collection_id === collectionId);
  return `<option value="">Uncategorized / not applicable</option>` + categories.map((c) =>
    `<option value="${esc(c.id)}" ${c.id === selectedId ? "selected" : ""}>${esc(c.name)}</option>`
  ).join("");
}

function refreshSourceCategorySelect(collectionSelectId, categorySelectId, selectedId = "") {
  const collectionId = $(collectionSelectId)?.value || "";
  const select = $(categorySelectId);
  if (!select) return;
  select.innerHTML = sourceCategoryOptions(collectionId, selectedId);
}

async function openAddSource(templateId = null) {
  if (!state.collections.length) return toast("Collections are not loaded", true);
  await ensureSourceCategories();
  const template = templateId ? state.sources.find((x) => x.id === templateId) : null;
  const collectionId = template?.collection_id || state.collections[0]?.id || "";
  const defaultInterval = template?.interval_seconds || state.effective?.sources?.default_interval_seconds;
  openModal(template ? "Clone directory source" : "Add directory source", `
    <div class="form-grid">
      <label>Collection<select id="sourceCollection" onchange="refreshSourceCategorySelect('sourceCollection','sourceCategory')">${sourceCollectionOptions(collectionId)}</select></label>
      <label>Category<select id="sourceCategory">${sourceCategoryOptions(collectionId, template?.category_id || "")}</select></label>
      <label>Type<select id="sourceType">
        <option value="directory" ${template?.source_type !== "repository" ? "selected" : ""}>Directory</option>
        <option value="repository" ${template?.source_type === "repository" ? "selected" : ""}>Repository</option>
      </select></label>
      <label>Interval<select id="sourceInterval">${sourceIntervalOptions(defaultInterval)}</select></label>
    </div>
    <label>Path visible inside RAG container<input id="sourcePath" type="text" value="${esc(template?.path || "")}" placeholder="/sources/project"></label>
    <label>Extra exclude patterns (one per line)<textarea id="sourceExcludes" rows="5" placeholder="build/**&#10;dist/**">${esc((template?.exclude_patterns || []).join("\n"))}</textarea></label>
    <label class="check"><input id="sourceEnabled" type="checkbox" ${template?.enabled === false ? "" : "checked"}> enabled</label>
    <button class="btn primary" onclick="createSource()">${template ? "Create clone" : "Create source"}</button>`);
}

function cloneSource(id) {
  return openAddSource(id);
}

async function createSource() {
  try {
    const collectionId = $("sourceCollection").value;
    const body = {
      path: $("sourcePath").value.trim(),
      interval_seconds: Number($("sourceInterval").value),
      exclude_patterns: $("sourceExcludes").value.split("\n").map((x) => x.trim()).filter(Boolean),
      enabled: $("sourceEnabled").checked,
      source_type: $("sourceType").value,
      category_id: $("sourceCategory").value || null,
    };
    await api(`/api/collections/${collectionId}/sources`, { method: "POST", body });
    closeModal();
    await loadCollectionSources();
    toast("Directory source created; first sync queued");
  } catch (e) { toast(e.message, true); }
}

async function editSource(id) {
  const src = state.sources.find((x) => x.id === id);
  if (!src) return;
  await ensureSourceCategories();
  openModal("Edit directory source", `
    <div class="form-grid">
      <label>Collection<select id="sourceEditCollection" onchange="refreshSourceCategorySelect('sourceEditCollection','sourceEditCategory')">${sourceCollectionOptions(src.collection_id)}</select></label>
      <label>Category<select id="sourceEditCategory">${sourceCategoryOptions(src.collection_id, src.category_id || "")}</select></label>
      <label>Type<select id="sourceEditType">
        <option value="directory" ${src.source_type !== "repository" ? "selected" : ""}>Directory</option>
        <option value="repository" ${src.source_type === "repository" ? "selected" : ""}>Repository</option>
      </select></label>
      <label>Interval<select id="sourceEditInterval">${sourceIntervalOptions(src.interval_seconds)}</select></label>
    </div>
    <div class="row"><button class="btn mini" onclick="renameSelectedSourceCategory()">Rename selected category</button></div>
    <label>Path<input id="sourceEditPath" type="text" value="${esc(src.path)}"></label>
    <label>Extra exclude patterns (one per line)<textarea id="sourceEditExcludes" rows="5">${esc((src.exclude_patterns || []).join("\n"))}</textarea></label>
    <label class="check"><input id="sourceEditEnabled" type="checkbox" ${src.enabled ? "checked" : ""}> enabled</label>
    <button class="btn primary" onclick="saveSource('${id}')">Save</button>`);
}

async function renameSelectedSourceCategory() {
  const categoryId = $("sourceEditCategory")?.value;
  if (!categoryId) return toast("Select a category first", true);
  const category = state.categories.find((c) => c.id === categoryId);
  if (!category) return;
  await renameBookCategory(category.id, category.name);
  await ensureSourceCategories();
  refreshSourceCategorySelect("sourceEditCollection", "sourceEditCategory", categoryId);
}

async function saveSource(id) {
  try {
    await api(`/api/collection-sources/${id}`, { method: "PATCH", body: {
      collection_id: $("sourceEditCollection").value,
      category_id: $("sourceEditCategory").value || null,
      source_type: $("sourceEditType").value,
      path: $("sourceEditPath").value.trim(),
      interval_seconds: Number($("sourceEditInterval").value),
      enabled: $("sourceEditEnabled").checked,
      exclude_patterns: $("sourceEditExcludes").value.split("\n").map((x) => x.trim()).filter(Boolean),
    }});
    closeModal();
    await loadCollectionSources();
    await loadCollections();
    toast("Directory source updated");
  } catch (e) { toast(e.message, true); }
}

async function syncSource(id) {
  try {
    toast("Syncing directory source…");
    const r = await api(`/api/collection-sources/${id}/sync`, { method: "POST", body: {} });
    await loadCollectionSources();
    await loadCollections();
    const s = r.stats;
    toast(`Sync complete: +${s.new} ~${s.changed} -${s.deleted}`);
  } catch (e) { toast(e.message, true); }
}
async function deleteSource(id) {
  if (!(await askAction("Delete directory source", "Existing documents stay in the collection.", "Delete", true))) return;
  try {
    await api(`/api/collection-sources/${id}`, { method: "DELETE" });
    await loadCollectionSources();
    toast("Directory source deleted");
  } catch (e) { toast(e.message, true); }
}

function toggleDocCollectionFilter() {
  const panel = $("docCollectionFilterPanel");
  if (panel) panel.classList.toggle("hidden");
}
function cycleDocCollectionFilter(id) {
  if (state.bulkBusy) return;
  const current = state.docCollectionFilters.get(id) || "neutral";
  const next = current === "neutral" ? "include" : current === "include" ? "exclude" : "neutral";
  if (next === "neutral") state.docCollectionFilters.delete(id);
  else state.docCollectionFilters.set(id, next);
  renderDocCollectionFilter();
  resetDocFilters();
}
function clearDocCollectionFilters() {
  if (state.bulkBusy) return;
  state.docCollectionFilters.clear();
  renderDocCollectionFilter();
  resetDocFilters();
}
function renderDocCollectionFilter() {
  const panel = $("docCollectionFilterPanel");
  const btn = $("docCollectionFilterBtn");
  if (!panel || !btn) return;
  const active = [...state.docCollectionFilters.values()].filter((v) => v !== "neutral").length;
  btn.textContent = active ? `Collections · ${active}` : "Collections";
  panel.innerHTML = `<div class="collection-filter-head"><span>Click to cycle: include → exclude → neutral</span><button class="btn mini" type="button" onclick="clearDocCollectionFilters()">Clear</button></div>` +
    state.collections.map((c) => {
      const mode = state.docCollectionFilters.get(c.id) || "neutral";
      const mark = mode === "include" ? "+" : mode === "exclude" ? "−" : "•";
      return `<button type="button" class="collection-filter-chip ${mode}" onclick="cycleDocCollectionFilter('${c.id}')"><span>${mark}</span>${esc(c.name)}</button>`;
    }).join("");
}
function selectedDocCollectionFilterIds(mode) {
  return [...state.docCollectionFilters.entries()].filter(([, value]) => value === mode).map(([id]) => id);
}

/* ---------------- documents ---------------- */
let docsDebounce = null;
function resetDocFilters(){state.docFilterEpoch++;state.docsOffset=0;state.selectedDocs.clear();updateDocsSelectionUI();return loadDocuments();}
function debouncedDocs() { clearTimeout(docsDebounce); docsDebounce = setTimeout(resetDocFilters, 350); }
function docsPage(dir) { state.docsOffset = Math.max(0, state.docsOffset + dir * state.docsLimit); loadDocuments(); }
function classificationSummary(d) {
  const queued = d.meta?.auto_classify && !d.auto_suggestion;
  if (queued) return `<span class="badge pending">Category queued</span>`;
  const sug = d.auto_suggestion;
  if (!sug) return `<span class="small">${esc(d.category || "Uncategorized")}</span>`;
  if (sug.status === "classification_error") return `<span class="small" style="color:var(--red)">Classification error · ${esc(sug.error || "")}</span>`;
  const existing = sug.existing_category || sug.existing_collection;
  const dest = sug.routed_to || existing || d.category;
  const confidence = Math.round((Number(sug.confidence) || 0) * 100);
  if (!dest) return `<span class="small muted">Uncategorized · no matching category</span>`;
  return `<span class="small">${sug.auto_applied ? "Categorized ✓" : "Review"} · ${esc(dest)} · ${confidence}%</span>`;
}

function updateDocsSelectionUI() {
  const count = state.selectedDocs.size;
  const label = $("docsSelectedCount");
  if (label) label.textContent = `${count} selected`;
  for(const id of ["btnBulkClassifySelected","btnBulkMoveSelected","btnBulkReindexSelected","btnBulkRetrySelected","btnBulkCancelSelected","btnBulkDeleteSelected","btnClearSelection"]) $(id).disabled=!count||state.bulkBusy;
  $("btnSelectFiltered").disabled=state.bulkBusy;
  for (const id of ["docFilterStatus","docSearch","docCollectionFilterBtn"]) $(id).disabled=state.bulkBusy;
  document.querySelectorAll(".doc-select").forEach(el=>el.disabled=state.bulkBusy);
  const page = $("docsSelectPage");
  if (page) {
    const ids = state.currentDocIds || [];
    page.disabled=state.bulkBusy;
    page.checked = ids.length > 0 && ids.every((id) => state.selectedDocs.has(id));
    page.indeterminate = ids.some((id) => state.selectedDocs.has(id)) && !page.checked;
  }
}
function toggleDocSelection(id, checked) {
  if (checked) state.selectedDocs.add(id); else state.selectedDocs.delete(id);
  updateDocsSelectionUI();
}
function toggleDocsPage(checked) {
  for (const id of state.currentDocIds || []) {
    if (checked) state.selectedDocs.add(id); else state.selectedDocs.delete(id);
  }
  document.querySelectorAll(".doc-select").forEach((el) => { el.checked = checked; });
  updateDocsSelectionUI();
}
function clearDocsSelection(){state.selectedDocs.clear();document.querySelectorAll(".doc-select").forEach(el=>el.checked=false);updateDocsSelectionUI();}
function documentParams(limit,offset){
  const params=new URLSearchParams({limit,offset});
  for(const id of selectedDocCollectionFilterIds("include")) params.append("collection_ids",id);
  for(const id of selectedDocCollectionFilterIds("exclude")) params.append("exclude_collection_ids",id);
  if($("docFilterStatus").value) params.set("status",$("docFilterStatus").value);
  if($("docSearch").value) params.set("q",$("docSearch").value);
  return params;
}
async function selectFilteredDocs(){
  if(state.bulkBusy)return;
  state.bulkBusy=true;updateDocsSelectionUI();
  const epoch=state.docFilterEpoch,ids=new Set(),limit=100,params=documentParams(limit,0);
  try{
    for(let offset=0;;offset+=limit){
      params.set("offset",offset);
      const data=await api("/api/documents?"+params);
      if(epoch!==state.docFilterEpoch)return;
      data.items.forEach(d=>ids.add(d.id));
      if(offset+data.items.length>=data.total||!data.items.length)break;
    }
    state.selectedDocs=ids;await loadDocuments();
  }catch(e){toast(e.message,true);}
  finally{state.bulkBusy=false;updateDocsSelectionUI();}
}
async function runBulkAction(ids,label,worker){
  if(!ids.length||state.bulkBusy)return;
  state.bulkBusy=true;updateDocsSelectionUI();
  const failures=[];let done=0,skipped=0;
  try{
    for(const id of ids){
      try{if(await worker(id)===false)skipped++;else done++;state.selectedDocs.delete(id);}
      catch(e){failures.push({id,message:e.message});state.selectedDocs.add(id);}
      $("docsSelectedCount").textContent=(done+skipped+failures.length)+"/"+ids.length+" processed";
    }
    try{await loadCollections();await loadDocuments();}catch(e){toast(e.message,true);}
    if(failures.length)openModal(esc(label+" results"),"<p>"+done+" succeeded · "+skipped+" skipped · "+failures.length+" failed. Failed documents remain selected.</p><ul>"+failures.map(f=>"<li>"+esc(f.id)+" — "+esc(f.message)+"</li>").join("")+"</ul>");
    else toast(label+": "+done+" succeeded, "+skipped+" skipped");
  }finally{state.bulkBusy=false;updateDocsSelectionUI();}
}
async function bulkClassifySelected(){
  const ids=[...state.selectedDocs];if(!ids.length||state.bulkBusy)return;
  if(!await askAction("Reclassify",ids.length+" selected documents. Only indexed documents are eligible.","Reclassify"))return;
  state.bulkBusy=true;updateDocsSelectionUI();let queued=0,skipped=0;
  try{
    for(let i=0;i<ids.length;i+=500){
      const batch=ids.slice(i,i+500),r=await api("/api/documents/classify-bulk",{method:"POST",body:{document_ids:batch}});
      queued+=r.queued;skipped+=batch.length-r.queued;batch.forEach(id=>state.selectedDocs.delete(id));
    }
    toast(queued+" queued · "+skipped+" not eligible");
  }catch(e){toast(queued+" queued before error: "+e.message,true);}
  finally{state.bulkBusy=false;await loadDocuments();updateDocsSelectionUI();}
}
async function bulkMoveSelected(){
  const ids=[...state.selectedDocs];if(!ids.length||state.bulkBusy)return;
  const target=await askAction("Move documents",ids.length+" selected documents. Active documents and incompatible embeddings are rejected.","Move",false,null,state.collections);
  if(!target)return;
  await runBulkAction(ids,"Move",async id=>!(await api("/api/documents/"+id+"/move",{method:"POST",body:{target_collection_id:target}})).unchanged);
}
async function bulkDocumentAction(action,title,message,danger=false){
  const ids=[...state.selectedDocs];if(!ids.length||state.bulkBusy)return;
  if(!await askAction(title,ids.length+" selected documents. "+message,title,danger))return;
  await runBulkAction(ids,title,id=>api("/api/documents/"+id+(action==="delete"?"":"/"+action),action==="delete"?{method:"DELETE"}:{method:"POST",body:{}}));
}
function bulkReindexSelected(){return bulkDocumentAction("reindex","Reindex","Current chunks and vectors will be rebuilt.");}
function bulkRetrySelected(){return bulkDocumentAction("retry","Retry","Only failed or waiting documents can be retried.");}
function bulkCancelSelected(){return bulkDocumentAction("cancel","Cancel processing","Partial chunks and vectors may be removed.",true);}
function bulkDeleteSelected(){return bulkDocumentAction("delete","Delete","Documents and vectors will be permanently removed.",true);}
async function loadDocuments(){
  const params=documentParams(state.docsLimit,state.docsOffset);
  const epoch=state.docFilterEpoch;
  try {
    const data = await api("/api/documents?" + params);
    if(epoch!==state.docFilterEpoch)return;
    $("docsTotal").textContent = `${data.total} document(s) · page ${Math.floor(state.docsOffset / state.docsLimit) + 1}`;
    $("docsPrev").disabled = state.docsOffset === 0;
    $("docsNext").disabled = state.docsOffset + state.docsLimit >= data.total;
    state.currentDocIds = data.items.map((d) => d.id);
    state.docsHasPendingAI = data.items.some((d) => d.meta?.auto_classify && !d.auto_suggestion);
    $("docsTable").innerHTML = `<table><thead><tr>
      <th></th><th>Document</th><th>Collection</th><th>Category</th><th>Status</th><th>Classification</th><th>Progress</th><th>Size</th><th>Updated</th><th></th>
    </tr></thead><tbody>` + data.items.map((d) => {
      const pct = d.chunks_total ? Math.round((d.chunks_embedded / d.chunks_total) * 100) : 0;
      const selected = state.selectedDocs.has(d.id) ? "checked" : "";
      return `<tr>
        <td><input type="checkbox" class="doc-select" ${selected} onchange="toggleDocSelection('${d.id}', this.checked)"></td>
        <td><div class="docname">${esc(d.filename)}</div><div class="docpath">${esc(d.source_path)}</div></td>
        <td>${esc(d.collection)}</td>
        <td>${esc(d.category || "Uncategorized")}</td>
        <td>${badge(d.status)}${d.error ? `<div class="docpath" style="color:var(--red);max-width:260px">${esc(d.error.slice(0, 120))}</div>` : ""}</td>
        <td>${classificationSummary(d)}</td>
        <td>${d.chunks_total ? `<span class="mini-progress"><div style="width:${pct}%"></div></span> <span class="small muted">${d.chunks_embedded}/${d.chunks_total}</span>` : "—"}</td>
        <td class="small muted">${fmtBytes(d.size_bytes)}</td>
        <td class="small muted">${fmtDate(d.updated_at)}</td>
        <td><div class="row" style="margin:0;flex-wrap:nowrap">
          <button class="btn mini" onclick="docDetails('${d.id}')">Details</button>
          ${["error", "waiting"].includes(d.status) ? `<button class="btn mini" onclick="retryDoc('${d.id}')">Retry</button>` : ""}
          ${["pending", "extracting", "chunking", "embedding", "waiting", "cancelling"].includes(d.status) ? `<button class="btn mini danger" onclick="cancelDoc(&quot;${d.id}&quot;)">Cancel</button>` : ""}
          <button class="btn mini" onclick="classifyDoc('${d.id}', ${esc(JSON.stringify(d.filename))})">Classify</button>
          <button class="btn mini" onclick="openMoveDoc('${d.id}', ${esc(JSON.stringify(d.filename))}, '${d.collection_id}')">Move</button>
          <button class="btn mini" onclick="reindexDoc('${d.id}', ${esc(JSON.stringify(d.filename))})">Reindex</button>
          <button class="btn mini danger" onclick="deleteDoc('${d.id}', ${esc(JSON.stringify(d.filename))})">Delete</button>
        </div></td>
      </tr>`;
    }).join("") + `</tbody></table>`;
    updateDocsSelectionUI();
  } catch (e) { toast(e.message, true); }
}
async function retryDoc(id) { try { await api(`/api/documents/${id}/retry`, { method: "POST" }); toast("Document requeued"); loadDocuments(); } catch (e) { toast(e.message, true); } }
async function cancelDoc(id) {
  if (!(await askAction("Cancel processing", "Partial chunks and vectors will be removed.", "Cancel processing", true))) return;
  try {
    const r = await api(`/api/documents/${id}/cancel`, { method: "POST" });
    toast(r.status === "cancelling" ? "Cancellation requested; waiting for a safe boundary" : "Document cancelled");
    loadDocuments();
    loadCollections();
  } catch (e) { toast(e.message, true); }
}

function openMoveDoc(id, name, currentCollectionId) {
  const options = state.collections
    .filter((c) => c.id !== currentCollectionId)
    .map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`)
    .join("");
  if (!options) { toast("No other collection is available", true); return; }
  openModal(`Move ${esc(name)}`, `
    <p class="small muted">Select the destination collection. Indexed documents can move without reindexing only between collections using the same embedding model and dimension.</p>
    <div class="row">
      <select id="moveCollectionSelect">${options}</select>
      <button class="btn primary" onclick="moveDoc('${id}')">Move document</button>
    </div>`);
}
async function moveDoc(id) {
  const target = $("moveCollectionSelect")?.value;
  if (!target) return;
  try {
    const r = await api(`/api/documents/${id}/move`, {
      method: "POST",
      body: { target_collection_id: target },
    });
    closeModal();
    toast(`Moved to ${r.to_collection}`);
    await loadCollections();
    await loadDocuments();
  } catch (e) { toast(e.message, true); }
}
async function reindexDoc(id, name) {
  if (!(await askAction("Reindex document", `Reindex '${name}'? Current chunks/vectors will be recreated.`, "Reindex"))) return;
  try { await api(`/api/documents/${id}/reindex`, { method: "POST", body: {} }); toast("Reindex started"); loadDocuments(); }
  catch (e) { toast(e.message, true); }
}
async function classifyDoc(id, name) {
  try {
    toast(`Classifying '${name}'…`);
    const r = await api(`/api/documents/${id}/classify`, { method: "POST", body: {} });
    const existing = r.existing_category || r.existing_collection;
    if (existing && r.auto_applied) {
      toast(`Categorized as ${existing}`);
      await loadDocuments();
      return;
    }
    await loadDocuments();
    openModal("Category not found", `<p>No existing category matched <b>${esc(name)}</b>.</p><p class="muted">Create a category in Settings, then classify again.</p>`);
  } catch (e) { toast(e.message, true); }
}

async function deleteDoc(id, name) {
  if (!(await askAction("Delete document", `Delete '${name}' and its vectors?`, "Delete", true))) return;
  try { await api(`/api/documents/${id}`, { method: "DELETE" }); toast("Document deleted"); loadDocuments(); loadCollections(); }
  catch (e) { toast(e.message, true); }
}
async function docDetails(id) {
  try {
    const d = await api(`/api/documents/${id}`);
    openModal(esc(d.filename), `
      <p class="small muted">${esc(d.source_path)} · collection <b>${esc(d.collection)}</b> · ${fmtBytes(d.size_bytes)} · sha256 <code>${esc(d.content_sha256.slice(0, 16))}…</code></p>
      <p>${badge(d.status)} ${d.chunks_embedded}/${d.chunks_total} chunks · ${d.attempts} attempt(s)</p>
      ${d.error ? `<div class="problem"><b>Error:</b><div class="err-msg" style="max-height:none">${esc(d.error)}</div></div>` : ""}
      <h3 class="small">Ingestion parameters</h3><pre>${esc(JSON.stringify(d.ingest_config || {}, null, 2))}</pre>
      ${d.auto_suggestion ? `<h3 class="small">Accepted automatic suggestion</h3><pre>${esc(JSON.stringify(d.auto_suggestion, null, 2))}</pre>` : ""}
      <h3 class="small">Metadata</h3><pre>${esc(JSON.stringify(d.meta || {}, null, 2))}</pre>
      <h3 class="small">First chunks</h3>
      ${(d.chunk_preview || []).map((c) => `<div class="result-card"><div class="result-loc">#${c.seq} · ${esc(JSON.stringify(c.loc))} · ${c.embedded ? "✓ embedded" : "pending"}</div><div class="result-content">${esc(c.preview)}…</div></div>`).join("") || `<p class="muted small">No chunks yet.</p>`}`);
  } catch (e) { toast(e.message, true); }
}
function openModal(title, html) { $("modalTitle").innerHTML = title; $("modalBody").innerHTML = html; $("modal").classList.remove("hidden"); }
function closeModal() { $("modal").classList.add("hidden"); }

/* ---------------- search ---------------- */
function renderSearchCollections() {
  $("searchCollections").innerHTML = state.collections.map((c) =>
    `<label class="check chip"><input type="checkbox" class="searchCol" value="${esc(c.name)}"> ${esc(c.name)}${c.default_search ? '' : ' · default OFF'}</label>`).join("")
    || `<span class="muted small">No collections.</span>`;
}
function renderReferenceSearchResults(results, message = "") {
  const evidence = $("evidencePreview");
  const ranked = $("rankedResults");
  if (!evidence || !ranked) return;
  if (!results?.length) {
    evidence.innerHTML = `<p class="muted small">${esc(message || "No results.")}</p>`;
    ranked.innerHTML = `<p class="muted small">No ranked evidence.</p>`;
    return;
  }
  const top = results[0];
  evidence.innerHTML = `
    <div class="card-head"><h2>Evidence Preview</h2><span class="result-count">${results.length} result(s)</span></div>
    <div class="evidence-body">
      <div class="evidence-source">${esc(top.document || top.source_path || "Top result")}</div>
      <div class="result-loc">${esc(top.collection || "")}${top.location_label ? " · " + esc(top.location_label) : ""}</div>
      <div class="evidence-text">${esc(top.content || "")}</div>
      <div class="evidence-grounding">Top-ranked retrieved evidence · score ${Number(top.rerank_score ?? top.score ?? 0).toFixed(3)}</div>
      <div class="evidence-sources">${results.slice(0, 3).map((x) => `
        <div class="evidence-source-card"><b>${esc(x.document || x.source_path || "Document")}</b><span>${esc(x.location_label || x.collection || "")}</span></div>`).join("")}</div>
    </div>`;
  ranked.innerHTML = `
    <div class="card-head"><h2>Ranked Results</h2><span class="result-count">${results.length}</span></div>
    <div class="ranked-list">${results.map((x, i) => `
      <div class="ranked-row">
        <span class="rank-number">${i + 1}</span>
        <div><b>${esc(x.document || x.source_path || "Document")}</b><span>${esc(x.location_label || x.collection || "")}${x.selection?.reason ? " · " + esc(x.selection.reason) : ""}</span></div>
        <em>${esc(x.selection?.method || "retrieval")}</em>
        <strong>${Number(x.rerank_score ?? x.score ?? 0).toFixed(3)}</strong>
      </div>`).join("")}</div>`;
}

async function doSearch() {
  const query = $("searchQuery").value.trim();
  if (!query) return;
  const cols = [...document.querySelectorAll(".searchCol:checked")].map((c) => c.value);
  renderReferenceSearchResults([], "Searching…");
  try {
    const documentId = $("searchDocumentId").value.trim();
    const author = $("searchAuthor").value.trim();
    const sourcePath = $("searchSourcePath").value.trim();
    const chapter = $("searchChapter").value.trim();
    const page = +$("searchPage").value || null;
    const r = await api("/api/search", {
      method: "POST",
      body: {
        query,
        collections: cols.length ? cols : null,
        document_ids: documentId ? [documentId] : null,
        authors: author ? [author] : null,
        source_paths: sourcePath ? [sourcePath] : null,
        chapters: chapter ? [chapter] : null,
        pages: page ? [page] : null,
        top_k: +$("searchTopK").value || null,
        min_score: +$("searchMinScore").value || 0,
        include_graph: true,
      },
    });
    const timings = r.retrieval?.timings_ms || {};
    const graph = r.graph || {};
    $("searchDiagnostics").classList.remove("hidden");
    $("searchDiagnostics").innerHTML = `
      <div class="card-head"><h2>Query trace</h2><span class="muted small">trace <code>${esc(r.trace_id ?? "—")}</code></span></div>
      <div class="row wrap">
        <span>embedding <b>${Math.round(Number(timings.embedding || 0))} ms</b></span>
        <span>dense <b>${Math.round(Number(timings.dense || 0))} ms</b></span>
        <span>lexical <b>${Math.round(Number(timings.lexical || 0))} ms</b></span>
        <span>rerank <b>${Math.round(Number(r.rerank?.duration_ms || 0))} ms</b></span>
        <span>hybrid <b>${r.retrieval?.hybrid ? "yes" : "no"}</b></span>
        <span>lexical fallback <b>${esc(r.retrieval?.lexical_skipped_reason || (r.retrieval?.hybrid ? "used" : "—"))}</b></span>
        <span>usefulness <b>${Number(r.retrieval?.usefulness?.score || 0).toFixed(3)}</b></span>
        <span>top-k <b>${r.retrieval?.effective_top_k ?? "—"}/${r.retrieval?.requested_top_k ?? "—"}</b></span>
        <span>context <b>~${r.retrieval?.token_cost?.returned_context_tokens ?? 0} tokens</b></span>
      </div>`;
    $("evidenceGraph").classList.remove("hidden");
    const edges = graph.edges || [];
    $("evidenceGraph").innerHTML = `
      <div class="card-head"><h2>Evidence Graph</h2><span class="muted small">${graph.available === false ? "not materialized yet" : ((graph.nodes || []).length + " nodes · " + edges.length + " edges")}</span></div>
      ${graph.available === false ? `<p class="muted">${esc(graph.reason || "Graph unavailable.")}</p>` :
        edges.length ? `<div class="upload-list">${edges.map(e => `<div class="upload-item"><span><b>${esc(e.concept)}</b></span><span class="muted"><code>${esc(e.source)}</code> → <code>${esc(e.target)}</code>${e.query_match ? " · query match" : ""}</span></div>`).join("")}</div>` :
        `<p class="muted">No cross-document evidence edges for this result set.</p>`}`;
    if (!r.results.length) {
      renderReferenceSearchResults([], r.message || "No results.");
      await loadSearchMetrics();
      return;
    }
    renderReferenceSearchResults(r.results);
    await loadSearchMetrics();
  } catch (e) { renderReferenceSearchResults([], e.message); toast(e.message, true); }
}

async function loadSearchMetrics() {
  try {
    const data = await api("/api/search/metrics?hours=24&recent_limit=10");
    const s = data.summary || {};
    state.searchSummary = s;
    renderDashboardOverview(state.health);
    const recent = data.recent || [];
    const parts = [
      `<span><b>${s.queries || 0}</b> queries/24h</span>`,
      `<span><b>${s.zero_results || 0}</b> zero results</span>`,
      `<span><b>${s.errors || 0}</b> errors</span>`,
      `<span>average <b>${s.avg_total_ms ?? "—"} ms</b></span>`,
      `<span>p95 <b>${s.p95_total_ms ?? "—"} ms</b></span>`,
      `<span>embedding <b>${s.avg_embedding_ms ?? "—"} ms</b></span>`,
      `<span>DB <b>${s.avg_db_ms ?? "—"} ms</b></span>`,
      `<span>average top score <b>${s.avg_top_score ?? "—"}</b></span>`,
      `<span>usefulness <b>${s.avg_usefulness_score ?? "—"}</b></span>`,
      `<span>model input <b>~${s.avg_model_input_tokens ?? "—"} tokens</b></span>`,
      `<span>returned context <b>~${s.avg_returned_context_tokens ?? "—"} tokens</b></span>`,
    ];
    let html = `<div class="row wrap">${parts.join("")}</div>`;
    if (recent.length) {
      html += `<div class="upload-list">` + recent.map((x) => {
        const filters = Object.keys(x.filters || {}).length ? ` · <code>${esc(JSON.stringify(x.filters))}</code>` : "";
        const score = x.top_score != null ? ` · ${Number(x.top_score).toFixed(3)}` : "";
        return `<div class="upload-item"><span>${esc(x.query_text)}${filters}</span><span class="muted">${x.result_count} result. · ${Math.round(x.total_ms)} ms${score}</span></div>`;
      }).join("") + `</div>`;
    } else {
      html += `<p class="muted">No searches recorded.</p>`;
    }
    $("searchMetrics").innerHTML = html;
  } catch (e) {
    $("searchMetrics").textContent = "Failed to load metrics: " + e.message;
  }
}

/* ---------------- settings ---------------- */
async function loadSettings() {
  const data = await api("/api/settings");
  state.settings = data.settings;
  state.effective = data.effective;
}
async function loadSettingsTab() {
  await loadSettings();
  await loadBookCategories();
  $("lmUrl").value = state.settings.lmstudio_url || "";
  $("keepRerankerLoaded").checked = (state.effective?.rerank?.mode === "resident");
  $("overridesJson").value = JSON.stringify(state.settings.overrides || {}, null, 2);
  $("apiTokenField").value = token();
  $("mcpInfo").innerHTML = `<p>MCP endpoint for clients (Claude Code, etc.):</p>
    <pre>claude mcp add --transport http rag ${location.origin}/mcp</pre>
    <p class="muted small">Exposed tools: rag_search (com filtros), rag_search_metrics, list_collections, get_document.</p>`;
  await loadModels(false);
}
async function loadBookCategories() {
  const books = state.collections.find((c) => c.name === "Computer Books");
  if (!books) return;
  try {
    const data = await api(`/api/categories?collection_id=${books.id}`);
    state.categories = data.categories || [];
    const box = $("categoriesList");
    if (!box) return;
    box.innerHTML = state.categories.length
      ? `<div class="upload-list">${state.categories.map((c) => `
          <div class="upload-item">
            <span><b>${esc(c.name)}</b><span class="muted small"> · ${c.doc_count} document(s)${c.description ? " · " + esc(c.description) : ""}</span></span>
            <span class="row">
              <button class="btn mini" onclick="renameBookCategory('${c.id}', ${esc(JSON.stringify(c.name))})">Rename</button>
              <button class="btn mini danger" onclick="deleteBookCategory('${c.id}', ${esc(JSON.stringify(c.name))})">Delete</button>
            </span>
          </div>`).join("")}</div>`
      : `<p class="muted">No categories yet. Create one here, then run classification.</p>`;
  } catch (e) { toast(e.message, true); }
}

async function createBookCategory() {
  const books = state.collections.find((c) => c.name === "Computer Books");
  const name = $("newCategoryName")?.value.trim();
  const description = $("newCategoryDescription")?.value.trim() || "";
  if (!books || !name) return toast("Enter a category name", true);
  try {
    await api(`/api/collections/${books.id}/categories`, {
      method: "POST", body: { name, description }
    });
    $("newCategoryName").value = "";
    $("newCategoryDescription").value = "";
    await loadBookCategories();
    toast(`Category '${name}' created`);
  } catch (e) { toast(e.message, true); }
}

async function renameBookCategory(id, current) {
  const next = await askAction("Rename category", "Enter the new category name.", "Rename", false, current);
  if (!next) return;
  try {
    await api(`/api/categories/${id}`, { method: "PATCH", body: { name: next.trim() } });
    await loadBookCategories();
  } catch (e) { toast(e.message, true); }
}

async function deleteBookCategory(id, name) {
  if (!(await askAction("Delete category", `Delete '${name}'? Its books remain in Computer Books as Uncategorized.`, "Delete", true))) return;
  try {
    await api(`/api/categories/${id}`, { method: "DELETE" });
    await loadBookCategories();
    await loadDocuments();
  } catch (e) { toast(e.message, true); }
}

async function loadModels(verbose) {
  $("modelsInfo").textContent = "querying LM Studio…";
  try {
    const data = await api("/api/lmstudio/models");
    state.models = data.models;
    const emb = state.settings.embedding_model || "";
    const llm = state.settings.llm_model || "";
    const reranker = state.settings.reranker_model || state.effective?.rerank?.model || "";
    const opts = (filter, sel) => state.models.filter(filter)
      .map((m) => `<option value="${esc(m.id)}" ${m.id === sel ? "selected" : ""}>${esc(m.id)}${m.type === "unknown" ? " (unknown type)" : ""}${m.state === "loaded" ? " ●" : ""}</option>`).join("");
    $("rerankerModel").innerHTML = `<option value="">— select —</option>` + opts((m) => m.type !== "embedding", reranker);
    $("embeddingModel").innerHTML = `<option value="">— select —</option>` + opts((m) => m.type !== "llm", emb);
    $("llmModel").innerHTML = `<option value="">— select —</option>` + opts((m) => m.type !== "embedding", llm);
    $("autoModel").innerHTML = opts((m) => m.type !== "embedding", llm) || `<option value="">no model</option>`;
    $("modelsInfo").textContent = `${state.models.length} modelo(s) em ${data.url}`;
    buildManualForm();
    if (verbose) toast("Model list refreshed");
  } catch (e) {
    $("modelsInfo").textContent = "";
    if (verbose) toast(e.message, true);
    else $("modelsInfo").textContent = "LM Studio unavailable — configure the URL and test";
  }
}
async function testLM() {
  $("lmTestResult").innerHTML = `<span class="spinner"></span> testing…`;
  try {
    const r = await api("/api/lmstudio/test", { method: "POST", body: { url: $("lmUrl").value.trim() || null } });
    $("lmTestResult").innerHTML = r.reachable
      ? `<span style="color:var(--green)">✓ connected — ${r.models} model(s) available</span>`
      : `<span style="color:var(--red)">✗ ${esc(r.error || "failure")}</span>`;
  } catch (e) { $("lmTestResult").innerHTML = `<span style="color:var(--red)">✗ ${esc(e.message)}</span>`; }
}
async function saveLM() {
  try {
    await api("/api/settings", { method: "PUT", body: { lmstudio_url: $("lmUrl").value.trim() } });
    toast("LM Studio URL saved");
    await loadSettings(); await loadModels(true);
  } catch (e) { toast(e.message, true); }
}
async function saveModels() {
  try {
    await api("/api/settings", { method: "PUT", body: { embedding_model: $("embeddingModel").value || null, llm_model: $("llmModel").value || null, reranker_model: $("rerankerModel").value || null, overrides: { rerank: { mode: $("keepRerankerLoaded").checked ? "resident" : "switch" } } } });
    await loadSettings(); buildManualForm();
    toast("Models saved");
  } catch (e) { toast(e.message, true); }
}
async function saveOverrides() {
  let parsed;
  try { parsed = JSON.parse($("overridesJson").value || "{}"); }
  catch (e) { $("overridesMsg").innerHTML = `<span style="color:var(--red)">Invalid JSON: ${esc(e.message)}</span>`; return; }
  try {
    await api("/api/settings", { method: "PUT", body: { overrides: parsed } });
    await loadSettings();
    $("overridesMsg").innerHTML = `<span style="color:var(--green)">✓ saved</span>`;
    buildManualForm();
  } catch (e) { $("overridesMsg").innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`; }
}

async function pollDocumentsClassification() {
  if (state.activeTab !== "documents" || !state.docsHasPendingAI) return;
  await loadDocuments();
}

/* ---------------- boot ---------------- */
(async function init() {
  try {
    await loadSettings();
    await loadCollections();
    await loadModels(false);
    buildManualForm();
    refreshStaged();
    pollStatus();
    pollHealth();
    loadSearchMetrics();
    showTab(localStorage.getItem("rag_active_tab") || "dashboard", { persist: false });
    setInterval(pollStatus, 1500);
    setInterval(pollDocumentsClassification, 2000);
    setInterval(pollHealth, 60000);
    setInterval(() => loadModels(false), 10 * 60 * 1000);
  } catch (e) {
    if (!token()) showAuthDialog();
    console.error(e);
  }
})();
