"use strict";

let API_KEY = localStorage.getItem("shorts_api_key") || null;
let CURRENT_JOB = null;
let POLLING = false;
let UPLOADED_PATH = null;

const $ = (id) => document.getElementById(id);

/* ---------------- helpers ---------------- */

function setVisible(el, show) {
  el.classList.toggle("hidden", !show);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (API_KEY) headers["X-API-Key"] = API_KEY;
  const res = await fetch(path, { ...options, headers });
  if (res.status === 401) {
    logOut();
    throw new Error("Invalid or expired key — please log in again.");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch (_) { /* non-JSON error */ }
    throw new Error(detail);
  }
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

function logOut() {
  API_KEY = null;
  localStorage.removeItem("shorts_api_key");
  showLogin();
}

function esc(value) {
  const d = document.createElement("div");
  d.textContent = String(value ?? "");
  return d.innerHTML;
}

function fmtTime(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.max(0, Math.round(seconds));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${String(r).padStart(2, "0")}`;
}

/* ---------------- views ---------------- */

function showLogin() {
  setVisible($("login-view"), true);
  setVisible($("app-view"), false);
}

function showApp() {
  setVisible($("login-view"), false);
  setVisible($("app-view"), true);
  setVisible($("job-form"), true);
  setVisible($("progress-view"), false);
  setVisible($("results-view"), false);
  setVisible($("back-btn"), false);
}

function showProgress() {
  setVisible($("job-form"), false);
  setVisible($("progress-view"), true);
  setVisible($("results-view"), false);
  setVisible($("back-btn"), false);
}

function showResults(regenerate = false) {
  setVisible($("job-form"), regenerate);
  setVisible($("progress-view"), false);
  setVisible($("results-view"), true);
  setVisible($("back-btn"), true);
}

/* ---------------- login ---------------- */

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const key = $("login-key").value.trim();
  const err = $("login-error");
  setVisible(err, false);
  if (!key) return;
  API_KEY = key;
  try {
    await api("/api/verify");
    localStorage.setItem("shorts_api_key", key);
    $("login-key").value = "";
    bootApp();
  } catch (ex) {
    API_KEY = null;
    err.textContent = ex.message || "Login failed";
    setVisible(err, true);
  }
});

/* ---------------- source tabs + upload ---------------- */

let SOURCE_TAB = "url";

document.querySelectorAll("[data-src-tab]").forEach((btn) => {
  btn.addEventListener("click", () => {
    SOURCE_TAB = btn.dataset.srcTab;
    document.querySelectorAll("[data-src-tab]").forEach((b) =>
      b.classList.toggle("active", b === btn)
    );
    setVisible($("src-url"), SOURCE_TAB === "url");
    setVisible($("src-upload"), SOURCE_TAB === "upload");
    setVisible($("form-error"), false);
  });
});

const dropzone = $("dropzone");
dropzone.addEventListener("click", () => $("file-input").click());
dropzone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropzone.classList.add("drag");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("drag"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("drag");
  if (e.dataTransfer.files.length) selectFile(e.dataTransfer.files[0]);
});
$("file-input").addEventListener("change", (e) => {
  if (e.target.files.length) selectFile(e.target.files[0]);
});

function selectFile(file) {
  UPLOADED_PATH = null;
  const meta = $("file-meta");
  meta.textContent = `${file.name} (${(file.size / (1024 * 1024)).toFixed(1)} MB)`;
  setVisible(meta, true);
}

/* ---------------- submit ---------------- */

function aspectRatio() {
  const sel = $("aspect-ratio").value;
  if (sel === "custom") {
    const custom = $("aspect-custom").value.trim();
    if (!custom) throw new Error("Enter a custom aspect ratio like 3:4");
    return custom;
  }
  return sel;
}

$("aspect-ratio").addEventListener("change", () => {
  setVisible($("aspect-custom"), $("aspect-ratio").value === "custom");
});

/** Keep the caption override + hint + style options in sync with the master. */
function syncCaptionControls() {
  const master = $("burn-captions").checked;
  const force = $("force-captions");
  force.disabled = !master;
  force.closest("label").style.opacity = master ? "1" : "0.5";
  setVisible($("caption-style"), master);
  $("captions-hint").textContent = !master
    ? "Subtitles will not be added."
    : force.checked
      ? "Adding subtitles regardless of existing captions."
      : "Subtitles are only added if the source doesn't already have them.";
}
$("burn-captions").addEventListener("change", syncCaptionControls);
$("force-captions").addEventListener("change", syncCaptionControls);
syncCaptionControls();

/* ---------------- caption style live preview ---------------- */

const CAP_DEFAULTS = {
  "cap-color": "#ffffff",
  "cap-active": "#ffd700",
  "cap-outline": "#000000",
};

function syncCaptionColors() {
  const preview = $("caption-preview");
  const size = parseInt($("cap-fontsize").value, 10) || 48;
  preview.style.setProperty("--c-text", $("cap-color").value);
  preview.style.setProperty("--c-active", $("cap-active").value);
  preview.style.setProperty("--c-outline", $("cap-outline").value);
  preview.style.setProperty("--c-size", `${size}px`);
  $("cap-size-val").textContent = size;
  document.querySelectorAll(".swatch").forEach((sw) => {
    sw.style.setProperty("--swatch", $(sw.dataset.colorFor).value);
  });
}
Object.keys(CAP_DEFAULTS).forEach((id) => {
  $(id).addEventListener("input", syncCaptionColors);
});
$("cap-fontsize").addEventListener("input", syncCaptionColors);
$("cap-reset").addEventListener("click", () => {
  Object.entries(CAP_DEFAULTS).forEach(([id, value]) => { $(id).value = value; });
  $("cap-fontsize").value = 48;
  syncCaptionColors();
  saveFormState();
});
syncCaptionColors();

/* ---------------- form state persistence ---------------- */

const FORM_STATE_KEY = "shorts_form_state";

function collectFormState() {
  return {
    tab: SOURCE_TAB,
    url: $("video-url").value,
    clips: $("num-clips").value,
    ratio: $("aspect-ratio").value,
    custom: $("aspect-custom").value,
    format: $("format").value,
    focus: $("focus").value,
    burn: $("burn-captions").checked,
    force: $("force-captions").checked,
    color: $("cap-color").value,
    active: $("cap-active").value,
    outline: $("cap-outline").value,
    size: $("cap-fontsize").value,
  };
}

function saveFormState() {
  try {
    localStorage.setItem(FORM_STATE_KEY, JSON.stringify(collectFormState()));
  } catch (_) { /* storage may be unavailable */ }
}

function restoreFormState() {
  let s = null;
  try { s = JSON.parse(localStorage.getItem(FORM_STATE_KEY) || "null"); } catch (_) { /* ignore */ }
  if (!s) return;

  if (s.tab === "url" || s.tab === "upload") {
    SOURCE_TAB = s.tab;
    document.querySelectorAll("[data-src-tab]").forEach((b) =>
      b.classList.toggle("active", b.dataset.srcTab === SOURCE_TAB)
    );
    setVisible($("src-url"), SOURCE_TAB === "url");
    setVisible($("src-upload"), SOURCE_TAB === "upload");
  }
  if (s.url) $("video-url").value = s.url;
  if (s.clips) $("num-clips").value = s.clips;
  if (s.ratio) $("aspect-ratio").value = s.ratio;
  if (s.custom) $("aspect-custom").value = s.custom;
  if (s.format) $("format").value = s.format;
  if (s.focus) $("focus").value = s.focus;
  $("burn-captions").checked = !!s.burn;
  $("force-captions").checked = !!s.force;
  if (s.color) $("cap-color").value = s.color;
  if (s.active) $("cap-active").value = s.active;
  if (s.outline) $("cap-outline").value = s.outline;
  if (s.size) $("cap-fontsize").value = s.size;

  setVisible($("aspect-custom"), $("aspect-ratio").value === "custom");
  syncCaptionControls();
  syncCaptionColors();
}

$("job-form").addEventListener("input", saveFormState);
$("job-form").addEventListener("change", saveFormState);

$("job-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("form-error");
  setVisible(err, false);

  let url;
  try {
    if (SOURCE_TAB === "upload") {
      const fileInput = $("file-input");
      if (!fileInput.files.length) throw new Error("Choose a video file to upload.");
      $("submit-btn").disabled = true;
      $("submit-btn").textContent = "Uploading…";
      const formData = new FormData();
      formData.append("file", fileInput.files[0]);
      const upload = await api("/api/upload", { method: "POST", body: formData });
      url = upload.local_path;
      UPLOADED_PATH = url;
    } else {
      url = $("video-url").value.trim();
      if (!url) throw new Error("Paste a YouTube URL to process.");
    }

    const numClips = parseInt($("num-clips").value, 10);
    if (!numClips || numClips < 1 || numClips > 20) {
      throw new Error("Number of clips must be between 1 and 20.");
    }

    const captionOptions = {
      karaoke: true,
      text_color: $("cap-color").value,
      active_color: $("cap-active").value,
      outline_color: $("cap-outline").value,
      font_size: parseInt($("cap-fontsize").value, 10) || 48,
    };

    const params = {
      url,
      num_clips: numClips,
      aspect_ratio: aspectRatio(),
      format: $("format").value,
      detect_captions: !$("force-captions").checked,
      burn_captions: $("burn-captions").checked,
      force_captions: $("force-captions").checked,
      caption_options: captionOptions,
      focus: $("focus").value.trim() || null,
    };

    const job = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    });

    history.replaceState(null, "", `?job=${job.job_id}`);
    CURRENT_JOB = job;
    initializeProgress(job);
    showProgress();
    startPolling(job);
  } catch (ex) {
    err.textContent = ex.message || "Failed to start the job.";
    setVisible(err, true);
  } finally {
    $("submit-btn").disabled = false;
    $("submit-btn").textContent = "Generate shorts";
  }
});

/* ---------------- progress / polling ---------------- */

const STAGES = [
  { name: "Queued", match: /queued/i },
  { name: "Downloading", match: /\[download/i },
  { name: "Transcribing", match: /\[transcrib/i },
  { name: "Analyzing highlights", match: /\[highlights/i },
  { name: "Rendering clips", match: /\[clip/i },
  { name: "Checking captions", match: /\[captions/i },
  { name: "Burning subtitles", match: /burning/i },
  { name: "Done", match: /completed/i },
];

function initializeProgress(job) {
  $("job-id").textContent = `Job ${job.job_id}`;
  $("log-tail").textContent = "";
  const list = $("step-list");
  list.innerHTML = "";
  STAGES.forEach((s, i) => {
    const li = document.createElement("li");
    li.dataset.stage = i;
    li.textContent = s.name;
    list.appendChild(li);
  });
}

function stageFromLogs(logs) {
  let active = 0;
  for (const line of logs) {
    for (let i = STAGES.length - 2; i >= 0; i--) {
      if (STAGES[i].match.test(line.message || "")) {
        active = Math.max(active, i);
        break;
      }
    }
  }
  return active;
}

function renderStage(active, completed) {
  document.querySelectorAll("#step-list li").forEach((li) => {
    const i = parseInt(li.dataset.stage, 10);
    li.classList.toggle("active", !completed && i === active);
    li.classList.toggle("done", completed || i < active);
  });
}

function startPolling(job) {
  if (POLLING) return;
  POLLING = true;
  const interval = setInterval(async () => {
    let data;
    try {
      data = await api(`/api/jobs/${job.job_id}`);
    } catch (ex) {
      clearInterval(interval);
      POLLING = false;
      $("log-tail").textContent += `\n[error] ${ex.message}`;
      return;
    }

    if (CURRENT_JOB && CURRENT_JOB.job_id !== job.job_id) {
      clearInterval(interval);
      POLLING = false;
      return;
    }

    try {
      const logsRes = await api(`/api/jobs/${job.job_id}/logs`);
      renderLogs(logsRes.logs);
      const stage = stageFromLogs(logsRes.logs);
      renderStage(stage, data.status === "completed");
    } catch (_) { /* logs endpoint may 404 between restarts */ }

    if (data.status === "completed" || data.status === "failed" || data.status === "cancelled") {
      clearInterval(interval);
      POLLING = false;
      renderStage(STAGES.length - 2, data.status === "completed");
      if (data.status === "completed") {
        renderResults(data);
        showResults(false);
      } else {
        $("log-tail").textContent +=
          `\n\n[${data.status.toUpperCase()}] ${data.error || ""}`.trimEnd();
      }
    }
  }, 2000);
}

function renderLogs(logs) {
  const tail = (logs || []).slice(-40).map((l) => `[${l.level}] ${l.message}`).join("\n");
  const el = $("log-tail");
  if (tail !== el.dataset.lastTail) {
    el.textContent = tail;
    el.dataset.lastTail = tail;
    el.scrollTop = el.scrollHeight;
  }
}

$("cancel-btn").addEventListener("click", async () => {
  if (!CURRENT_JOB) return;
  try {
    await api(`/api/jobs/${CURRENT_JOB.job_id}/cancel`, { method: "POST" });
  } catch (ex) {
    $("log-tail").textContent += `\n[cancel] ${ex.message}`;
  }
});

/* ---------------- results ---------------- */

function renderResults(job) {
  const shorts = (job.result && job.result.shorts) || [];
  $("results-summary").textContent =
    `${shorts.length} short${shorts.length === 1 ? "" : "s"} rendered from "${job.url}"`;

  const grid = $("results-grid");
  grid.innerHTML = "";
  shorts.forEach((clip, i) => {
    const filename =
      clip.filename || (clip.served_url && clip.served_url.split("/").pop());
    const clipUrl = filename
      ? `/clip/${job.video_id}/${encodeURIComponent(filename)}`
      : clip.served_url || clip.clip_url;

    const card = document.createElement("div");
    card.className = "short-card";
    card.innerHTML = `
      <div class="score-badge">${esc(clip.score)}</div>
      <video controls playsinline preload="metadata"
             src="${esc(clipUrl)}"></video>
      <div class="short-body">
        <h3>${i + 1}. ${esc(clip.title || "Untitled")}</h3>
        <p class="hook">"${esc(clip.hook_sentence || "")}"</p>
        <p class="muted reason">${esc(clip.virality_reason || "")}</p>
        <p class="muted">
          <span class="chip">${fmtTime(clip.start_time)} – ${fmtTime(clip.end_time)}</span>
        </p>
        <a class="btn download" href="${esc(clipUrl)}" download
           data-filename="${esc(filename || "")}">Download</a>
      </div>`;

    const dl = card.querySelector("a.download");
    if (clipUrl) {
      dl.href = clipUrl;
      dl.setAttribute("download", filename || "");
    } else {
      dl.classList.add("disabled");
      dl.textContent = "Download failed";
      dl.removeAttribute("href");
    }

    grid.appendChild(card);
  });
}

$("back-btn").addEventListener("click", () => {
  CURRENT_JOB = null;
  UPLOADED_PATH = null;
  $("file-input").value = "";
  setVisible($("file-meta"), false);
  history.replaceState(null, "", location.pathname);
  showApp();
});

/* ---------------- media modal ---------------- */

function openMedia() {
  api("/api/media")
    .then((data) => {
      renderMediaShorts(data);
      renderMediaUploads(data);
      renderMediaData(data);
      setMediaTab("shorts");
      setVisible($("media-modal"), true);
    })
    .catch((ex) => alert("Failed to load media: " + ex.message));
}

function setMediaTab(name) {
  document.querySelectorAll("[data-media-tab]").forEach((b) =>
    b.classList.toggle("active", b.dataset.mediaTab === name)
  );
  setVisible($("media-shorts"), name === "shorts");
  setVisible($("media-uploads"), name === "uploads");
  setVisible($("media-data"), name === "data");
}

document.querySelectorAll("[data-media-tab]").forEach((btn) => {
  btn.addEventListener("click", () => setMediaTab(btn.dataset.mediaTab));
});
$("media-btn").addEventListener("click", openMedia);
$("media-close").addEventListener("click", () => setVisible($("media-modal"), false));
$("media-modal").addEventListener("click", (e) => {
  if (e.target === $("media-modal")) setVisible($("media-modal"), false);
});

function clipCard(clip, videoId) {
  const filename =
    clip.filename || (clip.served_url && clip.served_url.split("/").pop());
  const clipUrl = filename
    ? `/clip/${videoId}/${encodeURIComponent(filename)}`
    : clip.served_url || "";
  const card = document.createElement("div");
  card.className = "short-card";
  card.innerHTML = `
      <div class="score-badge">${esc(clip.score)}</div>
      <video controls playsinline preload="metadata" src="${esc(clipUrl)}"></video>
      <div class="short-body">
        <h3>${esc(clip.title || "Untitled")}</h3>
        <p class="muted"><span class="chip">${fmtTime(clip.start_time)} – ${fmtTime(clip.end_time)}</span></p>
        ${clip.hook_sentence ? `<p class="hook">"${esc(clip.hook_sentence)}"</p>` : ""}
        <a class="btn download" href="${esc(clipUrl)}" download="${esc(filename || "")}">Download</a>
      </div>`;
  return card;
}

function renderMediaShorts(data) {
  const pane = $("media-shorts");
  const clips = [];
  for (const v of data.videos || []) {
    for (const c of v.clips || []) clips.push({ ...c, _videoId: v.id });
  }
  if (!clips.length) {
    pane.innerHTML = `<p class="muted empty">No shorts generated yet.</p>`;
    return;
  }
  const grid = document.createElement("div");
  grid.className = "grid";
  clips.forEach((c) => grid.appendChild(clipCard(c, c._videoId)));
  pane.innerHTML = "";
  pane.appendChild(grid);
}

function renderMediaUploads(data) {
  const pane = $("media-uploads");
  const uploads = (data.videos || []).filter((v) => v.source_video_url);
  if (!uploads.length) {
    pane.innerHTML = `<p class="muted empty">No uploaded videos yet.</p>`;
    return;
  }
  const list = document.createElement("div");
  list.className = "upload-list";
  uploads.forEach((v) => {
    const name = v.source_video_url.split("/").pop();
    const url = `/uploads/${v.id}/${encodeURIComponent(name)}`;
    const item = document.createElement("div");
    item.className = "upload-item";
    item.innerHTML = `
      <video controls preload="metadata" src="${esc(url)}"></video>
      <div class="upload-info">
        <strong>${esc(name)}</strong>
        <span class="status-badge ${esc(v.status)}">${esc(v.status)}</span>
        <span class="muted">${esc(v.created_at || "")}</span>
      </div>`;
    list.appendChild(item);
  });
  pane.innerHTML = "";
  pane.appendChild(list);
}

function renderMediaData(data) {
  const pane = $("media-data");
  const videos = (data.videos || [])
    .slice()
    .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  if (!videos.length) {
    pane.innerHTML = `<p class="muted empty">No jobs processed yet.</p>`;
    return;
  }
  const list = document.createElement("div");
  list.className = "data-list";
  for (const v of videos) {
    const row = document.createElement("details");
    row.className = "data-row";
    row.innerHTML = `
      <summary>
        <span class="status-badge ${esc(v.status)}">${esc(v.status)}</span>
        <span class="data-url">${esc(v.url || "")}</span>
        <span class="muted">${esc(v.created_at || "")}</span>
      </summary>
      <div class="data-detail">
        <p class="muted">video_id: ${esc(v.id)}</p>
        <p class="muted">completed: ${esc(v.completed_at || "—")}</p>
        <p class="muted">burned captions: ${esc(v.has_burned_captions === null || v.has_burned_captions === undefined ? "—" : v.has_burned_captions)}</p>
        ${v.error ? `<p class="error-message">${esc(v.error)}</p>` : ""}
        <button class="btn ghost load-logs" data-job="${esc(v.job_id || "")}">Load logs</button>
        <pre class="log hidden"></pre>
      </div>`;
    list.appendChild(row);
  }
  pane.innerHTML = "";
  pane.appendChild(list);

  pane.querySelectorAll(".load-logs").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const pre = btn.nextElementSibling;
      if (pre.dataset.loaded) {
        setVisible(pre, true);
        return;
      }
      try {
        const res = await api(`/api/jobs/${btn.dataset.job}/logs`);
        pre.textContent = (res.logs || [])
          .map((l) => `[${l.level}] ${l.message}`)
          .join("\n");
        pre.dataset.loaded = "1";
        setVisible(pre, true);
      } catch (ex) {
        pre.textContent = "Failed to load logs: " + ex.message;
        setVisible(pre, true);
      }
    });
  });
}

/* ---------------- boot / URL restore ---------------- */

function bootApp() {
  restoreFormState();
  showApp();
  const jobId = new URLSearchParams(location.search).get("job");
  if (jobId) restoreJob(jobId);
}

async function restoreJob(jobId) {
  let data;
  try {
    data = await api(`/api/jobs/${jobId}`);
  } catch (ex) {
    return; // job gone — stay on the form
  }
  if (!data || data.job_id !== jobId) return;
  CURRENT_JOB = data;
  if (data.status === "completed" && data.result && data.result.shorts) {
    renderResults(data);
    showResults(false);
  } else if (data.status === "queued" || data.status === "running") {
    initializeProgress(data);
    showProgress();
    startPolling(data);
  } else if (data.status === "failed") {
    initializeProgress(data);
    showProgress();
    $("log-tail").textContent = `[FAILED] ${data.error || "unknown error"}`;
    setVisible($("cancel-btn"), false);
  }
}

(async () => {
  if (API_KEY) {
    try {
      await api("/api/verify");
      bootApp();
      return;
    } catch (_) { /* fall through to login */ }
  }
  showLogin();
})();