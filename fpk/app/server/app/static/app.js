(() => {
  "use strict";

  // Detect gateway prefix cleanly from injected <base href> first, then fallback to location.pathname
  const API_BASE = (() => {
    const baseEl = document.querySelector("base");
    if (baseEl && baseEl.getAttribute("href")) {
      return baseEl.getAttribute("href").replace(/\/+$/, "");
    }
    const p = window.location.pathname.replace(/\/+$/, "");
    if (!p || p === "/index.html") return "";
    return p.endsWith("/index.html") ? p.slice(0, -11) : p;
  })();

  const state = {
    view: "tracks",
    userId: 1,
    overview: null,
    config: {},
    devices: [],
    selectedMount: "",
    search: "",
    // View filters
    libraryId: null,
    suffix: "",
    hasLyric: null,
    hasCover: null,
    sort: "default",
    page: 1,
    pageSize: 50,
    // Drill-down context
    activeAlbum: null,
    activeArtist: null,
    activePlaylist: null,
    folderLibId: null,
    folderRelPath: "",
    deviceRelPath: "",
    // Multi-selection map: trackId -> {id, title, size}
    selectedTracks: new Map(),
    currentPageItems: [],
    // Transfer polling
    transferStatus: { current: null, history: [] },
    pollTimer: null,
    // Dedup & Organize state
    dedupMode: "exact_hash",
    dedupLibId: null,
    dedupTolerance: 3,
    dedupCrossFormat: false,
    dedupPage: 1,
    dedupChecked: new Set(),
    dedupGroups: [],
    orgTab: "archive",
    orgLibId: null,
    orgSubpath: "",
    orgLayout: "artist_album",
    orgRename: false,
    orgExportWithMove: false,
    orgCleanEmpty: true,
    orgPreview: null,
    orgHealth: null,
    orgJob: { status: "idle" },
  };

  // ---------- Helpers ----------
  async function api(path, opts = {}) {
    const url = `${API_BASE}${path}`;
    const res = await fetch(url, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    if (!res.ok) {
      let msg = `请求失败 (${res.status})`;
      try {
        const j = await res.json();
        if (j && j.detail) msg = j.detail;
      } catch (_) {}
      throw new Error(msg);
    }
    return res.json();
  }

  function esc(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtBytes(b) {
    if (!b || b <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let v = b;
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i++;
    }
    return `${v.toFixed(i >= 2 ? 1 : 0)} ${units[i]}`;
  }

  function fmtDur(ms) {
    if (!ms || ms <= 0) return "--:--";
    const s = Math.floor(ms / 1000);
    const m = Math.floor(s / 60);
    const r = s % 60;
    return `${m}:${String(r).padStart(2, "0")}`;
  }

  function fmtTime(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    return d.toLocaleString("zh-CN", { hour12: false });
  }

  function coverUrl(type, guid, thumb = 160) {
    if (!type || !guid) return "";
    return `${API_BASE}/api/cover/${type}/${encodeURIComponent(guid)}?thumb=${thumb}`;
  }

  function toast(msg, kind = "info") {
    const c = document.getElementById("toast-container");
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = msg;
    c.appendChild(el);
    setTimeout(() => el.remove(), 3800);
  }

  function getActiveDevice() {
    return state.devices.find((d) => d.mountpoint === state.selectedMount) || null;
  }

  function requireDevice() {
    const dev = getActiveDevice();
    if (!dev) {
      toast("请先将 Echo 通过 USB 连接到飞牛 NAS，并点击右上角「刷新」检测设备", "warn");
      return null;
    }
    return dev;
  }

  // ---------- Device Bar & Overview ----------
  function renderDeviceBar() {
    const sel = document.getElementById("device-select");
    const dot = document.getElementById("device-dot");
    const space = document.getElementById("device-space");

    if (!state.devices.length) {
      dot.className = "status-dot offline";
      sel.innerHTML = `<option value="">未检测到 Echo 外挂存储</option>`;
      space.textContent = "请连接 USB";
      return;
    }

    if (!state.selectedMount || !state.devices.some((d) => d.mountpoint === state.selectedMount)) {
      const remembered = state.config.target_mount;
      if (remembered && state.devices.some((d) => d.mountpoint === remembered)) {
        state.selectedMount = remembered;
      } else {
        state.selectedMount = state.devices[0].mountpoint;
      }
    }

    sel.innerHTML = state.devices
      .map((d) => {
        const prefix = d.is_echo_likely ? "🎧 " : "💾 ";
        return `<option value="${esc(d.mountpoint)}" ${d.mountpoint === state.selectedMount ? "selected" : ""}>${prefix}${esc(d.label)} (${esc(d.fstype)})</option>`;
      })
      .join("");

    const active = getActiveDevice();
    dot.className = "status-dot online";
    space.textContent = active ? `可用 ${fmtBytes(active.free_bytes)} / ${fmtBytes(active.total_bytes)}` : "";
  }

  async function refreshDevices(silent = false) {
    try {
      const res = await api("/api/devices");
      state.devices = res.devices || [];
      renderDeviceBar();
      if (!silent) {
        if (state.devices.length) {
          toast(`已检测到 ${state.devices.length} 个外挂存储设备`, "ok");
        } else {
          toast("当前 /vol00 下未检测到已挂载的外挂存储", "warn");
        }
      }
      if (state.view === "device") renderDeviceView();
    } catch (e) {
      if (!silent) toast(e.message, "error");
    }
  }

  async function loadOverview() {
    try {
      const data = await api(`/api/overview?user_id=${state.userId}`);
      state.overview = data;
      state.config = data.config || {};
      state.devices = data.devices || [];
      if (!state.folderLibId && data.libraries && data.libraries.length) {
        state.folderLibId = data.libraries[0].id;
      }

      // Users select
      const uSel = document.getElementById("user-select");
      if (data.users && data.users.length) {
        uSel.innerHTML = data.users
          .map((u) => `<option value="${u.id}" ${u.id === state.userId ? "selected" : ""}>${esc(u.name)}</option>`)
          .join("");
      }

      if (data.available) {
        const st = data.stats;
        document.getElementById("badge-tracks").textContent = st.tracks.toLocaleString();
        document.getElementById("badge-albums").textContent = st.albums.toLocaleString();
        document.getElementById("badge-artists").textContent = st.artists.toLocaleString();
        document.getElementById("badge-favorites").textContent = st.favorites.toLocaleString();
        document.getElementById("badge-playlists").textContent = st.playlists.toLocaleString();
        document.getElementById("sidebar-stats").textContent =
          `已刮削歌词 ${st.lyrics.toLocaleString()} 首 · 飞牛音乐只读模式`;
      } else {
        document.getElementById("sidebar-stats").textContent = "未检测到飞牛音乐数据库";
      }
      renderDeviceBar();
    } catch (e) {
      toast(`加载飞牛音乐概览失败: ${e.message}`, "error");
    }
  }

  // ---------- Selection Dock ----------
  function updateSelectionDock() {
    const dock = document.getElementById("selection-dock");
    const n = state.selectedTracks.size;
    if (n === 0) {
      dock.classList.add("hidden");
      return;
    }
    dock.classList.remove("hidden");
    let bytes = 0;
    for (const item of state.selectedTracks.values()) bytes += item.size || 0;
    document.getElementById("dock-count").textContent = `已选 ${n} 首曲目`;
    document.getElementById("dock-size").textContent = `约 ${fmtBytes(bytes)}`;
    const dev = getActiveDevice();
    document.getElementById("dock-hint").textContent = dev
      ? `目标：${dev.label} (可用 ${fmtBytes(dev.free_bytes)}) · 自动携带封面与歌词`
      : "尚未接入 Echo 外挂存储，连接后点击即可发送";
  }

  function toggleTrackSelection(track, checked) {
    if (checked) {
      state.selectedTracks.set(track.id, { id: track.id, title: track.title, size: track.size || 0 });
    } else {
      state.selectedTracks.delete(track.id);
    }
    updateSelectionDock();
  }

  // ---------- Transfer Actions ----------
  async function startTransfer(payload) {
    const dev = requireDevice();
    if (!dev) return;
    try {
      const job = await api("/api/transfer/start", {
        method: "POST",
        body: JSON.stringify({
          mountpoint: dev.mountpoint,
          user_id: state.userId,
          ...payload,
        }),
      });
      toast(`已开始任务：${job.label}（共 ${job.total_tracks} 首）`, "ok");
      await pollTransferStatus();
    } catch (e) {
      toast(e.message, "error");
    }
  }

  async function pollTransferStatus() {
    try {
      const data = await api("/api/transfer/status");
      const prevRunning = state.transferStatus.current?.status === "running";
      state.transferStatus = data;
      const cur = data.current;
      const banner = document.getElementById("transfer-banner");
      const badge = document.getElementById("badge-transfer-active");

      if (cur && cur.status === "running") {
        banner.classList.remove("hidden");
        badge.classList.remove("hidden");
        const pct = cur.total_tracks > 0 ? Math.round((cur.processed_tracks / cur.total_tracks) * 100) : 0;
        document.getElementById("tb-title").textContent =
          `${cur.label} → ${cur.device_label} (${cur.processed_tracks}/${cur.total_tracks})`;
        const curName = cur.current_track ? `${cur.current_track.title} - ${cur.current_track.artist}` : "准备中...";
        document.getElementById("tb-sub").textContent =
          `当前：${curName} · 速度 ${fmtBytes(cur.speed_bps)}/s · 封面 ${cur.covers_written} · 歌词 ${cur.lyrics_written}`;
        document.getElementById("tb-fill").style.width = `${pct}%`;
        document.getElementById("tb-pct").textContent = `${pct}%`;
      } else {
        banner.classList.add("hidden");
        badge.classList.add("hidden");
        if (prevRunning && cur) {
          if (cur.status === "completed") {
            toast(
              `传输完成！复制 ${cur.copied} / 转码 ${cur.transcoded} / 跳过 ${cur.skipped}，封面 ${cur.covers_written}，歌词 ${cur.lyrics_written}`,
              "ok"
            );
          } else if (cur.status === "cancelled") {
            toast("传输任务已取消", "warn");
          }
          refreshDevices(true);
        }
      }

      if (state.view === "transfers") {
        renderTransfersView();
      }
    } catch (_) {}
  }

  // ---------- Render Track Table ----------
  function renderTrackRowsHTML(items) {
    state.currentPageItems = items;
    if (!items.length) {
      return `<div class="empty-state"><h3>暂无符合条件的曲目</h3><p>试试切换筛选条件或搜索关键词</p></div>`;
    }
    const allChecked = items.length > 0 && items.every((t) => state.selectedTracks.has(t.id));
    const rows = items
      .map((t) => {
        const checked = state.selectedTracks.has(t.id);
        const cov = coverUrl(t.cover_type, t.cover_guid, 100);
        const fmtTag = `<span class="tag tag-fmt">${esc(t.suffix)}</span>`;
        const hiRes =
          t.sample_rate >= 88200 || t.bit_depth >= 24 || t.suffix === "dsf"
            ? `<span class="tag tag-hi">${t.suffix === "dsf" ? "DSD" : `${t.bit_depth || 24}b/${Math.round((t.sample_rate || 0) / 1000)}k`}</span>`
            : "";
        const compatTag =
          t.compat === "transcode"
            ? `<span class="tag tag-warn" title="Echo 不原生支持此格式，传输时可自动转码为 FLAC">转FLAC</span>`
            : "";
        const cueTag = t.is_cue ? `<span class="tag tag-blue">CUE</span>` : "";
        const covTag = t.has_cover ? `<span class="tag tag-ok">封面</span>` : "";
        const lrcTag = t.has_lyric
          ? `<span class="tag tag-ok" title="来源: ${esc(t.lyric_source_label)}">歌词</span>`
          : "";
        const favTag = t.is_favorite ? `<span class="tag tag-fav">♥</span>` : "";

        return `
        <tr class="${checked ? "selected" : ""}" data-tid="${t.id}">
          <td style="width:36px">
            <input type="checkbox" class="row-check" data-tid="${t.id}" ${checked ? "checked" : ""} />
          </td>
          <td>
            <div class="track-main" data-detail="${t.id}">
              <div class="cover-thumb">
                ${cov ? `<img src="${cov}" loading="lazy" alt="" onerror="this.remove()"/>` : "♪"}
              </div>
              <div class="track-title-col">
                <div class="track-title" title="${esc(t.title)}">${esc(t.title)}</div>
                <div class="track-artist" title="${esc(t.artist)}">${esc(t.artist)}</div>
              </div>
            </div>
          </td>
          <td class="album-cell" title="${esc(t.album)}">${esc(t.album || "—")}</td>
          <td><div class="tag-list">${fmtTag}${hiRes}${cueTag}${compatTag}${covTag}${lrcTag}${favTag}</div></td>
          <td style="white-space:nowrap;color:var(--text-dim);font-size:12.5px">${fmtDur(t.duration_ms)}</td>
          <td style="white-space:nowrap;color:var(--text-dim);font-size:12.5px">${fmtBytes(t.size)}</td>
          <td style="width:110px;text-align:right">
            <button class="btn btn-ghost btn-sm" data-send-one="${t.id}" title="直接发送这首歌到 Echo">发送</button>
          </td>
        </tr>`;
      })
      .join("");

    return `
      <div class="track-table-wrap">
        <table class="track-table">
          <thead>
            <tr>
              <th style="width:36px"><input type="checkbox" id="check-all-page" ${allChecked ? "checked" : ""} /></th>
              <th>歌曲 / 歌手</th>
              <th>专辑</th>
              <th>格式与刮削状态</th>
              <th>时长</th>
              <th>大小</th>
              <th style="text-align:right">操作</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  function bindTrackTableEvents(container) {
    const checkAll = container.querySelector("#check-all-page");
    if (checkAll) {
      checkAll.addEventListener("change", () => {
        for (const item of state.currentPageItems) {
          toggleTrackSelection(item, checkAll.checked);
        }
        container.querySelectorAll(".row-check").forEach((cb) => {
          cb.checked = checkAll.checked;
          cb.closest("tr")?.classList.toggle("selected", checkAll.checked);
        });
      });
    }
    container.querySelectorAll(".row-check").forEach((cb) => {
      cb.addEventListener("change", () => {
        const tid = Number(cb.dataset.tid);
        const item = state.currentPageItems.find((x) => x.id === tid);
        if (item) toggleTrackSelection(item, cb.checked);
        cb.closest("tr")?.classList.toggle("selected", cb.checked);
      });
    });
    container.querySelectorAll("[data-detail]").forEach((el) => {
      el.addEventListener("click", () => openTrackDetail(Number(el.dataset.detail)));
    });
    container.querySelectorAll("[data-send-one]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const tid = Number(btn.dataset.sendOne);
        const item = state.currentPageItems.find((x) => x.id === tid);
        startTransfer({ track_ids: [tid], label: item ? `发送《${item.title}》` : "发送单曲" });
      });
    });
  }

  function renderPagination(total, page, pageSize, onPage) {
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    const el = document.createElement("div");
    el.className = "pagination";
    el.innerHTML = `
      <div>共 <strong>${total.toLocaleString()}</strong> 条，第 ${page} / ${totalPages} 页</div>
      <div class="page-btns">
        <button class="btn btn-sm" id="pg-first" ${page <= 1 ? "disabled" : ""}>首页</button>
        <button class="btn btn-sm" id="pg-prev" ${page <= 1 ? "disabled" : ""}>上一页</button>
        <button class="btn btn-sm" id="pg-next" ${page >= totalPages ? "disabled" : ""}>下一页</button>
        <button class="btn btn-sm" id="pg-last" ${page >= totalPages ? "disabled" : ""}>末页</button>
      </div>`;
    el.querySelector("#pg-first").onclick = () => onPage(1);
    el.querySelector("#pg-prev").onclick = () => onPage(page - 1);
    el.querySelector("#pg-next").onclick = () => onPage(page + 1);
    el.querySelector("#pg-last").onclick = () => onPage(totalPages);
    return el;
  }

  // ---------- Views ----------
  async function renderTracksView() {
    const root = document.getElementById("content-area");
    root.innerHTML = `<div class="loading-state">正在加载飞牛音乐曲库...</div>`;

    const params = new URLSearchParams({
      q: state.search,
      user_id: String(state.userId),
      sort: state.sort,
      page: String(state.page),
      page_size: String(state.pageSize),
    });
    if (state.libraryId) params.set("library_id", String(state.libraryId));
    if (state.suffix) params.set("suffix", state.suffix);
    if (state.hasLyric !== null) params.set("has_lyric", String(state.hasLyric));
    if (state.hasCover !== null) params.set("has_cover", String(state.hasCover));

    if (state.activeAlbum) params.set("album_id", String(state.activeAlbum.id));
    if (state.activeArtist) params.set("artist_id", String(state.activeArtist.id));
    if (state.activePlaylist) params.set("playlist_id", String(state.activePlaylist.id));
    if (state.view === "favorites") params.set("favorites", "true");

    try {
      const data = await api(`/api/tracks?${params.toString()}`);
      const libs = state.overview?.libraries || [];
      const fmts = state.overview?.formats || [];

      let headerHTML = "";
      if (state.view === "favorites") {
        headerHTML = `
          <div class="hero-banner">
            <div>
              <div class="hero-title">♥ 飞牛音乐「我喜欢的音乐」</div>
              <div class="hero-desc">在飞牛音乐 App 或网页中点击红心收藏的曲目会实时出现在这里，支持一键整批同步到 Echo 并自动生成播放列表。</div>
            </div>
            <button class="btn btn-primary" id="btn-sync-favorites" ${data.total === 0 ? "disabled" : ""}>🚀 一键同步全部喜欢 (${data.total})</button>
          </div>`;
      } else if (state.activeAlbum) {
        headerHTML = `
          <div class="hero-banner">
            <div style="display:flex;align-items:center;gap:14px">
              <div class="cover-thumb" style="width:64px;height:64px">
                ${state.activeAlbum.cover_guid ? `<img src="${coverUrl(state.activeAlbum.cover_type, state.activeAlbum.cover_guid, 200)}" />` : "◉"}
              </div>
              <div>
                <div class="hero-title">${esc(state.activeAlbum.name)}</div>
                <div class="hero-desc">${esc(state.activeAlbum.artist)} · 共 ${data.total} 首曲目</div>
              </div>
            </div>
            <div class="view-actions">
              <button class="btn" id="btn-back-drill">← 返回专辑墙</button>
              <button class="btn btn-primary" id="btn-send-whole-album">🚀 发送整张专辑到 Echo</button>
            </div>
          </div>`;
      } else if (state.activeArtist) {
        headerHTML = `
          <div class="hero-banner">
            <div>
              <div class="hero-title">◎ ${esc(state.activeArtist.name)}</div>
              <div class="hero-desc">共 ${data.total} 首曲目</div>
            </div>
            <div class="view-actions">
              <button class="btn" id="btn-back-drill">← 返回歌手库</button>
              <button class="btn btn-primary" id="btn-send-whole-artist">🚀 发送该歌手全部曲目</button>
            </div>
          </div>`;
      } else if (state.activePlaylist) {
        headerHTML = `
          <div class="hero-banner">
            <div>
              <div class="hero-title">☰ 歌单：${esc(state.activePlaylist.name)}</div>
              <div class="hero-desc">共 ${data.total} 首曲目 · 发送时自动在 Echo 的 Playlists/ 生成同名 .m3u8 歌单</div>
            </div>
            <div class="view-actions">
              <button class="btn" id="btn-back-drill">← 返回歌单列表</button>
              <button class="btn btn-primary" id="btn-send-whole-playlist">🚀 一键同步此歌单到 Echo</button>
            </div>
          </div>`;
      } else {
        headerHTML = `
          <div class="view-header">
            <div>
              <h1 class="view-title">全部歌曲</h1>
              <div class="view-subtitle">实时直读飞牛音乐库 · 勾选或点击曲目预览刮削封面与歌词</div>
            </div>
          </div>
          <div class="filter-bar">
            <select id="flt-lib">
              <option value="">全部媒体库</option>
              ${libs.map((l) => `<option value="${l.id}" ${state.libraryId === l.id ? "selected" : ""}>${esc(l.path)} (${l.track_count})</option>`).join("")}
            </select>
            <select id="flt-fmt">
              <option value="">全部格式</option>
              ${fmts.map((f) => `<option value="${esc(f.suffix)}" ${state.suffix === f.suffix ? "selected" : ""}>${esc(f.suffix.toUpperCase())} (${f.count})</option>`).join("")}
            </select>
            <select id="flt-sort">
              <option value="default" ${state.sort === "default" ? "selected" : ""}>默认排序</option>
              <option value="recent" ${state.sort === "recent" ? "selected" : ""}>最新入库</option>
              <option value="title" ${state.sort === "title" ? "selected" : ""}>标题 A-Z</option>
              <option value="size_desc" ${state.sort === "size_desc" ? "selected" : ""}>文件大小降序</option>
            </select>
            <button class="chip ${state.hasCover === true ? "active" : ""}" id="chip-cover">仅看有封面</button>
            <button class="chip ${state.hasLyric === true ? "active" : ""}" id="chip-lyric">仅看有歌词</button>
          </div>`;
      }

      root.innerHTML = headerHTML + renderTrackRowsHTML(data.items);
      bindTrackTableEvents(root);

      if (data.total > state.pageSize) {
        root.appendChild(
          renderPagination(data.total, data.page, data.pageSize, (p) => {
            state.page = p;
            renderTracksView();
          })
        );
      }

      // Bind header buttons
      const btnSyncFav = root.querySelector("#btn-sync-favorites");
      if (btnSyncFav) {
        btnSyncFav.onclick = () => startTransfer({ favorites: true, label: "同步「我喜欢的音乐」" });
      }
      const btnBack = root.querySelector("#btn-back-drill");
      if (btnBack) {
        btnBack.onclick = () => {
          const target = state.activeAlbum ? "albums" : state.activeArtist ? "artists" : "playlists";
          state.activeAlbum = null;
          state.activeArtist = null;
          state.activePlaylist = null;
          state.page = 1;
          switchView(target);
        };
      }
      const btnSendAlbum = root.querySelector("#btn-send-whole-album");
      if (btnSendAlbum) {
        btnSendAlbum.onclick = () =>
          startTransfer({ album_ids: [state.activeAlbum.id], label: `发送专辑《${state.activeAlbum.name}》` });
      }
      const btnSendArtist = root.querySelector("#btn-send-whole-artist");
      if (btnSendArtist) {
        btnSendArtist.onclick = () =>
          startTransfer({ artist_ids: [state.activeArtist.id], label: `发送歌手「${state.activeArtist.name}」` });
      }
      const btnSendPl = root.querySelector("#btn-send-whole-playlist");
      if (btnSendPl) {
        btnSendPl.onclick = () =>
          startTransfer({ playlist_id: state.activePlaylist.id, label: `同步歌单「${state.activePlaylist.name}」` });
      }

      const fltLib = root.querySelector("#flt-lib");
      if (fltLib) {
        fltLib.onchange = () => {
          state.libraryId = fltLib.value ? Number(fltLib.value) : null;
          state.page = 1;
          renderTracksView();
        };
      }
      const fltFmt = root.querySelector("#flt-fmt");
      if (fltFmt) {
        fltFmt.onchange = () => {
          state.suffix = fltFmt.value;
          state.page = 1;
          renderTracksView();
        };
      }
      const fltSort = root.querySelector("#flt-sort");
      if (fltSort) {
        fltSort.onchange = () => {
          state.sort = fltSort.value;
          state.page = 1;
          renderTracksView();
        };
      }
      const chipCover = root.querySelector("#chip-cover");
      if (chipCover) {
        chipCover.onclick = () => {
          state.hasCover = state.hasCover === true ? null : true;
          state.page = 1;
          renderTracksView();
        };
      }
      const chipLyric = root.querySelector("#chip-lyric");
      if (chipLyric) {
        chipLyric.onclick = () => {
          state.hasLyric = state.hasLyric === true ? null : true;
          state.page = 1;
          renderTracksView();
        };
      }
    } catch (e) {
      root.innerHTML = `<div class="empty-state"><h3>加载失败</h3><p>${esc(e.message)}</p></div>`;
    }
  }

  async function renderAlbumsView() {
    if (state.activeAlbum) return renderTracksView();
    const root = document.getElementById("content-area");
    root.innerHTML = `<div class="loading-state">正在加载专辑墙...</div>`;
    const params = new URLSearchParams({
      q: state.search,
      page: String(state.page),
      page_size: "40",
    });
    if (state.libraryId) params.set("library_id", String(state.libraryId));
    try {
      const data = await api(`/api/albums?${params.toString()}`);
      const libs = state.overview?.libraries || [];
      const cards = data.items
        .map((a) => {
          const cov = coverUrl(a.cover_type, a.cover_guid, 300);
          return `
          <div class="media-card" data-album-id="${a.id}">
            <div class="card-art">
              ${cov ? `<img src="${cov}" loading="lazy" alt="" onerror="this.remove()"/>` : "◉"}
              <button class="card-quick-send" data-send-album="${a.id}">发送整张</button>
            </div>
            <div class="card-title" title="${esc(a.name)}">${esc(a.name)}</div>
            <div class="card-sub" title="${esc(a.artist)}">${esc(a.artist)}</div>
            <div class="card-meta"><span>${a.track_count} 首</span><span>${fmtBytes(a.total_size)}</span></div>
          </div>`;
        })
        .join("");

      root.innerHTML = `
        <div class="view-header">
          <div>
            <h1 class="view-title">专辑墙</h1>
            <div class="view-subtitle">悬停卡片可直接发送整张专辑到 Echo，点击查看专辑曲目明细</div>
          </div>
          <div class="view-actions">
            <select id="album-lib-sel" style="background:var(--bg-card);color:var(--text);border:1px solid var(--border);border-radius:7px;padding:6px 10px">
              <option value="">全部媒体库</option>
              ${libs.map((l) => `<option value="${l.id}" ${state.libraryId === l.id ? "selected" : ""}>${esc(l.path)}</option>`).join("")}
            </select>
          </div>
        </div>
        ${data.items.length ? `<div class="card-grid">${cards}</div>` : `<div class="empty-state"><h3>暂无匹配专辑</h3></div>`}`;

      if (data.total > 40) {
        root.appendChild(
          renderPagination(data.total, data.page, 40, (p) => {
            state.page = p;
            renderAlbumsView();
          })
        );
      }

      root.querySelector("#album-lib-sel").onchange = (e) => {
        state.libraryId = e.target.value ? Number(e.target.value) : null;
        state.page = 1;
        renderAlbumsView();
      };
      root.querySelectorAll("[data-album-id]").forEach((card) => {
        card.addEventListener("click", () => {
          const aid = Number(card.dataset.albumId);
          state.activeAlbum = data.items.find((x) => x.id === aid);
          state.page = 1;
          renderTracksView();
        });
      });
      root.querySelectorAll("[data-send-album]").forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          const aid = Number(btn.dataset.sendAlbum);
          const al = data.items.find((x) => x.id === aid);
          startTransfer({ album_ids: [aid], label: `发送专辑《${al?.name || aid}》` });
        });
      });
    } catch (e) {
      root.innerHTML = `<div class="empty-state"><h3>加载失败</h3><p>${esc(e.message)}</p></div>`;
    }
  }

  async function renderArtistsView() {
    if (state.activeArtist) return renderTracksView();
    const root = document.getElementById("content-area");
    root.innerHTML = `<div class="loading-state">正在加载歌手库...</div>`;
    const params = new URLSearchParams({
      q: state.search,
      page: String(state.page),
      page_size: "48",
    });
    try {
      const data = await api(`/api/artists?${params.toString()}`);
      const cards = data.items
        .map((ar) => {
          const cov = coverUrl(ar.cover_type, ar.cover_guid, 240);
          return `
          <div class="media-card" data-artist-id="${ar.id}">
            <div class="card-art round">
              ${cov ? `<img src="${cov}" loading="lazy" alt="" onerror="this.remove()"/>` : "◎"}
              <button class="card-quick-send" data-send-artist="${ar.id}">发送全部</button>
            </div>
            <div class="card-title" style="text-align:center" title="${esc(ar.name)}">${esc(ar.name)}</div>
            <div class="card-meta" style="justify-content:center;gap:10px">
              <span>${ar.album_count} 专辑</span><span>${ar.track_count} 首</span>
            </div>
          </div>`;
        })
        .join("");

      root.innerHTML = `
        <div class="view-header">
          <div>
            <h1 class="view-title">歌手库</h1>
            <div class="view-subtitle">按曲目数排序，点击歌手查看其全部曲目</div>
          </div>
        </div>
        ${data.items.length ? `<div class="card-grid">${cards}</div>` : `<div class="empty-state"><h3>暂无匹配歌手</h3></div>`}`;

      if (data.total > 48) {
        root.appendChild(
          renderPagination(data.total, data.page, 48, (p) => {
            state.page = p;
            renderArtistsView();
          })
        );
      }

      root.querySelectorAll("[data-artist-id]").forEach((card) => {
        card.addEventListener("click", () => {
          const aid = Number(card.dataset.artistId);
          state.activeArtist = data.items.find((x) => x.id === aid);
          state.page = 1;
          renderTracksView();
        });
      });
      root.querySelectorAll("[data-send-artist]").forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          const aid = Number(btn.dataset.sendArtist);
          const ar = data.items.find((x) => x.id === aid);
          startTransfer({ artist_ids: [aid], label: `发送歌手「${ar?.name || aid}」` });
        });
      });
    } catch (e) {
      root.innerHTML = `<div class="empty-state"><h3>加载失败</h3><p>${esc(e.message)}</p></div>`;
    }
  }

  async function renderPlaylistsView() {
    if (state.activePlaylist) return renderTracksView();
    const root = document.getElementById("content-area");
    root.innerHTML = `<div class="loading-state">正在读取飞牛音乐歌单...</div>`;
    try {
      const data = await api(`/api/playlists?user_id=${state.userId}`);
      const items = data.items || [];
      const body = items.length
        ? `<div class="card-grid">
            ${items
              .map(
                (p) => `
              <div class="media-card" data-pl-id="${p.id}">
                <div class="card-art">☰
                  <button class="card-quick-send" data-send-pl="${p.id}">一键同步</button>
                </div>
                <div class="card-title">${esc(p.name)}</div>
                <div class="card-meta"><span>${p.track_count} 首曲目</span><span>自动导出 .m3u8</span></div>
              </div>`
              )
              .join("")}
          </div>`
        : `<div class="empty-state">
            <h3>飞牛音乐中还没有创建歌单</h3>
            <p>推荐技巧：在飞牛音乐 App 里新建一个名为「Echo」的歌单，把平时想带出门听的歌加进去，回到这里一键即可同步到 Echo 并生成播放列表！</p>
          </div>`;

      root.innerHTML = `
        <div class="view-header">
          <div>
            <h1 class="view-title">飞牛音乐歌单</h1>
            <div class="view-subtitle">实时同步飞牛音乐用户歌单，传输时自动在 Echo 生成 .m3u8 播放列表</div>
          </div>
          <button class="btn" id="btn-reload-pl">刷新歌单</button>
        </div>
        ${body}`;

      root.querySelector("#btn-reload-pl").onclick = renderPlaylistsView;
      root.querySelectorAll("[data-pl-id]").forEach((card) => {
        card.addEventListener("click", () => {
          const pid = Number(card.dataset.plId);
          state.activePlaylist = items.find((x) => x.id === pid);
          state.page = 1;
          renderTracksView();
        });
      });
      root.querySelectorAll("[data-send-pl]").forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          const pid = Number(btn.dataset.sendPl);
          const pl = items.find((x) => x.id === pid);
          startTransfer({ playlist_id: pid, label: `同步歌单「${pl?.name || pid}」` });
        });
      });
    } catch (e) {
      root.innerHTML = `<div class="empty-state"><h3>加载失败</h3><p>${esc(e.message)}</p></div>`;
    }
  }

  async function renderFoldersView() {
    const root = document.getElementById("content-area");
    const libs = state.overview?.libraries || [];
    if (!libs.length) {
      root.innerHTML = `<div class="empty-state"><h3>未发现飞牛音乐媒体库</h3></div>`;
      return;
    }
    if (!state.folderLibId) state.folderLibId = libs[0].id;
    root.innerHTML = `<div class="loading-state">正在读取目录...</div>`;

    try {
      const params = new URLSearchParams({
        library_id: String(state.folderLibId),
        rel_path: state.folderRelPath,
        user_id: String(state.userId),
      });
      const data = await api(`/api/folders?${params.toString()}`);
      const parts = data.rel_path ? data.rel_path.split("/") : [];

      let crumbs = `<button class="crumb-btn" data-crumb="">根目录 (${esc(data.library_path)})</button>`;
      let acc = "";
      for (const part of parts) {
        acc = acc ? `${acc}/${part}` : part;
        crumbs += `<span>/</span><button class="crumb-btn" data-crumb="${esc(acc)}">${esc(part)}</button>`;
      }

      const dirHTML = data.dirs.length
        ? `<div class="folder-list">
            ${data.dirs
              .map(
                (d) => `
              <div class="folder-item" data-open-dir="${esc(d.rel_path)}">
                <div class="folder-name" title="${esc(d.name)}"><span>📁</span><span>${esc(d.name)}</span></div>
                <button class="btn btn-sm" data-send-dir="${esc(d.path)}">发送目录</button>
              </div>`
              )
              .join("")}
          </div>`
        : "";

      root.innerHTML = `
        <div class="view-header">
          <div>
            <h1 class="view-title">文件夹浏览</h1>
            <div class="view-subtitle">按真实目录浏览音乐与有声书，自动匹配飞牛音乐的刮削封面与歌词</div>
          </div>
          <div class="view-actions">
            <select id="folder-lib-sel" style="background:var(--bg-card);color:var(--text);border:1px solid var(--border);border-radius:7px;padding:6px 10px">
              ${libs.map((l) => `<option value="${l.id}" ${state.folderLibId === l.id ? "selected" : ""}>${esc(l.path)} (${l.track_count})</option>`).join("")}
            </select>
            ${data.rel_path ? `<button class="btn btn-primary" id="btn-send-cur-folder">🚀 发送当前整个目录</button>` : ""}
          </div>
        </div>
        <div class="breadcrumb">${crumbs}</div>
        ${dirHTML}
        ${data.tracks.length ? renderTrackRowsHTML(data.tracks) : (!data.dirs.length ? `<div class="empty-state"><h3>此目录为空</h3></div>` : "")}`;

      bindTrackTableEvents(root);

      root.querySelector("#folder-lib-sel").onchange = (e) => {
        state.folderLibId = Number(e.target.value);
        state.folderRelPath = "";
        renderFoldersView();
      };
      root.querySelectorAll("[data-crumb]").forEach((b) => {
        b.onclick = () => {
          state.folderRelPath = b.dataset.crumb;
          renderFoldersView();
        };
      });
      root.querySelectorAll("[data-open-dir]").forEach((el) => {
        el.onclick = () => {
          state.folderRelPath = el.dataset.openDir;
          renderFoldersView();
        };
      });
      root.querySelectorAll("[data-send-dir]").forEach((btn) => {
        btn.onclick = (e) => {
          e.stopPropagation();
          const p = btn.dataset.sendDir;
          startTransfer({ folder_path: p, label: `发送目录「${p.split("/").pop()}」` });
        };
      });
      const sendCur = root.querySelector("#btn-send-cur-folder");
      if (sendCur) {
        sendCur.onclick = () => startTransfer({ folder_path: data.abs_path });
      }
    } catch (e) {
      root.innerHTML = `<div class="empty-state"><h3>读取目录失败</h3><p>${esc(e.message)}</p></div>`;
    }
  }

  async function renderDeviceView() {
    const root = document.getElementById("content-area");
    if (!state.devices.length) {
      root.innerHTML = `
        <div class="view-header">
          <div>
            <h1 class="view-title">Echo 外挂存储管理</h1>
            <div class="view-subtitle">实时检测挂载在飞牛 /vol00 下的 Echo 播放器内置存储与 TF 卡</div>
          </div>
          <button class="btn btn-primary" id="btn-dev-scan">刷新检测设备</button>
        </div>
        <div class="empty-state">
          <h3>当前未检测到 Echo 外挂存储</h3>
          <p>请使用 USB 数据线将 Echo 连接到飞牛 NAS，待飞牛自动挂载到 /vol00/SD_card 或 /vol00/Internal_storage 后点击「刷新检测设备」。</p>
        </div>`;
      root.querySelector("#btn-dev-scan").onclick = () => refreshDevices(false);
      return;
    }

    const active = getActiveDevice() || state.devices[0];
    state.selectedMount = active.mountpoint;
    renderDeviceBar();

    const cardsHTML = state.devices
      .map((d) => {
        const isAct = d.mountpoint === active.mountpoint;
        return `
        <div class="device-card ${isAct ? "active-dev" : ""}" data-pick-dev="${esc(d.mountpoint)}" style="cursor:pointer">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
            <strong>${d.is_echo_likely ? "🎧" : "💾"} ${esc(d.label)}</strong>
            <span class="tag ${d.has_echo_dir ? "tag-ok" : "tag-blue"}">${esc(d.role_hint)}</span>
          </div>
          <div style="font-size:12px;color:var(--text-muted);margin-top:4px">${esc(d.mountpoint)} · ${esc(d.fstype)}</div>
          <div class="disk-bar"><div class="disk-fill" style="width:${d.used_percent}%"></div></div>
          <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--text-dim)">
            <span>已用 ${fmtBytes(d.used_bytes)} (${d.used_percent}%)</span>
            <span>可用 <strong>${fmtBytes(d.free_bytes)}</strong> / ${fmtBytes(d.total_bytes)}</span>
          </div>
        </div>`;
      })
      .join("");

    root.innerHTML = `
      <div class="view-header">
        <div>
          <h1 class="view-title">Echo 外挂存储管理</h1>
          <div class="view-subtitle">浏览 Echo 设备内已有音乐与封面、清理空间，或在拔线前安全刷新磁盘缓存</div>
        </div>
        <div class="view-actions">
          <button class="btn" id="btn-dev-refresh">刷新列表</button>
          <button class="btn btn-ok" id="btn-dev-sync">安全刷盘 (sync)</button>
        </div>
      </div>
      <div class="device-card-grid">${cardsHTML}</div>
      <div id="device-files-zone"><div class="loading-state">正在读取设备文件...</div></div>`;

    root.querySelector("#btn-dev-refresh").onclick = () => refreshDevices(false);
    root.querySelector("#btn-dev-sync").onclick = async () => {
      try {
        await api("/api/devices/sync", { method: "POST", body: JSON.stringify({ mountpoint: active.mountpoint }) });
        toast("磁盘缓存已安全刷盘，可在飞牛「外接存储」推出后拔出 Echo", "ok");
      } catch (e) {
        toast(e.message, "error");
      }
    };
    root.querySelectorAll("[data-pick-dev]").forEach((el) => {
      el.onclick = () => {
        state.selectedMount = el.dataset.pickDev;
        state.deviceRelPath = "";
        renderDeviceView();
      };
    });

    // Load device files
    try {
      const fdata = await api(
        `/api/devices/browse?mountpoint=${encodeURIComponent(active.mountpoint)}&rel_path=${encodeURIComponent(state.deviceRelPath)}`
      );
      const zone = root.querySelector("#device-files-zone");
      const parts = fdata.rel_path ? fdata.rel_path.split("/") : [];
      let crumbs = `<button class="crumb-btn" data-dcrumb="">${esc(active.label)}</button>`;
      let acc = "";
      for (const p of parts) {
        acc = acc ? `${acc}/${p}` : p;
        crumbs += `<span>/</span><button class="crumb-btn" data-dcrumb="${esc(acc)}">${esc(p)}</button>`;
      }

      const rows = fdata.items
        .map(
          (it) => `
          <tr>
            <td>
              <div style="display:flex;align-items:center;gap:8px;${it.is_dir ? "cursor:pointer;font-weight:600" : ""}" ${it.is_dir ? `data-dopen="${esc(it.rel_path)}"` : ""}>
                <span>${it.is_dir ? "📁" : "🎵"}</span>
                <span>${esc(it.name)}</span>
              </div>
            </td>
            <td style="color:var(--text-dim);font-size:12.5px">${it.is_dir ? "目录" : fmtBytes(it.size)}</td>
            <td style="color:var(--text-dim);font-size:12.5px">${fmtTime(it.mtime)}</td>
            <td style="text-align:right">
              ${it.name !== ".echo" ? `<button class="btn btn-danger btn-sm" data-ddel="${esc(it.rel_path)}">删除</button>` : `<span class="tag tag-blue">系统索引</span>`}
            </td>
          </tr>`
        )
        .join("");

      zone.innerHTML = `
        <div class="breadcrumb">${crumbs}</div>
        ${
          fdata.items.length
            ? `<div class="track-table-wrap"><table class="track-table">
                <thead><tr><th>名称</th><th>大小</th><th>修改时间</th><th style="text-align:right">操作</th></tr></thead>
                <tbody>${rows}</tbody>
              </table></div>`
            : `<div class="empty-state"><h3>此目录为空</h3></div>`
        }`;

      zone.querySelectorAll("[data-dcrumb]").forEach((b) => {
        b.onclick = () => {
          state.deviceRelPath = b.dataset.dcrumb;
          renderDeviceView();
        };
      });
      zone.querySelectorAll("[data-dopen]").forEach((el) => {
        el.onclick = () => {
          state.deviceRelPath = el.dataset.dopen;
          renderDeviceView();
        };
      });
      zone.querySelectorAll("[data-ddel]").forEach((btn) => {
        btn.onclick = async () => {
          const rel = btn.dataset.ddel;
          if (!confirm(`确定要从 Echo 中永久删除「${rel}」吗？\n（不会影响飞牛 NAS 上的原始音乐文件）`)) return;
          try {
            await api("/api/devices/delete", {
              method: "POST",
              body: JSON.stringify({ mountpoint: active.mountpoint, rel_path: rel }),
            });
            toast(`已删除 ${rel}`, "ok");
            refreshDevices(true);
            renderDeviceView();
          } catch (e) {
            toast(e.message, "error");
          }
        };
      });
    } catch (e) {
      root.querySelector("#device-files-zone").innerHTML = `<div class="empty-state"><h3>读取失败</h3><p>${esc(e.message)}</p></div>`;
    }
  }

  function renderTransfersView() {
    const root = document.getElementById("content-area");
    const { current, history } = state.transferStatus;

    let curHTML = `<div class="empty-state"><h3>当前没有正在进行的传输任务</h3><p>在曲库中选择喜欢的歌曲、专辑或歌单，点击「发送到 Echo」即可开始传输</p></div>`;
    if (current) {
      const pct = current.total_tracks > 0 ? Math.round((current.processed_tracks / current.total_tracks) * 100) : 0;
      const statusBadge =
        current.status === "running"
          ? `<span class="tag tag-blue">传输中</span>`
          : current.status === "completed"
          ? `<span class="tag tag-ok">已完成</span>`
          : `<span class="tag tag-warn">已取消</span>`;

      const recentRows = (current.recent_items || [])
        .map(
          (it) => `
          <tr>
            <td>${esc(it.title)} <span style="color:var(--text-muted);font-size:12px">- ${esc(it.artist)}</span></td>
            <td><span class="tag ${it.status === "failed" ? "tag-warn" : it.status === "skipped" ? "tag-fmt" : "tag-ok"}">${esc(it.status)}</span> ${esc(it.note || "")}</td>
            <td>${it.cover ? `<span class="tag tag-ok">封面✓</span>` : ""} ${it.lyric ? `<span class="tag tag-ok">歌词✓</span>` : ""} ${it.tags ? `<span class="tag tag-blue">标签✓</span>` : ""}</td>
          </tr>`
        )
        .join("");

      curHTML = `
        <div class="transfer-card">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap">
            <div>
              <strong style="font-size:15px">${esc(current.label)}</strong> ${statusBadge}
              <div style="font-size:12px;color:var(--text-dim);margin-top:3px">目标设备：${esc(current.device_label)} (${esc(current.mountpoint)})</div>
            </div>
            ${current.status === "running" ? `<button class="btn btn-danger btn-sm" id="btn-tr-cancel">取消任务</button>` : ""}
          </div>
          <div class="disk-bar" style="height:10px;margin:14px 0 6px"><div class="disk-fill" style="width:${pct}%"></div></div>
          <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--text-dim)">
            <span>进度 ${current.processed_tracks} / ${current.total_tracks} (${pct}%)</span>
            <span>已传输 ${fmtBytes(current.transferred_bytes)} · ${fmtBytes(current.speed_bps)}/s</span>
          </div>
          <div class="stat-grid">
            <div class="stat-box"><div class="num" style="color:var(--ok)">${current.copied}</div><div class="lbl">直拷成功</div></div>
            <div class="stat-box"><div class="num" style="color:#cbb7ff">${current.transcoded}</div><div class="lbl">转码/分轨 FLAC</div></div>
            <div class="stat-box"><div class="num">${current.skipped}</div><div class="lbl">跳过已存在</div></div>
            <div class="stat-box"><div class="num" style="color:#9ec0ff">${current.covers_written}</div><div class="lbl">封面已写入</div></div>
            <div class="stat-box"><div class="num" style="color:#7ef0bf">${current.lyrics_written}</div><div class="lbl">LRC 歌词已导出</div></div>
            <div class="stat-box"><div class="num" style="color:var(--danger)">${current.failed}</div><div class="lbl">失败</div></div>
          </div>
          ${
            recentRows
              ? `<div class="track-table-wrap"><table class="track-table">
                  <thead><tr><th>最近处理曲目</th><th>结果</th><th>刮削资源写入</th></tr></thead>
                  <tbody>${recentRows}</tbody>
                </table></div>`
              : ""
          }
        </div>`;
    }

    const histRows = (history || [])
      .map(
        (h) => `
        <tr>
          <td><strong>${esc(h.label)}</strong><div style="font-size:11.5px;color:var(--text-muted)">${fmtTime(h.started_at)}</div></td>
          <td>${esc(h.device_label)}</td>
          <td>复制 ${h.copied} / 转码 ${h.transcoded} / 跳过 ${h.skipped} / 失败 ${h.failed}</td>
          <td>封面 ${h.covers_written} · 歌词 ${h.lyrics_written}</td>
          <td>${fmtBytes(h.transferred_bytes)}</td>
        </tr>`
      )
      .join("");

    root.innerHTML = `
      <div class="view-header">
        <div>
          <h1 class="view-title">传输任务与历史记录</h1>
          <div class="view-subtitle">实时查看音频、飞牛刮削封面与歌词的写入进度</div>
        </div>
      </div>
      ${curHTML}
      <h3 style="margin:20px 0 10px;font-size:15px">历史传输记录</h3>
      ${
        histRows
          ? `<div class="track-table-wrap"><table class="track-table">
              <thead><tr><th>任务</th><th>目标设备</th><th>曲目统计</th><th>封面与歌词</th><th>数据量</th></tr></thead>
              <tbody>${histRows}</tbody>
            </table></div>`
          : `<div class="empty-state"><h3>暂无历史记录</h3></div>`
      }`;

    const cBtn = root.querySelector("#btn-tr-cancel");
    if (cBtn) {
      cBtn.onclick = async () => {
        await api("/api/transfer/cancel", { method: "POST" });
        toast("正在取消传输...", "warn");
      };
    }
  }

  async function renderSettingsView() {
    const root = document.getElementById("content-area");
    const cfg = await api("/api/config");
    state.config = cfg;

    root.innerHTML = `
      <div class="view-header">
        <div>
          <h1 class="view-title">传输与 Echo 适配设置</h1>
          <div class="view-subtitle">所有修改仅作用于写入 Echo 的副本文件，绝不修改飞牛 NAS 原始曲库</div>
        </div>
        <button class="btn btn-primary" id="btn-save-cfg">保存设置</button>
      </div>

      <div class="settings-grid">
        <div class="settings-card">
          <h3>📁 目录组织与命名</h3>
          <div class="setting-row">
            <div><div class="setting-label">Echo 音乐目录结构</div><div class="setting-help">推荐使用「歌手 / 专辑 / 曲目」，最契合 Echo Cover Flow</div></div>
            <select id="cfg-folder_layout">
              <option value="artist_album" ${cfg.folder_layout === "artist_album" ? "selected" : ""}>Music/歌手/专辑/曲目</option>
              <option value="album" ${cfg.folder_layout === "album" ? "selected" : ""}>Music/专辑/曲目</option>
              <option value="preserve_rel" ${cfg.folder_layout === "preserve_rel" ? "selected" : ""}>保持 NAS 相对目录</option>
              <option value="flat" ${cfg.folder_layout === "flat" ? "selected" : ""}>扁平放入 Music/</option>
            </select>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">音乐根目录名</div><div class="setting-help">Echo 外挂盘内的音乐主目录</div></div>
            <input type="text" id="cfg-target_music_subdir" value="${esc(cfg.target_music_subdir)}" />
          </div>
          <div class="setting-row">
            <div><div class="setting-label">有声书自动归入 Audiobooks/</div><div class="setting-help">来自 /books 媒体库或有声书目录自动放入 Audiobooks，支持 AIYeon 断点续播</div></div>
            <label class="switch"><input type="checkbox" id="cfg-auto_route_audiobooks" ${cfg.auto_route_audiobooks ? "checked" : ""}/><span class="slider"></span></label>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">跳过 Echo 上已存在的同名文件</div><div class="setting-help">开启后重复同步只补齐缺失的封面和 .lrc 歌词，极速增量同步</div></div>
            <label class="switch"><input type="checkbox" id="cfg-skip_existing" ${cfg.skip_existing ? "checked" : ""}/><span class="slider"></span></label>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">歌单/喜欢自动生成 .m3u8</div><div class="setting-help">同步歌单或「我喜欢的」时自动在 Playlists/ 生成播放列表</div></div>
            <label class="switch"><input type="checkbox" id="cfg-generate_m3u" ${cfg.generate_m3u ? "checked" : ""}/><span class="slider"></span></label>
          </div>
        </div>

        <div class="settings-card">
          <h3>🎨 飞牛刮削封面与歌词（Echo 专优）</h3>
          <div class="setting-row">
            <div><div class="setting-label">内嵌标准 JPEG 封面到音频副本</div><div class="setting-help">自动把飞牛刮削的 WebP/PNG/大图转为基线 RGB JPEG 并内嵌</div></div>
            <label class="switch"><input type="checkbox" id="cfg-embed_cover" ${cfg.embed_cover ? "checked" : ""}/><span class="slider"></span></label>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">同时生成专辑目录 folder.jpg</div><div class="setting-help">双保险确保 Cover Flow 与专辑页封面秒显</div></div>
            <label class="switch"><input type="checkbox" id="cfg-write_folder_cover" ${cfg.write_folder_cover ? "checked" : ""}/><span class="slider"></span></label>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">封面最大边长 (px)</div><div class="setting-help">Echo 推荐 500px，兼顾清晰度与极速扫描</div></div>
            <select id="cfg-cover_max_size">
              <option value="300" ${cfg.cover_max_size == 300 ? "selected" : ""}>300 × 300 (极速)</option>
              <option value="500" ${cfg.cover_max_size == 500 ? "selected" : ""}>500 × 500 (推荐)</option>
              <option value="800" ${cfg.cover_max_size == 800 ? "selected" : ""}>800 × 800 (高清)</option>
              <option value="1000" ${cfg.cover_max_size == 1000 ? "selected" : ""}>1000 × 1000</option>
            </select>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">导出同名 .lrc 外置歌词</div><div class="setting-help">优先使用飞牛音乐用户偏好 / 云端刮削同步歌词，兼容 Echo 最高优先级读取</div></div>
            <label class="switch"><input type="checkbox" id="cfg-export_lrc" ${cfg.export_lrc ? "checked" : ""}/><span class="slider"></span></label>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">同时内嵌歌词到音频标签</div><div class="setting-help">写入 USLT / LYRICS / ©lyr 标签</div></div>
            <label class="switch"><input type="checkbox" id="cfg-embed_lyrics" ${cfg.embed_lyrics ? "checked" : ""}/><span class="slider"></span></label>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">写入飞牛刮削元数据标签</div><div class="setting-help">将飞牛音乐整理好的歌名、歌手、专辑、年份、曲目号写入 Echo 副本</div></div>
            <label class="switch"><input type="checkbox" id="cfg-write_scraped_tags" ${cfg.write_scraped_tags ? "checked" : ""}/><span class="slider"></span></label>
          </div>
        </div>

        <div class="settings-card">
          <h3>🎧 格式兼容与 CUE 整轨处理</h3>
          <div class="setting-row">
            <div><div class="setting-label">不支持格式自动无损转码 FLAC</div><div class="setting-help">自动用 ffmpeg 将 WMA / APE / DFF / AIFF / DTS 转为 Echo 原生支持的 FLAC</div></div>
            <label class="switch"><input type="checkbox" id="cfg-transcode_unsupported" ${cfg.transcode_unsupported ? "checked" : ""}/><span class="slider"></span></label>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">CUE 整轨曲目处理方式</div><div class="setting-help">自动分轨可单曲发送并独立携带封面歌词；整轨复制保留原音频 + .cue</div></div>
            <select id="cfg-cue_mode">
              <option value="split_flac" ${cfg.cue_mode === "split_flac" ? "selected" : ""}>自动无损分轨为独立 FLAC (推荐)</option>
              <option value="copy_cue" ${cfg.cue_mode === "copy_cue" ? "selected" : ""}>直接复制整轨音频 + .cue</option>
            </select>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">传输完成后自动刷盘 (sync)</div><div class="setting-help">防止 USB 拔线导致 FAT32/exFAT 文件未落盘</div></div>
            <label class="switch"><input type="checkbox" id="cfg-auto_sync_disk" ${cfg.auto_sync_disk ? "checked" : ""}/><span class="slider"></span></label>
          </div>
        </div>
      </div>`;

    root.querySelector("#btn-save-cfg").onclick = async () => {
      const updates = {
        folder_layout: root.querySelector("#cfg-folder_layout").value,
        target_music_subdir: root.querySelector("#cfg-target_music_subdir").value.trim() || "Music",
        auto_route_audiobooks: root.querySelector("#cfg-auto_route_audiobooks").checked,
        skip_existing: root.querySelector("#cfg-skip_existing").checked,
        generate_m3u: root.querySelector("#cfg-generate_m3u").checked,
        embed_cover: root.querySelector("#cfg-embed_cover").checked,
        write_folder_cover: root.querySelector("#cfg-write_folder_cover").checked,
        cover_max_size: Number(root.querySelector("#cfg-cover_max_size").value),
        export_lrc: root.querySelector("#cfg-export_lrc").checked,
        embed_lyrics: root.querySelector("#cfg-embed_lyrics").checked,
        write_scraped_tags: root.querySelector("#cfg-write_scraped_tags").checked,
        transcode_unsupported: root.querySelector("#cfg-transcode_unsupported").checked,
        cue_mode: root.querySelector("#cfg-cue_mode").value,
        auto_sync_disk: root.querySelector("#cfg-auto_sync_disk").checked,
      };
      try {
        state.config = await api("/api/config", { method: "POST", body: JSON.stringify({ updates }) });
        toast("设置已保存", "ok");
      } catch (e) {
        toast(e.message, "error");
      }
    };
  }

  // ---------- Track Detail Drawer ----------
  async function openTrackDetail(trackId) {
    const backdrop = document.getElementById("drawer-backdrop");
    const drawer = document.getElementById("detail-drawer");
    const body = document.getElementById("drawer-body");
    backdrop.classList.remove("hidden");
    drawer.classList.remove("hidden");
    body.innerHTML = `<div class="loading-state">正在读取飞牛刮削详情...</div>`;

    try {
      const t = await api(`/api/tracks/${trackId}?user_id=${state.userId}`);
      const cov = coverUrl(t.cover_type, t.cover_guid, 500);
      const streamUrl = `${API_BASE}/api/tracks/${trackId}/stream`;
      const coverInfo = t.cover_resolved
        ? `${t.cover_mime || "image"} (${fmtBytes(t.cover_size_bytes)}) → 发送时自动转为基线 JPEG`
        : "未刮削到封面";
      const lyricInfo = t.lyric
        ? `${t.lyric.source_label}${t.lyric.is_synced ? " · 含时间轴 LRC" : " · 纯文本歌词"}`
        : "飞牛音乐暂无此曲歌词";

      body.innerHTML = `
        ${cov ? `<img class="drawer-cover" src="${cov}" alt="cover" />` : `<div class="drawer-cover" style="display:flex;align-items:center;justify-content:center;font-size:48px;color:var(--text-muted)">♪</div>`}
        <div style="text-align:center;margin-bottom:12px">
          <div style="font-size:17px;font-weight:700">${esc(t.title)}</div>
          <div style="font-size:13px;color:var(--text-dim);margin-top:2px">${esc(t.artist)} · ${esc(t.album || "未知专辑")}</div>
        </div>
        <div style="display:flex;gap:8px;justify-content:center;margin-bottom:12px">
          <button class="btn btn-primary" id="drawer-send-btn">🚀 发送这首歌到 Echo</button>
        </div>
        ${t.file_exists ? `<audio class="drawer-audio" controls preload="none" src="${streamUrl}"></audio>` : ""}
        <div class="meta-kv">
          <div class="meta-k">格式规格</div>
          <div class="meta-v">${esc(t.suffix.toUpperCase())} · ${t.bit_depth ? t.bit_depth + "bit / " : ""}${t.sample_rate ? (t.sample_rate / 1000).toFixed(1) + "kHz · " : ""}${fmtBytes(t.size)}</div>
          <div class="meta-k">Echo 兼容</div>
          <div class="meta-v">${t.compat === "native" ? `<span class="tag tag-ok">原生直接播放</span>` : `<span class="tag tag-warn">自动无损转码 FLAC</span>`}${t.is_cue ? ` <span class="tag tag-blue">CUE 分轨 (${esc(t.start_offset)} - ${esc(t.end_offset)})</span>` : ""}</div>
          <div class="meta-k">专辑封面</div>
          <div class="meta-v">${esc(coverInfo)}</div>
          <div class="meta-k">刮削歌词</div>
          <div class="meta-v">${esc(lyricInfo)}</div>
          <div class="meta-k">NAS 路径</div>
          <div class="meta-v" style="font-size:11.5px;color:var(--text-dim)">${esc(t.path)}</div>
        </div>
        <div style="font-weight:600;font-size:13px;margin-bottom:6px">歌词预览</div>
        <div class="lyric-box">${t.lyric ? esc(t.lyric.content) : "暂无歌词内容"}</div>`;

      body.querySelector("#drawer-send-btn").onclick = () => {
        startTransfer({ track_ids: [t.id], label: `发送《${t.title}》` });
      };
    } catch (e) {
      body.innerHTML = `<div class="empty-state"><h3>读取失败</h3><p>${esc(e.message)}</p></div>`;
    }
  }

  function closeDrawer() {
    document.getElementById("drawer-backdrop").classList.add("hidden");
    document.getElementById("detail-drawer").classList.add("hidden");
    const audio = document.querySelector("#detail-drawer audio");
    if (audio) audio.pause();
  }

  // ---------- Dedup, Organize & Recycle Views ----------
  async function refreshRecycleBadge() {
    try {
      const data = await api("/api/recycle");
      const totalFiles = (data.batches || []).reduce((acc, b) => acc + (b.file_count || 0), 0);
      const badge = document.getElementById("badge-recycle");
      if (badge) {
        badge.textContent = String(totalFiles);
        badge.classList.toggle("hidden", totalFiles === 0);
      }
    } catch (_) {}
  }

  async function renderDedupView() {
    const root = document.getElementById("content-area");
    root.innerHTML = `<div class="loading-state">正在扫描飞牛音乐曲库重复文件...</div>`;

    const libs = state.overview?.libraries || [];
    const params = new URLSearchParams({
      mode: state.dedupMode,
      duration_tolerance_sec: String(state.dedupTolerance),
      cross_format_only: String(state.dedupCrossFormat),
      q: state.search,
      user_id: String(state.userId),
      page: String(state.dedupPage),
      page_size: "25",
    });
    if (state.dedupLibId) params.set("library_id", String(state.dedupLibId));

    try {
      const data = await api(`/api/dedup/scan?${params.toString()}`);
      state.dedupGroups = data.groups || [];
      // Default-select all non-recommended duplicates on this page
      state.dedupChecked = new Set();
      for (const g of state.dedupGroups) {
        for (const it of g.items) {
          if (!it.recommended_keep) state.dedupChecked.add(it.path);
        }
      }

      const modeDesc = {
        exact_hash: "直接比对飞牛音乐计算的音频文件精确哈希（完全一致的副本），100% 零误伤。",
        smart_song: "按「歌名 + 歌手 + 时长校验」智能聚合跨格式/多码率重复曲目，自动过滤垃圾标签，默认推荐保留 DSD / FLAC 等最高音质版本。",
        same_filename: "排查分散在不同文件夹中的同名音频文件，自动推荐保留最高音质版本。",
      }[state.dedupMode];

      const groupsHTML = state.dedupGroups.length
        ? state.dedupGroups
            .map((g, gIdx) => {
              const rows = g.items
                .map((it) => {
                  const isDel = state.dedupChecked.has(it.path);
                  return `
                  <div class="dup-item-row ${it.recommended_keep ? "keep-row" : isDel ? "del-row" : ""}" data-gidx="${gIdx}">
                    <input type="checkbox" class="dup-cb" data-gidx="${gIdx}" data-path="${esc(it.path)}" ${isDel ? "checked" : ""} />
                    <div style="flex:1;min-width:0">
                      <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
                        ${
                          it.recommended_keep
                            ? `<span class="tag tag-ok">★ 推荐保留</span>`
                            : `<span class="tag tag-warn">待清理副本</span>`
                        }
                        <span class="tag tag-hi">${esc(it.quality_label)}</span>
                        <span style="font-size:12.5px;font-weight:600">${esc(it.file_name)}</span>
                        ${it.has_cover ? `<span class="tag tag-ok">封面</span>` : ""}
                        ${it.has_lyric ? `<span class="tag tag-ok">歌词</span>` : ""}
                        ${it.is_favorite ? `<span class="tag tag-fav">♥</span>` : ""}
                      </div>
                      <div class="dup-path" title="${esc(it.path)}">${esc(it.path)}</div>
                    </div>
                    <div style="text-align:right;white-space:nowrap;font-size:12px;color:var(--text-dim)">
                      <div><strong>${fmtBytes(it.size)}</strong></div>
                      <div>${fmtDur(it.duration_ms)}</div>
                    </div>
                    <button class="btn btn-ghost btn-sm" data-dup-detail="${it.id}">试听</button>
                  </div>`;
                })
                .join("");

              return `
              <div class="dup-group">
                <div class="dup-group-head">
                  <div>
                    <span class="dup-group-title">${esc(g.title)}</span>
                    <span style="color:var(--text-dim);margin-left:8px;font-size:12.5px">${esc(g.artist)}</span>
                  </div>
                  <div class="dup-group-meta">
                    <span class="tag tag-blue">${g.count} 个副本</span>
                    ${g.is_cross_format ? `<span class="tag tag-hi">跨格式: ${esc(g.formats.join(" / ").toUpperCase())}</span>` : ""}
                    <span>可释放 <strong>${fmtBytes(g.reclaimable_bytes)}</strong></span>
                  </div>
                </div>
                <div>${rows}</div>
              </div>`;
            })
            .join("")
        : `<div class="empty-state"><h3>🎉 未发现符合条件的重复文件</h3><p>当前曲库非常干净，或尝试切换去重模式/媒体库筛选</p></div>`;

      root.innerHTML = `
        <div class="view-header">
          <div>
            <h1 class="view-title">曲库智能去重</h1>
            <div class="view-subtitle">所有清理默认移入「安全回收站 (.echo_recycle)」，支持随时一键原路恢复；清理完成后记得在飞牛音乐点「重新扫描」刷新索引</div>
          </div>
        </div>

        <div class="sub-tabs">
          <button class="sub-tab ${state.dedupMode === "exact_hash" ? "active" : ""}" data-dmode="exact_hash">🎯 精确哈希去重（零误伤）</button>
          <button class="sub-tab ${state.dedupMode === "smart_song" ? "active" : ""}" data-dmode="smart_song">🎵 同曲多音质去重（保留无损）</button>
          <button class="sub-tab ${state.dedupMode === "same_filename" ? "active" : ""}" data-dmode="same_filename">📄 同名文件排查</button>
        </div>

        <div class="filter-bar">
          <select id="dedup-lib">
            <option value="">全部媒体库</option>
            ${libs.map((l) => `<option value="${l.id}" ${state.dedupLibId === l.id ? "selected" : ""}>${esc(l.path)}</option>`).join("")}
          </select>
          ${
            state.dedupMode === "smart_song"
              ? `<select id="dedup-tol" title="时长误差容忍度">
                  <option value="2" ${state.dedupTolerance === 2 ? "selected" : ""}>时长误差 ≤ 2秒 (严格)</option>
                  <option value="3" ${state.dedupTolerance === 3 ? "selected" : ""}>时长误差 ≤ 3秒 (推荐)</option>
                  <option value="5" ${state.dedupTolerance === 5 ? "selected" : ""}>时长误差 ≤ 5秒 (宽松)</option>
                </select>`
              : ""
          }
          ${
            state.dedupMode !== "exact_hash"
              ? `<button class="chip ${state.dedupCrossFormat ? "active" : ""}" id="dedup-cross">仅看跨格式（如 FLAC + MP3）</button>`
              : ""
          }
          <span style="font-size:12px;color:var(--text-muted);margin-left:auto">${esc(modeDesc)}</span>
        </div>

        <div class="hero-banner">
          <div>
            <div class="hero-title">共发现 ${data.total_groups.toLocaleString()} 组重复 · ${data.total_duplicate_files.toLocaleString()} 个可清理副本</div>
            <div class="hero-desc">按推荐策略保留最高音质版本，预计可释放空间 <strong>${fmtBytes(data.total_reclaimable_bytes)}</strong>（每组均强制至少保留 1 份，不会误删全部）</div>
          </div>
          <div class="view-actions">
            <button class="btn btn-danger" id="btn-dedup-clean-page" ${data.total_groups === 0 ? "disabled" : ""}>移入回收站（本页已选 <span id="dedup-page-cnt">${state.dedupChecked.size}</span> 个）</button>
            <button class="btn btn-primary" id="btn-dedup-clean-all" ${data.total_duplicate_files === 0 ? "disabled" : ""}>⚡ 一键按推荐策略清理全部 (${data.total_duplicate_files})</button>
          </div>
        </div>

        ${groupsHTML}`;

      if (data.total_groups > 25) {
        root.appendChild(
          renderPagination(data.total_groups, data.page, 25, (p) => {
            state.dedupPage = p;
            renderDedupView();
          })
        );
      }

      // Bind mode tabs & filters
      root.querySelectorAll("[data-dmode]").forEach((btn) => {
        btn.onclick = () => {
          state.dedupMode = btn.dataset.dmode;
          state.dedupPage = 1;
          renderDedupView();
        };
      });
      root.querySelector("#dedup-lib").onchange = (e) => {
        state.dedupLibId = e.target.value ? Number(e.target.value) : null;
        state.dedupPage = 1;
        renderDedupView();
      };
      const tolEl = root.querySelector("#dedup-tol");
      if (tolEl) {
        tolEl.onchange = () => {
          state.dedupTolerance = Number(tolEl.value);
          state.dedupPage = 1;
          renderDedupView();
        };
      }
      const crossEl = root.querySelector("#dedup-cross");
      if (crossEl) {
        crossEl.onclick = () => {
          state.dedupCrossFormat = !state.dedupCrossFormat;
          state.dedupPage = 1;
          renderDedupView();
        };
      }

      // Guardrail: never allow checking ALL items in one duplicate group
      root.querySelectorAll(".dup-cb").forEach((cb) => {
        cb.addEventListener("change", () => {
          const gIdx = Number(cb.dataset.gidx);
          const group = state.dedupGroups[gIdx];
          const path = cb.dataset.path;
          if (cb.checked) {
            const checkedInGroup = group.items.filter((x) => x.path === path || state.dedupChecked.has(x.path)).length;
            if (checkedInGroup >= group.items.length) {
              cb.checked = false;
              toast("安全保护：每组重复歌曲必须至少保留 1 个版本，不能全部删除！", "warn");
              return;
            }
            state.dedupChecked.add(path);
            cb.closest(".dup-item-row")?.classList.add("del-row");
          } else {
            state.dedupChecked.delete(path);
            cb.closest(".dup-item-row")?.classList.remove("del-row");
          }
          const cntEl = root.querySelector("#dedup-page-cnt");
          if (cntEl) cntEl.textContent = String(state.dedupChecked.size);
        });
      });

      root.querySelectorAll("[data-dup-detail]").forEach((btn) => {
        btn.onclick = () => openTrackDetail(Number(btn.dataset.dupDetail));
      });

      root.querySelector("#btn-dedup-clean-page").onclick = async () => {
        const paths = Array.from(state.dedupChecked);
        if (!paths.length) {
          toast("请先勾选要清理的副本", "warn");
          return;
        }
        if (!confirm(`确定将本页选中的 ${paths.length} 个重复文件移入安全回收站吗？\n（可随时在「安全回收站」一键原路恢复）`)) return;
        try {
          const res = await api("/api/dedup/clean", {
            method: "POST",
            body: JSON.stringify({ file_paths: paths, reason: `去重清理 (${state.dedupMode})`, include_sidecar_lrc: true }),
          });
          toast(`已将 ${res.moved_count} 个文件移入安全回收站（释放 ${fmtBytes(res.reclaimed_bytes)}）`, "ok");
          refreshRecycleBadge();
          renderDedupView();
        } catch (e) {
          toast(e.message, "error");
        }
      };

      root.querySelector("#btn-dedup-clean-all").onclick = async () => {
        if (
          !confirm(
            `即将把当前筛选条件下全部 ${data.total_groups} 组中的 ${data.total_duplicate_files} 个非推荐副本（约 ${fmtBytes(data.total_reclaimable_bytes)}）移入安全回收站。\n\n每组最高音质版本将完整保留，且随时可一键原路恢复。确定继续吗？`
          )
        )
          return;
        try {
          const res = await api("/api/dedup/clean-all", {
            method: "POST",
            body: JSON.stringify({
              mode: state.dedupMode,
              library_id: state.dedupLibId,
              duration_tolerance_sec: state.dedupTolerance,
              cross_format_only: state.dedupCrossFormat,
              q: state.search,
              user_id: state.userId,
              include_sidecar_lrc: true,
            }),
          });
          toast(`一键去重完成！已移入安全回收站 ${res.moved_count} 个文件，释放 ${fmtBytes(res.reclaimed_bytes)}`, "ok");
          refreshRecycleBadge();
          renderDedupView();
        } catch (e) {
          toast(e.message, "error");
        }
      };
    } catch (e) {
      root.innerHTML = `<div class="empty-state"><h3>扫描去重失败</h3><p>${esc(e.message)}</p></div>`;
    }
  }

  async function pollOrganizeStatus() {
    try {
      const st = await api("/api/organize/status");
      const wasRunning = state.orgJob?.status === "running";
      state.orgJob = st;
      if (wasRunning && st.status !== "running") {
        toast(`整理任务已${st.status === "completed" ? "完成" : "取消"}：成功 ${st.succeeded} / 跳过 ${st.skipped} / 失败 ${st.failed}`, st.status === "completed" ? "ok" : "warn");
      }
      if (state.view === "organize") {
        const box = document.getElementById("org-job-box");
        if (box) box.innerHTML = renderOrgJobBoxHTML(st);
        const cBtn = document.getElementById("btn-org-cancel");
        if (cBtn) cBtn.onclick = async () => { await api("/api/organize/cancel", { method: "POST" }); toast("正在取消整理任务...", "warn"); };
      }
    } catch (_) {}
  }

  function renderOrgJobBoxHTML(st) {
    if (!st || st.status === "idle") return "";
    const pct = st.total > 0 ? Math.round((st.processed / st.total) * 100) : 0;
    const isRun = st.status === "running";
    return `
      <div class="transfer-card" style="border-color:rgba(91,140,255,0.4)">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:10px">
          <div>
            <strong>${esc(st.label)}</strong>
            <span class="tag ${isRun ? "tag-blue" : st.status === "completed" ? "tag-ok" : "tag-warn"}" style="margin-left:6px">${isRun ? "执行中" : st.status === "completed" ? "已完成" : "已取消"}</span>
          </div>
          ${isRun ? `<button class="btn btn-danger btn-sm" id="btn-org-cancel">取消任务</button>` : ""}
        </div>
        <div class="disk-bar" style="height:8px;margin:10px 0 6px"><div class="disk-fill" style="width:${pct}%"></div></div>
        <div style="font-size:12px;color:var(--text-dim);display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px">
          <span>进度 ${st.processed} / ${st.total} (${pct}%) · 成功 ${st.succeeded} · 跳过 ${st.skipped} · 失败 ${st.failed}</span>
          <span>${st.current_item ? `当前：${esc(st.current_item)}` : (st.result && Object.keys(st.result).length ? esc(JSON.stringify(st.result)) : "")}</span>
        </div>
      </div>`;
  }

  async function renderOrganizeView() {
    const root = document.getElementById("content-area");
    const libs = state.overview?.libraries || [];
    if (!libs.length) {
      root.innerHTML = `<div class="empty-state"><h3>未检测到飞牛音乐媒体库</h3></div>`;
      return;
    }
    if (!state.orgLibId) state.orgLibId = libs[0].id;
    const libOpts = libs
      .map((l) => `<option value="${l.id}" ${state.orgLibId === l.id ? "selected" : ""}>${esc(l.path)} (${l.track_count} 首)</option>`)
      .join("");

    let tabBody = "";
    if (state.orgTab === "archive") {
      let previewHTML = "";
      if (state.orgPreview) {
        const pv = state.orgPreview;
        const rows = (pv.samples || [])
          .map(
            (s) => `
            <tr>
              <td style="font-size:12px;color:var(--text-dim);word-break:break-all">${esc(s.src_rel)}</td>
              <td class="preview-arrow">→</td>
              <td style="font-size:12px;color:#7ef0bf;word-break:break-all">${esc(s.dst_rel)}</td>
            </tr>`
          )
          .join("");
        previewHTML = `
          <div class="transfer-card">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap">
              <div>
                <strong>归档预览计划</strong>
                <div style="font-size:12px;color:var(--text-dim);margin-top:2px">扫描 ${pv.total_scanned} 首 · 待整理移动 <strong>${pv.to_move}</strong> 首 · 已规范无需动 ${pv.already_clean} 首 · CUE跳过 ${pv.skipped_cue} · 同名冲突跳过 ${pv.conflicts}</div>
              </div>
              <button class="btn btn-primary" id="btn-org-exec" ${pv.to_move === 0 ? "disabled" : ""}>确认执行归档 (${pv.to_move} 首)</button>
            </div>
            ${
              rows
                ? `<div class="track-table-wrap" style="margin-top:12px;max-height:420px;overflow-y:auto"><table class="track-table">
                    <thead><tr><th>原相对路径</th><th></th><th>整理后目标路径</th></tr></thead>
                    <tbody>${rows}</tbody>
                  </table></div>`
                : `<div style="margin-top:12px;color:var(--text-dim)">所选目录内所有曲目均已符合目标规范，无需移动。</div>`
            }
          </div>`;
      }

      tabBody = `
        <div class="settings-card" style="margin-bottom:16px">
          <h3>📂 目录规范归档（先预览、后移动）</h3>
          <div class="setting-row">
            <div><div class="setting-label">目标媒体库</div><div class="setting-help">仅在选定的飞牛音乐媒体库内部进行目录归整</div></div>
            <select id="org-lib">${libOpts}</select>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">限定子目录（可选）</div><div class="setting-help">留空整理整个库；建议先填一个杂乱子目录名试用，例如「待整理」</div></div>
            <input type="text" id="org-subpath" placeholder="如：待整理/老歌" value="${esc(state.orgSubpath)}" />
          </div>
          <div class="setting-row">
            <div><div class="setting-label">归档目录规则</div><div class="setting-help">按飞牛音乐刮削好的歌手和专辑自动建文件夹</div></div>
            <select id="org-layout">
              <option value="artist_album" ${state.orgLayout === "artist_album" ? "selected" : ""}>歌手 / 专辑 / 文件</option>
              <option value="album" ${state.orgLayout === "album" ? "selected" : ""}>专辑 / 文件</option>
            </select>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">同时规范化文件名</div><div class="setting-help">重命名为「曲目号 歌名.ext」，关闭则保留原始文件名</div></div>
            <label class="switch"><input type="checkbox" id="org-rename" ${state.orgRename ? "checked" : ""}/><span class="slider"></span></label>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">归档时顺带补齐 folder.jpg 与 .lrc</div><div class="setting-help">从飞牛音乐导出刮削封面与同步歌词到整理后的专辑目录</div></div>
            <label class="switch"><input type="checkbox" id="org-export-move" ${state.orgExportWithMove ? "checked" : ""}/><span class="slider"></span></label>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">移动完成后自动删除变空的旧目录</div><div class="setting-help">保持曲库清爽无空壳文件夹</div></div>
            <label class="switch"><input type="checkbox" id="org-clean-empty" ${state.orgCleanEmpty ? "checked" : ""}/><span class="slider"></span></label>
          </div>
          <div style="margin-top:14px;display:flex;gap:10px">
            <button class="btn btn-primary" id="btn-org-preview">🔍 生成归档预览计划</button>
          </div>
        </div>
        ${previewHTML}`;
    } else if (state.orgTab === "junk") {
      let healthHTML = "";
      if (state.orgHealth) {
        const h = state.orgHealth;
        healthHTML = `
          <div class="transfer-card">
            <div class="stat-grid">
              <div class="stat-box"><div class="num" style="color:var(--warn)">${h.junk_count}</div><div class="lbl">系统垃圾 (._* / .DS_Store) · ${fmtBytes(h.junk_bytes)}</div></div>
              <div class="stat-box"><div class="num" style="color:#9ec0ff">${h.empty_dir_count}</div><div class="lbl">空文件夹</div></div>
              <div class="stat-box"><div class="num">${h.orphan_lrc_count}</div><div class="lbl">孤立 .lrc（无对应音频）</div></div>
              <div class="stat-box"><div class="num" style="color:var(--text-dim)">${h.missing_db_files}</div><div class="lbl">飞牛待刷新失效索引</div></div>
            </div>
            <div style="display:flex;gap:16px;flex-wrap:wrap;margin:12px 0;font-size:13px">
              <label><input type="checkbox" id="jk-files" checked /> 清理系统垃圾文件 (${h.junk_count})</label>
              <label><input type="checkbox" id="jk-dirs" checked /> 清理空文件夹 (${h.empty_dir_count})</label>
              <label><input type="checkbox" id="jk-lrc" /> 清理孤立 .lrc (${h.orphan_lrc_count})</label>
            </div>
            <button class="btn btn-danger" id="btn-jk-run">🧹 立即执行清理</button>
            ${
              h.empty_dir_samples.length
                ? `<div style="margin-top:14px;font-size:12px;color:var(--text-dim)"><strong>空目录示例：</strong><div class="lyric-box" style="margin-top:6px;max-height:150px">${esc(h.empty_dir_samples.join("\n"))}</div></div>`
                : ""
            }
            ${
              h.junk_samples.length
                ? `<div style="margin-top:12px;font-size:12px;color:var(--text-dim)"><strong>垃圾文件示例：</strong><div class="lyric-box" style="margin-top:6px;max-height:150px">${esc(h.junk_samples.map((x) => x.rel).join("\n"))}</div></div>`
                : ""
            }
          </div>`;
      }
      tabBody = `
        <div class="settings-card" style="margin-bottom:16px">
          <h3>✨ 杂物与空目录体检清理</h3>
          <div class="setting-row">
            <div><div class="setting-label">选择体检媒体库</div><div class="setting-help">快速找出 macOS 拷贝残留的 ._*、.DS_Store、Thumbs.db、空文件夹与孤立歌词</div></div>
            <div style="display:flex;gap:8px">
              <select id="jk-lib">${libOpts}</select>
              <button class="btn btn-primary" id="btn-jk-scan">开始体检扫描</button>
            </div>
          </div>
        </div>
        ${healthHTML}`;
    } else {
      tabBody = `
        <div class="settings-card">
          <h3>🎨 将飞牛刮削的封面与歌词补齐到 NAS 曲库</h3>
          <div class="setting-row">
            <div><div class="setting-label">目标媒体库</div><div class="setting-help">把飞牛音乐数据库里的刮削封面和 LRC 导出到真实音乐文件夹，方便 Emby / Navidrome / 任意播放器直接读取</div></div>
            <select id="exp-lib">${libOpts}</select>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">限定子目录（可选）</div><div class="setting-help">留空处理整个库；可填单个歌手或专辑目录先试用</div></div>
            <input type="text" id="exp-subpath" placeholder="如：孙燕姿" value="${esc(state.orgSubpath)}" />
          </div>
          <div class="setting-row">
            <div><div class="setting-label">在专辑目录生成标准 JPEG folder.jpg</div><div class="setting-help">自动把 WebP 转基线 JPEG，不会动原音频文件</div></div>
            <label class="switch"><input type="checkbox" id="exp-jpg" checked /><span class="slider"></span></label>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">在音频旁生成同名 UTF-8 .lrc 歌词</div><div class="setting-help">优先使用飞牛音乐云端刮削同步歌词</div></div>
            <label class="switch"><input type="checkbox" id="exp-lrc" checked /><span class="slider"></span></label>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">同时把封面/标签/歌词内嵌写入 NAS 音频文件（谨慎）</div><div class="setting-help">默认关闭以保持 NAS 原始音频零改动；开启则会修改音频文件标签</div></div>
            <label class="switch"><input type="checkbox" id="exp-embed" /><span class="slider"></span></label>
          </div>
          <div class="setting-row">
            <div><div class="setting-label">覆盖已存在的 folder.jpg / .lrc</div><div class="setting-help">关闭则仅补齐缺失文件</div></div>
            <label class="switch"><input type="checkbox" id="exp-overwrite" /><span class="slider"></span></label>
          </div>
          <div style="margin-top:14px">
            <button class="btn btn-primary" id="btn-exp-run">✨ 开始补齐刮削资源</button>
          </div>
        </div>`;
    }

    root.innerHTML = `
      <div class="view-header">
        <div>
          <h1 class="view-title">曲库整理工具箱</h1>
          <div class="view-subtitle">目录规范归档、macOS 残留与空文件夹清理、飞牛刮削封面/歌词一键回补</div>
        </div>
      </div>
      <div class="sub-tabs">
        <button class="sub-tab ${state.orgTab === "archive" ? "active" : ""}" data-otab="archive">📂 目录规范归档</button>
        <button class="sub-tab ${state.orgTab === "junk" ? "active" : ""}" data-otab="junk">✨ 杂物与空目录清理</button>
        <button class="sub-tab ${state.orgTab === "export" ? "active" : ""}" data-otab="export">🎨 补齐刮削封面与歌词到 NAS</button>
      </div>
      <div id="org-job-box">${renderOrgJobBoxHTML(state.orgJob)}</div>
      ${tabBody}`;

    root.querySelectorAll("[data-otab]").forEach((b) => {
      b.onclick = () => {
        state.orgTab = b.dataset.otab;
        renderOrganizeView();
      };
    });

    if (state.orgTab === "archive") {
      root.querySelector("#btn-org-preview").onclick = async () => {
        state.orgLibId = Number(root.querySelector("#org-lib").value);
        state.orgSubpath = root.querySelector("#org-subpath").value.trim();
        state.orgLayout = root.querySelector("#org-layout").value;
        state.orgRename = root.querySelector("#org-rename").checked;
        state.orgExportWithMove = root.querySelector("#org-export-move").checked;
        state.orgCleanEmpty = root.querySelector("#org-clean-empty").checked;
        try {
          toast("正在生成归档预览...", "info");
          const p = new URLSearchParams({
            library_id: String(state.orgLibId),
            rel_subpath: state.orgSubpath,
            layout: state.orgLayout,
            rename_files: String(state.orgRename),
          });
          state.orgPreview = await api(`/api/organize/preview?${p.toString()}`);
          renderOrganizeView();
        } catch (e) {
          toast(e.message, "error");
        }
      };
      const execBtn = root.querySelector("#btn-org-exec");
      if (execBtn) {
        execBtn.onclick = async () => {
          if (!confirm(`确定按预览计划移动整理 ${state.orgPreview.to_move} 首曲目吗？\n完成后请在飞牛音乐点击「重新扫描」更新曲库路径。`)) return;
          try {
            await api("/api/organize/execute", {
              method: "POST",
              body: JSON.stringify({
                library_id: state.orgLibId,
                rel_subpath: state.orgSubpath,
                layout: state.orgLayout,
                rename_files: state.orgRename,
                export_scraped_assets: state.orgExportWithMove,
                clean_empty_dirs: state.orgCleanEmpty,
                user_id: state.userId,
              }),
            });
            state.orgPreview = null;
            toast("已启动后台目录归档任务", "ok");
            pollOrganizeStatus();
          } catch (e) {
            toast(e.message, "error");
          }
        };
      }
    } else if (state.orgTab === "junk") {
      root.querySelector("#btn-jk-scan").onclick = async () => {
        state.orgLibId = Number(root.querySelector("#jk-lib").value);
        try {
          toast("正在扫描目录健康状况...", "info");
          state.orgHealth = await api(`/api/organize/health?library_id=${state.orgLibId}`);
          renderOrganizeView();
        } catch (e) {
          toast(e.message, "error");
        }
      };
      const runBtn = root.querySelector("#btn-jk-run");
      if (runBtn) {
        runBtn.onclick = async () => {
          if (!confirm("确定清理所选的系统垃圾文件和空目录吗？")) return;
          try {
            const res = await api("/api/organize/clean-junk", {
              method: "POST",
              body: JSON.stringify({
                library_id: state.orgLibId,
                remove_junk_files: root.querySelector("#jk-files").checked,
                remove_empty_dirs: root.querySelector("#jk-dirs").checked,
                remove_orphan_lrcs: root.querySelector("#jk-lrc").checked,
              }),
            });
            toast(`清理完成！删除垃圾文件 ${res.removed_junk} 个、空目录 ${res.removed_empty_dirs} 个、孤立歌词 ${res.removed_orphan_lrcs} 个`, "ok");
            state.orgHealth = await api(`/api/organize/health?library_id=${state.orgLibId}`);
            renderOrganizeView();
          } catch (e) {
            toast(e.message, "error");
          }
        };
      }
    } else {
      root.querySelector("#btn-exp-run").onclick = async () => {
        state.orgLibId = Number(root.querySelector("#exp-lib").value);
        state.orgSubpath = root.querySelector("#exp-subpath").value.trim();
        const embed = root.querySelector("#exp-embed").checked;
        if (embed && !confirm("你已开启「内嵌写入 NAS 音频文件标签」，这会修改音频文件本身。确定继续吗？")) return;
        try {
          await api("/api/organize/export-assets", {
            method: "POST",
            body: JSON.stringify({
              library_id: state.orgLibId,
              rel_subpath: state.orgSubpath,
              export_folder_jpg: root.querySelector("#exp-jpg").checked,
              export_lrc: root.querySelector("#exp-lrc").checked,
              embed_into_audio: embed,
              overwrite: root.querySelector("#exp-overwrite").checked,
              user_id: state.userId,
            }),
          });
          toast("已启动后台刮削资源补齐任务", "ok");
          pollOrganizeStatus();
        } catch (e) {
          toast(e.message, "error");
        }
      };
    }
  }

  async function renderRecycleView() {
    const root = document.getElementById("content-area");
    root.innerHTML = `<div class="loading-state">正在读取安全回收站...</div>`;
    try {
      const data = await api("/api/recycle");
      const batches = data.batches || [];
      refreshRecycleBadge();

      const totalFiles = batches.reduce((s, b) => s + b.file_count, 0);
      const totalBytes = batches.reduce((s, b) => s + b.total_bytes, 0);

      const cards = batches.length
        ? batches
            .map(
              (b) => `
            <div class="transfer-card">
              <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap">
                <div>
                  <strong>${esc(b.reason)}</strong>
                  <span class="tag tag-blue" style="margin-left:6px">${b.file_count} 个文件 · ${fmtBytes(b.total_bytes)}</span>
                  <div style="font-size:12px;color:var(--text-muted);margin-top:2px">批次 ${esc(b.batch_id)} · ${fmtTime(b.created_at)}</div>
                </div>
                <div class="view-actions">
                  <button class="btn btn-ok btn-sm" data-rec-restore="${esc(b.batch_id)}">↩ 一键原路恢复</button>
                  <button class="btn btn-danger btn-sm" data-rec-purge="${esc(b.batch_id)}">彻底删除</button>
                </div>
              </div>
              <div class="lyric-box" style="margin-top:10px;max-height:140px;font-size:11.5px">${esc((b.sample_items || []).map((x) => x.orig_path).join("\n"))}</div>
            </div>`
            )
            .join("")
        : `<div class="empty-state"><h3>♻ 安全回收站为空</h3><p>通过「智能去重」清理的文件会先安全暂存在各媒体库的 .echo_recycle 目录，确认无误后再彻底删除释放磁盘空间</p></div>`;

      root.innerHTML = `
        <div class="view-header">
          <div>
            <h1 class="view-title">安全回收站</h1>
            <div class="view-subtitle">当前暂存 ${totalFiles} 个文件，占用 ${fmtBytes(totalBytes)} · 随时可一键原路恢复</div>
          </div>
          ${batches.length ? `<button class="btn btn-danger" id="btn-rec-purge-all">彻底清空全部回收站 (${fmtBytes(totalBytes)})</button>` : ""}
        </div>
        ${cards}`;

      const purgeAll = root.querySelector("#btn-rec-purge-all");
      if (purgeAll) {
        purgeAll.onclick = async () => {
          if (!confirm(`确定永久删除回收站内全部 ${totalFiles} 个文件并释放 ${fmtBytes(totalBytes)} 空间吗？此操作不可撤销！`)) return;
          const res = await api("/api/recycle/purge", { method: "POST", body: JSON.stringify({}) });
          toast(`已彻底删除 ${res.deleted_files} 个文件，释放 ${fmtBytes(res.freed_bytes)}`, "ok");
          renderRecycleView();
        };
      }
      root.querySelectorAll("[data-rec-restore]").forEach((btn) => {
        btn.onclick = async () => {
          const bid = btn.dataset.recRestore;
          const res = await api("/api/recycle/restore", { method: "POST", body: JSON.stringify({ batch_id: bid }) });
          toast(`已原路恢复 ${res.restored} 个文件`, "ok");
          renderRecycleView();
        };
      });
      root.querySelectorAll("[data-rec-purge]").forEach((btn) => {
        btn.onclick = async () => {
          const bid = btn.dataset.recPurge;
          if (!confirm("确定永久删除该批次文件以释放磁盘空间吗？此操作不可撤销！")) return;
          const res = await api("/api/recycle/purge", { method: "POST", body: JSON.stringify({ batch_id: bid }) });
          toast(`已彻底删除 ${res.deleted_files} 个文件，释放 ${fmtBytes(res.freed_bytes)}`, "ok");
          renderRecycleView();
        };
      });
    } catch (e) {
      root.innerHTML = `<div class="empty-state"><h3>读取失败</h3><p>${esc(e.message)}</p></div>`;
    }
  }

  // ---------- View Router ----------
  function switchView(view) {
    state.view = view;
    state.page = 1;
    if (view !== "albums") state.activeAlbum = null;
    if (view !== "artists") state.activeArtist = null;
    if (view !== "playlists") state.activePlaylist = null;

    document.querySelectorAll(".nav-item").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === view);
    });

    if (view === "tracks" || view === "favorites") renderTracksView();
    else if (view === "albums") renderAlbumsView();
    else if (view === "artists") renderArtistsView();
    else if (view === "playlists") renderPlaylistsView();
    else if (view === "folders") renderFoldersView();
    else if (view === "dedup") renderDedupView();
    else if (view === "organize") renderOrganizeView();
    else if (view === "recycle") renderRecycleView();
    else if (view === "device") renderDeviceView();
    else if (view === "transfers") renderTransfersView();
    else if (view === "settings") renderSettingsView();
  }

  // ---------- Event Wiring ----------
  function init() {
    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.activeAlbum = null;
        state.activeArtist = null;
        state.activePlaylist = null;
        switchView(btn.dataset.view);
      });
    });

    const searchInput = document.getElementById("global-search");
    const clearSearch = document.getElementById("clear-search");
    let searchDebounce = null;
    searchInput.addEventListener("input", () => {
      state.search = searchInput.value.trim();
      clearSearch.classList.toggle("hidden", !state.search);
      clearTimeout(searchDebounce);
      searchDebounce = setTimeout(() => {
        state.page = 1;
        if (["tracks", "favorites", "albums", "artists", "dedup"].includes(state.view)) {
          if (state.view === "dedup") state.dedupPage = 1;
          switchView(state.view);
        } else {
          switchView("tracks");
        }
      }, 260);
    });
    clearSearch.addEventListener("click", () => {
      searchInput.value = "";
      state.search = "";
      clearSearch.classList.add("hidden");
      switchView(state.view);
    });

    document.getElementById("device-select").addEventListener("change", (e) => {
      state.selectedMount = e.target.value;
      renderDeviceBar();
      updateSelectionDock();
      if (state.view === "device") {
        state.deviceRelPath = "";
        renderDeviceView();
      }
    });

    document.getElementById("btn-refresh-devices").addEventListener("click", () => refreshDevices(false));
    document.getElementById("btn-sync-disk").addEventListener("click", async () => {
      const dev = requireDevice();
      if (!dev) return;
      try {
        await api("/api/devices/sync", { method: "POST", body: JSON.stringify({ mountpoint: dev.mountpoint }) });
        toast(`已对 ${dev.label} 完成安全刷盘`, "ok");
      } catch (e) {
        toast(e.message, "error");
      }
    });

    document.getElementById("user-select").addEventListener("change", async (e) => {
      state.userId = Number(e.target.value) || 1;
      await loadOverview();
      switchView(state.view);
    });

    document.getElementById("btn-clear-selection").addEventListener("click", () => {
      state.selectedTracks.clear();
      updateSelectionDock();
      document.querySelectorAll(".row-check, #check-all-page").forEach((cb) => (cb.checked = false));
      document.querySelectorAll("tr.selected").forEach((tr) => tr.classList.remove("selected"));
    });

    document.getElementById("btn-send-selected").addEventListener("click", () => {
      const ids = Array.from(state.selectedTracks.keys());
      if (!ids.length) return;
      startTransfer({ track_ids: ids, label: `发送已选 ${ids.length} 首曲目` });
    });

    document.getElementById("btn-open-transfer-view").addEventListener("click", () => switchView("transfers"));
    document.getElementById("btn-cancel-transfer").addEventListener("click", async () => {
      await api("/api/transfer/cancel", { method: "POST" });
      toast("正在取消传输...", "warn");
    });

    document.getElementById("btn-close-drawer").addEventListener("click", closeDrawer);
    document.getElementById("drawer-backdrop").addEventListener("click", closeDrawer);

    // Boot
    loadOverview().then(() => {
      switchView("tracks");
      pollTransferStatus();
      pollOrganizeStatus();
      refreshRecycleBadge();
      state.pollTimer = setInterval(() => {
        pollTransferStatus();
        pollOrganizeStatus();
      }, 1500);
      // Periodically auto-detect newly plugged Echo USB devices
      setInterval(() => refreshDevices(true), 8000);
    });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
