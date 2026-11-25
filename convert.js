// ── ChatGPT Conversation Exporter ────────────────────────────────────
// The Open WebUI bookmark runs this on chatgpt.com and sends the ZIP to the import tab.
// ─────────────────────────────────────────────────────────────────────

(async (importPage) => {
  if (location.origin !== "https://chatgpt.com") {
    alert("Нажми закладку во вкладке chatgpt.com.");
    return;
  }
  if (!importPage) return;
  const previous = document.getElementById("chatgpt-exporter-overlay");
  if (previous?.dataset.exporting === "true") return;
  if (previous) previous.remove();
  const API = "/backend-api";
  const PAGE_SIZE = 100;
  const DELAY = 500;
  const HEADERS = {
    "Content-Type": "application/json",
    Accept: "application/json",
    "Oai-Device-Id": crypto.randomUUID(),
    "Oai-Language": "en-US",
  };

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // ── UI overlay ──────────────────────────────────────────────────────

  const overlay = document.createElement("div");
  overlay.id = "chatgpt-exporter-overlay";
  overlay.dataset.exporting = "true";
  overlay.innerHTML = `
    <div style="position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:99999;
      display:flex;align-items:center;justify-content:center;font-family:-apple-system,BlinkMacSystemFont,sans-serif">
      <div style="background:#1e293b;border-radius:16px;padding:40px;max-width:500px;width:90%;color:#e2e8f0;box-shadow:0 25px 50px rgba(0,0,0,0.4)">
        <h2 style="margin:0 0 8px;font-size:20px;color:#f8fafc">ChatGPT Exporter</h2>
        <p id="cge-status" style="color:#94a3b8;font-size:14px;margin:0 0 20px">Starting...</p>
        <div style="width:100%;height:8px;background:#334155;border-radius:4px;overflow:hidden;margin-bottom:8px">
          <div id="cge-bar" style="height:100%;background:#3b82f6;border-radius:4px;transition:width 0.3s;width:0%"></div>
        </div>
        <p id="cge-detail" style="color:#64748b;font-size:13px;margin:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"></p>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const ui = {
    status: overlay.querySelector("#cge-status"),
    bar: overlay.querySelector("#cge-bar"),
    detail: overlay.querySelector("#cge-detail"),
    set(status, pct, detail) {
      if (status) this.status.textContent = status;
      if (pct != null) this.bar.style.width = pct + "%";
      if (detail) this.detail.textContent = detail;
    },
    done(msg) {
      overlay.dataset.exporting = "false";
      this.status.textContent = msg;
      this.bar.style.width = "100%";
      this.bar.style.background = "#22c55e";
      this.detail.textContent = "You can close this overlay by clicking anywhere.";
      overlay.querySelector("div").style.cursor = "pointer";
      overlay.addEventListener("click", () => overlay.remove());
    },
    error(msg) {
      overlay.dataset.exporting = "false";
      this.status.textContent = msg;
      this.bar.style.background = "#ef4444";
      this.detail.textContent = "Click anywhere to close.";
      overlay.addEventListener("click", () => overlay.remove());
    },
  };

  // ── Get token ───────────────────────────────────────────────────────

  ui.set("Getting session token...");
  let token;
  let accountId;
  try {
    const session = await fetch("/api/auth/session").then((r) => r.json());
    token = session.accessToken;
    accountId = session.account?.id;
    if (!token) throw new Error("No accessToken in session");
  } catch {
    ui.error("Failed to get session token. Are you logged in?");
    return;
  }

  // ── API helper ──────────────────────────────────────────────────────

  async function apiGet(path, options = {}) {
    const resp = await fetch(`${API}/${path}`, {
      ...options,
      headers: { ...HEADERS, Authorization: `Bearer ${token}`, ...options.headers },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  const personalizationHeaders = accountId ? { "ChatGPT-Account-ID": accountId } : {};

  async function readMemorySummary() {
    const response = await fetch(`${API}/memories/about_you/summary/stream?source=personalization-setting`, {
      method: "POST",
      headers: { ...HEADERS, ...personalizationHeaders, Authorization: `Bearer ${token}`, Accept: "text/event-stream" },
      body: "{}",
      signal: AbortSignal.timeout(120000),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    const sections = new Map();
    let order = [];
    for (const record of (await response.text()).split(/\r?\n\r?\n/)) {
      const lines = record.split(/\r?\n/);
      const event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
      if (event !== "section_types" && event !== "section") continue;
      const data = JSON.parse(lines.filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).replace(/^ /, "")).join("\n"));
      if (event === "section_types") {
        order = Array.isArray(data.sections) ? data.sections.map((section) => section.id) : [];
        continue;
      }
      const section = data.section;
      if (!section?.id || typeof section.description !== "string" || !section.description.trim()) continue;
      const description = section.description.trim();
      const title = typeof section.title === "string" ? section.title.trim() : "";
      sections.set(section.id, title ? `## ${title}\n\n${description}` : description);
    }
    const summary = [...new Set([...order, ...sections.keys()])]
      .map((id) => sections.get(id)).filter(Boolean);
    return summary.length ? summary : null;
  }

  const personalization = Promise.all([
    readMemorySummary().catch(() => null),
    apiGet("user_system_messages", { headers: personalizationHeaders, signal: AbortSignal.timeout(30000) }).then((data) => {
      if (data.enabled === false) return "";
      return typeof data.about_model_message === "string" ? data.about_model_message : null;
    }).catch(() => null),
  ]);

  const MIME_TO_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/svg+xml": ".svg", "application/pdf": ".pdf",
    "text/plain": ".txt", "text/html": ".html", "text/csv": ".csv",
    "application/json": ".json", "application/zip": ".zip",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
  };

  // ── File references ─────────────────────────────────────────────────

  function extractFileReferences(convo) {
    const references = new Map();
    const add = (id, name) => {
      if (id && !references.has(id)) references.set(id, name);
    };

    for (const { message } of Object.values(convo.mapping || {})) {
      for (const part of message?.content?.parts || []) {
        if (part?.content_type !== "image_asset_pointer") continue;
        const match = part.asset_pointer?.match(/^(?:file-service|sediment):\/\/(.+)$/);
        add(match?.[1], part.metadata?.dalle?.prompt ? "dalle_image.png" : "image.png");
      }
      for (const attachment of message?.metadata?.attachments || []) {
        add(attachment.id, attachment.name || "attachment");
      }
      for (const citation of message?.metadata?.citations || []) {
        add(citation.metadata?.file_id || citation.file_id, citation.metadata?.title || citation.title || "citation");
      }
    }
    return references;
  }

  async function downloadFile(fileId, fallbackName) {
    const meta = await apiGet(`files/download/${fileId}`);
    if (!meta.download_url) throw new Error("No download_url");
    const response = await fetch(meta.download_url);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const contentType = response.headers.get("content-type") || "";
    let filename = meta.file_name || fallbackName || fileId;
    // Add extension from content-type if missing
    if (!filename.includes(".") && contentType) {
      const mime = contentType.split(";")[0].trim();
      const ext = MIME_TO_EXT[mime];
      if (ext) filename += ext;
    }
    return { filename, data: new Uint8Array(await response.arrayBuffer()) };
  }

  function deduplicateFilename(name, usedNames) {
    if (!usedNames.has(name)) { usedNames.add(name); return name; }
    const dot = name.lastIndexOf(".");
    const base = dot > 0 ? name.slice(0, dot) : name;
    const ext = dot > 0 ? name.slice(dot) : "";
    let i = 1;
    while (usedNames.has(`${base}_${i}${ext}`)) i++;
    const deduped = `${base}_${i}${ext}`;
    usedNames.add(deduped);
    return deduped;
  }

  function sanitize(name) {
    return name.replace(/[<>:"/\\|?*]/g, "_").replace(/^[. ]+|[. ]+$/g, "").slice(0, 80) || "untitled";
  }

  // ── Fetch conversation list ─────────────────────────────────────────

  ui.set("Fetching conversation list...");
  const conversations = [];
  let offset = 0;
  try {
    while (true) {
      const data = await apiGet(`conversations?offset=${offset}&limit=${PAGE_SIZE}`);
      const items = data.items || [];
      if (!items.length) break;
      conversations.push(...items);
      const total = data.total || conversations.length;
      ui.set(`Fetching conversation list... ${conversations.length}/${total}`);
      offset += PAGE_SIZE;
      if (offset >= total) break;
      await sleep(DELAY);
    }
  } catch (e) {
    ui.error(`Failed to fetch conversations: ${e.message}`);
    return;
  }

  if (!conversations.length) {
    ui.done("No conversations found.");
    return;
  }

  ui.set(`Found ${conversations.length} conversations. Downloading...`);

  // ── Download conversations and files ────────────────────────────────

  const zipEntries = [];
  let totalFiles = 0;
  let failedFiles = 0;
  const total = conversations.length;
  let downloaded = 0;

  for (let i = 0; i < total; i++) {
    const { id: cid, title: rawTitle } = conversations[i];
    const title = rawTitle || "Untitled";
    const fname = `${sanitize(title)}_${cid.slice(0, 8)}`;
    const pct = Math.round(((i + 1) / total) * 100);

    ui.set(`Downloading ${i + 1} of ${total} (${pct}%)`, pct, title);

    try {
      const convo = await apiGet(`conversation/${cid}`);
      const jsonStr = JSON.stringify(convo);

      const usedNames = new Set();
      for (const [fileId, fallbackName] of extractFileReferences(convo)) {
        totalFiles++;
        try {
          const { filename, data } = await downloadFile(fileId, fallbackName);
          const actualName = deduplicateFilename(filename, usedNames);
          zipEntries.push({ path: `files/${fname}/${actualName}`, data });
          await sleep(DELAY);
        } catch {
          failedFiles++;
        }
      }

      zipEntries.push({ path: `json/${fname}.json`, data: jsonStr });
      downloaded++;
    } catch {
      // Continue with the remaining conversations.
    }

    await sleep(DELAY);
  }

  if (!downloaded) {
    ui.error("Не удалось выгрузить ни одной беседы.");
    return;
  }
  const [summary, preferences] = await personalization;
  const memory = { memory_summary: summary, user_preferences: preferences };

  const failed = total - downloaded;
  let doneMsg = `ZIP передан на страницу импорта. Бесед ${downloaded}/${total}.`;
  if (failed) doneMsg += ` (${failed} failed)`;
  if (totalFiles) doneMsg += ` ${totalFiles - failedFiles}/${totalFiles} files downloaded.`;
  ui.set(`Готово к импорту. Бесед ${downloaded}/${total}.`, 100);
  const openImport = document.createElement("button");
  openImport.type = "button";
  openImport.textContent = "Открыть вкладку импорта";
  openImport.style.cssText = "display:block;margin-top:16px;padding:9px 14px;border:1px solid #60a5fa;" +
    "border-radius:8px;background:#1d4ed8;color:#fff;font:inherit;cursor:pointer";
  ui.detail.after(openImport);
  openImport.addEventListener("click", async () => {
    const transfer = openImportTab(importPage);
    if (!transfer) {
      ui.set("Браузер заблокировал открытие вкладки. Разреши его и нажми кнопку снова.");
      return;
    }
    openImport.disabled = true;
    ui.set("Передаю ZIP во вкладку импорта…");
    try {
      const entries = [...zipEntries, {path: "personalization.json", data: JSON.stringify(memory)}];
      const zipBlob = buildZipBlob(entries);
      await transfer.send(zipBlob);
      openImport.remove();
      ui.done(doneMsg);
    } catch {
      transfer.cancel();
      ui.set("Вкладка импорта не приняла ZIP. Можно повторить передачу.");
      openImport.disabled = false;
    }
  });

  // ── Transfer to the import tab ──────────────────────────────────────

  function openImportTab(importPage) {
    const target = new URL(importPage);
    const transferId = crypto.randomUUID();
    target.hash = new URLSearchParams({ transfer: transferId }).toString();
    let tab, markReady, markReceived;
    const ready = new Promise((resolve) => { markReady = resolve; });
    const received = new Promise((resolve) => { markReceived = resolve; });
    const listener = (event) => {
      if (event.source !== tab || event.origin !== target.origin || event.data?.transfer !== transferId) return;
      if (event.data.type === "chatgpt-export/ready") markReady();
      if (event.data.type === "chatgpt-export/received") markReceived(event.data.accepted === true);
    };
    window.addEventListener("message", listener);
    const cleanup = () => window.removeEventListener("message", listener);
    tab = window.open(target.href, "_blank");
    if (!tab || tab.closed) {
      cleanup();
      return null;
    }
    const wait = async (promise) => {
      let timer;
      try {
        return await Promise.race([promise, new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error("Import tab did not respond")), 15_000);
        })]);
      } finally {
        clearTimeout(timer);
      }
    };
    return {
      async send(archive) {
        try {
          if (tab.closed) throw new Error("Import tab was closed");
          await wait(ready);
          tab.postMessage({ type: "chatgpt-export/archive", transfer: transferId, format: 2, archive }, target.origin);
          if (!await wait(received)) throw new Error("Import tab rejected the archive");
        } finally {
          cleanup();
        }
      },
      cancel() {
        try {
          if (!tab.closed) tab.postMessage({ type: "chatgpt-export/cancel", transfer: transferId }, target.origin);
        } finally {
          cleanup();
        }
      },
    };
  }

  // ── ZIP serialization ───────────────────────────────────────────────

  function buildZipBlob(entries) {
    if (entries.length > 0xffff) throw new Error("Too many ZIP entries");
    const te = new TextEncoder();
    const parts = [];
    const cdParts = [];
    let offset = 0;

    for (const entry of entries) {
      const pathBytes = te.encode(entry.path);
      const data = typeof entry.data === "string" ? te.encode(entry.data) : entry.data;
      const length = data.length;
      if (pathBytes.length > 0xffff || offset + 30 + pathBytes.length + length > 0xffffffff) {
        throw new Error("ZIP size limit exceeded");
      }
      const crc = crc32(data);

      // Local file header (30 bytes)
      const lh = new DataView(new ArrayBuffer(30));
      lh.setUint32(0, 0x04034b50, true);
      lh.setUint16(4, 20, true);
      lh.setUint16(6, 0x0800, true); // UTF-8 filename
      lh.setUint16(8, 0, true); // store
      lh.setUint32(14, crc, true);
      lh.setUint32(18, length, true);
      lh.setUint32(22, length, true);
      lh.setUint16(26, pathBytes.length, true);

      parts.push(new Uint8Array(lh.buffer), pathBytes, data);

      // Central directory entry (46 bytes)
      const cd = new DataView(new ArrayBuffer(46));
      cd.setUint32(0, 0x02014b50, true);
      cd.setUint16(4, 20, true);
      cd.setUint16(6, 20, true);
      cd.setUint16(8, 0x0800, true); // UTF-8 filename
      cd.setUint16(10, 0, true); // store
      cd.setUint32(16, crc, true);
      cd.setUint32(20, length, true);
      cd.setUint32(24, length, true);
      cd.setUint16(28, pathBytes.length, true);
      cd.setUint32(42, offset, true);

      cdParts.push(new Uint8Array(cd.buffer), pathBytes);

      offset += 30 + pathBytes.length + length;
    }

    const cdSize = cdParts.reduce((s, p) => s + p.length, 0);
    if (offset + cdSize + 22 > 0xffffffff) throw new Error("ZIP size limit exceeded");

    // End of central directory (22 bytes)
    const eocd = new DataView(new ArrayBuffer(22));
    eocd.setUint32(0, 0x06054b50, true);
    eocd.setUint16(8, entries.length, true);
    eocd.setUint16(10, entries.length, true);
    eocd.setUint32(12, cdSize, true);
    eocd.setUint32(16, offset, true);

    return new Blob([...parts, ...cdParts, new Uint8Array(eocd.buffer)], {
      type: "application/zip",
    });
  }

  function crc32(bytes) {
    const table = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      let c = i;
      for (let j = 0; j < 8; j++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      table[i] = c;
    }
    let crc = 0xffffffff;
    for (let i = 0; i < bytes.length; i++) crc = table[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
    return (crc ^ 0xffffffff) >>> 0;
  }
})(/* OPEN_WEBUI_IMPORT_PAGE */ null);
