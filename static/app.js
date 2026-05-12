/* ===========================================================================
 * Throughput Tester — front-end controller
 * Single-page app with router across Test / Results / Logs / Settings views.
 * ========================================================================= */
(() => {
'use strict';

// ─── Helpers ────────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const fmt = (v, d = 2) => (v == null || Number.isNaN(v)) ? "—" : Number(v).toFixed(d);
const fmtInt = (v) => (v == null) ? "—" : Math.round(v).toString();
const fmtTime = (ts) => {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString();
};

const LS = "throughput-ui-v2";
const ui = JSON.parse(localStorage.getItem(LS) || "{}");
const saveUI = () => localStorage.setItem(LS, JSON.stringify(ui));

// ─── State ──────────────────────────────────────────────────────────────────
const state = {
  config: {},
  status: {},
  role: "either",
  nodeName: "",
  toolsAvailable: {},
  encoders: {},
  clipsDir: "",
  clips: [],
  history: [],
  testing: false,
  lastByStream: {},
};

// ─── Router ─────────────────────────────────────────────────────────────────
const VIEWS = ["test", "results", "logs", "settings"];
const router = {
  go(view) {
    if (!VIEWS.includes(view)) view = "test";
    $$(".view").forEach((el) => {
      el.hidden = el.dataset.view !== view;
    });
    $$(".nav-item").forEach((el) => {
      el.classList.toggle("active", el.dataset.view === view);
    });
    if (location.hash.slice(1) !== view) {
      history.replaceState(null, "", "#" + view);
    }
    // Per-view hooks
    if (view === "results") refreshResults();
    if (view === "logs") refreshLogList();
    if (view === "settings") refreshSettings();
  },
};

window.addEventListener("hashchange", () => router.go(location.hash.slice(1)));
$$(".nav-item").forEach((a) => {
  a.addEventListener("click", (e) => {
    e.preventDefault();
    router.go(a.dataset.view);
  });
});

// ─── Chart ──────────────────────────────────────────────────────────────────
let chart;
const initChart = () => {
  if (chart) return;
  const ctx = $("chart").getContext("2d");
  chart = new Chart(ctx, {
    type: "line",
    data: {
      labels: [],
      datasets: [
        { label: "Throughput (Mbps)", data: [], borderColor: "#3b82f6",
          backgroundColor: "rgba(59,130,246,0.10)", tension: 0.3,
          borderWidth: 2, pointRadius: 0, fill: true, yAxisID: "y" },
        { label: "Loss (%)", data: [], borderColor: "#ef4444",
          backgroundColor: "rgba(239,68,68,0.06)", tension: 0.3,
          borderWidth: 2, pointRadius: 0, fill: false, yAxisID: "y1" },
        { label: "RTT (ms)", data: [], borderColor: "#f59e0b",
          backgroundColor: "rgba(245,158,11,0.06)", tension: 0.3,
          borderWidth: 2, pointRadius: 0, fill: false, yAxisID: "y1", hidden: true },
      ],
    },
    options: {
      animation: false,
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          position: "bottom", align: "end",
          labels: {
            color: "#a1a1aa", boxWidth: 10, boxHeight: 10,
            font: { size: 11, family: "Inter, system-ui" },
            padding: 14,
          },
        },
        tooltip: {
          backgroundColor: "#18181c", borderColor: "#27272a", borderWidth: 1,
          titleColor: "#fafafa", bodyColor: "#d4d4d8",
          padding: 10, displayColors: false,
        },
      },
      scales: {
        x: {
          ticks: { color: "#71717a", maxTicksLimit: 10, font: { size: 10.5 } },
          grid: { color: "#1f1f24", drawBorder: false },
        },
        y: {
          position: "left",
          ticks: { color: "#71717a", font: { size: 10.5 } },
          grid: { color: "#1f1f24", drawBorder: false },
          title: { display: true, text: "Mbps", color: "#71717a", font: { size: 10.5 } },
        },
        y1: {
          position: "right",
          ticks: { color: "#71717a", font: { size: 10.5 } },
          grid: { display: false, drawBorder: false },
          title: { display: true, text: "Loss / RTT", color: "#71717a", font: { size: 10.5 } },
        },
      },
    },
  });
};
const resetChart = () => {
  if (!chart) return;
  chart.data.labels = [];
  chart.data.datasets.forEach((d) => (d.data = []));
  chart.update("none");
};
const pushSample = (label, throughput, loss, rtt) => {
  if (!chart) return;
  chart.data.labels.push(label);
  chart.data.datasets[0].data.push(throughput);
  chart.data.datasets[1].data.push(loss);
  chart.data.datasets[2].data.push(rtt);
  if (chart.data.labels.length > 240) {
    chart.data.labels.shift();
    chart.data.datasets.forEach((d) => d.data.shift());
  }
  chart.update("none");
};

// ─── Role switch ────────────────────────────────────────────────────────────
const setRole = async (role, push = true) => {
  state.role = role;
  $$(".role-opt").forEach((b) => b.classList.toggle("active", b.dataset.role === role));
  // Update visibility for sender-only / receiver-only
  applyRoleVisibility();
  if (push) {
    try {
      await fetch("/api/role", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ role, node_name: state.nodeName }),
      });
    } catch (e) { /* non-fatal */ }
  }
};
const applyRoleVisibility = () => {
  const role = state.role;
  const isReceiver = role === "receiver";
  $("config-grid").style.display = isReceiver ? "none" : "";
  $("receiver-panel").style.display = isReceiver ? "" : "none";
  $("test-actions").style.display = isReceiver ? "none" : "";
  $("test-sub").textContent = isReceiver
    ? "This node listens for inbound tests. The sender node initiates."
    : "Configure and run a throughput test from this node.";
};
$$(".role-opt").forEach((b) => {
  b.addEventListener("click", () => setRole(b.dataset.role));
});

// ─── Mode / source visibility ───────────────────────────────────────────────
const refreshVisibility = () => {
  const mode = $("mode").value;
  const source = $("source").value;
  const isAuto = (mode === "auto");
  const isVideo = (mode === "srt" || mode === "ffmpeg_udp" || isAuto);
  const isFile = isVideo && source === "file" && !isAuto;
  const isTestpattern = isVideo && source === "testpattern";
  $$(".srt-only").forEach((el) => el.style.display = (mode === "srt" || isAuto) ? "" : "none");
  $$(".video-only").forEach((el) => el.style.display = isVideo ? "" : "none");
  $$(".testpattern-only").forEach((el) => el.style.display = isTestpattern ? "" : "none");
  $$(".file-only").forEach((el) => el.style.display = isFile ? "" : "none");
  $$(".manual-only").forEach((el) => el.style.display = isAuto ? "none" : "");
  $$(".auto-only").forEach((el) => el.style.display = isAuto ? "" : "none");
  // Bitrate visibility for ping
  document.querySelectorAll(".bitrate-field").forEach((el) =>
    el.style.display = mode === "ping" ? "none" : "");
  // KPI relevance per mode -- only show stats the chosen test actually measures.
  // Element opts in via data-modes="srt ping" etc.; if the current mode isn't
  // in that list the KPI tile is hidden.
  document.querySelectorAll(".kpi[data-modes]").forEach((el) => {
    const supported = el.getAttribute("data-modes").split(/\s+/);
    el.style.display = supported.includes(mode) ? "" : "none";
  });
  // Chart series relevance per mode. Datasets are [0]=Throughput, [1]=Loss,
  // [2]=RTT. Toggle the `hidden` flag rather than mutating data so toggling
  // back later works without a reset.
  if (chart) {
    const series = {
      iperf3:     { throughput: true,  loss: true,  rtt: false },
      srt:        { throughput: true,  loss: true,  rtt: true  },
      ffmpeg_udp: { throughput: true,  loss: false, rtt: false },
      ping:       { throughput: false, loss: true,  rtt: true  },
      auto:       { throughput: true,  loss: true,  rtt: true  },
    }[mode] || { throughput: true, loss: true, rtt: false };
    chart.data.datasets[0].hidden = !series.throughput;
    chart.data.datasets[1].hidden = !series.loss;
    chart.data.datasets[2].hidden = !series.rtt;
    chart.update("none");
  }
  // Update bitrate enabled state
  const useNative = isFile && $("bitrate-native").checked;
  $("bitrate-slider").disabled = useNative;
  $("bitrate-num").disabled = useNative;
};

// ─── Bitrate / duration controls ───────────────────────────────────────────
const setBitrate = (v) => {
  v = Math.max(0.1, Number(v) || 0);
  $("bitrate-slider").value = Math.min(100, Math.round(v));
  $("bitrate-num").value = v;
  ui.bitrate = v; saveUI();
};
$("bitrate-slider").addEventListener("input", (e) => setBitrate(e.target.value));
$("bitrate-num").addEventListener("input", (e) => setBitrate(e.target.value));
$("duration-num").addEventListener("change", (e) => { ui.duration = Number(e.target.value); saveUI(); });
$("mode").addEventListener("change", () => { ui.mode = $("mode").value; saveUI(); refreshVisibility(); });
$("source").addEventListener("change", () => { ui.source = $("source").value; saveUI(); refreshVisibility(); refreshTestPatternPreview(); });
$("resolution").addEventListener("change", () => { ui.resolution = $("resolution").value; saveUI(); refreshTestPatternPreview(); });
$("framerate").addEventListener("change", () => { ui.framerate = $("framerate").value; saveUI(); refreshTestPatternPreview(); });
$("bitrate-native").addEventListener("change", () => { ui.bitrateNative = $("bitrate-native").checked; saveUI(); refreshVisibility(); });
$("source-file").addEventListener("change", () => { ui.sourceFile = $("source-file").value; saveUI(); refreshClipInfo(); });
$("streams").addEventListener("change", () => { ui.streams = $("streams").value; saveUI(); });

// ─── Test pattern preview ──────────────────────────────────────────────────
const refreshTestPatternPreview = () => {
  if ($("source").value !== "testpattern") return;
  $("preview-img").src = `/api/preview?resolution=${encodeURIComponent($("resolution").value)}&framerate=${$("framerate").value}&_=${Date.now()}`;
};

// ─── Live preview polling ──────────────────────────────────────────────────
let previewTimer = null;
const startLivePreview = () => {
  if (previewTimer) clearInterval(previewTimer);
  const tick = () => {
    const t = Date.now();
    $("send-preview").src = "/api/preview-send?_=" + t;
    $("recv-preview").src = "/api/preview-recv?_=" + t;
  };
  tick();
  previewTimer = setInterval(tick, 1500);
};
const stopLivePreview = () => {
  if (previewTimer) { clearInterval(previewTimer); previewTimer = null; }
};
["send-preview", "recv-preview"].forEach((id) => {
  $(id).addEventListener("error", (e) => { e.target.style.opacity = "0.25"; });
  $(id).addEventListener("load",  (e) => { e.target.style.opacity = "1"; });
});

// ─── Clips ──────────────────────────────────────────────────────────────────
const refreshClips = async () => {
  try {
    const r = await fetch("/api/clips");
    const data = await r.json();
    state.clips = data.clips || [];
    state.clipsDir = data.clips_dir || "";
    if ($("clips-dir")) $("clips-dir").textContent = state.clipsDir + "/";
    const sel = $("source-file");
    sel.innerHTML = '<option value="">— pick a file —</option>' +
      state.clips.map(c => `<option value="${c.name}">${c.name} (${(c.size/1e6).toFixed(1)} MB)</option>`).join("");
    if (ui.sourceFile) sel.value = ui.sourceFile;
  } catch (e) { console.warn("clips listing failed", e); }
};
const refreshClipInfo = () => {
  const name = $("source-file").value;
  const c = state.clips.find(c => c.name === name);
  $("clip-info").textContent = c
    ? `${c.name} · ${(c.size/1e6).toFixed(1)} MB · ${c.path}`
    : "No clip selected.";
};

// Browse... button — opens a native Open-File dialog via pywebview,
// copies the chosen file into CLIPS_DIR, refreshes the dropdown, and
// selects the new entry.
const browseBtn = document.getElementById("source-file-browse");
if (browseBtn) {
  browseBtn.addEventListener("click", async () => {
    browseBtn.disabled = true;
    browseBtn.textContent = "Opening…";
    try {
      const r = await fetch("/api/pick-clip", { method: "POST" });
      const j = await r.json();
      if (!r.ok) {
        alert("Picker failed: " + (j.error || r.status));
      } else if (!j.cancelled && j.name) {
        await refreshClips();
        $("source-file").value = j.name;
        ui.sourceFile = j.name;
        saveUI();
        refreshClipInfo();
      }
    } catch (e) {
      alert("Picker error: " + e.message);
    } finally {
      browseBtn.disabled = false;
      browseBtn.textContent = "Browse…";
    }
  });
}

// ─── Config ─────────────────────────────────────────────────────────────────
const loadConfig = async () => {
  const [cfg, status, role] = await Promise.all([
    fetch("/api/config").then(r => r.json()),
    fetch("/api/status").then(r => r.json()),
    fetch("/api/role").then(r => r.json()),
  ]);
  state.config = cfg;
  state.status = status;
  state.role = role.role || "either";
  state.nodeName = role.node_name || cfg.node_name || "";
  state.toolsAvailable = status.tools || {};
  state.encoders = status.encoders || {};
  state.app = status.app || {};

  // App version display
  const ve = document.getElementById("app-version-current");
  if (ve) {
    const v = state.app.version || "?";
    const repo = state.app.repo || "";
    ve.textContent = repo && !repo.includes("REPLACE_ME")
      ? `${v} (${repo})` : v;
  }
  // One-shot "✓ Updated to vX" toast, fired the first time /api/status
  // is hit after a successful in-app update (the backend consumes the
  // flag on read, so subsequent polls return false).
  if (state.app.update_just_completed) {
    const prev = state.app.previous_version
      ? ` from ${state.app.previous_version}` : "";
    showToast(`✓ Updated to ${state.app.version}${prev}`, "success", 8000);
  }
  // Hide the in-app updater on platforms where Setup.exe doesn't apply
  // (Linux / Docker / macOS) -- those update via apt / git / docker pull.
  const updateCheckBtn = document.getElementById("update-check");
  if (updateCheckBtn) {
    const supported = state.app.updater_supported;
    updateCheckBtn.style.display = supported ? "" : "none";
    if (!supported) {
      const status = document.getElementById("update-status");
      if (status) {
        status.textContent =
          `Auto-updater is Windows-only. On ${state.app.platform} update via ` +
          `git pull / docker pull / apt as appropriate.`;
      }
    }
  }

  // Top bar
  $("node-name-display").textContent = state.nodeName || `(unnamed @ ${location.host})`;
  $("peer-display").textContent = cfg.peer_host || "not set";

  // Local IP -- shown so the operator knows what to type into the sender's
  // "peer host" field on the other machine. Click-to-copy for convenience.
  const net = status.network || {};
  const ipEl = $("local-ip-display");
  const ipWrap = $("local-ip-wrap");
  if (ipEl) {
    ipEl.textContent = net.primary || "—";
    if (net.all && net.all.length > 1) {
      ipWrap.title = `Click to copy. All addresses: ${net.all.join(", ")}`;
    }
    if (net.primary && !ipWrap._wired) {
      ipWrap._wired = true;
      ipWrap.style.cursor = "pointer";
      ipWrap.addEventListener("click", () => {
        navigator.clipboard?.writeText(net.primary).then(
          () => showToast(`Copied ${net.primary}`, "success", 1500),
          () => {},
        );
      });
    }
  }

  // Form values
  $("mode").value = ui.mode ?? cfg.default_mode ?? "srt";
  $("source").value = ui.source ?? "testpattern";
  $("resolution").value = ui.resolution ?? "1280x720";
  $("framerate").value = ui.framerate ?? "30";
  $("streams").value = ui.streams ?? "1";
  $("bitrate-native").checked = ui.bitrateNative ?? true;
  setBitrate(ui.bitrate ?? cfg.default_bitrate_mbps ?? 10);
  $("duration-num").value = ui.duration ?? cfg.default_duration_s ?? 30;
  $("peer-host").value = ui.peer ?? cfg.peer_host ?? "";
  $("peer-port").value = ui.peerPort ?? cfg.peer_api_port ?? 8080;

  // Tool strip
  const tools = state.toolsAvailable;
  $("tool-strip").innerHTML = Object.entries(tools)
    .map(([k, v]) => `<span class="tool ${v ? "ok" : "bad"}">${k}</span>`).join("");

  setRole(state.role, /*push*/ false);
  refreshVisibility();
  refreshTestPatternPreview();
};

// ─── Test control ───────────────────────────────────────────────────────────
$("peer-host").addEventListener("change", (e) => { ui.peer = e.target.value; saveUI(); $("peer-display").textContent = e.target.value || "not set"; });
$("peer-port").addEventListener("change", (e) => { ui.peerPort = Number(e.target.value); saveUI(); });

$("start").addEventListener("click", async () => {
  resetChart();
  state.lastByStream = {};
  ["throughput","loss","jitter","rtt","retrans","drop"].forEach((k) => $("kpi-"+k).textContent = "—");
  const mode = $("mode").value;
  const source = $("source").value;
  const useNative = (source === "file" && $("bitrate-native").checked);
  const body = {
    mode,
    peer: $("peer-host").value,
    duration: Number($("duration-num").value),
    latency_ms: Number($("srt-latency").value),
  };
  if (mode === "auto") {
    body.start_mbps       = Number($("auto-start").value);
    body.ceiling_mbps     = Number($("auto-ceiling").value);
    body.probe_duration_s = Number($("auto-probe").value);
    body.soak_duration_s  = Number($("auto-soak").value);
    body.loss_pct_max     = Number($("auto-loss").value);
    body.rtt_ms_max       = Number($("auto-rtt").value);
    body.source           = "testpattern";
    body.resolution       = "1280x720";
    body.framerate        = 30;
    resetAutoUI();
  } else if (mode !== "ping") {
    body.source = source;
    body.source_file = $("source-file").value || undefined;
    body.resolution = $("resolution").value;
    body.framerate = Number($("framerate").value);
    body.bitrate_mbps = useNative ? "native" : Number($("bitrate-num").value);
    body.streams = Number($("streams").value);
  } else {
    body.bitrate_mbps = Number($("bitrate-num").value);
  }
  $("start").disabled = true; $("stop").disabled = false;
  setStatus("starting…", "running");
  startLivePreview();
  try {
    const r = await fetch("/api/test/start", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "start failed");
    setStatus(`running · ${mode} · ${body.streams || 1} stream${(body.streams || 1) > 1 ? "s" : ""}`, "running");
  } catch (e) {
    setStatus("error: " + e.message, "error");
    $("start").disabled = false; $("stop").disabled = true;
    stopLivePreview();
  }
});

$("stop").addEventListener("click", async () => {
  await fetch("/api/test/stop", { method: "POST" });
  setStatus("stopping…", "running");
});

const setStatus = (text, cls) => {
  const el = $("status-line");
  el.textContent = text;
  el.className = "card-meta " + (cls || "");
};

// ─── WebSocket ──────────────────────────────────────────────────────────────
let ws;
const dot = $("conn-pill");
const connect = () => {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    dot.classList.add("ok");
    dot.querySelector("span:last-child").textContent = "Connected";
  };
  ws.onclose = () => {
    dot.classList.remove("ok");
    dot.querySelector("span:last-child").textContent = "Disconnected";
    setTimeout(connect, 1500);
  };
  ws.onmessage = (e) => {
    let m;
    try { m = JSON.parse(e.data); } catch { return; }
    if (m.type === "start") {
      resetChart();
      state.lastByStream = {};
      setStatus(`running · ${m.mode} · role=${m.role} · streams=${m.streams || 1}`, "running");
      if (m.role === "receiver" && state.role !== "sender") {
        $("receiver-title").textContent =
          `Connected — receiving ${m.mode} stream`;
        $("receiver-sub").textContent =
          `${m.streams || 1} stream(s) on port ${m.params?.port || "?"}`;
        document.querySelector("#recv-indicator").classList.remove("idle");
        document.querySelector("#recv-indicator").classList.add("active", "pulse");
      }
      startLivePreview();
    } else if (m.type === "sample") {
      handleSample(m);
    } else if (m.type === "done") {
      handleDone(m);
    } else if (m.type === "hello") {
      // session snapshot
    }
  };
};
connect();

// ─── Auto-test UI helpers ───────────────────────────────────────────────────
const resetAutoUI = () => {
  $("auto-result").style.display = "none";
  $("auto-meta").textContent = "starting…";
  ["probe","narrow","soak"].forEach((p) => {
    const el = document.querySelector('.phase[data-phase="' + p + '"]');
    if (el) el.className = "phase";
    const d = $("phase-" + p + "-detail");
    if (d) d.textContent = "—";
  });
  document.querySelector("#auto-attempts tbody").innerHTML = "";
};
const setPhase = (phase, cls, detail) => {
  const el = document.querySelector('.phase[data-phase="' + phase + '"]');
  if (!el) return;
  el.classList.remove("running", "pass", "fail");
  if (cls) el.classList.add(cls);
  if (detail !== undefined) {
    const d = $("phase-" + phase + "-detail");
    if (d) d.textContent = detail;
  }
};
const appendAttempt = (a) => {
  const tb = document.querySelector("#auto-attempts tbody");
  const r = a.result || {};
  const ok = a.status === "pass";
  tb.insertAdjacentHTML("beforeend",
    `<tr>
       <td>${a.phase}</td>
       <td class="num">${fmt(a.target_mbps)}</td>
       <td class="num">${fmt(r.avg_send_mbps)}</td>
       <td class="num">${fmt(r.loss_pct, 3)}</td>
       <td class="num">${fmt(r.max_rtt_ms, 1)}</td>
       <td><span class="row-status ${ok ? 'ok' : 'err'}">${a.status}</span></td>
     </tr>`);
  const wrap = tb.closest(".auto-attempts-wrap");
  if (wrap) wrap.scrollTop = wrap.scrollHeight;
};
const handleAutoSample = (m) => {
  const d = m.data || {};
  const phase = d.phase;
  if (phase === "probe" || phase === "narrow" || phase === "soak") {
    if (d.status === "running") {
      setPhase(phase, "running",
        (phase === "narrow" && d.lo_mbps != null)
          ? `${fmt(d.lo_mbps)}–${fmt(d.hi_mbps)} · trying ${fmt(d.target_mbps)} Mbps`
          : `trying ${fmt(d.target_mbps)} Mbps${phase === "soak" && d.duration_s ? ` for ${d.duration_s}s` : ""}`);
      $("auto-meta").textContent = `${phase} · target ${fmt(d.target_mbps)} Mbps`;
    } else if (d.status === "pass" || d.status === "fail") {
      appendAttempt(d);
      setPhase(phase, d.status,
        `${d.status.toUpperCase()} at ${fmt(d.target_mbps)} Mbps`);
    } else if (d.status === "started") {
      setPhase(phase, "running",
        phase === "probe"
          ? `from ${fmt(d.start_mbps)} → ${fmt(d.ceiling_mbps)} Mbps`
          : `between ${fmt(d.lo_mbps)} – ${fmt(d.hi_mbps)} Mbps`);
    }
  } else if (phase === "done" && d.status === "final") {
    $("auto-result").style.display = "";
    const perStream = d.max_stable_per_stream_mbps ?? d.max_stable_mbps ?? 0;
    const total = d.max_stable_total_mbps ?? (perStream * (d.streams || 1));
    const streams = d.streams || 1;
    $("auto-max-mbps").textContent = fmt(perStream);
    $("auto-streams-count").textContent = streams;
    $("auto-total-mbps").textContent = fmt(total);
    const soak = d.soak_passed;
    const ss = $("auto-soak-state");
    ss.textContent = soak === true ? "Soak passed" : soak === false ? "Soak failed" : "Soak not run";
    ss.parentElement.className = "auto-headline-soak " + (soak === true ? "pass" : soak === false ? "fail" : "");
    $("auto-meta").textContent = `complete · ${d.attempts} attempts · ${streams} stream${streams > 1 ? "s" : ""}`;
    renderRecommendations(d.recommendations, streams);
  }
};

const renderRecommendations = (rec, streams) => {
  const tb = $("recommend-tbody");
  const hi = $("recommend-highlight");
  if (!rec) {
    tb.innerHTML = "";
    hi.classList.add("empty");
    hi.innerHTML = "Not enough headroom to make a recommendation.";
    return;
  }
  const all = [].concat(rec.comfortable || [], rec.tight || [], rec.infeasible || []);
  const top = rec.highest_safe;
  if (top) {
    hi.classList.remove("empty");
    hi.innerHTML =
      `Each stream can comfortably carry <span class="preset-name">${top.preset}</span> ` +
      `at ${fmt(top.bitrate_mbps)} Mbps. With ${streams} stream${streams > 1 ? "s" : ""} running ` +
      `concurrently, the total ingress is around ${fmt(top.bitrate_mbps * streams)} Mbps.`;
  } else {
    hi.classList.add("empty");
    hi.innerHTML =
      `Network capacity is below the lowest preset (480p30 ≈ 1.5 Mbps). ` +
      `Reduce stream count or improve the link.`;
  }
  tb.innerHTML = all.map((p) => {
    const fitLabel = p.fit === "comfortable" ? "Comfortable"
                   : p.fit === "tight"       ? "Tight (edge of capacity)"
                                              : "Won't fit";
    return `<tr class="${p.fit}">
      <td class="mono">${p.preset}</td>
      <td class="num">${fmt(p.bitrate_mbps)} Mbps</td>
      <td>${p.description}</td>
      <td><span class="fit-${p.fit}">${fitLabel}</span></td>
    </tr>`;
  }).join("");
};

// ─── Sample handling ────────────────────────────────────────────────────────
const handleSample = (m) => {
  if (m.mode === "auto") { handleAutoSample(m); return; }
  const d = m.data || {};
  const sid = m.stream_id || 0;
  const label = (d.end_s != null) ? d.end_s.toFixed(0) + "s" : new Date().toLocaleTimeString().slice(3, 8);
  let throughput = null, loss = null, rtt = null, jitter = null;
  let retrans = null, drop = null;
  if (m.mode === "iperf3") {
    throughput = d.throughput_mbps;
    loss = d.loss_pct; jitter = d.jitter_ms;
  } else if (m.mode === "srt") {
    throughput = m.role === "sender" ? d.mbpsSendRate : d.mbpsRecvRate;
    rtt = d.msRTT;
    retrans = (m.role === "sender") ? d.pktRetransTotal : d.pktRcvRetransTotal;
    drop = (m.role === "sender") ? d.pktSndDropTotal : d.pktRcvDropTotal;
    const sent = (m.role === "sender") ? d.pktSentTotal : d.pktRecvTotal;
    const lost = (m.role === "sender") ? d.pktSndLossTotal : d.pktRcvLossTotal;
    if (sent && lost != null) loss = (100 * lost / sent);
  } else if (m.mode === "ffmpeg_udp") {
    throughput = (m.role === "sender") ? d.send_mbps : d.recv_mbps;
  } else if (m.mode === "ping") {
    rtt = d.rtt_ms;
  }
  state.lastByStream[sid] = { throughput, loss, jitter, rtt, retrans, drop, ts: Date.now() };
  const streams = Object.values(state.lastByStream).filter(s => Date.now() - s.ts < 4000);
  const sum = (k) => streams.reduce((a, s) => a + (s[k] || 0), 0);
  const max = (k) => streams.reduce((a, s) => Math.max(a, s[k] ?? 0), 0);
  const totalThroughput = sum("throughput");
  const maxLoss = streams.some(s => s.loss != null) ? max("loss") : null;
  const maxJitter = streams.some(s => s.jitter != null) ? max("jitter") : null;
  const maxRtt = streams.some(s => s.rtt != null) ? max("rtt") : null;
  const totalRetrans = streams.some(s => s.retrans != null) ? sum("retrans") : null;
  const totalDrop = streams.some(s => s.drop != null) ? sum("drop") : null;
  if (totalThroughput) $("kpi-throughput").textContent =
     fmt(totalThroughput, 2) + (streams.length > 1 ? ` Σ${streams.length}` : "");
  if (maxLoss != null)    $("kpi-loss").textContent = fmt(maxLoss, 2);
  if (maxJitter != null)  $("kpi-jitter").textContent = fmt(maxJitter, 2);
  if (maxRtt != null)     $("kpi-rtt").textContent = fmt(maxRtt, 1);
  if (totalRetrans != null) $("kpi-retrans").textContent = fmtInt(totalRetrans);
  if (totalDrop != null)    $("kpi-drop").textContent = fmtInt(totalDrop);
  pushSample(label, totalThroughput || throughput, maxLoss ?? loss, maxRtt ?? rtt);
};

const handleDone = (m) => {
  const s = m.summary || {};
  $("start").disabled = false; $("stop").disabled = true;
  stopLivePreview();
  if (s.error) setStatus("error: " + s.error, "error");
  else setStatus("done · last run completed", "done");
  document.querySelector("#recv-indicator").classList.remove("active");
  document.querySelector("#recv-indicator").classList.add("pulse");
  // Receiver: revert to idle state once the test has completed.
  if (state.role !== "sender") {
    const t = document.getElementById("receiver-title");
    const sub = document.getElementById("receiver-sub");
    if (t) t.textContent = "Listening for inbound tests";
    if (sub) sub.textContent = "Last test completed. Ready for the next one.";
  }
  // refresh results if user is on that view
  if (!document.querySelector('[data-view="results"]').hidden) refreshResults();
};

// ─── Results view ───────────────────────────────────────────────────────────
const refreshResults = async () => {
  try {
    const r = await fetch("/api/history?limit=200");
    const rows = await r.json();
    state.history = rows;
    renderResults();
  } catch (e) {
    console.warn("history load failed", e);
  }
};
const renderResults = () => {
  const search = $("results-search").value.trim().toLowerCase();
  const fMode = $("results-filter-mode").value;
  const fRole = $("results-filter-role").value;
  const filtered = state.history.filter((row) => {
    if (fMode && row.mode !== fMode) return false;
    if (fRole && row.role !== fRole) return false;
    if (search) {
      const blob = JSON.stringify(row).toLowerCase();
      if (!blob.includes(search)) return false;
    }
    return true;
  });
  const tb = $("results-body");
  if (filtered.length === 0) {
    tb.innerHTML = "";
    $("results-empty").style.display = "";
    return;
  }
  $("results-empty").style.display = "none";
  tb.innerHTML = filtered.map((row) => {
    const sum = pickFinalSummary(row.summary);
    const params = row.params || {};
    const status = sum.error
      ? '<span class="row-status err">error</span>'
      : (sum.return_code != null && sum.return_code !== 0
          ? '<span class="row-status partial">rc=' + sum.return_code + '</span>'
          : '<span class="row-status ok">ok</span>');
    return `<tr data-id="${row.id}">
      <td class="mono small">${fmtTime(row.started)}</td>
      <td><span class="row-mode ${row.mode}">${row.mode}</span></td>
      <td>${row.role || "—"}</td>
      <td class="num">${params.bitrate_mbps ?? "—"}</td>
      <td class="num">${fmt(sum.throughput_mbps)}</td>
      <td class="num">${fmt(sum.loss_pct)}</td>
      <td class="num">${fmt(sum.jitter_ms)}</td>
      <td class="num">${fmt(sum.rtt_avg_ms ?? sum.msRTT)}</td>
      <td>${status}</td>
      <td><button class="btn btn-ghost" data-act="logs" data-id="${row.id}">Logs</button></td>
    </tr>`;
  }).join("");
  // Wire row "Logs" buttons
  tb.querySelectorAll('button[data-act="logs"]').forEach((b) => {
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      router.go("logs");
      setTimeout(() => loadLog(b.dataset.id), 50);
    });
  });
};

const pickFinalSummary = (summary) => {
  if (!summary) return {};
  // New session format stores summary["stream_0"] = {…} per stream.
  // Fall back to flat shape (older history rows).
  if (summary.stream_0) return summary.stream_0;
  return summary;
};

$("results-search").addEventListener("input", renderResults);
$("results-filter-mode").addEventListener("change", renderResults);
$("results-filter-role").addEventListener("change", renderResults);
$("results-refresh").addEventListener("click", refreshResults);

$("results-export-json").addEventListener("click", () => {
  const blob = new Blob([JSON.stringify(state.history, null, 2)], {type: "application/json"});
  triggerDownload(blob, `throughput-history-${Date.now()}.json`);
});
$("results-export-csv").addEventListener("click", () => {
  const rows = state.history.map(r => {
    const s = pickFinalSummary(r.summary);
    const p = r.params || {};
    return [
      new Date((r.started||0)*1000).toISOString(),
      r.mode, r.role, p.bitrate_mbps, p.duration, p.streams,
      s.throughput_mbps ?? "", s.loss_pct ?? "",
      s.jitter_ms ?? "", s.rtt_avg_ms ?? s.msRTT ?? "",
      s.error ?? "",
    ].map(v => (v == null ? "" : String(v).replace(/"/g, '""'))).map(v => `"${v}"`).join(",");
  });
  const header = '"started","mode","role","bitrate_mbps","duration","streams","throughput_mbps","loss_pct","jitter_ms","rtt_ms","error"';
  const blob = new Blob([header + "\n" + rows.join("\n")], {type: "text/csv"});
  triggerDownload(blob, `throughput-history-${Date.now()}.csv`);
});
const triggerDownload = (blob, name) => {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
};

// ─── Logs view ──────────────────────────────────────────────────────────────
let currentLogId = null;
const refreshLogList = async () => {
  try {
    const r = await fetch("/api/logs?list=1");
    const data = await r.json();
    const sel = $("logs-select");
    sel.innerHTML = '<option value="">— select a log —</option>' +
      (data.logs || []).map(l => {
        const t = new Date(l.mtime * 1000).toLocaleString();
        return `<option value="${l.id}">${t} · ${l.id} · ${(l.size/1024).toFixed(1)} KB</option>`;
      }).join("");
    if (currentLogId) sel.value = currentLogId;
    if (!sel.value && data.logs && data.logs[0]) loadLog(data.logs[0].id);
  } catch (e) {
    console.warn("logs list failed", e);
  }
};
const loadLog = async (id) => {
  currentLogId = id;
  $("logs-select").value = id;
  try {
    const r = await fetch(`/api/logs?id=${encodeURIComponent(id)}`);
    const data = await r.json();
    renderLog(data.lines || []);
  } catch (e) {
    $("log-output").textContent = "Failed to load log: " + e.message;
  }
};
const renderLog = (lines) => {
  const filter = $("logs-search").value.toLowerCase();
  const out = $("log-output");
  const fragments = lines
    .filter(l => !filter || l.toLowerCase().includes(filter))
    .map(l => {
      let cls = "";
      if (/error|fatal|exc|fail/i.test(l)) cls = "l-err";
      else if (/warn/i.test(l)) cls = "l-warn";
      else if (/^=====/.test(l)) cls = "l-marker";
      return `<span class="${cls}">${escapeHtml(l)}</span>`;
    });
  out.innerHTML = fragments.join("\n");
  if ($("logs-autoscroll").checked) out.scrollTop = out.scrollHeight;
};
const escapeHtml = (s) => s.replace(/[&<>"']/g, c => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));
$("logs-select").addEventListener("change", (e) => { if (e.target.value) loadLog(e.target.value); });
$("logs-search").addEventListener("input", () => {
  if (currentLogId) loadLog(currentLogId);
});
$("logs-download").addEventListener("click", async () => {
  if (!currentLogId) return;
  const r = await fetch(`/api/logs?id=${encodeURIComponent(currentLogId)}`);
  const data = await r.json();
  const blob = new Blob([data.lines.join("\n")], {type: "text/plain"});
  triggerDownload(blob, `test_${currentLogId}.log`);
});
// Auto-poll the current log every 2s while a test is running
setInterval(() => {
  if (state.testing && currentLogId) loadLog(currentLogId);
}, 2000);

// ─── Settings view ──────────────────────────────────────────────────────────
const refreshSettings = () => {
  const cfg = state.config;
  $("set-node-name").value = state.nodeName;
  $("set-node-role").value = state.role;
  $("set-peer-host").value = cfg.peer_host || "";
  $("set-peer-port").value = cfg.peer_api_port || 8080;
  $("set-default-mode").value = cfg.default_mode || "srt";
  $("set-default-bitrate").value = cfg.default_bitrate_mbps || 10;
  $("set-default-duration").value = cfg.default_duration_s || 30;
  // Environment checklist
  const tools = state.toolsAvailable;
  const enc = state.encoders;
  // srt-live-transmit isn't shipped on Windows (no upstream binary).
  // SRT mode still works via ffmpeg's bundled libsrt -- mark it neutral,
  // not "bad", so users don't think their install is broken.
  const rows = [
    ...Object.entries(tools).map(([k, v]) => {
      if (k === "srt-live-transmit" && !v) {
        return {
          name: k,
          ok: true,                                    // neutral, not red
          kind: "using ffmpeg libsrt fallback",
        };
      }
      return { name: k, ok: v, kind: v ? "installed" : "not found" };
    }),
    { name: "h264_v4l2m2m (Pi 4 HW encoder)", ok: !!enc.h264_v4l2m2m, kind: enc.h264_v4l2m2m ? "available" : "absent" },
    { name: "clips directory", ok: true, kind: state.clipsDir || "—" },
  ];
  $("env-checklist").innerHTML = rows.map(r => `
    <div class="check-row">
      <span class="check-name">${r.name}</span>
      <span class="check-status ${r.ok ? 'ok' : 'bad'}">${r.kind}</span>
    </div>
  `).join("");
};
// ─── Updater ────────────────────────────────────────────────────────────────
const setUpdateStatus = (msg, kind) => {
  const el = $("update-status");
  el.textContent = msg || "";
  el.style.color = kind === "error" ? "#f87171"
                  : kind === "ok" ? "#86efac" : "";
};
const showInstallBtn = (show) => {
  $("update-action").style.display = show ? "" : "none";
};
let pendingAssetUrl = null;
const setUpdateBadge = (show) => {
  const b = document.getElementById("update-badge");
  if (b) b.style.display = show ? "" : "none";
};
// Silent background check on startup (and once an hour after), so users
// land on a Settings tab with the orange dot already telling them an
// update exists -- no manual "Check for updates" click needed.
const silentUpdateCheck = async () => {
  if (!state.app || !state.app.updater_supported) return;
  try {
    const r = await fetch("/api/check-update");
    const j = await r.json();
    if (j.status === "update-available") {
      setUpdateBadge(true);
      // Also pre-populate the Settings panel so the user lands ready.
      setUpdateStatus(j.message + (j.asset_size
        ? ` (${(j.asset_size / 1024 / 1024).toFixed(1)} MB)` : ""), "ok");
      pendingAssetUrl = j.asset_url;
      showInstallBtn(Boolean(j.asset_url));
    } else {
      setUpdateBadge(false);
    }
  } catch { /* silent */ }
};
$("update-check").addEventListener("click", async () => {
  setUpdateStatus("checking…");
  showInstallBtn(false);
  try {
    const r = await fetch("/api/check-update");
    const j = await r.json();
    if (j.status === "update-available") {
      setUpdateStatus(j.message + (j.asset_size
        ? ` (${(j.asset_size / 1024 / 1024).toFixed(1)} MB)` : ""), "ok");
      pendingAssetUrl = j.asset_url;
      showInstallBtn(Boolean(j.asset_url));
      setUpdateBadge(true);
    } else if (j.status === "up-to-date") {
      setUpdateStatus(j.message, "ok");
    } else if (j.status === "up-to-date") {
      setUpdateStatus(j.message, "ok");
      setUpdateBadge(false);
    } else if (j.status === "rate-limited" || j.status === "no-releases" || j.status === "dev-build" || j.status === "not-configured") {
      // Informational, not an error. GitHub's 60-req/hr anon quota is
      // easy to burn through during dev; show in muted text, not red.
      setUpdateStatus(j.message || j.status, "");
      setUpdateBadge(false);
    } else {
      setUpdateStatus(j.message || j.status, "error");
      setUpdateBadge(false);
    }
  } catch (e) {
    setUpdateStatus("check failed: " + e.message, "error");
  }
});
// Toast helper -- auto-dismisses after timeout ms.
const showToast = (msg, kind, timeout = 6000) => {
  const wrap = document.getElementById("toast-container");
  if (!wrap) return;
  const el = document.createElement("div");
  el.className = "toast" + (kind ? ` ${kind}` : "");
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), timeout);
};

// Full-screen overlay used during the install phase.
const showUpdateOverlay = (show) => {
  const ov = document.getElementById("update-overlay");
  if (ov) ov.style.display = show ? "flex" : "none";
};
const setOverlayProgress = (phase, downloaded, total, message) => {
  const titleEl = document.getElementById("update-overlay-title");
  const subEl   = document.getElementById("update-overlay-sub");
  const barEl   = document.getElementById("update-overlay-bar");
  const noteEl  = document.getElementById("update-overlay-note");
  const inlineBar   = document.getElementById("update-progress-bar");
  const inlineLabel = document.getElementById("update-progress-label");
  const wrap = document.getElementById("update-progress-wrap");
  if (wrap) wrap.style.display = "";
  const pct = total > 0 ? Math.min(100, Math.round(100 * downloaded / total)) : 0;
  if (barEl) barEl.style.width = pct + "%";
  if (inlineBar) inlineBar.style.width = pct + "%";
  const human = (b) => (b / 1024 / 1024).toFixed(1) + " MB";
  let summary = message || phase;
  if (phase === "downloading" && total) {
    summary = `Downloading ${human(downloaded)} / ${human(total)} (${pct}%)`;
  } else if (phase === "installing") {
    summary = "Installing — the app will close and reopen…";
  }
  if (subEl) subEl.textContent = summary;
  if (inlineLabel) inlineLabel.textContent = summary;
  if (titleEl) titleEl.textContent =
    phase === "installing" ? "Installing update" : "Downloading update";
  if (noteEl) {
    noteEl.textContent = phase === "installing"
      ? "The app will close and reopen automatically. Don't close this window."
      : "This usually takes 5–15 seconds on a fast connection.";
  }
};

// Poll /api/update-progress every 400ms while installing.
let updatePollHandle = null;
const startUpdatePolling = () => {
  if (updatePollHandle) return;
  updatePollHandle = setInterval(async () => {
    try {
      const r = await fetch("/api/update-progress");
      const s = await r.json();
      setOverlayProgress(s.phase, s.downloaded, s.total, s.message);
      if (s.phase === "error") {
        clearInterval(updatePollHandle); updatePollHandle = null;
        showUpdateOverlay(false);
        $("update-install").disabled = false;
        setUpdateStatus("Update failed: " + (s.error || s.message), "error");
        showToast("Update failed: " + (s.error || s.message), null, 10000);
      }
      // When phase=installing, the app is about to exit. The next poll
      // will throw -- the catch below treats that as "we're restarting".
    } catch (e) {
      // Network error = app is exiting for the installer. That's the
      // normal happy path; switch the overlay to a final "restarting" state.
      clearInterval(updatePollHandle); updatePollHandle = null;
      const title = document.getElementById("update-overlay-title");
      const sub   = document.getElementById("update-overlay-sub");
      const bar   = document.getElementById("update-overlay-bar");
      const note  = document.getElementById("update-overlay-note");
      if (title) title.textContent = "Restarting…";
      if (sub)   sub.textContent   = "Installer is running. The new version will open in a few seconds.";
      if (bar)   { bar.style.width = "100%"; }
      if (note)  note.textContent = "You can safely close this window if it doesn't auto-close.";
    }
  }, 400);
};

$("update-install").addEventListener("click", async () => {
  if (!confirm("Download and install the update? The app will close and "
             + "reopen automatically.")) return;
  $("update-install").disabled = true;
  showUpdateOverlay(true);
  setOverlayProgress("downloading", 0, 0, "Starting download…");
  try {
    const r = await fetch("/api/install-update", { method: "POST" });
    const j = await r.json();
    if (j.ok) {
      startUpdatePolling();
    } else {
      setUpdateStatus(j.error || "install failed", "error");
      showUpdateOverlay(false);
      $("update-install").disabled = false;
    }
  } catch (e) {
    // Network error here is expected once the app exits — ignore.
    setUpdateStatus("Installer launched — closing app…", "ok");
  }
});

$("settings-save").addEventListener("click", async () => {
  const newCfg = {
    peer_host: $("set-peer-host").value,
    peer_api_port: Number($("set-peer-port").value),
    default_mode: $("set-default-mode").value,
    default_bitrate_mbps: Number($("set-default-bitrate").value),
    default_duration_s: Number($("set-default-duration").value),
  };
  state.nodeName = $("set-node-name").value;
  try {
    await Promise.all([
      fetch("/api/config", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(newCfg),
      }),
      fetch("/api/role", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ role: $("set-node-role").value, node_name: state.nodeName }),
      }),
    ]);
    $("settings-save-status").textContent = "Saved";
    setTimeout(() => $("settings-save-status").textContent = "", 2000);
    // Re-sync visible state
    await loadConfig();
  } catch (e) {
    $("settings-save-status").textContent = "save failed: " + e.message;
  }
});

// ─── Boot ───────────────────────────────────────────────────────────────────
const boot = async () => {
  initChart();
  await loadConfig();
  await refreshClips();
  refreshClipInfo();
  router.go(location.hash.slice(1) || "test");
  // Quietly check for app updates on boot + once an hour. Surfaces an
  // orange dot on the Settings nav item if a newer release exists; the
  // user can then click through to Settings to install.
  silentUpdateCheck();
  setInterval(silentUpdateCheck, 60 * 60 * 1000);
  // periodic refresh of node status
  setInterval(async () => {
    try {
      const r = await fetch("/api/status");
      const s = await r.json();
      state.testing = !!s.session?.active;
    } catch (e) {}
  }, 5000);
};
boot();

})();
