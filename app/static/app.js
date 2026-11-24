/* RAG Server — administrative interface (vanilla JS, no external dependencies) */
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
  collections: [], sources: [], events: [], models: [], settings: {}, effective: null,
  mode: "manual", suggestion: null, docsOffset: 0, docsLimit: 50,
  pollTick: 0, lastActive: 0, uploading: false,
  selectedDocs: new Set(), currentDocIds: [], docsHasPendingAI: false, activeTab: "dashboard",
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
  if (resp.status === 401) { $("tokenBanner").classList.remove("hidden"); throw new Error("API token required"); }
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
function saveToken() { localStorage.setItem("rag_token", $("tokenInput").value.trim()); $("tokenBanner").classList.add("hidden"); location.reload(); }
function saveTokenFromField() { localStorage.setItem("rag_token", $("apiTokenField").value.trim()); toast("Token saved in this browser"); }
const fmtBytes = (n) => { if (!n && n !== 0) return "—"; const u = ["B", "KB", "MB", "GB"]; let i = 0; while (n >= 1024 && i < 3) { n /= 1024; i++; } return n.toFixed(i ? 1 : 0) + " " + u[i]; };
const fmtDate = (s) => s ? new Date(s).toLocaleString("en-US", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";
const fmtTime = (s) => s ? new Date(s).toLocaleTimeString("en-US") : "";

const TAB_META = {
  dashboard: ["Dashboard", "RAG Server overview, system health, and retrieval pipeline at a glance."],
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
  const autoOption = state.settings.llm_model
    ? `<option value="__auto__">✨ Automatic classification</option>`
    : `<option value="__auto__" disabled>✨ Automatic classification — configure LLM first</option>`;
  imp.innerHTML = autoOption + (opts || `<option value="">— create a collection —</option>`);
  if (prev === "__auto__" && state.settings.llm_model) imp.value = prev;
  else if (prev && state.collections.some((c) => c.id === prev)) imp.value = prev;
  const filt = $("docFilterCollection");
  const prevF = filt.value;
  filt.innerHTML = `<option value="">All collections</option>` + opts;
  filt.value = prevF;
}
function renderCollections() {
  $("collectionsGrid").innerHTML = state.collections.map((c) => `
    <div class="card col-card">
      <h3>${esc(c.name)}</h3>
      <div class="desc">${esc(c.description || "")}</div>
      <div class="col-stats">
        <span><b>${c.doc_count}</b> docs</span>
        <span><b>${c.ready_count}</b> indexed</span>
        <span><b>${c.chunk_count}</b> chunks</span>
        ${Number(c.error_count) ? `<span style="color:var(--red)"><b>${c.error_count}</b> errors</span>` : ""}
        ${Number(c.staged_count) ? `<span style="color:var(--yellow)"><b>${c.staged_count}</b> waiting</span>` : ""}
      </div>
      <div class="muted small">${c.embedding_model ? "🧬 " + esc(c.embedding_model) : "no embeddings yet"}</div>
      <div class="row" style="margin-top:10px">
        <button class="btn mini ${c.default_search ? 'primary' : ''}" onclick="toggleDefaultSearch('${c.id}', ${!c.default_search})">Default search: ${c.default_search ? 'ON' : 'OFF'}</button>
        <button class="btn mini" onclick="renameCollection('${c.id}', '${esc(c.name)}')">Rename</button>
        <button class="btn mini" onclick="reindexCollectionModal('${c.id}')">Reindex all</button>
        <button class="btn mini danger" onclick="deleteCollection('${c.id}', '${esc(c.name)}')">Delete</button>
      </div>
    </div>`).join("") || `<p class="muted">No collections yet. Create one from the Import tab.</p>`;
}
async function createCollection() {
  const name = $("newCollectionName").value.trim();
  if (!name) return toast("Enter a collection name", true);
  try {
    const col = await api("/api/collections", { method: "POST", body: { name } });
    $("newCollectionName").value = "";
    await loadCollections();
    $("importCollection").value = col.id;
    onImportCollectionChange();
    toast(`Collection '${name}' created`);
  } catch (e) { toast(e.message, true); }
}
async function renameCollection(id, currentName) {
  const next = prompt("New collection name:", currentName);
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
  if (!confirm(`Delete collection '${name}' and ALL of its documents/vectors?`)) return;
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
  openModal(`Reindex collection '${esc(c.name)}'`, `
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
    if (xhr.status === 401) { $("tokenBanner").classList.remove("hidden"); box.innerHTML = ""; return; }
    let data;
    try { data = JSON.parse(xhr.responseText); } catch { data = { detail: xhr.responseText }; }
    if (xhr.status >= 400) { box.innerHTML = ""; toast(data.detail || "Upload failed", true); return; }
    renderUploadResults(data);
    await refreshStaged(); await loadCollections();
    const parts = [];
    if (data.queued && data.route_mode === "manual_destination") parts.push(`${data.queued} queued for ingestion in ${data.destination_collection} (manual destination)`);
    if (data.queued && data.route_mode === "automatic_classification") parts.push(`${data.queued} queued for ingestion; AI classification follows indexing`);
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
    const inboxName = state.effective?.classification?.staging_collection_name;
    const inbox = state.collections.find((c) => c.name === inboxName);
    if (!inbox) return;
    colId = inbox.id;
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
  $("problems").innerHTML = (s.problems || []).map((p) => `
    <div class="problem ${p.status === "waiting" ? "waiting" : ""}">
      <b>${esc(p.filename)}</b> ${badge(p.status)}
      ${p.status === "error" ? `<button class="btn mini" onclick="retryDoc('${p.id}')">Retry</button>` : ""}
      <div class="err-msg">${esc(p.error || "")}</div>
    </div>`).join("");
}
async function pollHealth() {
  let h = null;
  try { h = await api("/api/health"); } catch { /* offline */ }
  const version = h?.build?.version || "dev";
  const envName = h?.build?.environment || "dev";
  if ($("versionText")) $("versionText").textContent = `${version} · ${envName.toUpperCase()}`;
  if ($("sidebarVersion")) $("sidebarVersion").classList.toggle("offline", !h);
  const status = document.querySelector(".app-header-status");
  if (status) status.classList.toggle("offline", !h || h.status !== "ok");
  $("healthCards").innerHTML = h ? `
    <div class="card"><div class="stat-label">PostgreSQL + pgvector</div>
      <div class="stat" style="color:${h.database ? "var(--green)" : "var(--red)"}">${h.database ? "online" : "offline"}</div></div>
    <div class="card"><div class="stat-label">LM Studio</div>
      <div class="stat" style="color:${h.lmstudio?.reachable ? "var(--green)" : "var(--red)"}">${h.lmstudio?.reachable ? "online" : "offline"}</div>
      <div class="muted small">${esc(h.lmstudio?.url || "")}${h.lmstudio?.reachable && state.models.length ? ` · ${state.models.length} known model(s)` : ""}</div>
      ${h.lmstudio?.error ? `<div class="small" style="color:var(--yellow)">${esc(h.lmstudio.error)}</div>` : ""}</div>
    <div class="card"><div class="stat-label">Ingestion worker</div>
      <div class="stat" style="color:${h.worker?.paused ? "var(--yellow)" : "var(--green)"}">${h.worker?.paused ? "paused" : h.worker?.running ? "active" : "stopped"}</div>
      <div class="muted small">automatically resumes after restart</div></div>` : "";
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
  $("eventsLog").innerHTML = events.map((e) => `
    <div class="event ${esc(e.level)}">
      <span class="ts">${fmtDate(e.ts)}</span>
      <span class="event-level">${esc((e.level || "info").toUpperCase())}</span>
      <span class="msg">${esc(e.message)}</span>
    </div>`).join("") || `<p class="muted small event-empty">No events match these filters.</p>`;
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
  if (!$("sourcesList")) return;
  try {
    const data = await api("/api/collection-sources");
    state.sources = data.sources || [];
    $("sourcesList").innerHTML = state.sources.length ? `<div class="upload-list">` + state.sources.map((s) => {
      const stats = s.last_stats || {};
      const summary = s.last_sync_at
        ? `last ${fmtDate(s.last_sync_at)} · +${stats.new || 0} ~${stats.changed || 0} -${stats.deleted || 0}`
        : "not synced yet";
      return `<div class="upload-item">
        <span><b>${esc(s.collection)}</b> · <code>${esc(s.path)}</code><br><span class="muted small">${summary}${s.last_error ? ` · error: ${esc(s.last_error)}` : ""}</span></span>
        <span class="row" style="margin:0">
          <span class="pill">${s.enabled ? "ON" : "OFF"} · ${formatInterval(Number(s.interval_seconds))}</span>
          <button class="btn mini" onclick="syncSource('${s.id}')">Sync now</button>
          <button class="btn mini" onclick="editSource('${s.id}')">Edit</button>
          <button class="btn mini danger" onclick="deleteSource('${s.id}')">Delete</button>
        </span>
      </div>`;
    }).join("") + `</div>` : `<p class="muted">No directory sources configured.</p>`;
  } catch (e) { $("sourcesList").textContent = e.message; }
}
function openAddSource() {
  if (!state.collections.length) return toast("Create a collection first", true);
  const collectionOptions = state.collections.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
  const defaultInterval = state.effective?.sources?.default_interval_seconds;
  openModal("Add directory source", `
    <div class="form-grid">
      <label>Collection<select id="sourceCollection">${collectionOptions}</select></label>
      <label>Interval<select id="sourceInterval">${sourceIntervalOptions(defaultInterval)}</select></label>
    </div>
    <label>Path visible inside RAG container<input id="sourcePath" type="text" placeholder="/sources/project"></label>
    <label>Extra exclude patterns (one per line)<textarea id="sourceExcludes" rows="4" placeholder="build/**&#10;dist/**"></textarea></label>
    <button class="btn primary" onclick="createSource()">Create source</button>`);
}
async function createSource() {
  try {
    const collectionId = $("sourceCollection").value;
    const body = {
      path: $("sourcePath").value.trim(),
      interval_seconds: Number($("sourceInterval").value),
      exclude_patterns: $("sourceExcludes").value.split("\n").map((x) => x.trim()).filter(Boolean),
    };
    await api(`/api/collections/${collectionId}/sources`, { method: "POST", body });
    closeModal();
    await loadCollectionSources();
    toast("Directory source created; first sync queued");
  } catch (e) { toast(e.message, true); }
}
function editSource(id) {
  const s = state.sources.find((x) => x.id === id);
  if (!s) return;
  openModal("Edit directory source", `
    <label>Path<input id="sourceEditPath" type="text" value="${esc(s.path)}"></label>
    <div class="form-grid">
      <label>Interval<select id="sourceEditInterval">${sourceIntervalOptions(s.interval_seconds)}</select></label>
      <label class="check"><input id="sourceEditEnabled" type="checkbox" ${s.enabled ? "checked" : ""}> enabled</label>
    </div>
    <label>Extra exclude patterns (one per line)<textarea id="sourceEditExcludes" rows="4">${esc((s.exclude_patterns || []).join("\n"))}</textarea></label>
    <button class="btn primary" onclick="saveSource('${id}')">Save</button>`);
}
async function saveSource(id) {
  try {
    await api(`/api/collection-sources/${id}`, { method: "PATCH", body: {
      path: $("sourceEditPath").value.trim(),
      interval_seconds: Number($("sourceEditInterval").value),
      enabled: $("sourceEditEnabled").checked,
      exclude_patterns: $("sourceEditExcludes").value.split("\n").map((x) => x.trim()).filter(Boolean),
    }});
    closeModal();
    await loadCollectionSources();
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
  if (!confirm("Delete this directory source? Existing documents stay in the collection.")) return;
  try {
    await api(`/api/collection-sources/${id}`, { method: "DELETE" });
    await loadCollectionSources();
    toast("Directory source deleted");
  } catch (e) { toast(e.message, true); }
}

/* ---------------- documents ---------------- */
let docsDebounce = null;
function debouncedDocs() { clearTimeout(docsDebounce); docsDebounce = setTimeout(loadDocuments, 350); }
function docsPage(dir) { state.docsOffset = Math.max(0, state.docsOffset + dir * state.docsLimit); loadDocuments(); }
function classificationSummary(d) {
  const queued = d.meta?.auto_classify && !d.auto_suggestion;
  if (queued) return `<span class="badge pending">AI queued</span>`;
  const s = d.auto_suggestion;
  if (!s) return `<span class="muted small">—</span>`;
  if (s.status === "classification_error") return `<span class="small" style="color:var(--red)">AI error · ${esc(s.error || "")}</span>`;
  const dest = s.routed_to || s.existing_collection || s.proposed_collection?.name || "review";
  const confidence = Math.round((Number(s.confidence) || 0) * 100);
  const prefix = s.auto_applied ? "AI ✓" : "AI review";
  return `<span class="small ${s.auto_applied ? "" : "muted"}">${prefix} · ${esc(dest)} · ${confidence}%</span>`;
}
function updateDocsSelectionUI() {
  const count = state.selectedDocs.size;
  const label = $("docsSelectedCount");
  const btn = $("btnBulkClassifySelected");
  if (label) label.textContent = `${count} selected`;
  if (btn) btn.disabled = count === 0;
  const page = $("docsSelectPage");
  if (page) {
    const ids = state.currentDocIds || [];
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
async function queueBulkClassification(body, label) {
  try {
    const r = await api("/api/documents/classify-bulk", { method: "POST", body });
    state.selectedDocs.clear();
    updateDocsSelectionUI();
    await loadDocuments();
    toast(r.queued ? `${r.queued} document(s) queued for AI reclassification` : `No indexed documents matched ${label}`);
  } catch (e) { toast(e.message, true); }
}
async function bulkClassifySelected() {
  const ids = [...state.selectedDocs];
  if (!ids.length) return;
  await queueBulkClassification({ document_ids: ids }, "the selection");
}
async function bulkClassifyFiltered() {
  const collectionId = $("docFilterCollection").value || null;
  const q = $("docSearch").value.trim() || null;
  const scope = collectionId || q ? "the current filters" : "the entire indexed library";
  await queueBulkClassification({ all_filtered: true, collection_id: collectionId, q }, scope);
}
async function loadDocuments() {
  const params = new URLSearchParams({ limit: state.docsLimit, offset: state.docsOffset });
  if ($("docFilterCollection").value) params.set("collection_id", $("docFilterCollection").value);
  if ($("docFilterStatus").value) params.set("status", $("docFilterStatus").value);
  if ($("docSearch").value) params.set("q", $("docSearch").value);
  try {
    const data = await api("/api/documents?" + params);
    $("docsTotal").textContent = `${data.total} document(s) · page ${Math.floor(state.docsOffset / state.docsLimit) + 1}`;
    $("docsPrev").disabled = state.docsOffset === 0;
    $("docsNext").disabled = state.docsOffset + state.docsLimit >= data.total;
    state.currentDocIds = data.items.map((d) => d.id);
    state.docsHasPendingAI = data.items.some((d) => d.meta?.auto_classify && !d.auto_suggestion);
    $("docsTable").innerHTML = `<table><thead><tr>
      <th></th><th>Document</th><th>Collection</th><th>Status</th><th>AI classification</th><th>Progress</th><th>Size</th><th>Updated</th><th></th>
    </tr></thead><tbody>` + data.items.map((d) => {
      const pct = d.chunks_total ? Math.round((d.chunks_embedded / d.chunks_total) * 100) : 0;
      const selected = state.selectedDocs.has(d.id) ? "checked" : "";
      return `<tr>
        <td><input type="checkbox" class="doc-select" ${selected} onchange="toggleDocSelection('${d.id}', this.checked)"></td>
        <td><div class="docname">${esc(d.filename)}</div><div class="docpath">${esc(d.source_path)}</div></td>
        <td>${esc(d.collection)}</td>
        <td>${badge(d.status)}${d.error ? `<div class="docpath" style="color:var(--red);max-width:260px">${esc(d.error.slice(0, 120))}</div>` : ""}</td>
        <td>${classificationSummary(d)}</td>
        <td>${d.chunks_total ? `<span class="mini-progress"><div style="width:${pct}%"></div></span> <span class="small muted">${d.chunks_embedded}/${d.chunks_total}</span>` : "—"}</td>
        <td class="small muted">${fmtBytes(d.size_bytes)}</td>
        <td class="small muted">${fmtDate(d.updated_at)}</td>
        <td><div class="row" style="margin:0;flex-wrap:nowrap">
          <button class="btn mini" onclick="docDetails('${d.id}')">Details</button>
          ${["error", "waiting"].includes(d.status) ? `<button class="btn mini" onclick="retryDoc('${d.id}')">Retry</button>` : ""}
          ${["pending", "extracting", "chunking", "embedding", "waiting", "cancelling"].includes(d.status) ? `<button class="btn mini danger" onclick="cancelDoc(&quot;${d.id}&quot;)">Cancel</button>` : ""}
          <button class="btn mini" onclick="classifyDoc('${d.id}', '${esc(d.filename)}')">Classify</button>
          <button class="btn mini" onclick="openMoveDoc('${d.id}', '${esc(d.filename)}', '${d.collection_id}')">Move</button>
          <button class="btn mini" onclick="reindexDoc('${d.id}', '${esc(d.filename)}')">Reindex</button>
          <button class="btn mini danger" onclick="deleteDoc('${d.id}', '${esc(d.filename)}')">Delete</button>
        </div></td>
      </tr>`;
    }).join("") + `</tbody></table>`;
    updateDocsSelectionUI();
  } catch (e) { toast(e.message, true); }
}
async function retryDoc(id) { try { await api(`/api/documents/${id}/retry`, { method: "POST" }); toast("Document requeued"); loadDocuments(); } catch (e) { toast(e.message, true); } }
async function cancelDoc(id) {
  if (!confirm("Cancel processing for this document? Partial chunks and vectors will be removed.")) return;
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
  if (!confirm(`Reindex '${name}'? Current chunks/vectors will be recreated.`)) return;
  try { await api(`/api/documents/${id}/reindex`, { method: "POST", body: {} }); toast("Reindex started"); loadDocuments(); }
  catch (e) { toast(e.message, true); }
}
async function classifyDoc(id, name) {
  try {
    toast(`Classifying '${name}'…`);
    const r = await api(`/api/documents/${id}/classify`, { method: "POST", body: {} });
    state.classificationSuggestions = state.classificationSuggestions || {};
    state.classificationSuggestions[id] = r;
    const confidence = Math.round((r.confidence || 0) * 100);
    const destination = r.existing_collection
      ? `Existing collection: <b>${esc(r.existing_collection)}</b>`
      : `Proposed new collection: <b>${esc(r.proposed_collection?.name || "")}</b>`;
    openModal(`Classify '${esc(name)}'`, `
      <p>${destination}</p>
      <p><b>Confidence:</b> ${confidence}%</p>
      <p class="muted">${esc(r.rationale || "")}</p>
      <button class="btn primary" onclick="applyClassification('${id}')">Apply suggestion</button>`);
  } catch (e) { toast(e.message, true); }
}

async function applyClassification(id) {
  const r = state.classificationSuggestions?.[id];
  if (!r) return;
  try {
    let target = null;
    if (r.existing_collection) {
      target = state.collections.find((c) => c.name === r.existing_collection);
      if (!target) throw new Error(`Collection '${r.existing_collection}' no longer exists`);
    } else if (r.proposed_collection) {
      target = await api("/api/collections", { method: "POST", body: {
        name: r.proposed_collection.name, description: r.proposed_collection.description || ""
      }});
      await loadCollections();
    }
    if (!target) throw new Error("Classifier did not return a destination");
    const moved = await api(`/api/documents/${id}/move`, {
      method: "POST", body: { target_collection_id: target.id },
    });
    closeModal();
    await loadDocuments();
    toast(`Moved to ${moved.to_collection}`);
  } catch (e) { toast(e.message, true); }
}

async function deleteDoc(id, name) {
  if (!confirm(`Delete '${name}' and its vectors?`)) return;
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
async function doSearch() {
  const query = $("searchQuery").value.trim();
  if (!query) return;
  const cols = [...document.querySelectorAll(".searchCol:checked")].map((c) => c.value);
  $("searchResults").innerHTML = `<p class="muted"><span class="spinner"></span> Searching…</p>`;
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
      },
    });
    if (!r.results.length) {
      $("searchResults").innerHTML = `<div class="card"><p class="muted">${esc(r.message || "No results.")}</p></div>`;
      await loadSearchMetrics();
      return;
    }
    $("searchResults").innerHTML = r.results.map((x) => `
      <div class="result-card">
        <div class="result-head">
          <span class="result-source">📄 ${esc(x.source_path)}</span>
          <span class="result-score">score ${x.score.toFixed(3)}</span>
        </div>
        <div class="result-loc">${esc(x.collection)}${x.location_label ? " · " + esc(x.location_label) : ""} · chunk #${x.chunk_seq}</div>
        <div class="result-content">${esc(x.content)}</div>
        <div class="result-meta">doc: ${esc(x.document)} · id: <code>${esc(x.document_id)}</code>${x.metadata?.title ? " · " + esc(x.metadata.title) : ""}</div>
      </div>`).join("");
    await loadSearchMetrics();
  } catch (e) { $("searchResults").innerHTML = ""; toast(e.message, true); }
}

async function loadSearchMetrics() {
  try {
    const data = await api("/api/search/metrics?hours=24&recent_limit=10");
    const s = data.summary || {};
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
  $("lmUrl").value = state.settings.lmstudio_url || "";
  $("keepRerankerLoaded").checked = (state.effective?.rerank?.mode === "resident");
  $("overridesJson").value = JSON.stringify(state.settings.overrides || {}, null, 2);
  $("apiTokenField").value = token();
  $("mcpInfo").innerHTML = `<p>MCP endpoint for clients (Claude Code, etc.):</p>
    <pre>claude mcp add --transport http rag ${location.origin}/mcp</pre>
    <p class="muted small">Exposed tools: rag_search (com filtros), rag_search_metrics, list_collections, get_document.</p>`;
  await loadModels(false);
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

/* ---------------- dev live reload ---------------- */
let devReloadToken = null;
async function pollDevReload() {
  let delay = 1000;
  try {
    const data = await api("/api/dev/reload");
    delay = data.poll_ms || delay;
    if (!data.enabled) return;
    if (devReloadToken === null) {
      devReloadToken = data.token;
    } else if (data.token && data.token !== devReloadToken) {
      location.reload();
      return;
    }
  } catch (_) {
    // The backend can be briefly unavailable during reload.
  }
  setTimeout(pollDevReload, delay);
}

/* ---------------- boot ---------------- */
(async function init() {
  pollDevReload();
  try {
    await loadSettings();
    await loadCollections();
    await loadModels(false);
    buildManualForm();
    refreshStaged();
    pollStatus();
    pollHealth();
    showTab(localStorage.getItem("rag_active_tab") || "dashboard", { persist: false });
    setInterval(pollStatus, 1500);
    setInterval(pollDocumentsClassification, 2000);
    setInterval(pollHealth, 60000);
    setInterval(() => loadModels(false), 10 * 60 * 1000);
  } catch (e) {
    if (!token()) $("tokenBanner").classList.remove("hidden");
    console.error(e);
  }
})();
