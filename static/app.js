/* yt-dll frontend: polling без перезагрузки страницы */
const $ = (s) => document.querySelector(s);
const state = { data: null, tab: "queue", playerList: [], playerIdx: 0 };

function fmtBytes(b) {
  if (!b) return "";
  const u = ["Б", "КБ", "МБ", "ГБ"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(1) + " " + u[i];
}
function fmtDur(s) {
  if (s == null) return "";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(ss).padStart(2, "0");
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), 4000);
}
async function api(path, body) {
  const r = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body || {}),
  });
  const j = await r.json().catch(() => ({ ok: false, error: "bad json" }));
  if (!r.ok && j.ok === undefined) j.ok = false;
  return j;
}

/* ---------- tabs ---------- */
document.querySelectorAll(".tab").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.tab = b.dataset.tab;
    document.querySelectorAll(".tabpage").forEach((p) => p.classList.add("hidden"));
    $("#tab-" + state.tab).classList.remove("hidden");
  })
);

/* ---------- render ---------- */
function render() {
  const d = state.data;
  if (!d) return;
  const q = d.queue || [], dl = d.downloaded || [];
  const bq = $("#badge-queue"), bd = $("#badge-dl");
  bq.textContent = q.length; bq.classList.toggle("hidden", !q.length);
  bd.textContent = dl.length; bd.classList.toggle("hidden", !dl.length);
  renderQueue(q, d.current_id);
  renderDownloaded(dl);
  renderPlaylists(d.playlists || [], dl);
  if (state._settingsLoaded !== true) fillSettings(d.settings || {}, d.cookies_present);
}

function renderQueue(q, currentId) {
  const box = $("#queue-list");
  const now = $("#now-downloading");
  if (!q.length) {
    now.innerHTML = "";
    box.innerHTML = '<div class="glass card muted">Очередь пуста — вставьте ссылку выше.</div>';
    return;
  }
  const cur = q.find((x) => x.status === "downloading");
  now.innerHTML = cur ? `
    <div class="glass card item">
      <div class="item-top"><span class="status st-downloading">● скачивается</span>
        <div class="item-title">${esc(cur.title)}</div></div>
      <div class="progress"><div style="width:${(cur.progress || 0).toFixed(1)}%"></div></div>
      <div class="item-meta"><span>${(cur.progress || 0).toFixed(1)}%</span>
        <span>${esc(cur.size || "")}</span><span>${esc(cur.speed || "")}</span>
        <span>${cur.eta ? "ETA " + esc(cur.eta) : ""}</span>
        <span>${esc(cur.stage || "")}</span></div>
    </div>` : "";
  box.innerHTML = "";
  q.filter((x) => x.status !== "downloading").forEach((x, idx) => {
    const el = document.createElement("div");
    el.className = "glass item";
    const st = x.status === "error"
      ? '<span class="status st-error">ошибка</span>'
      : '<span class="status st-queued">в очереди</span>';
    el.innerHTML = `
      <div class="item-top">${st}<div class="item-title">${esc(x.title)}</div></div>
      <div class="item-meta"><span>#${idx + 1}</span>
        <span>${x.duration ? "⏱ " + fmtDur(x.duration) : ""}</span>
        <span class="small">${esc(x.url)}</span></div>
      ${x.status === "error" ? `<div class="error-text">${esc(x.error || "ошибка загрузки")}</div>` : ""}
      <div class="item-actions">
        <button class="icon-btn" data-a="up" ${idx === 0 ? "disabled" : ""}>↑</button>
        <button class="icon-btn" data-a="down">↓</button>
        <button class="icon-btn" data-a="top">⤒ в начало</button>
        ${x.status === "error" ? '<button class="icon-btn" data-a="retry">↻ повторить</button>' : ""}
        <button class="icon-btn" data-a="rm">✕ убрать</button>
      </div>`;
    el.querySelectorAll("button").forEach((b) =>
      b.addEventListener("click", () => queueAction(x.id, b.dataset.a)));
    box.appendChild(el);
  });
}

async function queueAction(id, a) {
  if (a === "rm") await api("/api/queue/remove", { id });
  else if (a === "retry") await api("/api/queue/retry", { id });
  else if (a === "up") await api("/api/queue/move", { id, to: "up" });
  else if (a === "down") await api("/api/queue/move", { id, to: "down" });
  else if (a === "top") await api("/api/queue/move", { id, to: "top" });
  refresh();
}

function allDownloaded() { return (state.data?.downloaded || []); }

function renderDownloaded(dl) {
  const q = ($("#search-input").value || "").toLowerCase();
  const box = $("#dl-list");
  const items = dl.filter((d) => (d.title || "").toLowerCase().includes(q));
  $("#dl-count").textContent = items.length + " / " + dl.length;
  box.innerHTML = "";
  if (!items.length) {
    box.innerHTML = '<div class="glass card muted">Пока ничего не загружено.</div>';
    return;
  }
  items.forEach((d) => {
    const el = document.createElement("div");
    el.className = "glass vcard";
    el.innerHTML = `
      <h4>${esc(d.title)}</h4>
      <div class="meta">${d.duration ? "⏱ " + fmtDur(d.duration) + " · " : ""}${fmtBytes(d.filesize)}${d.external ? " · на диске" : ""}</div>
      <div class="actions">
        <button class="icon-btn" data-a="play">▶ Смотреть</button>
        <button class="icon-btn" data-a="pl">＋ В плейлист</button>
        <button class="icon-btn" data-a="del">🗑 Удалить</button>
      </div>`;
    el.querySelector('[data-a="play"]').addEventListener("click", () => openPlayer([d], 0));
    el.querySelector('[data-a="del"]').addEventListener("click", async () => {
      if (!confirm("Удалить файл «" + d.title + "» с диска?")) return;
      const r = await api("/api/files/delete", { filepath: d.filepath });
      if (!r.ok) toast(r.error || "не удалено", true); else refresh();
    });
    el.querySelector('[data-a="pl"]').addEventListener("click", () => addToPlaylist(d));
    box.appendChild(el);
  });
}

function renderPlaylists(pls, dl) {
  const box = $("#pl-list");
  box.innerHTML = "";
  if (!pls.length) {
    box.innerHTML = '<div class="glass card muted">Плейлистов пока нет — создайте первый выше.</div>';
    return;
  }
  const byId = Object.fromEntries(dl.map((d) => [d.id, d]));
  pls.forEach((pl) => {
    const el = document.createElement("div");
    el.className = "glass item";
    const lis = pl.items.map((did, i) => {
      const d = byId[did];
      const nm = d ? d.title : "(файл удалён)";
      return `<li><span title="${esc(nm)}">${i + 1}. ${esc(nm)}</span>
        <button class="icon-btn" data-i="${i}" data-a="pplay">▶</button>
        <button class="icon-btn" data-i="${i}" data-a="pup">↑</button>
        <button class="icon-btn" data-i="${i}" data-a="pdown">↓</button>
        <button class="icon-btn" data-i="${i}" data-a="prm">✕</button></li>`;
    }).join("");
    el.innerHTML = `
      <div class="pl-head"><b>${esc(pl.name)}</b><span class="muted small">${pl.items.length} видео</span>
        <button class="icon-btn" data-a="playall">▶ Смотреть всё</button>
        <button class="icon-btn" data-a="rename">✎</button>
        <button class="icon-btn" data-a="del">🗑</button></div>
      <ul class="pl-items">${lis || '<li><span class="muted">пусто — добавьте видео из «Загруженные»</span></li>'}</ul>`;
    el.querySelector('[data-a="playall"]').addEventListener("click", () => {
      const vids = pl.items.map((id) => byId[id]).filter(Boolean);
      if (!vids.length) return toast("Нет доступных видео", true);
      openPlayer(vids, 0);
    });
    el.querySelector('[data-a="rename"]').addEventListener("click", async () => {
      const name = prompt("Новое название:", pl.name);
      if (name === null) return;
      await api("/api/playlists/rename", { playlist_id: pl.id, name });
      refresh();
    });
    el.querySelector('[data-a="del"]').addEventListener("click", async () => {
      if (!confirm("Удалить плейлист «" + pl.name + "»? Файлы останутся.")) return;
      await api("/api/playlists/delete", { playlist_id: pl.id });
      refresh();
    });
    el.querySelectorAll("li button").forEach((b) => b.addEventListener("click", async () => {
      const did = pl.items[+b.dataset.i];
      if (b.dataset.a === "prm") await api("/api/playlists/remove", { playlist_id: pl.id, downloaded_id: did });
      else if (b.dataset.a === "pup") await api("/api/playlists/move", { playlist_id: pl.id, downloaded_id: did, to: +b.dataset.i - 1 });
      else if (b.dataset.a === "pdown") await api("/api/playlists/move", { playlist_id: pl.id, downloaded_id: did, to: +b.dataset.i + 1 });
      else if (b.dataset.a === "pplay") {
        const vids = pl.items.map((id) => byId[id]).filter(Boolean);
        openPlayer(vids, Math.max(0, vids.findIndex((v) => v.id === did)));
        return;
      }
      refresh();
    }));
    box.appendChild(el);
  });
}

async function addToPlaylist(d) {
  const pls = state.data.playlists || [];
  if (!pls.length) return toast("Сначала создайте плейлист", true);
  const names = pls.map((p, i) => `${i + 1}. ${p.name}`).join("\n");
  const n = prompt("В какой плейлист добавить?\n" + names);
  if (n === null) return;
  const pl = pls[+n - 1];
  if (!pl) return toast("Нет такого плейлиста", true);
  await api("/api/playlists/add", { playlist_id: pl.id, downloaded_id: d.id });
  toast("Добавлено в «" + pl.name + "»");
  refresh();
}

/* ---------- player ---------- */
function openPlayer(list, idx) {
  state.playerList = list; state.playerIdx = idx;
  loadPlayerItem();
  $("#player-modal").classList.remove("hidden");
}
function loadPlayerItem() {
  const d = state.playerList[state.playerIdx];
  if (!d) return;
  $("#player-title").textContent = d.title || "";
  const v = $("#player");
  v.src = "/videos/" + encodeURIComponent(d.filepath);
  v.play().catch(() => {});
}
$("#player-close").addEventListener("click", () => {
  $("#player").pause();
  $("#player-modal").classList.add("hidden");
});
$("#player-modal").addEventListener("click", (e) => {
  if (e.target.id === "player-modal") { $("#player").pause(); $("#player-modal").classList.add("hidden"); }
});
$("#player-prev").addEventListener("click", () => {
  if (state.playerIdx > 0) { state.playerIdx--; loadPlayerItem(); }
});
$("#player-next").addEventListener("click", () => {
  if (state.playerIdx < state.playerList.length - 1) { state.playerIdx++; loadPlayerItem(); }
});
$("#player").addEventListener("ended", () => {
  if (state.playerIdx < state.playerList.length - 1) { state.playerIdx++; loadPlayerItem(); }
});

/* ---------- add / preview ---------- */
async function doAdd() {
  const inp = $("#url-input");
  const url = inp.value.trim();
  if (!url) return;
  const btn = $("#btn-add");
  btn.disabled = true;
  const r = await api("/api/add", { url });
  btn.disabled = false;
  if (!r.ok) { toast(r.error || "не добавлено", true); return; }
  inp.value = "";
  $("#preview").classList.add("hidden");
  toast("Добавлено в очередь");
  refresh();
}
async function doPreview() {
  const url = $("#url-input").value.trim();
  if (!url) return;
  const box = $("#preview");
  box.classList.remove("hidden");
  box.innerHTML = '<span class="muted">Получаю информацию…</span>';
  const r = await api("/api/info?url=" + encodeURIComponent(url));
  if (!r.ok) { box.innerHTML = '<span class="error-text">' + esc(r.error || "ошибка") + "</span>"; return; }
  box.innerHTML = `
    <div class="row"><div style="flex:1"><b>${esc(r.title || url)}</b>
      <div class="muted small">${esc(r.uploader || "")} ${r.duration ? "· ⏱ " + fmtDur(r.duration) : ""}
      ${r.duplicate ? '· <span class="status st-error">уже в очереди/загружено</span>' : '· <span class="status st-done">можно качать</span>'}</div></div>
      ${r.duplicate ? "" : '<button id="pv-add" class="btn primary">＋ В очередь</button>'}</div>`;
  const b = $("#pv-add");
  if (b) b.addEventListener("click", doAdd);
}
$("#btn-add").addEventListener("click", doAdd);
$("#btn-preview").addEventListener("click", doPreview);
$("#url-input").addEventListener("keydown", (e) => { if (e.key === "Enter") doAdd(); });
$("#search-input").addEventListener("input", () => renderDownloaded(allDownloaded()));

/* ---------- playlists create ---------- */
$("#btn-pl-create").addEventListener("click", async () => {
  const name = $("#pl-name").value.trim();
  if (!name) return;
  await api("/api/playlists/create", { name });
  $("#pl-name").value = "";
  refresh();
});

/* ---------- settings ---------- */
function fillSettings(s, cookiesPresent) {
  state._settingsLoaded = true;
  $("#set-resolution").value = s.resolution || "720";
  $("#set-ru").checked = s.prefer_ru_audio !== false;
  $("#set-proxy-on").checked = !!s.proxy_enabled;
  $("#set-proxy-url").value = s.proxy_url || "";
  $("#set-browser").value = s.cookies_browser || "chromium";
  $("#set-use-cookies").checked = !!s.use_cookies;
  $("#cookies-status").textContent = cookiesPresent
    ? "cookies.txt на месте — можно использовать."
    : "cookies.txt отсутствует.";
}
$("#btn-save-settings").addEventListener("click", async () => {
  const body = {
    resolution: $("#set-resolution").value,
    prefer_ru_audio: $("#set-ru").checked,
    proxy_enabled: $("#set-proxy-on").checked,
    proxy_url: $("#set-proxy-url").value,
    use_cookies: $("#set-use-cookies").checked,
    cookies_browser: $("#set-browser").value,
  };
  const r = await api("/api/settings", body);
  $("#settings-saved").textContent = r.ok ? "✓ сохранено " + new Date().toLocaleTimeString() : ("✕ " + (r.error || ""));
  if (r.ok && r.settings) fillSettingsKeep(r.settings);
  refresh();
});
function fillSettingsKeep(s) {
  state._settingsLoaded = true;
  $("#set-resolution").value = s.resolution;
  $("#set-ru").checked = s.prefer_ru_audio;
  $("#set-proxy-on").checked = s.proxy_enabled;
  $("#set-proxy-url").value = s.proxy_url;
  $("#set-browser").value = s.cookies_browser;
  $("#set-use-cookies").checked = s.use_cookies;
}
$("#btn-cookies").addEventListener("click", async (e) => {
  const b = e.target;
  b.disabled = true;
  b.textContent = "Копирую… (до ~1 мин)";
  const r = await api("/api/cookies/export", { browser: $("#set-browser").value });
  b.disabled = false;
  b.textContent = "⤓ Скопировать cookies из браузера";
  if (r.ok) { toast("✓ " + r.message); $("#set-use-cookies").checked = true; }
  else toast("✕ " + (r.error || "ошибка"), true);
  refresh();
});
$("#btn-cookies-clear").addEventListener("click", async () => {
  await api("/api/cookies/clear", {});
  $("#set-use-cookies").checked = false;
  toast("cookies.txt удалён");
  refresh();
});

/* ---------- polling ---------- */
async function refresh() {
  try {
    const r = await fetch("/api/state");
    if (!r.ok) throw new Error(r.status);
    state.data = await r.json();
    $("#net-status").classList.remove("off");
    render();
  } catch (e) {
    $("#net-status").classList.add("off");
  }
}
refresh();
setInterval(refresh, 1000);
