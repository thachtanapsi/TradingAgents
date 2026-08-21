"use strict";

const uiToken = document.querySelector('meta[name="tradingagents-ui-token"]').content;
const form = document.getElementById("run-form");
const runButton = document.getElementById("run-button");
const tickerInput = document.getElementById("ticker");
const dateInput = document.getElementById("analysis-date");
const dateWrap = document.getElementById("date-wrap");
const collectWrap = document.getElementById("collect-wrap");
const collectInput = document.getElementById("collect-evidence");
const hostedConfirmWrap = document.getElementById("hosted-confirm-wrap");
const hostedConfirm = document.getElementById("confirm-hosted-cost");
const formError = document.getElementById("form-error");
const workspace = document.getElementById("workspace");
const results = document.getElementById("results");
const jobStorageKey = "tradingagents-gx-current-job";
let currentJobId = sessionStorage.getItem(jobStorageKey);
let currentTabs = {};
let activeTab = "technical";
let hostedConfirmationRequired = true;
let lastJobRenderSignature = null;

loadPublicInfo().then(resumeStoredJob);

document.querySelectorAll('input[name="mode"]').forEach((input) => {
  input.addEventListener("change", () => {
    const live = selectedMode() === "live";
    dateWrap.classList.toggle("is-hidden", live);
    collectWrap.classList.toggle("is-hidden", !live);
    if (!live) collectInput.checked = false;
  });
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (runButton.disabled) return;
  formError.textContent = "";
  const ticker = tickerInput.value.trim().toUpperCase();
  const mode = selectedMode();
  if (!/^[A-Z0-9][A-Z0-9._-]{0,15}$/.test(ticker)) {
    formError.textContent = "Mã cổ phiếu không hợp lệ.";
    return;
  }
  if (mode === "close" && !dateInput.value) {
    formError.textContent = "Vui lòng chọn ngày phân tích.";
    return;
  }
  if (hostedConfirmationRequired && !hostedConfirm.checked) {
    formError.textContent = "Vui lòng xác nhận sử dụng hosted LLM trước khi chạy.";
    return;
  }
  const payload = {
    ticker,
    mode,
    analysis_date: mode === "close" ? dateInput.value : null,
    collect_evidence: mode === "live" && collectInput.checked,
    confirm_hosted_cost: hostedConfirm.checked,
  };
  lockRun(true);
  resetView(ticker);
  try {
    const response = await apiFetch("/api/runs", { method: "POST", body: JSON.stringify(payload) });
    const job = await response.json();
    if (!response.ok) throw new Error(job.error || "Không thể bắt đầu phân tích.");
    currentJobId = job.job_id;
    sessionStorage.setItem(jobStorageKey, currentJobId);
    lastJobRenderSignature = null;
    renderJob(job);
    schedulePoll();
  } catch (error) {
    formError.textContent = error.message || "Không thể bắt đầu phân tích.";
    lockRun(false);
  }
});

async function loadPublicInfo() {
  try {
    const response = await apiFetch("/api/info");
    const info = await response.json();
    if (!response.ok) throw new Error("info unavailable");
    hostedConfirmationRequired = Boolean(info.hosted_cost_confirmation_required);
    if (info.latest_close_date) {
      dateInput.max = info.latest_close_date;
      if (dateInput.value > info.latest_close_date) dateInput.value = info.latest_close_date;
    }
    hostedConfirmWrap.classList.toggle("is-hidden", !hostedConfirmationRequired);
    const profiles = info.llm || {};
    const labels = ["quick", "deep"].map((role) => {
      const profile = profiles[role] || {};
      return `${role}: ${profile.provider || "?"}/${profile.model || "?"}`;
    });
    document.getElementById("hosted-profile").textContent = `${labels.join(" · ")}. Hosted API có thể phát sinh chi phí.`;
  } catch (_error) {
    // Fail closed: if profile discovery fails, require explicit confirmation.
    hostedConfirmationRequired = true;
    hostedConfirmWrap.classList.remove("is-hidden");
  }
}

function selectedMode() {
  return document.querySelector('input[name="mode"]:checked').value;
}

function lockRun(locked) {
  runButton.disabled = locked;
  runButton.querySelector("span").textContent = locked ? "Đang phân tích…" : "Chạy phân tích";
}

function resetView(ticker) {
  workspace.classList.remove("is-hidden");
  results.classList.add("is-hidden");
  document.getElementById("run-identity").textContent = `${ticker} · preparing session`;
  document.getElementById("run-warning").classList.add("is-hidden");
  renderProgress([]);
  workspace.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function schedulePoll() {
  if (!currentJobId) return;
  try {
    const response = await apiFetch(`/api/runs/${currentJobId}`);
    const job = await response.json();
    if (!response.ok) {
      const error = new Error(job.error || "Không đọc được trạng thái run.");
      error.status = response.status;
      throw error;
    }
    renderJob(job);
    if (job.status === "queued" || job.status === "running") {
      window.setTimeout(schedulePoll, 1200);
    } else {
      lockRun(false);
      currentJobId = null;
      sessionStorage.removeItem(jobStorageKey);
    }
  } catch (error) {
    if (error.status === 404) {
      currentJobId = null;
      sessionStorage.removeItem(jobStorageKey);
      lockRun(false);
    } else {
      window.setTimeout(schedulePoll, 2000);
    }
    formError.textContent = error.message || "Mất kết nối với local UI.";
  }
}

function resumeStoredJob() {
  if (!currentJobId || !/^[0-9a-f]{32}$/.test(currentJobId)) {
    currentJobId = null;
    sessionStorage.removeItem(jobStorageKey);
    return;
  }
  lockRun(true);
  workspace.classList.remove("is-hidden");
  document.getElementById("run-identity").textContent = "Đang khôi phục run hiện tại…";
  schedulePoll();
}

async function apiFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-TradingAgents-UI-Token", uiToken);
  if (options.body) headers.set("Content-Type", "application/json");
  return fetch(path, { ...options, headers, credentials: "same-origin" });
}

function renderJob(job) {
  const signature = JSON.stringify({
    status: job.status,
    ticker: job.ticker,
    analysis_mode: job.analysis_mode,
    analysis_cutoff: job.analysis_cutoff,
    progress: job.progress || [],
    warnings: job.warnings || [],
    error: job.error || null,
    result: job.result || null,
  });
  if (signature === lastJobRenderSignature) return;
  lastJobRenderSignature = signature;
  const identity = [job.ticker, job.analysis_mode, job.analysis_cutoff || "freezing cutoff"];
  document.getElementById("run-identity").textContent = identity.join(" · ");
  renderProgress(job.progress || []);
  const warnings = [...(job.warnings || [])];
  if (job.error) warnings.push(job.error);
  const warningBox = document.getElementById("run-warning");
  warningBox.textContent = warnings.join("\n");
  warningBox.classList.toggle("is-hidden", warnings.length === 0);
  if (job.result) renderResult(job.result);
}

function renderProgress(rows) {
  const list = document.getElementById("progress-list");
  list.replaceChildren();
  rows.forEach((row) => {
    const item = document.createElement("div");
    item.className = `progress-item ${row.status}`;
    const icon = document.createElement("span");
    icon.className = "progress-icon";
    icon.textContent = row.status === "completed" ? "✓" : row.status === "running" ? "→" : row.status === "failed" || row.status === "unavailable" ? "!" : "○";
    const label = document.createElement("strong");
    label.textContent = row.label;
    const status = document.createElement("small");
    status.textContent = row.status.replace("_", " ");
    item.append(icon, label, status);
    list.append(item);
  });
  const completed = rows.filter((row) => row.status === "completed" || row.status === "unavailable").length;
  const total = rows.length || 7;
  document.getElementById("progress-bar").style.width = `${Math.round(completed / total * 100)}%`;
  document.getElementById("progress-count").textContent = `${completed} / ${total} hoàn tất`;
}

function renderResult(result) {
  results.classList.remove("is-hidden");
  document.getElementById("result-cutoff").textContent = `${result.analysis_mode} · cutoff ${result.analysis_cutoff}`;
  const summary = result.summary || {};
  setSummary("summary-action", summary.recommendation, "Unavailable");
  const action = document.getElementById("summary-action");
  action.className = summary.recommendation || "";
  document.getElementById("summary-rating").textContent = summary.detailed_rating ? `Rating gốc: ${summary.detailed_rating}` : "Chưa có quyết định cuối";
  setSummary("summary-confidence", summary.confidence, "Unavailable");
  document.getElementById("confidence-source").textContent = summary.confidence_source ? "Nguồn: Sentiment Analyst" : "Không suy diễn khi thiếu";
  const targetAvailable = summary.target_price_status === "available"
    && Number.isFinite(summary.target_price)
    && typeof summary.target_price_currency === "string"
    && /^[A-Z]{2,12}$/.test(summary.target_price_currency);
  const target = targetAvailable
    ? formatPrice(summary.target_price, summary.target_price_currency)
    : null;
  setSummary("summary-target", target, "Unavailable");
  const targetReason = typeof summary.target_price_reason === "string"
    ? summary.target_price_reason.trim()
    : "";
  const targetSource = document.getElementById("target-source");
  targetSource.textContent = targetAvailable
    ? ""
    : (targetReason || "Chưa có target đã xác thực");
  targetSource.hidden = targetAvailable;
  setSummary("summary-risk", summary.risk, "Unavailable");
  const chartTarget = targetAvailable && summary.target_price_currency === result.chart_currency
    ? summary.target_price
    : null;
  drawChart(result.chart || [], chartTarget);
  currentTabs = result.tabs || {};
  renderTab(activeTab);
  const finalText = result.final_analysis;
  const finalNode = document.getElementById("final-analysis");
  finalNode.textContent = finalText || "Chưa có quyết định cuối cùng.";
  finalNode.classList.toggle("unavailable", !finalText);
  document.getElementById("final-status").textContent = summary.recommendation || "Unavailable";
}

function setSummary(id, value, fallback) {
  document.getElementById(id).textContent = value || fallback;
}

function formatVnd(value) {
  return `${new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 2 }).format(value)} ₫`;
}

function formatPrice(value, currency) {
  const number = Number(value);
  if (!Number.isFinite(number) || !/^[A-Z]{2,12}$/.test(String(currency || ""))) return "Unavailable";
  return `${new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 2 }).format(number)} ${currency}`;
}

document.getElementById("tabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-tab]");
  if (!button) return;
  activeTab = button.dataset.tab;
  document.querySelectorAll("#tabs button").forEach((node) => node.classList.toggle("active", node === button));
  renderTab(activeTab);
});

function renderTab(tabName) {
  const container = document.getElementById("tab-content");
  container.replaceChildren();
  const sections = currentTabs[tabName] || [];
  sections.forEach((section) => {
    const article = document.createElement("article");
    article.className = "report-section";
    const title = document.createElement("h4");
    title.textContent = section.title || "Report";
    const body = document.createElement("pre");
    body.textContent = section.content || "Unavailable — stage chưa chạy hoặc không có evidence phù hợp cutoff.";
    body.classList.toggle("unavailable", !section.content);
    article.append(title, body);
    container.append(article);
  });
  if (sections.length === 0) {
    const empty = document.createElement("p");
    empty.className = "unavailable";
    empty.textContent = "Unavailable.";
    container.append(empty);
  }
}

function drawChart(points, targetPrice) {
  const svg = document.getElementById("price-chart");
  const empty = document.getElementById("chart-empty");
  svg.replaceChildren();
  if (!Array.isArray(points) || points.length < 2) {
    svg.classList.add("is-hidden");
    empty.classList.remove("is-hidden");
    return;
  }
  empty.classList.add("is-hidden");
  svg.classList.remove("is-hidden");
  const width = 1000, height = 330, left = 72, right = 18, top = 20, bottom = 42;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const values = points.map((point) => Number(point.close)).filter(Number.isFinite);
  const hasTarget = Number.isFinite(targetPrice);
  if (hasTarget) values.push(Number(targetPrice));
  let min = Math.min(...values), max = Math.max(...values);
  const padding = Math.max((max - min) * .1, max * .01, 1);
  min -= padding; max += padding;
  const x = (index) => left + index / (points.length - 1) * (width - left - right);
  const y = (value) => top + (max - value) / (max - min) * (height - top - bottom);

  for (let tick = 0; tick <= 4; tick += 1) {
    const value = max - (max - min) * tick / 4;
    const yPos = y(value);
    addSvg(svg, "line", { x1: left, y1: yPos, x2: width - right, y2: yPos, stroke: "#e7ecef", "stroke-width": 1 });
    const label = addSvg(svg, "text", { x: left - 12, y: yPos + 4, "text-anchor": "end", fill: "#7d8c9a", "font-size": 10 });
    label.textContent = new Intl.NumberFormat("vi-VN", { notation: "compact", maximumFractionDigits: 1 }).format(value);
  }

  const areaPoints = points.map((point, index) => `${x(index)},${y(Number(point.close))}`).join(" ");
  const area = `${left},${height-bottom} ${areaPoints} ${width-right},${height-bottom}`;
  addSvg(svg, "polygon", { points: area, fill: "rgba(62,114,255,.08)" });
  addSvg(svg, "polyline", { points: areaPoints, fill: "none", stroke: "#3e72ff", "stroke-width": 2.5, "stroke-linejoin": "round", "stroke-linecap": "round" });

  if (hasTarget) {
    const yTarget = y(Number(targetPrice));
    addSvg(svg, "line", { x1: left, y1: yTarget, x2: width-right, y2: yTarget, stroke: "#087f70", "stroke-width": 1.25, "stroke-dasharray": "6 5" });
    const label = addSvg(svg, "text", { x: width-right, y: yTarget-7, "text-anchor": "end", fill: "#087f70", "font-size": 10, "font-weight": 700 });
    label.textContent = `Target trong báo cáo: ${formatVnd(targetPrice)}`;
  }
  const first = addSvg(svg, "text", { x: left, y: height-12, fill: "#7d8c9a", "font-size": 10 });
  first.textContent = points[0].date;
  const last = addSvg(svg, "text", { x: width-right, y: height-12, "text-anchor": "end", fill: "#7d8c9a", "font-size": 10 });
  last.textContent = points[points.length-1].date;
}

function addSvg(parent, tag, attributes) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
  parent.append(node);
  return node;
}
