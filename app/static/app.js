/* RAG Server — interface administrativa (vanilla JS, sem dependências externas) */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const STATUS_LABEL = {
  staged: "Aguardando configuração", pending: "Na fila", extracting: "Extraindo",
  chunking: "Dividindo", embedding: "Embeddings", waiting: "Aguardando LM Studio",
  ready: "Indexado", error: "Erro", cancelling: "Cancelando", cancelled: "Cancelado",
};
const badge = (s) => `<span class="badge ${esc(s)}">${esc(STATUS_LABEL[s] || s)}</span>`;

let state = {
  collections: [], models: [], settings: {}, effective: null,
  mode: "manual", suggestion: null, docsOffset: 0, docsLimit: 50,
  pollTick: 0, lastActive: 0, uploading: false,
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
  if (resp.status === 401) { $("tokenBanner").classList.remove("hidden"); throw new Error("Token de API necessário"); }
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
function saveTokenFromField() { localStorage.setItem("rag_token", $("apiTokenField").value.trim()); toast("Token salvo neste navegador"); }
const fmtBytes = (n) => { if (!n && n !== 0) return "—"; const u = ["B", "KB", "MB", "GB"]; let i = 0; while (n >= 1024 && i < 3) { n /= 1024; i++; } return n.toFixed(i ? 1 : 0) + " " + u[i]; };
const fmtDate = (s) => s ? new Date(s).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";
const fmtTime = (s) => s ? new Date(s).toLocaleTimeString("pt-BR") : "";

function showTab(name) {
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
}
function fillCollectionSelects() {
  const opts = state.collections.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
  const imp = $("importCollection");
  const prev = imp.value;
  imp.innerHTML = opts || `<option value="">— crie uma coleção —</option>`;
  if (prev && state.collections.some((c) => c.id === prev)) imp.value = prev;
  const filt = $("docFilterCollection");
  const prevF = filt.value;
  filt.innerHTML = `<option value="">Todas as coleções</option>` + opts;
  filt.value = prevF;
}
function renderCollections() {
  $("collectionsGrid").innerHTML = state.collections.map((c) => `
    <div class="card col-card">
      <h3>${esc(c.name)}</h3>
      <div class="desc">${esc(c.description || "")}</div>
      <div class="col-stats">
        <span><b>${c.doc_count}</b> docs</span>
        <span><b>${c.ready_count}</b> indexados</span>
        <span><b>${c.chunk_count}</b> chunks</span>
        ${Number(c.error_count) ? `<span style="color:var(--red)"><b>${c.error_count}</b> erros</span>` : ""}
        ${Number(c.staged_count) ? `<span style="color:var(--yellow)"><b>${c.staged_count}</b> aguardando</span>` : ""}
      </div>
      <div class="muted small">${c.embedding_model ? "🧬 " + esc(c.embedding_model) : "sem embeddings ainda"}</div>
      <div class="row" style="margin-top:10px">
        <button class="btn mini" onclick="reindexCollectionModal('${c.id}')">Reindexar tudo</button>
        <button class="btn mini danger" onclick="deleteCollection('${c.id}', '${esc(c.name)}')">Excluir</button>
      </div>
    </div>`).join("") || `<p class="muted">Nenhuma coleção ainda. Crie uma na aba Importar (ex.: ARGUS, Documentação, Livros).</p>`;
}
async function createCollection() {
  const name = $("newCollectionName").value.trim();
  if (!name) return toast("Digite um nome para a coleção", true);
  try {
    const col = await api("/api/collections", { method: "POST", body: { name } });
    $("newCollectionName").value = "";
    await loadCollections();
    $("importCollection").value = col.id;
    onImportCollectionChange();
    toast(`Coleção '${name}' criada`);
  } catch (e) { toast(e.message, true); }
}
async function deleteCollection(id, name) {
  if (!confirm(`Excluir a coleção '${name}' e TODOS os seus documentos/vetores?`)) return;
  try { await api(`/api/collections/${id}`, { method: "DELETE" }); await loadCollections(); toast(`Coleção '${name}' excluída`); }
  catch (e) { toast(e.message, true); }
}
function reindexCollectionModal(id) {
  const c = state.collections.find((x) => x.id === id);
  const cfg = { ...(state.effective?.ingest || {}), ...(c.ingest_config || {}) };
  const modelOpts = state.models.map((m) => `<option value="${esc(m.id)}" ${m.id === (c.embedding_model || state.settings.embedding_model) ? "selected" : ""}>${esc(m.id)}</option>`).join("");
  openModal(`Reindexar coleção '${esc(c.name)}'`, `
    <p class="small muted">Reprocessa todos os documentos. Trocar o modelo de embedding apaga os vetores atuais e recria do zero.</p>
    <div class="form-grid">
      <label>Modelo de embedding<select id="rcModel">${modelOpts}</select></label>
      <label>chunk_size<input type="number" id="rcSize" value="${cfg.chunk_size || 1500}"></label>
      <label>chunk_overlap<input type="number" id="rcOverlap" value="${cfg.chunk_overlap ?? 200}"></label>
      <label>estratégia<select id="rcStrategy">${["recursive", "fixed", "markdown", "pages"].map((s) => `<option ${s === cfg.strategy ? "selected" : ""}>${s}</option>`).join("")}</select></label>
    </div>
    <button class="btn primary" onclick="reindexCollection('${id}')">Reindexar ${c.doc_count} documento(s)</button>`);
}
async function reindexCollection(id) {
  try {
    const body = {
      embedding_model: $("rcModel").value || null,
      config: { chunk_size: +$("rcSize").value, chunk_overlap: +$("rcOverlap").value, strategy: $("rcStrategy").value },
    };
    const r = await api(`/api/collections/${id}/reindex`, { method: "POST", body });
    closeModal(); toast(`Reindexando ${r.reindexing} documento(s)`); showTab("dashboard");
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
  if (!colId) return toast("Crie/selecione uma coleção primeiro", true);
  if (!items.length) return;
  if (state.uploading) return toast("Aguarde o upload atual terminar", true);
  state.uploading = true;
  const fd = new FormData();
  fd.append("collection_id", colId);
  for (const it of items) { fd.append("files", it.file, it.file.name); fd.append("paths", it.path); }
  const box = $("uploadResults");
  box.innerHTML = `<div class="row"><span class="spinner"></span> <span id="upProgressText">Enviando ${items.length} arquivo(s)…</span></div>
    <div class="progress-wrap"><div class="progress-bar" id="upBar"></div></div>`;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/documents/upload");
  if (token()) xhr.setRequestHeader("Authorization", "Bearer " + token());
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const pct = Math.round((e.loaded / e.total) * 100);
      $("upBar").style.width = pct + "%";
      $("upProgressText").textContent = `Enviando ${items.length} arquivo(s)… ${pct}% (${fmtBytes(e.loaded)} / ${fmtBytes(e.total)})`;
    }
  };
  xhr.onload = async () => {
    state.uploading = false;
    if (xhr.status === 401) { $("tokenBanner").classList.remove("hidden"); box.innerHTML = ""; return; }
    let data;
    try { data = JSON.parse(xhr.responseText); } catch { data = { detail: xhr.responseText }; }
    if (xhr.status >= 400) { box.innerHTML = ""; toast(data.detail || "Falha no upload", true); return; }
    renderUploadResults(data);
    await refreshStaged(); await loadCollections();
    const parts = [];
    if (data.queued) parts.push(`${data.queued} enfileirado(s) automaticamente`);
    if (data.staged) parts.push(`${data.staged} aguardando configuração`);
    if (data.duplicates) parts.push(`${data.duplicates} duplicado(s) ignorado(s)`);
    if (data.ignored_empty) parts.push(`${data.ignored_empty} vazio(s) ignorado(s)`);
    toast(parts.join(", ") || "Upload concluído");
  };
  xhr.onerror = () => { state.uploading = false; box.innerHTML = ""; toast("Erro de rede no upload", true); };
  xhr.send(fd);
  $("fileInput").value = ""; $("dirInput").value = "";
}
function renderUploadResults(data) {
  const icon = { pending: "⚙️", staged: "📄", duplicate: "♻️", ignored_empty: "⚪", unsupported: "🚫", too_large: "🐘", error: "❌" };
  const label = { pending: "importado e enfileirado", staged: "importado; aguardando configuração", duplicate: "duplicado (ignorado)", ignored_empty: "vazio (ignorado)", unsupported: "não suportado", too_large: "grande demais", error: "erro" };
  $("uploadResults").innerHTML = `<div class="upload-list">` + data.results.map((r) => `
    <div class="upload-item">
      <span>${icon[r.status] || ""} <span class="docpath">${esc(r.source_path)}</span></span>
      <span class="muted">${label[r.status] || r.status}${r.error ? " — " + esc(r.error) : ""}</span>
    </div>`).join("") + `</div>`;
}

/* ---------------- ingest config ---------------- */
function onImportCollectionChange() { buildManualForm(); refreshStaged(); }
async function refreshStaged() {
  const colId = $("importCollection").value;
  if (!colId) { $("stagedCount").textContent = ""; return; }
  try {
    const data = await api(`/api/documents?collection_id=${colId}&status=staged&limit=1`);
    $("stagedCount").textContent = `${data.total} documento(s) aguardando configuração`;
    $("btnStart").disabled = data.total === 0;
  } catch { /* ignore */ }
}
function buildManualForm() {
  const col = state.collections.find((c) => c.id === $("importCollection").value);
  const eff = state.effective?.ingest || { chunk_size: 1500, chunk_overlap: 200, min_chunk_size: 120, strategy: "recursive", embed_batch_size: 16 };
  const cfg = { ...eff, ...(col?.ingest_config || {}) };
  const model = state.settings.embedding_model || "";
  const embOpts = state.models
    .filter((m) => m.type !== "llm")
    .map((m) => `<option value="${esc(m.id)}" ${m.id === model ? "selected" : ""}>${esc(m.id)}${m.type === "unknown" ? " (tipo desconhecido)" : ""}</option>`).join("");
  $("manualForm").innerHTML = `
    <label>Modelo de embedding<select id="mModel">${embOpts || `<option value="">— configure em Configurações —</option>`}</select></label>
    <label>chunk_size (caracteres)<input type="number" id="mSize" value="${cfg.chunk_size}" min="200" max="8000"></label>
    <label>chunk_overlap (caracteres)<input type="number" id="mOverlap" value="${cfg.chunk_overlap}" min="0" max="1000"></label>
    <label>min_chunk_size<input type="number" id="mMin" value="${cfg.min_chunk_size}" min="0" max="2000"></label>
    <label>estratégia<select id="mStrategy">${["recursive", "fixed", "markdown", "pages"].map((s) => `<option ${s === cfg.strategy ? "selected" : ""}>${s}</option>`).join("")}</select></label>
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
  if (!colId) return toast("Selecione uma coleção", true);
  const model = $("autoModel").value;
  if (!model) return toast("Selecione um modelo LLM (aba Configurações carrega a lista)", true);
  const btn = $("btnSuggest");
  btn.disabled = true; btn.innerHTML = `<span class="spinner"></span> Analisando…`;
  $("suggestionBox").innerHTML = `<p class="muted small">Extraindo amostra e consultando o LLM — pode levar alguns segundos…</p>`;
  try {
    const s = await api("/api/ingest/suggest", { method: "POST", body: { collection_id: colId, model } });
    state.suggestion = s;
    renderSuggestion(s);
  } catch (e) {
    $("suggestionBox").innerHTML = "";
    toast(e.message, true);
  } finally { btn.disabled = false; btn.textContent = "Analisar documentos"; }
}
function renderSuggestion(s) {
  const c = s.config;
  const srcLabel = s.source === "llm" ? `🤖 sugerido por ${esc(s.model)}` : "⚙️ heurística (LLM indisponível/inválido)";
  $("suggestionBox").innerHTML = `
    <div class="suggestion">
      <h3>Parâmetros sugeridos — ${srcLabel}</h3>
      ${s.warning ? `<p class="small" style="color:var(--yellow)">⚠️ ${esc(s.warning)}</p>` : ""}
      <p class="rationale">${esc(s.rationale)}</p>
      <div class="form-grid">
        <label>chunk_size<input type="number" id="sSize" value="${c.chunk_size}"></label>
        <label>chunk_overlap<input type="number" id="sOverlap" value="${c.chunk_overlap}"></label>
        <label>min_chunk_size<input type="number" id="sMin" value="${c.min_chunk_size}"></label>
        <label>estratégia<select id="sStrategy">${["recursive", "fixed", "markdown", "pages"].map((x) => `<option ${x === c.strategy ? "selected" : ""}>${x}</option>`).join("")}</select></label>
      </div>
      <p class="muted small">Você pode editar os valores acima antes de iniciar, ou
        <a href="#" onclick="setMode('manual');return false" style="color:var(--accent)">voltar ao modo manual</a>.</p>
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
  if (!colId) return toast("Selecione uma coleção", true);
  const { config, embedding_model, suggestion } = currentConfig();
  if (!embedding_model) return toast("Selecione um modelo de embedding (aba Configurações ou modo manual)", true);
  try {
    const r = await api("/api/ingest/start", {
      method: "POST",
      body: {
        collection_id: colId, config, embedding_model, mode: state.mode,
        suggestion, save_as_collection_default: $("saveAsDefault").checked,
      },
    });
    toast(`Ingestão iniciada: ${r.started} documento(s)`);
    state.suggestion = null; $("suggestionBox").innerHTML = "";
    await refreshStaged(); showTab("dashboard");
  } catch (e) { toast(e.message, true); }
}
async function pauseIngest() { try { await api("/api/ingest/pause", { method: "POST" }); toast("Ingestão pausada — progresso preservado"); } catch (e) { toast(e.message, true); } }
async function resumeIngest() { try { await api("/api/ingest/resume", { method: "POST" }); toast("Ingestão retomada"); } catch (e) { toast(e.message, true); } }

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
  } catch { /* servidor offline: health mostrará */ }
}
function renderIngest(s) {
  const pct = s.chunks_total > 0 ? Math.round((s.chunks_embedded / s.chunks_total) * 100) : (s.active_documents ? 0 : 100);
  $("ingestBar").style.width = pct + "%";
  $("btnPause").classList.toggle("hidden", s.paused);
  $("btnResume").classList.toggle("hidden", !s.paused);
  const summary = s.paused
    ? `⏸️ <b>Pausada</b> — progresso preservado; clique em Retomar para continuar do ponto em que parou.`
    : s.active_documents > 0
      ? `Processando <b>${s.active_documents}</b> documento(s) — <b>${s.chunks_embedded}</b> de <b>${s.chunks_total}</b> chunks embedados (${pct}%), restam <b>${s.chunks_remaining}</b>.`
      : `Nenhuma ingestão ativa. ${s.counts.ready || 0} documento(s) indexado(s).`;
  $("ingestSummary").innerHTML = summary;
  const order = ["staged", "pending", "extracting", "chunking", "embedding", "waiting", "ready", "error"];
  $("ingestCounts").innerHTML = order
    .filter((k) => s.counts[k])
    .map((k) => `<span class="chip">${badge(k)} <b>${s.counts[k]}</b></span>`).join("")
    || `<span class="muted small">Nenhum documento ainda — importe na aba Importar.</span>`;
  $("currentDoc").innerHTML = s.current_document
    ? `Agora: <b>${esc(s.current_document.filename)}</b> — ${badge(s.current_document.status)} ${s.current_document.chunks_embedded}/${s.current_document.chunks_total} chunks`
    : "";
  $("problems").innerHTML = (s.problems || []).map((p) => `
    <div class="problem ${p.status === "waiting" ? "waiting" : ""}">
      <b>${esc(p.filename)}</b> ${badge(p.status)}
      ${p.status === "error" ? `<button class="btn mini" onclick="retryDoc('${p.id}')">Tentar novamente</button>` : ""}
      <div class="err-msg">${esc(p.error || "")}</div>
    </div>`).join("");
}
async function pollHealth() {
  let h = null;
  try { h = await api("/api/health"); } catch { /* offline */ }
  const pill = (label, ok, warn = false) =>
    `<span class="pill"><span class="dot ${ok ? (warn ? "warn" : "ok") : "bad"}"></span>${label}</span>`;
  $("healthPills").innerHTML = h
    ? pill("DB", h.database) + pill("LM Studio", h.lmstudio?.reachable) + pill(h.worker?.paused ? "pausado" : "worker", h.worker?.running, h.worker?.paused)
    : pill("servidor offline", false);
  $("healthCards").innerHTML = h ? `
    <div class="card"><div class="stat-label">PostgreSQL + pgvector</div>
      <div class="stat" style="color:${h.database ? "var(--green)" : "var(--red)"}">${h.database ? "online" : "offline"}</div></div>
    <div class="card"><div class="stat-label">LM Studio</div>
      <div class="stat" style="color:${h.lmstudio?.reachable ? "var(--green)" : "var(--red)"}">${h.lmstudio?.reachable ? "online" : "offline"}</div>
      <div class="muted small">${esc(h.lmstudio?.url || "")}${h.lmstudio?.reachable && state.models.length ? ` · ${state.models.length} modelo(s) conhecidos` : ""}</div>
      ${h.lmstudio?.error ? `<div class="small" style="color:var(--yellow)">${esc(h.lmstudio.error)}</div>` : ""}</div>
    <div class="card"><div class="stat-label">Worker de ingestão</div>
      <div class="stat" style="color:${h.worker?.paused ? "var(--yellow)" : "var(--green)"}">${h.worker?.paused ? "pausado" : h.worker?.running ? "ativo" : "parado"}</div>
      <div class="muted small">retomada automática após reinícios</div></div>` : "";
}
async function pollEvents() {
  try {
    const data = await api("/api/events?limit=60");
    $("eventsLog").innerHTML = data.events.map((e) => `
      <div class="event ${esc(e.level)}"><span class="ts">${fmtTime(e.ts)}</span><span class="msg">${esc(e.message)}</span></div>`).join("")
      || `<p class="muted small">Sem eventos.</p>`;
  } catch { /* ignore */ }
}

/* ---------------- documents ---------------- */
let docsDebounce = null;
function debouncedDocs() { clearTimeout(docsDebounce); docsDebounce = setTimeout(loadDocuments, 350); }
function docsPage(dir) { state.docsOffset = Math.max(0, state.docsOffset + dir * state.docsLimit); loadDocuments(); }
async function loadDocuments() {
  const params = new URLSearchParams({ limit: state.docsLimit, offset: state.docsOffset });
  if ($("docFilterCollection").value) params.set("collection_id", $("docFilterCollection").value);
  if ($("docFilterStatus").value) params.set("status", $("docFilterStatus").value);
  if ($("docSearch").value) params.set("q", $("docSearch").value);
  try {
    const data = await api("/api/documents?" + params);
    $("docsTotal").textContent = `${data.total} documento(s) · página ${Math.floor(state.docsOffset / state.docsLimit) + 1}`;
    $("docsPrev").disabled = state.docsOffset === 0;
    $("docsNext").disabled = state.docsOffset + state.docsLimit >= data.total;
    $("docsTable").innerHTML = `<table><thead><tr>
      <th>Documento</th><th>Coleção</th><th>Status</th><th>Progresso</th><th>Tamanho</th><th>Atualizado</th><th></th>
    </tr></thead><tbody>` + data.items.map((d) => {
      const pct = d.chunks_total ? Math.round((d.chunks_embedded / d.chunks_total) * 100) : 0;
      return `<tr>
        <td><div class="docname">${esc(d.filename)}</div><div class="docpath">${esc(d.source_path)}</div></td>
        <td>${esc(d.collection)}</td>
        <td>${badge(d.status)}${d.error ? `<div class="docpath" style="color:var(--red);max-width:260px">${esc(d.error.slice(0, 120))}</div>` : ""}</td>
        <td>${d.chunks_total ? `<span class="mini-progress"><div style="width:${pct}%"></div></span> <span class="small muted">${d.chunks_embedded}/${d.chunks_total}</span>` : "—"}</td>
        <td class="small muted">${fmtBytes(d.size_bytes)}</td>
        <td class="small muted">${fmtDate(d.updated_at)}</td>
        <td><div class="row" style="margin:0;flex-wrap:nowrap">
          <button class="btn mini" onclick="docDetails('${d.id}')">Detalhes</button>
          ${["error", "waiting"].includes(d.status) ? `<button class="btn mini" onclick="retryDoc('${d.id}')">Retry</button>` : ""}
          ${["pending", "extracting", "chunking", "embedding", "waiting", "cancelling"].includes(d.status) ? `<button class="btn mini danger" onclick="cancelDoc(&quot;${d.id}&quot;)">Cancelar</button>` : ""}
          <button class="btn mini" onclick="openMoveDoc('${d.id}', '${esc(d.filename)}', '${d.collection_id}')">Mover</button>
          <button class="btn mini" onclick="reindexDoc('${d.id}', '${esc(d.filename)}')">Reindexar</button>
          <button class="btn mini danger" onclick="deleteDoc('${d.id}', '${esc(d.filename)}')">Excluir</button>
        </div></td>
      </tr>`;
    }).join("") + `</tbody></table>`;
  } catch (e) { toast(e.message, true); }
}
async function retryDoc(id) { try { await api(`/api/documents/${id}/retry`, { method: "POST" }); toast("Documento reenfileirado"); loadDocuments(); } catch (e) { toast(e.message, true); } }
async function cancelDoc(id) {
  if (!confirm("Cancelar o processamento deste documento? Chunks e vetores parciais serão removidos.")) return;
  try {
    const r = await api(`/api/documents/${id}/cancel`, { method: "POST" });
    toast(r.status === "cancelling" ? "Cancelamento solicitado; aguardando fronteira segura" : "Documento cancelado");
    loadDocuments();
    loadCollections();
  } catch (e) { toast(e.message, true); }
}

function openMoveDoc(id, name, currentCollectionId) {
  const options = state.collections
    .filter((c) => c.id !== currentCollectionId)
    .map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`)
    .join("");
  if (!options) { toast("Não há outra coleção disponível", true); return; }
  openModal(`Mover ${esc(name)}`, `
    <p class="small muted">Selecione a coleção destino. Documentos já indexados só podem ser movidos sem reindexação entre coleções com o mesmo modelo e dimensão de embedding.</p>
    <div class="row">
      <select id="moveCollectionSelect">${options}</select>
      <button class="btn primary" onclick="moveDoc('${id}')">Mover documento</button>
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
    toast(`Movido para ${r.to_collection}`);
    await loadCollections();
    await loadDocuments();
  } catch (e) { toast(e.message, true); }
}
async function reindexDoc(id, name) {
  if (!confirm(`Reindexar '${name}'? Os chunks/vetores atuais serão recriados.`)) return;
  try { await api(`/api/documents/${id}/reindex`, { method: "POST", body: {} }); toast("Reindexação iniciada"); loadDocuments(); }
  catch (e) { toast(e.message, true); }
}
async function deleteDoc(id, name) {
  if (!confirm(`Excluir '${name}' e seus vetores?`)) return;
  try { await api(`/api/documents/${id}`, { method: "DELETE" }); toast("Documento excluído"); loadDocuments(); loadCollections(); }
  catch (e) { toast(e.message, true); }
}
async function docDetails(id) {
  try {
    const d = await api(`/api/documents/${id}`);
    openModal(esc(d.filename), `
      <p class="small muted">${esc(d.source_path)} · coleção <b>${esc(d.collection)}</b> · ${fmtBytes(d.size_bytes)} · sha256 <code>${esc(d.content_sha256.slice(0, 16))}…</code></p>
      <p>${badge(d.status)} ${d.chunks_embedded}/${d.chunks_total} chunks · ${d.attempts} tentativa(s)</p>
      ${d.error ? `<div class="problem"><b>Erro:</b><div class="err-msg" style="max-height:none">${esc(d.error)}</div></div>` : ""}
      <h3 class="small">Parâmetros de ingestão</h3><pre>${esc(JSON.stringify(d.ingest_config || {}, null, 2))}</pre>
      ${d.auto_suggestion ? `<h3 class="small">Sugestão automática aceita</h3><pre>${esc(JSON.stringify(d.auto_suggestion, null, 2))}</pre>` : ""}
      <h3 class="small">Metadados</h3><pre>${esc(JSON.stringify(d.meta || {}, null, 2))}</pre>
      <h3 class="small">Primeiros chunks</h3>
      ${(d.chunk_preview || []).map((c) => `<div class="result-card"><div class="result-loc">#${c.seq} · ${esc(JSON.stringify(c.loc))} · ${c.embedded ? "✓ embedado" : "pendente"}</div><div class="result-content">${esc(c.preview)}…</div></div>`).join("") || `<p class="muted small">Sem chunks ainda.</p>`}`);
  } catch (e) { toast(e.message, true); }
}
function openModal(title, html) { $("modalTitle").innerHTML = title; $("modalBody").innerHTML = html; $("modal").classList.remove("hidden"); }
function closeModal() { $("modal").classList.add("hidden"); }

/* ---------------- search ---------------- */
function renderSearchCollections() {
  $("searchCollections").innerHTML = state.collections.map((c) =>
    `<label class="check chip"><input type="checkbox" class="searchCol" value="${esc(c.name)}"> ${esc(c.name)}</label>`).join("")
    || `<span class="muted small">Nenhuma coleção.</span>`;
}
async function doSearch() {
  const query = $("searchQuery").value.trim();
  if (!query) return;
  const cols = [...document.querySelectorAll(".searchCol:checked")].map((c) => c.value);
  $("searchResults").innerHTML = `<p class="muted"><span class="spinner"></span> Buscando…</p>`;
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
      $("searchResults").innerHTML = `<div class="card"><p class="muted">${esc(r.message || "Nenhum resultado.")}</p></div>`;
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
      `<span><b>${s.zero_results || 0}</b> sem resultado</span>`,
      `<span><b>${s.errors || 0}</b> erros</span>`,
      `<span>média <b>${s.avg_total_ms ?? "—"} ms</b></span>`,
      `<span>p95 <b>${s.p95_total_ms ?? "—"} ms</b></span>`,
      `<span>embedding <b>${s.avg_embedding_ms ?? "—"} ms</b></span>`,
      `<span>DB <b>${s.avg_db_ms ?? "—"} ms</b></span>`,
      `<span>top score médio <b>${s.avg_top_score ?? "—"}</b></span>`,
    ];
    let html = `<div class="row wrap">${parts.join("")}</div>`;
    if (recent.length) {
      html += `<div class="upload-list">` + recent.map((x) => {
        const filters = Object.keys(x.filters || {}).length ? ` · <code>${esc(JSON.stringify(x.filters))}</code>` : "";
        const score = x.top_score != null ? ` · ${Number(x.top_score).toFixed(3)}` : "";
        return `<div class="upload-item"><span>${esc(x.query_text)}${filters}</span><span class="muted">${x.result_count} result. · ${Math.round(x.total_ms)} ms${score}</span></div>`;
      }).join("") + `</div>`;
    } else {
      html += `<p class="muted">Nenhuma busca registrada.</p>`;
    }
    $("searchMetrics").innerHTML = html;
  } catch (e) {
    $("searchMetrics").textContent = "Falha ao carregar métricas: " + e.message;
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
  $("mcpInfo").innerHTML = `<p>Endpoint MCP para clientes (Claude Code etc.):</p>
    <pre>claude mcp add --transport http rag ${location.origin}/mcp</pre>
    <p class="muted small">Ferramentas expostas: rag_search (com filtros), rag_search_metrics, list_collections, get_document.</p>`;
  await loadModels(false);
}
async function loadModels(verbose) {
  $("modelsInfo").textContent = "consultando LM Studio…";
  try {
    const data = await api("/api/lmstudio/models");
    state.models = data.models;
    const emb = state.settings.embedding_model || "";
    const llm = state.settings.llm_model || "";
    const reranker = state.settings.reranker_model || state.effective?.rerank?.model || "";
    const opts = (filter, sel) => state.models.filter(filter)
      .map((m) => `<option value="${esc(m.id)}" ${m.id === sel ? "selected" : ""}>${esc(m.id)}${m.type === "unknown" ? " (tipo desconhecido)" : ""}${m.state === "loaded" ? " ●" : ""}</option>`).join("");
    $("rerankerModel").innerHTML = `<option value="">— selecione —</option>` + opts((m) => m.type !== "embedding", reranker);
    $("embeddingModel").innerHTML = `<option value="">— selecione —</option>` + opts((m) => m.type !== "llm", emb);
    $("llmModel").innerHTML = `<option value="">— selecione —</option>` + opts((m) => m.type !== "embedding", llm);
    $("autoModel").innerHTML = opts((m) => m.type !== "embedding", llm) || `<option value="">nenhum modelo</option>`;
    $("modelsInfo").textContent = `${state.models.length} modelo(s) em ${data.url}`;
    buildManualForm();
    if (verbose) toast("Lista de modelos atualizada");
  } catch (e) {
    $("modelsInfo").textContent = "";
    if (verbose) toast(e.message, true);
    else $("modelsInfo").textContent = "LM Studio indisponível — configure a URL e teste";
  }
}
async function testLM() {
  $("lmTestResult").innerHTML = `<span class="spinner"></span> testando…`;
  try {
    const r = await api("/api/lmstudio/test", { method: "POST", body: { url: $("lmUrl").value.trim() || null } });
    $("lmTestResult").innerHTML = r.reachable
      ? `<span style="color:var(--green)">✓ conectado — ${r.models} modelo(s) disponíveis</span>`
      : `<span style="color:var(--red)">✗ ${esc(r.error || "falha")}</span>`;
  } catch (e) { $("lmTestResult").innerHTML = `<span style="color:var(--red)">✗ ${esc(e.message)}</span>`; }
}
async function saveLM() {
  try {
    await api("/api/settings", { method: "PUT", body: { lmstudio_url: $("lmUrl").value.trim() } });
    toast("URL do LM Studio salva");
    await loadSettings(); await loadModels(true);
  } catch (e) { toast(e.message, true); }
}
async function saveModels() {
  try {
    await api("/api/settings", { method: "PUT", body: { embedding_model: $("embeddingModel").value || null, llm_model: $("llmModel").value || null, reranker_model: $("rerankerModel").value || null, overrides: { rerank: { mode: $("keepRerankerLoaded").checked ? "resident" : "switch" } } } });
    await loadSettings(); buildManualForm();
    toast("Modelos salvos");
  } catch (e) { toast(e.message, true); }
}
async function saveOverrides() {
  let parsed;
  try { parsed = JSON.parse($("overridesJson").value || "{}"); }
  catch (e) { $("overridesMsg").innerHTML = `<span style="color:var(--red)">JSON inválido: ${esc(e.message)}</span>`; return; }
  try {
    await api("/api/settings", { method: "PUT", body: { overrides: parsed } });
    await loadSettings();
    $("overridesMsg").innerHTML = `<span style="color:var(--green)">✓ salvo</span>`;
    buildManualForm();
  } catch (e) { $("overridesMsg").innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`; }
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
    // O backend pode ficar indisponível por alguns instantes durante o reload.
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
    setInterval(pollStatus, 1500);
    setInterval(pollHealth, 60000);
    setInterval(() => loadModels(false), 10 * 60 * 1000);
  } catch (e) {
    if (!token()) $("tokenBanner").classList.remove("hidden");
    console.error(e);
  }
})();
