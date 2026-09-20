/* yt-dll frontend: polling без перезагрузки страницы */
const $ = (s) => document.querySelector(s);
const state = {
  data: null, tab: "queue",
  player: { list: [], idx: 0, plId: null, live: false, markWatched: false, waiting: false, open: false },
};

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
function fmtPub(ud) { // YYYYMMDD -> DD.MM.YYYY
  if (!ud || ud.length !== 8) return "";
  return ud.slice(6, 8) + "." + ud.slice(4, 6) + "." + ud.slice(0, 4);
}
function relIn(ts, now) { // "через 3 ч 5 мин" для будущего ts
  let s = Math.max(0, ts - now);
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60);
  if (d) return "через " + d + " дн " + h + " ч";
  if (h) return "через " + h + " ч " + m + " мин";
  if (m) return "через " + m + " мин";
  return "менее чем через минуту";
}
function relAgo(ts, now) {
  const s = Math.max(0, now - ts);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return d + " дн назад";
  if (h) return h + " ч назад";
  if (m) return m + " мин назад";
  return "только что";
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
  const q = d.queue || [], dl = d.downloaded || [], ch = d.channels || [];
  const bq = $("#badge-queue"), bd = $("#badge-dl"), bc = $("#badge-ch");
  bq.textContent = q.length; bq.classList.toggle("hidden", !q.length);
  bd.textContent = dl.length; bd.classList.toggle("hidden", !dl.length);
  bc.textContent = ch.length; bc.classList.toggle("hidden", !ch.length);
  renderPause(d);
  renderQueue(q);
  renderDownloaded(dl, d.now || Math.floor(Date.now() / 1000));
  renderPlaylists(d.playlists || [], dl, d.settings || {});
  renderChannels(ch, d.scanning || [], d.now || Math.floor(Date.now() / 1000));
  syncLivePlayer(d);
  if (state._settingsLoaded !== true) fillSettings(d.settings || {}, d.cookies_present);
}

function srcBadge(src) {
  return src === "channel" ? '<span class="status st-auto">🤖 авто</span>' : "";
}

/* ----- pause ----- */
function renderPause(d) {
  const paused = !!d.paused;
  const btn = $("#btn-pause");
  btn.innerHTML = paused ? "▶ Продолжить" : "⏸ Пауза";
  btn.classList.toggle("primary", paused);
  const active = (d.queue || []).filter((x) => x.status === "downloading").length;
  $("#paused-hint").textContent = paused
    ? "⏸ на паузе" + (d.queue && d.queue.length ? " · в очереди: " + d.queue.length : "")
    : (active ? "● качается" : "▶ воркер активен, очередь пуста");
  $("#paused-banner").classList.toggle("hidden", !paused);
}
$("#btn-pause").addEventListener("click", async () => {
  const paused = !(state.data && state.data.paused);
  const r = await api("/api/pause", { paused });
  if (!r.ok) toast(r.error || "ошибка", true);
  else toast(paused ? "⏸ Пауза: загрузки остановлены, прогресс сохранён" : "▶ Продолжение с того же места");
  refresh();
});

/* ----- queue ----- */
function renderQueue(q) {
  const box = $("#queue-list");
  const now = $("#now-downloading");
  const f = $("#q-filter").value || "all";
  const sort = $("#q-sort").value || "order";
  let items = q.filter((x) => f === "all" || (x.source || "manual") === f);
  const byPub = (a, b) => (b.upload_date || "").localeCompare(a.upload_date || "");
  if (sort === "pub") items = [...items].sort(byPub);
  else if (sort === "title") items = [...items].sort((a, b) => (a.title || "").localeCompare(b.title || ""));
  else if (sort === "added") items = [...items].sort((a, b) => (b.added_at || 0) - (a.added_at || 0));
  if (!q.length) {
    now.innerHTML = "";
    box.innerHTML = '<div class="glass card muted">Очередь пуста — вставьте ссылку выше.</div>';
    return;
  }
  const cur = q.find((x) => x.status === "downloading");
  now.innerHTML = cur ? `
    <div class="glass card item">
      <div class="item-top"><span class="status st-downloading">● скачивается</span>${srcBadge(cur.source)}
        <div class="item-title">${esc(cur.title)}</div></div>
      <div class="progress"><div style="width:${(cur.progress || 0).toFixed(1)}%"></div></div>
      <div class="item-meta"><span>${(cur.progress || 0).toFixed(1)}%</span>
        <span>${esc(cur.size || "")}</span><span>${esc(cur.speed || "")}</span>
        <span>${cur.eta ? "ETA " + esc(cur.eta) : ""}</span>
        <span>${esc(cur.stage || "")}</span></div>
    </div>` : "";
  box.innerHTML = "";
  if (!items.length) {
    box.innerHTML = '<div class="glass card muted">По фильтру ничего нет.</div>';
    return;
  }
  items.filter((x) => x.status !== "downloading").forEach((x) => {
    const el = document.createElement("div");
    el.className = "glass item";
    const st = x.status === "error"
      ? '<span class="status st-error">ошибка</span>'
      : (x.stage === "На паузе" ? '<span class="status st-paused">на паузе</span>'
        : '<span class="status st-queued">в очереди</span>');
    const pos = q.indexOf(x) + 1;
    el.innerHTML = `
      <div class="item-top">${st}${srcBadge(x.source)}<div class="item-title">${esc(x.title)}</div></div>
      <div class="item-meta"><span>#${pos}</span>
        <span>${x.duration ? "⏱ " + fmtDur(x.duration) : ""}</span>
        <span>${x.upload_date ? "📅 " + fmtPub(x.upload_date) : ""}</span>
        ${x.progress ? `<span>прогресс ${(+x.progress).toFixed(1)}% (докачается)</span>` : ""}
        <span class="small">${esc(x.url)}</span></div>
      ${x.status === "error" ? `<div class="error-text">${esc(x.error || "ошибка загрузки")}</div>` : ""}
      <div class="item-actions">
        <button class="icon-btn" data-a="up">↑</button>
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
$("#q-filter").addEventListener("change", () => renderQueue(state.data?.queue || []));
$("#q-sort").addEventListener("change", () => renderQueue(state.data?.queue || []));

/* ----- downloaded ----- */
function allDownloaded() { return (state.data?.downloaded || []); }

function watchedLine(d, now) {
  if (!d.watched_at) return "";
  const parts = [];
  if (!d.keep) {
    parts.push("🗑 файл " + relIn(d.watched_at + 24 * 3600, now));
  } else {
    parts.push("📌 хранится");
  }
  return `<div class="meta small muted">👁 просмотрено ${relAgo(d.watched_at, now)} · ${parts.join(" · ")}</div>`;
}

function renderDownloaded(dl, now) {
  const q = ($("#search-input").value || "").toLowerCase();
  const f = $("#dl-filter").value || "all";
  const sort = $("#dl-sort").value || "new";
  const box = $("#dl-list");
  let items = dl.filter((d) =>
    (d.title || "").toLowerCase().includes(q) && (f === "all" || (d.source || "manual") === f));
  const pub = (d) => d.upload_date || "";
  if (sort === "new") items = [...items].sort((a, b) => (b.downloaded_at || 0) - (a.downloaded_at || 0));
  else if (sort === "old") items = [...items].sort((a, b) => (a.downloaded_at || 0) - (b.downloaded_at || 0));
  else if (sort === "pub") items = [...items].sort((a, b) => pub(b).localeCompare(pub(a)));
  else if (sort === "title") items = [...items].sort((a, b) => (a.title || "").localeCompare(b.title || ""));
  else if (sort === "size") items = [...items].sort((a, b) => (b.filesize || 0) - (a.filesize || 0));
  $("#dl-count").textContent = items.length + " / " + dl.length;
  box.innerHTML = "";
  if (!items.length) {
    box.innerHTML = '<div class="glass card muted">Пока ничего не загружено.</div>';
    return;
  }
  items.forEach((d) => {
    const el = document.createElement("div");
    el.className = "glass vcard";
    const dlUrl = "/videos/" + encodeURIComponent(d.filepath || "") + "?dl=1";
    el.innerHTML = `
      <h4>${srcBadge(d.source)} ${esc(d.title)}</h4>
      <div class="meta">${d.duration ? "⏱ " + fmtDur(d.duration) + " · " : ""}${fmtBytes(d.filesize)}${d.external ? " · на диске" : ""}${d.upload_date ? " · 📅 " + fmtPub(d.upload_date) : ""}</div>
      ${watchedLine(d, now)}
      <div class="actions">
        <button class="icon-btn" data-a="play">▶ Смотреть</button>
        <a class="icon-btn" href="${dlUrl}" download>⬇ Скачать</a>
        <button class="icon-btn" data-a="pl">＋ В плейлист</button>
        <button class="icon-btn" data-a="keep" title="Не удалять автоматически">${d.keep ? "📌 Хранится" : "📍 Автоудаление"}</button>
        ${d.watched_at ? "" : '<button class="icon-btn" data-a="watched">✓ Просмотрено</button>'}
        <button class="icon-btn" data-a="del">🗑 Удалить</button>
      </div>`;
    el.querySelector('[data-a="play"]').addEventListener("click", () => openPlayer([d], 0, { title: d.title }));
    el.querySelector('[data-a="del"]').addEventListener("click", async () => {
      if (!confirm("Удалить файл «" + d.title + "» с диска?")) return;
      const r = await api("/api/files/delete", { filepath: d.filepath });
      if (!r.ok) toast(r.error || "не удалено", true); else refresh();
    });
    el.querySelector('[data-a="pl"]').addEventListener("click", () => addToPlaylist(d));
    el.querySelector('[data-a="keep"]').addEventListener("click", async () => {
      const r = await api("/api/keep", { id: d.id, keep: !d.keep });
      if (!r.ok) toast(r.error || "ошибка", true);
      else toast(d.keep ? "📍 Вернулось автоудаление" : "📌 Файл не будет удалён автоматически");
      refresh();
    });
    const w = el.querySelector('[data-a="watched"]');
    if (w) w.addEventListener("click", async () => {
      await api("/api/watched", { id: d.id });
      toast("👁 Помечено просмотренным — таймеры очистки запущены");
      refresh();
    });
    box.appendChild(el);
  });
}
$("#dl-filter").addEventListener("change", () => render());
$("#dl-sort").addEventListener("change", () => render());

/* ----- playlists ----- */
function renderPlaylists(pls, dl, settings) {
  const box = $("#pl-list");
  box.innerHTML = "";
  if (!pls.length) {
    box.innerHTML = '<div class="glass card muted">Плейлистов пока нет — создайте первый выше.</div>';
    return;
  }
  const byId = Object.fromEntries(dl.map((d) => [d.id, d]));
  const autoIds = new Set([settings.watch_pl_id, settings.subs_pl_id]);
  pls.forEach((pl) => {
    const el = document.createElement("div");
    el.className = "glass item";
    const tag = pl.permanent ? ' <span class="status st-lock" title="Постоянный плейлист">🔒 постоянный</span>'
      : (autoIds.has(pl.id) ? ' <span class="status st-auto">авто</span>' : "");
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
      <div class="pl-head"><b>${esc(pl.name)}</b>${tag}<span class="muted small">${pl.items.length} видео</span>
        <button class="icon-btn" data-a="playall">▶ Смотреть всё</button>
        <button class="icon-btn" data-a="rename">✎</button>
        ${pl.permanent ? "" : '<button class="icon-btn" data-a="del">🗑</button>'}</div>
      <ul class="pl-items">${lis || '<li><span class="muted">пусто — видео появятся автоматически после загрузки</span></li>'}</ul>`;
    const isAuto = autoIds.has(pl.id);
    el.querySelector('[data-a="playall"]').addEventListener("click", () => {
      const vids = pl.items.map((id) => byId[id]).filter(Boolean);
      if (!vids.length) return toast("Нет доступных видео", true);
      openPlayer(vids, 0, { plId: pl.id, live: true, markWatched: isAuto, title: pl.name });
    });
    el.querySelector('[data-a="rename"]').addEventListener("click", async () => {
      const name = prompt("Новое название:", pl.name);
      if (name === null) return;
      await api("/api/playlists/rename", { playlist_id: pl.id, name });
      refresh();
    });
    const delB = el.querySelector('[data-a="del"]');
    if (delB) delB.addEventListener("click", async () => {
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
        const idx = Math.max(0, vids.findIndex((v) => v.id === did));
        openPlayer(vids, idx, { plId: pl.id, live: true, markWatched: isAuto, title: pl.name });
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

/* ----- channels ----- */
function renderChannels(chs, scanning, now) {
  const box = $("#ch-list");
  box.innerHTML = "";
  if (!chs.length) {
    box.innerHTML = '<div class="glass card muted">Каналов пока нет — добавьте первый выше.</div>';
    return;
  }
  const busy = new Set(scanning || []);
  chs.forEach((c) => {
    const el = document.createElement("div");
    el.className = "glass item";
    el.innerHTML = `
      <div class="item-top">
        <label class="check" title="Включить/выключить проверки"><input type="checkbox" data-f="enabled" ${c.enabled ? "checked" : ""}></label>
        <div class="item-title">${esc(c.name || c.url)} ${busy.has(c.id) ? '<span class="status st-downloading">проверка…</span>' : ""}</div>
      </div>
      <div class="item-meta"><span class="small">${esc(c.url)}</span>
        <span>каждые ${c.interval_min} мин</span><span>новых за раз: ${c.max_videos}</span>
        <span>${c.last_check ? "проверен " + relAgo(c.last_check, now) + " (+" + (c.last_found || 0) + ")" : "ещё не проверялся"}</span></div>
      <div class="item-actions">
        <label class="small muted">интервал <input type="number" min="5" max="10080" value="${c.interval_min}" data-f="interval_min"></label>
        <label class="small muted">за раз <input type="number" min="1" max="50" value="${c.max_videos}" data-f="max_videos"></label>
        <button class="icon-btn" data-a="save">💾</button>
        <button class="icon-btn" data-a="check">↻ проверить сейчас</button>
        <button class="icon-btn" data-a="rm">🗑</button>
      </div>`;
    el.querySelector('[data-f="enabled"]').addEventListener("change", async (e) => {
      await api("/api/channels/update", { id: c.id, enabled: e.target.checked });
      refresh();
    });
    el.querySelector('[data-a="save"]').addEventListener("click", async () => {
      const r = await api("/api/channels/update", {
        id: c.id,
        interval_min: +el.querySelector('[data-f="interval_min"]').value,
        max_videos: +el.querySelector('[data-f="max_videos"]').value,
      });
      if (!r.ok) toast(r.error || "ошибка", true); else toast("Настройки канала сохранены");
      refresh();
    });
    el.querySelector('[data-a="check"]').addEventListener("click", async () => {
      const r = await api("/api/channels/check", { id: c.id });
      toast(r.ok ? "Проверка запущена (фон, ~1 мин)" : ("✕ " + (r.error || "")), !r.ok);
      refresh();
    });
    el.querySelector('[data-a="rm"]').addEventListener("click", async () => {
      if (!confirm("Перестать следить за «" + (c.name || c.url) + "»? Очередь и файлы не тронуты.")) return;
      await api("/api/channels/delete", { id: c.id });
      refresh();
    });
    box.appendChild(el);
  });
}
$("#btn-ch-add").addEventListener("click", async () => {
  const url = $("#ch-url").value.trim();
  if (!url) return;
  const r = await api("/api/channels/add", {
    url,
    interval_min: +$("#ch-interval").value || 60,
    max_videos: +$("#ch-max").value || 5,
  });
  if (!r.ok) { toast(r.error || "не добавлено", true); return; }
  $("#ch-url").value = "";
  toast("Канал добавлен, первая проверка запущена");
  refresh();
});

/* ---------- player ---------- */
function openPlayer(list, idx, opts = {}) {
  state.player = {
    list: [...list], idx: Math.max(0, idx), plId: opts.plId || null,
    live: !!opts.live, markWatched: !!opts.markWatched,
    waiting: false, open: true, title: opts.title || "",
  };
  loadPlayerItem();
  $("#player-modal").classList.remove("hidden");
}
function closePlayer() {
  $("#player").pause();
  $("#player-modal").classList.add("hidden");
  state.player.open = false;
  state.player.waiting = false;
}
function playerSub() {
  const p = state.player;
  const pos = p.list.length ? `${p.idx + 1} / ${p.list.length}` : "";
  const live = p.live ? " · live-обновление" : "";
  const wait = p.waiting ? " · жду следующее видео…" : "";
  const t = p.title ? p.title + " · " : "";
  return `${t}${pos}${live}${wait}`;
}
function loadPlayerItem() {
  const p = state.player;
  const d = p.list[p.idx];
  if (!d) {
    $("#player-title").textContent = p.title || "";
    $("#player-sub").textContent = playerSub();
    return;
  }
  $("#player-title").textContent = d.title || "";
  $("#player-sub").textContent = playerSub();
  const v = $("#player");
  v.src = "/videos/" + encodeURIComponent(d.filepath);
  v.play().catch(() => {});
}
/* добрасывание новых видео из плейлиста без прерывания просмотра */
function syncLivePlayer(d) {
  const p = state.player;
  if (!p.open || !p.live || !p.plId) return;
  const pl = (d.playlists || []).find((x) => x.id === p.plId);
  if (!pl) return;
  const byId = Object.fromEntries((d.downloaded || []).map((x) => [x.id, x]));
  const have = new Set(p.list.map((x) => x.id));
  let added = 0;
  pl.items.forEach((did) => {
    const v = byId[did];
    if (v && !have.has(did)) { p.list.push(v); have.add(did); added++; }
  });
  if (added) {
    toast("＋ В плейлист добавлено видео: " + added);
    if (p.waiting) { // ждали следующее — стартуем первое новое
      p.waiting = false;
      p.idx = p.list.length - added;
      loadPlayerItem();
    } else {
      $("#player-sub").textContent = playerSub();
    }
  }
}
async function markCurrentWatched() {
  const p = state.player;
  const d = p.list[p.idx];
  if (!p.markWatched || !d || d.id.startsWith("ext_")) return;
  try { await api("/api/watched", { id: d.id }); } catch (e) { /* silent */ }
}
$("#player-close").addEventListener("click", closePlayer);
$("#player-modal").addEventListener("click", (e) => {
  if (e.target.id === "player-modal") closePlayer();
});
$("#player-prev").addEventListener("click", () => {
  const p = state.player;
  if (p.idx > 0) { p.idx--; p.waiting = false; loadPlayerItem(); }
});
$("#player-next").addEventListener("click", () => {
  const p = state.player;
  if (p.idx < p.list.length - 1) { p.idx++; p.waiting = false; loadPlayerItem(); }
  else if (p.live) { p.waiting = true; $("#player-sub").textContent = playerSub(); toast("Жду следующее видео — оно начнётся само"); }
});
$("#player").addEventListener("ended", async () => {
  const p = state.player;
  await markCurrentWatched();
  if (p.idx < p.list.length - 1) { p.idx++; loadPlayerItem(); }
  else if (p.live) { p.waiting = true; $("#player-sub").textContent = playerSub(); }
  refresh();
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
$("#search-input").addEventListener("input", () => render());

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
