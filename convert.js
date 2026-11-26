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
  const events = [];
  function failureReason(error) {
    if (/^HTTP [1-5]\d\d$/.test(error.message)) return error.message;
    if (error.name === 'TimeoutError') return 'Истекло время ожидания ответа ChatGPT';
    if (error.message === 'No download_url') return 'ChatGPT не дал ссылку на скачивание';
    if (error instanceof TypeError) return 'Сетевой сбой при получении данных';
    if (error instanceof SyntaxError) return 'Некорректный ответ ChatGPT';
    return 'Не удалось обработать полученные данные';
  }

  const reportMemoryFailure = (section, error) => {
    events.push({ stage: 'Выгрузка памяти', section, reason: failureReason(error) });
    return null;
  };

  // ── UI overlay ──────────────────────────────────────────────────────

  const overlay = document.createElement("div");
  overlay.id = "chatgpt-exporter-overlay";
  overlay.dataset.exporting = "true";
  overlay.innerHTML = `
    <div style="position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:99999;
      display:flex;align-items:center;justify-content:center;font-family:-apple-system,BlinkMacSystemFont,sans-serif">
      <div style="background:#1e293b;border-radius:16px;padding:40px;max-width:500px;width:90%;color:#e2e8f0;box-shadow:0 25px 50px rgba(0,0,0,0.4)">
        <h2 style="margin:0 0 8px;font-size:20px;color:#f8fafc">ChatGPT Exporter</h2>
        <p id="cge-status" role="status" style="color:#94a3b8;font-size:14px;margin:0 0 20px"></p>
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
    timer: null,
    set(status, pct, detail = "") {
      clearInterval(this.timer);
      this.status.textContent = status;
      if (pct != null) this.bar.style.width = pct + "%";
      this.detail.textContent = detail;
    },
    wait(status, pct, detail) {
      this.set(status, pct, detail);
      const started = performance.now();
      this.timer = setInterval(() => {
        const seconds = Math.floor((performance.now() - started) / 1000);
        this.status.textContent = `${status} · ${seconds} с на этом этапе`;
      }, 1000);
    },
    done(msg) {
      overlay.dataset.exporting = "false";
      this.set(msg, 100, "Нажмите в любом месте, чтобы закрыть окно.");
      this.bar.style.background = "#22c55e";
      overlay.querySelector("div").style.cursor = "pointer";
      overlay.addEventListener("click", () => overlay.remove());
    },
    error(msg) {
      overlay.dataset.exporting = "false";
      this.set(msg, null, "Нажмите в любом месте, чтобы закрыть окно.");
      this.bar.style.background = "#ef4444";
      overlay.addEventListener("click", () => overlay.remove());
    },
  };

  // ── Get token ───────────────────────────────────────────────────────

  ui.wait("Проверяю сессию ChatGPT");
  let token;
  let accountId;
  try {
    const session = await fetch("/api/auth/session").then((r) => r.json());
    token = session.accessToken;
    accountId = session.account?.id;
    if (!token) throw new Error("No accessToken in session");
  } catch (error) {
    events.push({ stage: 'Получение сессии ChatGPT', reason: token ? failureReason(error) : 'Сессия ChatGPT недоступна' });
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

  const personalization = token ? Promise.all([
    readMemorySummary().then((summary) => {
      if (summary === null) events.push({ stage: 'Выгрузка памяти', section: 'Сводка памяти', reason: 'ChatGPT вернул пустую сводку' });
      return summary;
    }).catch((error) => reportMemoryFailure('Сводка памяти', error)),
    apiGet("user_system_messages", { headers: personalizationHeaders, signal: AbortSignal.timeout(30000) }).then((data) => {
      if (data.enabled === false) {
        events.push({ stage: 'Выгрузка памяти', section: 'Пользовательские инструкции', reason: 'Инструкции отключены в ChatGPT' });
        return '';
      }
      if (typeof data.about_model_message !== 'string' || !data.about_model_message.trim()) {
        events.push({ stage: 'Выгрузка памяти', section: 'Пользовательские инструкции', reason: 'ChatGPT не вернул текст инструкций' });
      }
      return typeof data.about_model_message === 'string' ? data.about_model_message : null;
    }).catch((error) => reportMemoryFailure('Пользовательские инструкции', error)),
  ]) : Promise.resolve([null, null]);

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

  async function downloadFile(fileId, fallbackName, conversationId, projectId, updateStage) {
    updateStage('Запрос ссылки на вложение');
    // ChatGPT's download route uses asterisks as separators in compound file IDs.
    const route = `files/download/${encodeURIComponent(fileId.replaceAll('#', '*'))}`;
    const compound = fileId.includes('#');
    const query = new URLSearchParams({ conversation_id: conversationId });
    if (projectId) query.set('gizmo_id', projectId);
    const meta = compound ? await apiGet(`${route}?${query}`, { headers: personalizationHeaders }) : await apiGet(route);
    if (!meta.download_url) throw new Error("No download_url");
    updateStage('Скачивание вложения');
    const response = await fetch(meta.download_url);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const contentType = response.headers.get("content-type") || "";
    const partName = compound ? fileId.slice(fileId.lastIndexOf('#') + 1) : '';
    let filename = /\.[a-z0-9]{1,10}$/i.test(partName) ? partName : meta.file_name || fallbackName || fileId;
    // Add extension from content-type if missing
    if (!filename.includes(".") && contentType) {
      const mime = contentType.split(";")[0].trim();
      const ext = MIME_TO_EXT[mime];
      if (ext) filename += ext;
    }
    return { filename, data: new Uint8Array(await response.arrayBuffer()) };
  }

  async function retryRead(read) {
    try {
      return await read();
    } catch (error) {
      if (!(error instanceof TypeError) && !/^HTTP (429|5\d\d)$/.test(error.message)) throw error;
      await sleep(2000);
      return read();
    }
  }

  function attachmentPath(name, fileId, usedNames) {
    let safeName = safePathComponent(name) || "attachment";
    if (isReservedName(safeName)) safeName = `_${safeName}`;
    const key = Array.from(safeName.normalize("NFC"), (char) => char.toUpperCase().toLowerCase())
      .join("").replace(/ß/g, "ss");
    if (!fileId.includes('#') && safeName === name && !usedNames.has(key)) {
      usedNames.add(key);
      return safeName;
    }
    // Preserve the source ID for compound files and files with renamed ZIP entries.
    const encodedId = Array.from(new TextEncoder().encode(fileId), (byte) => byte.toString(16).padStart(2, "0")).join("");
    return `id-${encodedId}/${safeName}`;
  }

  function safePathComponent(value) {
    return value.replace(/[<>:"/\\|?*\x00-\x1f]/g, "_").replace(/[. ]+$/g, "");
  }

  function isReservedName(name) {
    return /^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i.test(name.split(".", 1)[0]);
  }

  // ── Fetch conversation list ─────────────────────────────────────────

  const conversations = new Map();
  const projects = new Map();
  const memberships = new Map();
  let offset = 0;
  try {
    while (token) {
      ui.wait(`Запрашиваю список чатов. Получено: ${conversations.size}`);
      const data = await apiGet(`conversations?offset=${offset}&limit=${PAGE_SIZE}`);
      const items = data.items || [];
      if (!items.length) break;
      for (const item of items) if (item?.id) conversations.set(item.id, item);
      const total = data.total || conversations.size;
      offset += PAGE_SIZE;
      if (offset >= total) break;
      await sleep(DELAY);
    }
  } catch (e) {
    events.push({ stage: 'Получение списка чатов', reason: failureReason(e) });
  }

  async function readProjectPages(path, params, accept) {
    const query = new URLSearchParams(params);
    const seen = new Set();
    do {
      const page = await retryRead(() => apiGet(`${path}?${query}`, {
        headers: personalizationHeaders, signal: AbortSignal.timeout(30000),
      }));
      if (!Array.isArray(page.items)) throw new SyntaxError('Invalid project response');
      for (const item of page.items) accept(item);
      if (page.cursor == null || page.cursor === '') return;
      const cursor = String(page.cursor);
      if (seen.has(cursor)) throw new Error('Repeated project cursor');
      seen.add(cursor);
      query.set('cursor', cursor);
      await sleep(DELAY);
    } while (true);
  }

  if (token) {
    ui.wait('Получаю список проектов ChatGPT');
    try {
      await readProjectPages('gizmos/snorlax/sidebar', { conversations_per_gizmo: '0', limit: '20', owned_only: 'true' }, (item) => {
        const project = item?.gizmo?.gizmo;
        if (typeof project?.id === 'string' && project.id) {
          projects.set(project.id, { id: project.id, title: project.display?.name || 'Проект ChatGPT' });
        }
      });
    } catch (error) {
      events.push({ stage: 'Получение списка проектов', reason: failureReason(error) });
    }
    let projectNumber = 0;
    for (const project of projects.values()) {
      ui.wait(`Получаю чаты проекта ${++projectNumber} из ${projects.size}`, null, project.title);
      try {
        await readProjectPages(`gizmos/${encodeURIComponent(project.id)}/conversations`, { limit: '50', owned_only: 'true' }, (item) => {
          if (typeof item?.id !== 'string' || !item.id) return;
          if (!conversations.has(item.id)) conversations.set(item.id, item);
          if (!memberships.has(item.id)) memberships.set(item.id, new Set());
          memberships.get(item.id).add(project.id);
        });
      } catch (error) {
        events.push({ stage: 'Получение чатов проекта', project: project.id, reason: failureReason(error) });
      }
    }
  }

  if (!conversations.size && !projects.size && !events.length) {
    events.push({ stage: 'Получение списка чатов', reason: 'ChatGPT не вернул ни одного чата' });
  }

  // ── Download conversations and files ────────────────────────────────

  const chatEntries = [];
  const failures = { chats: {}, files: {} };
  function recordFailure(group, error, context) {
    const reason = failureReason(error);
    group[reason] = (group[reason] || 0) + 1;
    events.push({ ...context, reason });
  }
  let totalFiles = 0;
  let failedFiles = 0;
  const total = conversations.size;
  let downloaded = 0;
  let i = 0;
  const assignedProjects = new Set();

  for (const { id: cid, title: rawTitle } of conversations.values()) {
    const title = rawTitle || "Untitled";
    const chatName = safePathComponent(title).replace(/^[. ]+/, "").slice(0, 80) || "untitled";
    const stem = `${chatName}_${cid.slice(0, 8)}`;
    const fname = isReservedName(stem) ? `_${stem}` : stem;
    const pct = Math.round((i / total) * 100);
    const chat = `Чат ${++i} из ${total}`;

    ui.wait(`${chat}. Получаю сообщения из ChatGPT`, pct, title);

    try {
      const convo = await retryRead(() => apiGet(`conversation/${cid}`));
      const entries = [{ path: `json/${fname}.json`, data: JSON.stringify(convo) }];
      const projectIds = memberships.get(cid) || new Set();
      const declaredProject = convo.project_id || convo.gizmo_id;
      if (projects.has(declaredProject)) projectIds.add(declaredProject);
      const projectId = declaredProject || (projectIds.size === 1 ? projectIds.values().next().value : undefined);
      if (projectIds.size) {
        const records = [...projectIds].map((id) => ({ ...projects.get(id), conversation_ids: [convo.conversation_id || convo.id || cid] }));
        entries.push({ path: `projects.${i}.json`, data: JSON.stringify(records) });
      }

      const usedNames = new Set();
      const files = extractFileReferences(convo);
      let fileNumber = 0;
      for (const [fileId, fallbackName] of files) {
        ui.wait(`${chat}. Скачиваю вложение ${++fileNumber} из ${files.size}`, pct, `${title} · ${fallbackName}`);
        totalFiles++;
        let stage;
        try {
          const { filename, data } = await retryRead(() => downloadFile(fileId, fallbackName, cid, projectId, (value) => { stage = value; }));
          stage = 'Упаковка вложения';
          const actualName = attachmentPath(filename, fileId, usedNames);
          entries.push({ path: `files/${fname}/${actualName}`, data });
          await sleep(DELAY);
        } catch (error) {
          failedFiles++;
          recordFailure(failures.files, error, { stage, chat: cid, file: fileId });
        }
      }

      chatEntries.push(entries);
      for (const id of projectIds) assignedProjects.add(id);
      downloaded++;
    } catch (error) {
      recordFailure(failures.chats, error, { stage: 'Выгрузка чата', chat: cid });
    }

    await sleep(DELAY);
  }

  ui.wait("Получаю сводку памяти и пользовательские инструкции", 100);
  const [summary, preferences] = await personalization;
  const memory = { memory_summary: summary, user_preferences: preferences };

  const failed = total - downloaded;
  let doneMsg = `Данные переданы на страницу импорта. Бесед ${downloaded}/${total}.`;
  if (failed) doneMsg += ` Не выгружено чатов: ${failed}.`;
  if (totalFiles) doneMsg += ` Вложений: ${totalFiles - failedFiles}/${totalFiles}.`;
  ui.set(downloaded || projects.size ? `Готово к импорту. Бесед ${downloaded}/${total}. Проектов: ${projects.size}.`
    : 'Чаты не выгружены. Откройте вкладку импорта, чтобы сохранить журнал.', 100);
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
    ui.wait("Ожидаю готовности вкладки импорта");
    try {
      const otherProjects = [...projects.values()].filter(({ id }) => !assignedProjects.has(id));
      await transfer.send((limit) => chatEntries.length || otherProjects.length
        ? buildArchiveBatches(chatEntries, memory, limit, otherProjects) : [], {
        chats: total, exportedChats: downloaded, files: totalFiles, downloadedFiles: totalFiles - failedFiles, failures, events,
      });
      chatEntries.length = 0;
      openImport.remove();
      ui.done(doneMsg);
    } catch {
      transfer.cancel();
      ui.set("Вкладка импорта не приняла данные. Можно повторить передачу.");
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
      if (event.data.type === "chatgpt-export/ready") markReady(event.data.archiveLimit);
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
      async send(buildBatches, report) {
        try {
          if (tab.closed) throw new Error("Import tab was closed");
          const limit = await wait(ready);
          if (!Number.isSafeInteger(limit) || limit <= 0) throw new Error("Missing archive limit");
          const archives = await buildBatches(limit);
          ui.wait("Передаю данные и ожидаю подтверждения вкладки импорта");
          tab.postMessage({ type: "chatgpt-export/archive", transfer: transferId, format: 3, archives, report }, target.origin);
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

  async function buildArchiveBatches(chats, memory, limit, otherProjects) {
    const encoder = new TextEncoder();
    const entrySize = ({ path, data }) => 76 + 2 * encoder.encode(path).length
      + (typeof data === "string" ? encoder.encode(data).length : data.length);
    const personalization = { path: "personalization.json", data: JSON.stringify(memory) };
    const projectCatalog = otherProjects.length ? { path: 'projects.json', data: JSON.stringify(otherProjects) } : null;
    const overhead = 22 + entrySize(personalization);
    const target = Math.min(limit, 96 * 1024 * 1024);
    const archives = [];
    let entries = [], size = overhead, count = 0, packed = 0;
    async function pack() {
      ui.wait(`Собираю часть ${archives.length + 1}. Подготовлено чатов: ${packed} из ${chats.length}`);
      await sleep(0);
      archives.push(await buildZipBlob(entries));
      packed += count;
    }
    for (const chat of chats) {
      const chatSize = chat.reduce((total, entry) => total + entrySize(entry), 0);
      if (count && (size + chatSize > target || count >= 100 || entries.length + chat.length >= 30000)) {
        await pack();
        entries = [];
        size = overhead;
        count = 0;
      }
      for (const entry of chat) entries.push(entry);
      size += chatSize;
      count++;
    }
    if (projectCatalog && entries.length && size + entrySize(projectCatalog) > target) {
      await pack();
      entries = [];
      count = 0;
    }
    if (projectCatalog) entries.push(projectCatalog);
    entries.push(personalization);
    await pack();
    return archives;
  }

  async function buildZipBlob(entries) {
    if (entries.length > 0xffff) throw new Error("Too many ZIP entries");
    const te = new TextEncoder();
    const parts = [];
    const cdParts = [];
    let offset = 0;
    let yieldAfter = performance.now() + 100;

    for (const entry of entries) {
      if (performance.now() >= yieldAfter) {
        await sleep(0);
        yieldAfter = performance.now() + 100;
      }
      const pathBytes = te.encode(entry.path);
      const data = typeof entry.data === "string" ? te.encode(entry.data) : entry.data;
      const length = data.length;
      if (pathBytes.length > 0xffff || offset + 30 + pathBytes.length + length > 0xffffffff) {
        throw new Error("ZIP size limit exceeded");
      }
      const crc = await crc32(data);

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

  async function crc32(bytes) {
    const table = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      let c = i;
      for (let j = 0; j < 8; j++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      table[i] = c;
    }
    let crc = 0xffffffff;
    const blockSize = 16 * 1024 * 1024;
    for (let start = 0; start < bytes.length; start += blockSize) {
      if (start) await sleep(0);
      const end = Math.min(start + blockSize, bytes.length);
      for (let i = start; i < end; i++) crc = table[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
    }
    return (crc ^ 0xffffffff) >>> 0;
  }
})(/* OPEN_WEBUI_IMPORT_PAGE */ null);
