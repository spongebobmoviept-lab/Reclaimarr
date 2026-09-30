const state = { movies: [], selected: new Set(), sortBySize: false };

function $(sel) { return document.querySelector(sel); }

function activateTab(tab) {
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab}`));
  localStorage.setItem("reclaimarr-tab", tab);
  if (tab === "jobs") loadJobs();
  if (tab === "history") loadHistory();
  if (tab === "kept") loadKeptFiles();
  if (tab === "settings") { loadConnections(); loadSettings(); loadUserPicker(); loadStatus(); }
  if (tab === "log") loadLog();
}

function initTabs() {
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => activateTab(btn.dataset.tab));
  });
  const savedTab = localStorage.getItem("reclaimarr-tab");
  if (savedTab && document.querySelector(`#tab-${savedTab}`)) {
    activateTab(savedTab);
  }
}

function statusLabel(job) {
  if (!job) return "";
  return `${job.kind} · ${job.status}`;
}

function renderGrid(filter) {
  const grid = $("#grid");
  grid.innerHTML = "";
  const term = (filter || "").trim().toLowerCase();
  const downgradeFilter = $("#downgrade-filter").value;
  const movies = state.movies.filter((m) => {
    if (term && !m.title.toLowerCase().includes(term)) return false;
    if (downgradeFilter === "excluded" && !m.downgrade_excluded) return false;
    if (downgradeFilter === "eligible" && m.downgrade_excluded) return false;
    return true;
  });
  if (state.sortBySize) {
    movies.sort((a, b) => (b.file_size_gb || 0) - (a.file_size_gb || 0));
  }
  $("#movie-count").textContent = `${movies.length} movie${movies.length === 1 ? "" : "s"}`;

  for (const movie of movies) {
    const card = document.createElement("div");
    card.className = "poster-card";
    if (movie.job) card.classList.add("has-job");
    else if (movie.downgrade_excluded) card.classList.add("excluded-flag");

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "poster-select";
    checkbox.checked = state.selected.has(movie.id);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.selected.add(movie.id);
      else state.selected.delete(movie.id);
      updateBulkBar();
    });
    card.appendChild(checkbox);

    const img = document.createElement("img");
    img.src = movie.poster_url || "";
    img.alt = movie.title;
    img.loading = "lazy";
    card.appendChild(img);

    if (movie.downgrade_excluded) {
      const keepBadge = document.createElement("span");
      keepBadge.className = "keep-badge";
      keepBadge.textContent = "Kept";
      keepBadge.title = "Excluded from the downgrade workflow";
      card.appendChild(keepBadge);
    }

    if (movie.job) {
      const badge = document.createElement("span");
      badge.className = `status-badge ${movie.job.status}`;
      badge.textContent = movie.job.status;
      card.appendChild(badge);
    }

    const meta = document.createElement("div");
    meta.className = "poster-meta";
    const title = document.createElement("div");
    title.className = "poster-title";
    title.textContent = movie.title;
    const sub = document.createElement("div");
    sub.className = "poster-sub";
    const res = movie.resolution ? `${movie.resolution}p` : "—";
    const size = movie.file_size_gb ? `${movie.file_size_gb} GB` : "";
    sub.textContent = movie.job ? statusLabel(movie.job) : `${res} ${size}`.trim();
    meta.appendChild(title);
    meta.appendChild(sub);
    card.appendChild(meta);

    grid.appendChild(card);
  }
}

function updateBulkBar() {
  const bar = $("#bulk-bar");
  const count = state.selected.size;
  bar.classList.toggle("visible", count > 0);
  $("#bulk-count").textContent = `${count} selected`;
}

async function setDowngradeExclusion(movieIds, excluded) {
  await fetch("/api/movies/downgrade-exclusion", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ movie_ids: movieIds, excluded }),
  });
  state.selected.clear();
  updateBulkBar();
  await loadMovies();
}

async function forceDowngradeSelected(movieIds) {
  const btn = $("#bulk-force-downgrade");
  btn.disabled = true;
  for (const id of movieIds) {
    btn.textContent = `Triggering… (${movieIds.indexOf(id) + 1}/${movieIds.length})`;
    await fetch(`/api/debug/force-downgrade/${id}`, { method: "POST" });
    // Small stagger so we don't fire N indexer searches in the same instant.
    await new Promise((r) => setTimeout(r, 1500));
  }
  btn.textContent = "Force Downgrade Selected Now";
  btn.disabled = false;
  state.selected.clear();
  updateBulkBar();
  alert(`Triggered ${movieIds.length} movie(s). Check the Active Jobs and History tabs for results.`);
}

async function loadSessions() {
  const resp = await fetch("/api/sessions");
  const sessions = await resp.json();
  const container = $("#now-playing");
  container.innerHTML = "";
  for (const s of sessions) {
    const row = document.createElement("div");
    row.className = "session-row";

    const title = document.createElement("span");
    title.className = "session-title";
    title.textContent = s.title;

    const meta = document.createElement("span");
    meta.className = "session-meta";
    const res = s.resolution ? `${s.resolution}p` : "unknown res";
    meta.textContent = `${s.username} · ${res} · ${Math.round(s.watched_fraction * 100)}% watched`;

    row.appendChild(title);
    row.appendChild(meta);

    if (s.media_type === "movie" && s.resolution !== "2160" && s.resolution !== "4k") {
      const btn = document.createElement("button");
      btn.className = "btn-trigger";
      btn.textContent = "Check for 4K now";
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        btn.textContent = "Checking…";
        const r = await fetch(`/api/sessions/${encodeURIComponent(s.session_key)}/trigger-upgrade`, { method: "POST" });
        const data = await r.json();
        btn.textContent = data.result || "done";
        loadMovies();
      });
      row.appendChild(btn);
    }

    container.appendChild(row);
  }
}

async function loadMovies() {
  const resp = await fetch("/api/movies");
  state.movies = await resp.json();
  renderGrid($("#search").value);
}

async function loadJobs() {
  const resp = await fetch("/api/jobs");
  const jobs = await resp.json();
  const list = $("#jobs-list");
  list.innerHTML = "";

  for (const job of jobs) {
    const movie = state.movies.find((m) => m.id === job.movie_id);
    const row = document.createElement("div");
    row.className = "queue-row";

    const img = document.createElement("img");
    img.className = "queue-poster";
    img.src = (movie && movie.poster_url) || "";
    img.alt = "";
    row.appendChild(img);

    const main = document.createElement("div");
    main.className = "queue-main";

    const titleRow = document.createElement("div");
    titleRow.className = "queue-title-row";
    const title = document.createElement("span");
    title.className = "queue-title";
    title.textContent = (movie && movie.title) || `Movie #${job.movie_id}`;
    const kind = document.createElement("span");
    kind.className = "queue-kind";
    kind.textContent = job.kind;
    const badge = document.createElement("span");
    badge.className = `status-badge ${job.status}`;
    badge.textContent = job.status;
    titleRow.append(title, kind, badge);

    const track = document.createElement("div");
    track.className = "progress-track";
    const fill = document.createElement("div");
    const pct = job.download && job.download.percent_complete != null ? job.download.percent_complete : null;
    fill.className = "progress-fill" + (pct == null ? " indeterminate" : "");
    fill.style.width = pct != null ? `${pct}%` : "100%";
    track.appendChild(fill);

    const detail = document.createElement("div");
    detail.className = "queue-detail";
    if (job.download) {
      const pctText = pct != null ? `${pct}%` : "—";
      const size = job.download.size_gb != null ? `${job.download.size_gb} GB` : "?";
      const eta = job.download.timeleft || "unknown ETA";
      detail.title = job.download.release_title || "";
      detail.textContent = `${pctText} of ${size} · ${eta} left · ${job.download.download_client || "?"}`;
    } else if (job.status === "searching") {
      detail.textContent = `Searching indexers… (attempt ${job.attempt_count + 1})`;
    } else {
      detail.textContent = `Updated ${new Date(job.updated_at).toLocaleTimeString()}`;
    }

    main.append(titleRow, track, detail);
    row.appendChild(main);

    const actions = document.createElement("div");
    actions.className = "queue-actions";
    if (job.kind === "upgrade" && (job.status === "searching" || job.status === "downloading")) {
      const btn = document.createElement("button");
      btn.className = "btn-trigger";
      btn.textContent = "Force check now";
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        btn.textContent = "Forcing…";
        const r = await fetch(`/api/jobs/${job.movie_id}/force-check`, { method: "POST" });
        btn.textContent = r.ok ? "Forced" : "No active wait";
        setTimeout(loadJobs, 2000);
      });
      actions.appendChild(btn);
    }
    const cancelBtn = document.createElement("button");
    cancelBtn.className = "btn-trigger";
    cancelBtn.textContent = "Cancel";
    cancelBtn.addEventListener("click", async () => {
      if (!confirm(`Cancel tracking for this job? The file itself won't be touched.`)) return;
      cancelBtn.disabled = true;
      const r = await fetch(`/api/jobs/${job.movie_id}/cancel`, { method: "POST" });
      if (r.ok) loadJobs();
      else cancelBtn.textContent = "Failed";
    });
    actions.appendChild(cancelBtn);
    row.appendChild(actions);

    list.appendChild(row);
  }
}

async function loadHistory() {
  const resp = await fetch("/api/history");
  const entries = await resp.json();
  const list = $("#history-list");
  list.innerHTML = "";

  for (const h of entries) {
    const row = document.createElement("div");
    row.className = "queue-row";
    row.style.gridTemplateColumns = "46px 1fr auto";

    const img = document.createElement("img");
    img.className = "queue-poster";
    img.src = h.poster_url || "";
    img.alt = "";
    row.appendChild(img);

    const main = document.createElement("div");
    main.className = "queue-main";
    const titleRow = document.createElement("div");
    titleRow.className = "queue-title-row";
    const title = document.createElement("span");
    title.className = "queue-title";
    title.textContent = h.movie_title;
    const kind = document.createElement("span");
    kind.className = "queue-kind";
    kind.textContent = h.kind;
    titleRow.append(title, kind);

    const detail = document.createElement("div");
    detail.className = "queue-detail";
    detail.textContent = h.detail;

    main.append(titleRow, detail);
    row.appendChild(main);

    const badge = document.createElement("span");
    badge.className = `outcome-badge ${h.outcome}`;
    badge.textContent = h.outcome.replace("_", " ");
    row.appendChild(badge);

    list.appendChild(row);
  }
}

async function loadScanStatus() {
  const resp = await fetch("/api/scan-status");
  const s = await resp.json();
  const el = $("#scan-status-banner");
  if (!s.running) {
    el.classList.remove("visible");
    return;
  }
  el.classList.add("visible");
  el.textContent = `Scanning for downgrade candidates: ${s.checked}/${s.total_candidates} — now checking "${s.current_movie || "…"}"`
    + (s.last_skip_reason ? ` · last: ${s.last_skip_reason}` : "");
}

async function loadKeptFiles() {
  const resp = await fetch("/api/kept-files");
  const files = await resp.json();
  const list = $("#kept-list");
  list.innerHTML = "";

  for (const f of files) {
    const row = document.createElement("div");
    row.className = "queue-row";
    row.style.gridTemplateColumns = "1fr auto";

    const main = document.createElement("div");
    main.className = "queue-main";
    const titleRow = document.createElement("div");
    titleRow.className = "queue-title-row";
    const title = document.createElement("span");
    title.className = "queue-title";
    title.textContent = f.name;
    titleRow.appendChild(title);

    const detail = document.createElement("div");
    detail.className = "queue-detail";
    let deleteNote;
    if (f.age_days === null) {
      deleteNote = "age unknown — won't auto-delete until confirmed safe or manually removed";
    } else if (f.kind === "upgrade") {
      // Opposite of downgrade: this file itself is the permanent keeper —
      // it's the 4K version that goes away, reverting back to this one.
      deleteNote = f.auto_delete_in_days > 0
        ? `kept permanently — the 4K version reverts back to this in ${f.auto_delete_in_days}d`
        : "kept permanently — the 4K version is due to revert back to this now";
    } else {
      deleteNote = f.auto_delete_in_days > 0
        ? `auto-deletes in ${f.auto_delete_in_days}d`
        : "past its window — will auto-delete once nobody's watching it";
    }
    const ageNote = f.age_days === null ? "unknown age" : `kept ${f.age_days}d ago`;
    detail.textContent = `${f.size_gb} GB · ${ageNote} · ${deleteNote}`;

    main.append(titleRow, detail);
    row.appendChild(main);

    const actions = document.createElement("div");
    actions.className = "queue-actions";
    const btn = document.createElement("button");
    btn.className = "btn-trigger";
    btn.textContent = "Delete now";
    btn.addEventListener("click", async () => {
      if (!confirm(`Permanently delete "${f.name}"? This can't be undone.`)) return;
      btn.disabled = true;
      btn.textContent = "Deleting…";
      const r = await fetch("/api/kept-files/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: f.path }),
      });
      if (r.ok) loadKeptFiles();
      else btn.textContent = "Failed";
    });
    actions.appendChild(btn);
    row.appendChild(actions);

    list.appendChild(row);
  }
}

const SETTINGS_LABELS = {
  radarr_4k_profile_id: "Radarr 4K quality profile ID",
  radarr_1080p_profile_id: "Radarr 1080p-only quality profile ID",
  downgrade_threshold_gb: "Downgrade threshold (GB)",
  downgrade_size_ratio: "Downgrade size ratio (0-1)",
  max_4k_release_size_gb: "Max 4K release size (GB)",
  poll_interval_seconds: "Upgrade poll interval (seconds)",
  max_download_attempts: "Max download attempts",
  downgrade_scan_hour_utc: "Downgrade scan hour (UTC, 0-23)",
  downgrade_min_age_days: "Minimum age since added before eligible for downgrade (days)",
  downgrade_recent_watch_days: "Defer downgrade if watched within this many days",
  downgrade_recheck_days: "Downgrade re-check cooldown (days)",
  upgrade_retry_cooldown_minutes: "Upgrade retry cooldown (minutes)",
  keep_original_days: "Downgrade: keep original before auto-delete (days) / Upgrade: keep 4K before reverting to original (days)",
  never_terminate_session: "Never interrupt active playback for an upgrade (notify only, don't cut off the play)",
  min_download_speed_kbps: "Abandon a download below this speed (KB/s, 0 = never)",
  min_speed_check_after_minutes: "Grace period before checking download speed (minutes)",
  library_integrity_check_interval_hours: "Library integrity check interval (hours)",
  min_release_seeders: "Minimum seeders for a release to be picked",
  upgrade_no_release_cooldown_hours: "Retry cooldown when no 4K release exists at all (hours)",
  digest_hour_utc: "Daily Discord digest hour (UTC, 0-23)",
  discord_username: "Name the Discord webhook posts under",
  discord_startup_notice: "Post an \"online\" notice to Discord on every start",
  integrity_sanity_limit: "Skip an integrity pass if more than this many files look missing (mount probably down)",
  media_roots: "Media folders Reclaimarr may touch (container paths, comma-separated, e.g. /media2,/media4)",
};

const SETTINGS_GROUPS = [
  { title: "Quality Profiles", keys: ["radarr_4k_profile_id", "radarr_1080p_profile_id"] },
  {
    title: "Downgrade Rules",
    keys: [
      "downgrade_threshold_gb", "downgrade_size_ratio", "downgrade_min_age_days",
      "downgrade_recent_watch_days", "downgrade_recheck_days", "downgrade_scan_hour_utc",
    ],
  },
  {
    title: "Upgrade Rules",
    keys: ["max_4k_release_size_gb", "poll_interval_seconds", "upgrade_retry_cooldown_minutes", "upgrade_no_release_cooldown_hours", "never_terminate_session"],
  },
  { title: "Retention", keys: ["keep_original_days"] },
  {
    title: "Downloads & Safety",
    keys: ["max_download_attempts", "min_release_seeders", "min_download_speed_kbps", "min_speed_check_after_minutes"],
  },
  { title: "Media Folders", keys: ["media_roots"] },
  { title: "Discord", keys: ["digest_hour_utc", "discord_username", "discord_startup_notice"] },
  { title: "System", keys: ["library_integrity_check_interval_hours", "integrity_sanity_limit"] },
];

async function loadStatus() {
  const resp = await fetch("/api/status");
  const status = await resp.json();
  const banner = $("#status-banner");
  const bits = [];
  if (status.dry_run) bits.push("DRY RUN — no real changes will be made");
  else bits.push("LIVE — real Radarr changes will happen");
  if (status.never_terminate_session) bits.push("Playback termination disabled (safe)");
  if (!status.upgrade_workflow_enabled) bits.push("Upgrade workflow disabled");
  if (!status.downgrade_workflow_enabled) bits.push("Downgrade workflow disabled");
  if (bits.length) {
    banner.textContent = bits.join(" · ");
    banner.classList.add("visible");
  } else {
    banner.classList.remove("visible");
  }
}

async function loadUserPicker() {
  const resp = await fetch("/api/plex-users");
  const data = await resp.json();
  const picker = $("#user-picker");
  picker.innerHTML = "";
  if (!data.known_users.length) {
    picker.innerHTML = '<span class="muted">No users found yet (needs Tautulli configured, or nobody has watched anything). Everyone is currently allowed.</span>';
    return;
  }
  for (const username of data.known_users) {
    const label = document.createElement("label");
    label.className = "user-chip";
    const checked = data.allowed_usernames.includes(username);
    if (checked) label.classList.add("checked");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = username;
    checkbox.checked = checked;
    checkbox.addEventListener("change", () => label.classList.toggle("checked", checkbox.checked));
    label.appendChild(checkbox);
    label.appendChild(document.createTextNode(username));
    picker.appendChild(label);
  }
}

async function saveUsers() {
  const selected = Array.from(document.querySelectorAll("#user-picker input:checked")).map((i) => i.value);
  const resp = await fetch("/api/plex-users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ allowed_usernames: selected }),
  });
  $("#users-status").textContent = resp.ok ? "Saved." : "Failed to save.";
}

function buildSettingsField(key, value) {
  const field = document.createElement("div");
  field.className = "settings-field";
  const label = document.createElement("label");
  label.textContent = SETTINGS_LABELS[key] || key;
  const input = document.createElement("input");
  input.name = key;
  if (typeof value === "boolean") {
    input.type = "checkbox";
    input.checked = value;
    label.classList.add("checkbox-label");
    field.appendChild(input);
    field.appendChild(label);
  } else if (Array.isArray(value)) {
    input.type = "text";
    input.dataset.kind = "list";
    input.value = value.join(", ");
    field.appendChild(label);
    field.appendChild(input);
  } else if (typeof value === "string") {
    input.type = "text";
    input.dataset.kind = "text";
    input.value = value;
    field.appendChild(label);
    field.appendChild(input);
  } else {
    input.type = "number";
    input.step = "any";
    input.value = value;
    field.appendChild(label);
    field.appendChild(input);
  }
  return field;
}

async function loadSettings() {
  const resp = await fetch("/api/settings");
  const settings = await resp.json();
  const form = $("#settings-form");
  form.innerHTML = "";
  const remaining = new Set(Object.keys(settings));

  for (const group of SETTINGS_GROUPS) {
    const keysHere = group.keys.filter((k) => remaining.has(k));
    if (!keysHere.length) continue;
    const groupEl = document.createElement("div");
    groupEl.className = "settings-group";
    const heading = document.createElement("div");
    heading.className = "settings-group-title";
    heading.textContent = group.title;
    groupEl.appendChild(heading);
    const grid = document.createElement("div");
    grid.className = "settings-grid";
    for (const key of keysHere) {
      grid.appendChild(buildSettingsField(key, settings[key]));
      remaining.delete(key);
    }
    groupEl.appendChild(grid);
    form.appendChild(groupEl);
  }

  // Anything not explicitly grouped still shows up, ungrouped, rather than silently vanishing.
  if (remaining.size) {
    const grid = document.createElement("div");
    grid.className = "settings-grid";
    for (const key of remaining) grid.appendChild(buildSettingsField(key, settings[key]));
    form.appendChild(grid);
  }
}

async function saveSettings() {
  const form = $("#settings-form");
  const update = {};
  for (const input of form.querySelectorAll("input")) {
    if (input.type === "checkbox") update[input.name] = input.checked;
    else if (input.dataset.kind === "list") update[input.name] = input.value.split(",").map((v) => v.trim()).filter(Boolean);
    else if (input.dataset.kind === "text") update[input.name] = input.value;
    else update[input.name] = Number(input.value);
  }
  const resp = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
  });
  let detail = "";
  if (!resp.ok) { try { detail = (await resp.json()).detail || ""; } catch (e) { /* no body */ } }
  $("#settings-status").textContent = resp.ok ? "Saved." : "Failed to save" + (detail ? ": " + detail : ".");
}

const CONNECTIONS_LABELS = {
  plex_url: "Plex URL (e.g. http://192.168.1.10:32400)",
  plex_token: "Plex Token",
  radarr_url: "Radarr URL (e.g. http://192.168.1.10:7878)",
  radarr_api_key: "Radarr API Key",
  tautulli_url: "Tautulli URL (optional)",
  tautulli_api_key: "Tautulli API Key (optional)",
  discord_webhook_url: "Discord Webhook URL (optional)",
  qbit_url: "qBittorrent Web UI URL (optional, e.g. http://192.168.1.10:8080)",
  qbit_username: "qBittorrent username (optional)",
  qbit_password: "qBittorrent password (optional)",
};

async function loadConnections() {
  const resp = await fetch("/api/connections");
  const fields = await resp.json();
  const form = $("#connections-form");
  form.innerHTML = "";
  for (const [key, info] of Object.entries(fields)) {
    const field = document.createElement("div");
    field.className = "settings-field";
    const label = document.createElement("label");
    label.textContent = CONNECTIONS_LABELS[key] || key;
    const input = document.createElement("input");
    input.name = key;
    input.type = info.is_secret ? "password" : "text";
    input.placeholder = info.is_secret
      ? (info.value ? `Currently: ${info.value} — leave blank to keep` : "Not set")
      : "";
    if (!info.is_secret) input.value = info.value || "";
    field.appendChild(label);
    field.appendChild(input);
    form.appendChild(field);
  }
}

async function saveConnections() {
  const form = $("#connections-form");
  const update = {};
  for (const input of form.querySelectorAll("input")) {
    if (input.type === "password" && !input.value) continue; // unchanged
    update[input.name] = input.value;
  }
  const resp = await fetch("/api/connections", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
  });
  $("#connections-status").textContent = resp.ok ? "Saved — takes effect immediately, no restart needed." : "Failed to save.";
  if (resp.ok) loadConnections();
}

async function loadLog() {
  const resp = await fetch("/api/log?lines=300");
  const lines = await resp.json();
  $("#log-view").textContent = lines.join("\n");
}

initTabs();
$("#search").addEventListener("input", (e) => renderGrid(e.target.value));
$("#downgrade-filter").addEventListener("change", () => renderGrid($("#search").value));
$("#sort-by-size").addEventListener("click", (e) => {
  state.sortBySize = !state.sortBySize;
  e.target.classList.toggle("active", state.sortBySize);
  renderGrid($("#search").value);
});
$("#settings-save").addEventListener("click", saveSettings);
$("#connections-save").addEventListener("click", saveConnections);
$("#users-save").addEventListener("click", saveUsers);
$("#bulk-force-downgrade").addEventListener("click", () => {
  if (confirm(`Force-check ${state.selected.size} movie(s) for downgrade right now?`)) {
    forceDowngradeSelected([...state.selected]);
  }
});
$("#bulk-exclude").addEventListener("click", () => setDowngradeExclusion([...state.selected], true));
$("#bulk-include").addEventListener("click", () => setDowngradeExclusion([...state.selected], false));
$("#bulk-clear").addEventListener("click", () => { state.selected.clear(); updateBulkBar(); renderGrid($("#search").value); });
$("#run-downgrade-scan").addEventListener("click", async (e) => {
  e.target.disabled = true;
  // A manual click here means "check everything right now" — silently
  // skipping most of the library because of the internal recheck cooldown
  // (meant to space out the automatic daily scan, not a deliberate request
  // like this one) would make the button look broken. Confirmed live: this
  // was exactly the "why don't I have movies downgrading" issue.
  const r = await fetch("/api/debug/run-downgrade-scan?ignore_cooldown=true", { method: "POST" });
  const data = await r.json();
  $("#run-downgrade-status").textContent = r.ok ? "Scan started (full re-check, ignoring cooldown) — watch the banner above." : "Failed to start.";
  loadScanStatus();
  setTimeout(() => { e.target.disabled = false; }, 3000);
});
loadMovies();
loadSessions();
loadStatus();
loadScanStatus();
setInterval(loadMovies, 15000);
setInterval(loadSessions, 10000);
setInterval(loadScanStatus, 5000);

// Jobs/History/Kept only ever loaded on tab activation, so leaving a tab
// open (e.g. watching Active Jobs during a long scan) showed stale data
// until you clicked away and back — confirmed live during the full-library
// downgrade run. Re-fetch whichever of those tabs is currently visible.
function refreshActiveTab() {
  const activeBtn = document.querySelector(".tab.active");
  const tab = activeBtn ? activeBtn.dataset.tab : null;
  if (tab === "jobs") loadJobs();
  if (tab === "history") loadHistory();
  if (tab === "kept") loadKeptFiles();
}
setInterval(refreshActiveTab, 5000);
