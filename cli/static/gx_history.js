"use strict";

const uiToken = document.querySelector('meta[name="tradingagents-ui-token"]').content;
const filterForm = document.getElementById("filter-form");
const historyList = document.getElementById("history-list");
const listEmpty = document.getElementById("list-empty");
const viewer = document.getElementById("viewer");
const viewerEmpty = document.getElementById("viewer-empty");
const notice = document.getElementById("page-notice");
const previousPage = document.getElementById("previous-page");
const nextPage = document.getElementById("next-page");
const pageSize = 20;
const historyIdPattern = /^[0-9a-f]{64}$/;
const allowedStatuses = new Set(["completed", "running", "queued", "failed", "partial", "not_started", "unavailable", "not_run"]);
const provenanceContainers = new Set(["retail_social_signal", "media_tone", "official_disclosures", "editorial_media", "vn_macro", "sources", "source_results"]);
const provenanceScalars = ["provider", "vendor", "tool", "kind", "category", "ticker", "analysis_date", "status", "reason", "sample_size", "unique_authors", "count", "article_count", "observation_count", "point_in_time_quality", "window_start", "window_end", "as_of", "published_at", "period_start", "period_end", "fetch_id", "fetch_ids", "vendor_chain", "attempted_vendors", "input_stages", "actual_vendor_observed", "stale", "stale_indicators", "analysis_mode", "analysis_cutoff", "completed_at", "unavailable_at", "failed_at", "source_provider", "source_series", "source_url", "canonical_url", "url"];

const statusLabels = {
  completed: "Hoàn tất",
  running: "Đang chạy",
  queued: "Đang chờ",
  failed: "Thất bại",
  partial: "Một phần",
  not_started: "Chưa chạy",
  unavailable: "Không khả dụng",
  not_run: "Chưa chạy",
};

const sections = [
  { id: "overview", label: "Tổng quan", description: "Thông tin quan trọng nhất của phiên phân tích" },
  { id: "decision", label: "Quyết định cuối", description: "Kết luận của Portfolio Manager" },
  { id: "technical", label: "Phân tích kỹ thuật", description: "Xu hướng giá, động lượng và các vùng tham chiếu" },
  { id: "fundamentals", label: "Phân tích cơ bản", description: "Kết quả kinh doanh, định giá và sức khỏe tài chính" },
  { id: "sentiment", label: "Tâm lý thị trường", description: "Media tone, retail social signal và mức độ đồng thuận" },
  { id: "news", label: "Tin tức", description: "Tin doanh nghiệp, công bố chính thức và bối cảnh vĩ mô" },
  { id: "plans", label: "Kế hoạch đầu tư", description: "Luận điểm nghiên cứu và kế hoạch của Trader Agent" },
  { id: "debates", label: "Tranh luận", description: "Quan điểm Bull/Bear và ba Risk Analyst" },
  { id: "sources", label: "Nguồn dữ liệu", description: "Provenance và cảnh báo đã được làm sạch" },
];

const state = {
  page: 1,
  totalPages: 1,
  selectedId: null,
  activeSection: "overview",
  detailRequest: 0,
};

initializeTheme();
bindEvents();
loadHistory();

function bindEvents() {
  filterForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const from = document.getElementById("from-date").value;
    const to = document.getElementById("to-date").value;
    if (from && to && from > to) {
      showNotice("Khoảng ngày không hợp lệ: Từ ngày phải trước hoặc bằng Đến ngày.");
      return;
    }
    hideNotice();
    state.page = 1;
    state.selectedId = null;
    clearFragment();
    loadHistory();
  });

  previousPage.addEventListener("click", () => {
    if (state.page <= 1) return;
    state.page -= 1;
    state.selectedId = null;
    clearFragment();
    loadHistory();
  });

  nextPage.addEventListener("click", () => {
    if (state.page >= state.totalPages) return;
    state.page += 1;
    state.selectedId = null;
    clearFragment();
    loadHistory();
  });

  document.getElementById("refresh-button").addEventListener("click", () => loadHistory({ preserveSelection: true }));
  document.getElementById("theme-button").addEventListener("click", toggleTheme);
  document.getElementById("print-button").addEventListener("click", () => {
    if (!viewer.classList.contains("is-hidden")) window.print();
  });

  window.addEventListener("hashchange", () => {
    const id = fragmentHistoryId();
    if (id && id !== state.selectedId) loadDetail(id, { updateFragment: false });
  });
}

async function loadHistory(options = {}) {
  const preserveSelection = Boolean(options.preserveSelection);
  const params = new URLSearchParams({ page: String(state.page), page_size: String(pageSize) });
  const values = new FormData(filterForm);
  ["query", "mode", "status", "from", "to"].forEach((key) => {
    const value = String(values.get(key) || "").trim();
    if (value) params.set(key, value);
  });

  setListLoading();
  try {
    const response = await apiFetch(`/api/history?${params.toString()}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Không thể tải lịch sử research.");
    const items = Array.isArray(payload.items) ? payload.items : [];
    state.page = positiveInteger(payload.page, 1);
    state.totalPages = Math.max(1, positiveInteger(payload.total_pages, 1));
    renderList(items, payload);
    hideNotice();

    const fragmentId = fragmentHistoryId();
    let nextId = preserveSelection && state.selectedId ? state.selectedId : fragmentId;
    if (!nextId && items.length) nextId = items[0].history_id;
    if (nextId && historyIdPattern.test(nextId)) {
      await loadDetail(nextId, { updateFragment: !fragmentId, closeDrawer: false });
    } else if (!items.length) {
      clearViewer();
    }
  } catch (error) {
    renderList([], { total: 0, page: 1, total_pages: 1, skipped_invalid: 0 });
    clearViewer();
    showNotice(error.message || "Không thể tải lịch sử research.");
  }
}

function setListLoading() {
  document.getElementById("result-count").textContent = "Đang tải…";
  document.getElementById("skipped-count").textContent = "";
  historyList.replaceChildren();
  listEmpty.classList.add("is-hidden");
}

function renderList(items, payload) {
  historyList.replaceChildren();
  const total = Math.max(0, Number(payload.total) || 0);
  const skipped = Math.max(0, Number(payload.skipped_invalid) || 0);
  document.getElementById("result-count").textContent = `${new Intl.NumberFormat("vi-VN").format(total)} research`;
  document.getElementById("skipped-count").textContent = skipped ? `${skipped} session lỗi đã được bỏ qua` : "";
  document.getElementById("page-label").textContent = `20 / trang`;
  document.getElementById("pagination-label").textContent = `Trang ${state.page} / ${state.totalPages}`;
  previousPage.disabled = state.page <= 1;
  nextPage.disabled = state.page >= state.totalPages;
  listEmpty.classList.toggle("is-hidden", items.length > 0);

  items.forEach((item) => {
    if (!historyIdPattern.test(String(item.history_id || ""))) return;
    const button = createElement("button", "history-item");
    button.type = "button";
    button.dataset.historyId = item.history_id;
    button.classList.toggle("active", item.history_id === state.selectedId);
    button.setAttribute("aria-label", `Mở research ${plain(item.ticker, "—")} ngày ${plain(item.analysis_date, "—")}`);

    const row = createElement("span", "item-row");
    row.append(createElement("span", "item-ticker", plain(item.ticker, "—")));
    row.append(statusPill(item.status, "item-status"));
    button.append(row);
    button.append(createElement("span", "item-company", plain(item.company_name, "Báo cáo phân tích đầu tư")));

    const meta = createElement("span", "item-meta");
    meta.append(createElement("span", "", `${formatDate(item.analysis_date)} · ${modeLabel(item.analysis_mode)}`));
    const recommendation = item.summary?.detailed_rating || item.summary?.recommendation || `${Number(item.completed_stages) || 0}/${Number(item.total_stages) || 0} stage`;
    meta.append(createElement("span", "item-recommendation", plain(recommendation, "—")));
    button.append(meta);
    button.addEventListener("click", () => loadDetail(item.history_id));
    historyList.append(button);
  });
}

async function loadDetail(historyId, options = {}) {
  if (!historyIdPattern.test(String(historyId || ""))) return;
  const requestNumber = ++state.detailRequest;
  try {
    const response = await apiFetch(`/api/history/${historyId}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Không thể mở research đã chọn.");
    if (requestNumber !== state.detailRequest) return;
    state.selectedId = historyId;
    state.activeSection = "overview";
    if (options.updateFragment !== false) setFragment(historyId);
    renderDetail(payload);
    markSelectedListItem();
    hideNotice();
  } catch (error) {
    if (requestNumber !== state.detailRequest) return;
    showNotice(error.message || "Không thể mở research đã chọn.");
  }
}

function renderDetail(detail, options = {}) {
  const requestedSection = options.transient && sections.some((item) => item.id === state.activeSection)
    ? state.activeSection
    : "overview";
  viewerEmpty.classList.add("is-hidden");
  viewer.classList.remove("is-hidden");
  document.getElementById("hero-ticker").textContent = plain(detail.ticker, "—");
  document.getElementById("company-name").textContent = plain(detail.company_name, "Báo cáo phân tích đầu tư");
  const status = normalizeStatus(detail.status, "partial");
  document.getElementById("run-status").textContent = statusLabels[status] || status;
  document.getElementById("status-dot").className = `status-dot ${status}`;

  const heroMeta = document.getElementById("hero-meta");
  heroMeta.replaceChildren();
  [
    modeLabel(detail.analysis_mode),
    formatDate(detail.analysis_date),
    detail.analysis_cutoff ? `Cutoff ${formatDate(detail.analysis_cutoff, true)}` : null,
  ].filter(Boolean).forEach((value) => heroMeta.append(createElement("span", "pill", value)));

  document.getElementById("run-info").textContent = [
    detail.run_id ? `Run ${detail.run_id}` : null,
    detail.updated_at ? `Cập nhật ${formatDate(detail.updated_at, true)}` : null,
  ].filter(Boolean).join("\n");

  renderSectionNavigation();
  renderReportSections(detail);
  showSection(requestedSection);
  if (!options.transient) viewer.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderSectionNavigation() {
  const nav = document.getElementById("section-nav");
  nav.replaceChildren();
  sections.forEach((definition) => {
    const button = createElement("button", "section-button", definition.label);
    button.type = "button";
    button.dataset.section = definition.id;
    button.addEventListener("click", () => showSection(definition.id));
    nav.append(button);
  });
}

function renderReportSections(detail) {
  const content = document.getElementById("report-content");
  content.replaceChildren();
  sections.forEach((definition) => {
    const section = createElement("section", "report-section");
    section.dataset.section = definition.id;
    const heading = createElement("header", "section-heading");
    const headingText = document.createElement("div");
    headingText.append(createElement("h3", "", definition.label));
    headingText.append(createElement("p", "", definition.description));
    heading.append(headingText);
    if (!["overview", "sources"].includes(definition.id)) {
      const copyButton = createElement("button", "button section-copy", "Sao chép");
      copyButton.type = "button";
      copyButton.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(section.textContent || "");
          showNotice(`Đã sao chép phần ${definition.label}.`);
        } catch (_error) {
          showNotice("Trình duyệt không cho phép sao chép tự động.");
        }
      });
      heading.append(copyButton);
    }
    section.append(heading);
    renderSectionBody(section, definition.id, detail);
    content.append(section);
  });
}

function renderSectionBody(section, sectionId, detail) {
  if (sectionId === "overview") renderOverview(section, detail);
  else if (sectionId === "decision") renderMarkdownCard(section, detail.final_analysis, "Chưa có quyết định cuối cùng.");
  else if (sectionId === "technical") renderReportParts(section, detail.tabs?.technical, "Không có báo cáo kỹ thuật.");
  else if (sectionId === "fundamentals") renderReportParts(section, detail.tabs?.fundamental || detail.tabs?.fundamentals, "Không có báo cáo cơ bản.");
  else if (sectionId === "sentiment") renderLaneReport(section, detail, "sentiment", "Không có sentiment phù hợp cutoff.");
  else if (sectionId === "news") renderLaneReport(section, detail, "news", "Không có news phù hợp cutoff.");
  else if (sectionId === "plans") renderPlans(section, detail.plans);
  else if (sectionId === "debates") renderDebates(section, detail.debates);
  else if (sectionId === "sources") renderSources(section, detail.sources);
}

function renderOverview(section, detail) {
  const summary = detail.summary || {};
  const targetAvailable = summary.target_price_status === "available"
    && Number.isFinite(Number(summary.target_price))
    && /^[A-Z]{2,12}$/.test(String(summary.target_price_currency || ""));
  const targetReason = plain(
    summary.target_price_reason,
    "Portfolio Manager chưa cung cấp giá mục tiêu đã xác thực.",
  );
  const targetDetail = targetAvailable ? null : targetReason;
  const metrics = createElement("div", "metrics");
  metrics.append(
    metricCard("Xếp hạng", plain(summary.detailed_rating || summary.recommendation, "Unavailable"), true),
    metricCard(
      "Giá mục tiêu",
      targetAvailable ? formatPrice(summary.target_price, summary.target_price_currency) : "Unavailable",
      false,
      targetDetail,
    ),
    metricCard("Thời gian", plain(summary.time_horizon, "Unavailable")),
  );
  section.append(metrics);

  const progress = normalizeProgress(detail.progress);
  const completed = progress.filter((row) => row.status === "completed" || row.status === "unavailable").length;
  const total = progress.length;
  const percentage = total ? Math.round(completed / total * 100) : 0;
  const card = createElement("div", "card");
  card.append(createElement("h4", "card-title", "Tiến độ pipeline"));
  const row = createElement("div", "progress-row");
  row.append(createElement("span", "", `${completed}/${total} giai đoạn hoàn tất`));
  row.append(createElement("span", "", `${percentage}%`));
  card.append(row);
  const track = createElement("div", "progress-track");
  const bar = createElement("div", "progress-bar");
  bar.style.width = `${percentage}%`;
  track.append(bar);
  card.append(track);
  const chips = createElement("div", "stage-list");
  progress.forEach((stage) => chips.append(createElement("span", `stage-chip ${stage.status}`, `${stage.label}: ${statusLabels[stage.status] || stage.status}`)));
  card.append(chips);
  section.append(card);

  const summaryCard = createElement("div", "card");
  summaryCard.append(createElement("h4", "card-title", "Tóm tắt quyết định"));
  appendMarkdown(summaryCard, detail.final_analysis, "Chưa có quyết định cuối cùng.");
  section.append(summaryCard);
}

function renderLaneReport(section, detail, lane, emptyMessage) {
  const direct = detail.sections?.[lane];
  if (typeof direct === "string" && direct.trim()) {
    renderMarkdownCard(section, direct, emptyMessage);
    return;
  }
  const parts = normalizeReportParts(detail.tabs?.news || detail.tabs?.sentiment).filter((part) => {
    const sentiment = String(part.title || "").toLowerCase().includes("sentiment");
    return lane === "sentiment" ? sentiment : !sentiment;
  });
  renderReportParts(section, parts, emptyMessage);
}

function metricCard(label, value, featured = false, detail = null) {
  const card = createElement("div", `metric${featured ? " featured" : ""}`);
  card.append(createElement("span", "metric-label", label));
  card.append(createElement("strong", "metric-value", value));
  if (typeof detail === "string" && detail.trim()) {
    card.append(createElement("small", "metric-detail", detail));
  }
  return card;
}

function renderReportParts(section, rawParts, emptyMessage) {
  const parts = normalizeReportParts(rawParts);
  if (!parts.length) {
    section.append(createElement("div", "empty-content", emptyMessage));
    return;
  }
  parts.forEach((part) => {
    const card = createElement("div", "card");
    if (part.title) card.append(createElement("h4", "card-title", part.title));
    appendMarkdown(card, part.content, emptyMessage);
    section.append(card);
  });
}

function renderMarkdownCard(section, value, emptyMessage) {
  const card = createElement("div", "card");
  appendMarkdown(card, value, emptyMessage);
  section.append(card);
}

function renderPlans(section, plans) {
  const entries = [];
  if (Array.isArray(plans)) {
    plans.forEach((item) => {
      if (typeof item === "string") entries.push({ title: "Kế hoạch", content: item });
      else if (item && typeof item === "object") entries.push({ title: plain(item.title, "Kế hoạch"), content: plain(item.content || item.text, "") });
    });
  } else if (plans && typeof plans === "object") {
    const investment = plans.investment_plan || plans.investment;
    const trader = plans.trader_investment_plan || plans.trader;
    if (typeof investment === "string") entries.push({ title: "Luận điểm đầu tư", content: investment });
    if (typeof trader === "string") entries.push({ title: "Kế hoạch giao dịch", content: trader });
  }
  if (!entries.length) {
    section.append(createElement("div", "empty-content", "Chưa có kế hoạch đầu tư hoặc giao dịch."));
    return;
  }
  entries.forEach((entry) => {
    const card = createElement("div", "card");
    card.append(createElement("h4", "card-title", entry.title));
    appendMarkdown(card, entry.content, "Không có nội dung.");
    section.append(card);
  });
}

function renderDebates(section, debates) {
  const entries = normalizeDebates(debates);
  if (!entries.length) {
    section.append(createElement("div", "empty-content", "Không có dữ liệu tranh luận."));
    return;
  }
  entries.forEach((entry, index) => {
    const details = createElement("details", "debate");
    details.open = index === 0;
    details.append(createElement("summary", "", entry.title));
    const body = createElement("div", "debate-content");
    appendMarkdown(body, entry.content, "Không có nội dung.");
    details.append(body);
    section.append(details);
  });
}

function renderSources(section, sources) {
  const stages = normalizeSources(sources);
  if (!stages.length) {
    section.append(createElement("div", "empty-content", "Không có metadata nguồn dữ liệu."));
    return;
  }
  stages.forEach((stage) => {
    const block = createElement("section", "source-stage");
    const head = createElement("header", "source-stage-head");
    head.append(createElement("h4", "", plain(stage.stage || stage.name, "Nguồn dữ liệu")));
    head.append(statusPill(stage.status, "status-pill"));
    block.append(head);
    const list = createElement("div", "source-list");
    const sourceRows = [];
    const warnings = [];
    flattenProvenance(stage, plain(stage.stage || stage.name, ""), sourceRows, warnings, 0);
    sourceRows.forEach((source) => {
      const row = createElement("div", "source-item");
      row.append(createElement("div", "source-name", source.name));
      row.append(createElement("div", "source-detail", source.detail || "Không có mô tả bổ sung"));
      list.append(row);
    });
    warnings.forEach((warning) => {
      const row = createElement("div", "source-item warning-item");
      row.append(createElement("div", "source-name", "Cảnh báo"));
      row.append(createElement("div", "source-detail", plain(warning, "")));
      list.append(row);
    });
    if (!sourceRows.length && !warnings.length) list.append(createElement("div", "source-item", "Không có nguồn được ghi nhận"));
    block.append(list);
    section.append(block);
  });
}

function showSection(sectionId) {
  state.activeSection = sectionId;
  document.querySelectorAll(".report-section").forEach((section) => section.classList.toggle("active", section.dataset.section === sectionId));
  document.querySelectorAll(".section-button").forEach((button) => {
    const active = button.dataset.section === sectionId;
    button.classList.toggle("active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
  });
}

function appendMarkdown(parent, markdown, emptyMessage) {
  const container = createElement("div", "markdown");
  const value = typeof markdown === "string" ? markdown.trim() : "";
  if (!value) {
    container.append(createElement("div", "empty-content", emptyMessage));
  } else {
    parseMarkdownBlocks(value).forEach((node) => container.append(node));
  }
  parent.append(container);
}

function parseMarkdownBlocks(markdown) {
  const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
  const nodes = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    const trimmed = line.trim();
    if (!trimmed) { index += 1; continue; }

    if (trimmed.startsWith("```")) {
      const code = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith("```")) code.push(lines[index++]);
      if (index < lines.length) index += 1;
      const pre = document.createElement("pre");
      pre.append(createElement("code", "", code.join("\n")));
      nodes.push(pre);
      continue;
    }

    const heading = trimmed.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      const node = document.createElement(`h${heading[1].length}`);
      appendInlineMarkdown(node, heading[2]);
      nodes.push(node);
      index += 1;
      continue;
    }

    if (/^>\s?/.test(trimmed)) {
      const quoteLines = [];
      while (index < lines.length && /^>\s?/.test(lines[index].trim())) quoteLines.push(lines[index++].trim().replace(/^>\s?/, ""));
      const quote = document.createElement("blockquote");
      appendInlineMarkdown(quote, quoteLines.join(" "));
      nodes.push(quote);
      continue;
    }

    if (trimmed.includes("|") && index + 1 < lines.length && isTableSeparator(lines[index + 1])) {
      const headers = tableCells(trimmed);
      index += 2;
      const rows = [];
      while (index < lines.length && lines[index].trim() && lines[index].includes("|")) rows.push(tableCells(lines[index++]));
      nodes.push(buildTable(headers, rows));
      continue;
    }

    if (/^[-*+]\s+/.test(trimmed)) {
      const list = document.createElement("ul");
      while (index < lines.length && /^\s*[-*+]\s+/.test(lines[index])) {
        const item = document.createElement("li");
        appendInlineMarkdown(item, lines[index++].replace(/^\s*[-*+]\s+/, ""));
        list.append(item);
      }
      nodes.push(list);
      continue;
    }

    if (/^\d+[.)]\s+/.test(trimmed)) {
      const list = document.createElement("ol");
      while (index < lines.length && /^\s*\d+[.)]\s+/.test(lines[index])) {
        const item = document.createElement("li");
        appendInlineMarkdown(item, lines[index++].replace(/^\s*\d+[.)]\s+/, ""));
        list.append(item);
      }
      nodes.push(list);
      continue;
    }

    const paragraphLines = [trimmed];
    index += 1;
    while (index < lines.length && lines[index].trim() && !startsBlock(lines[index], lines[index + 1])) paragraphLines.push(lines[index++].trim());
    const paragraph = document.createElement("p");
    appendInlineMarkdown(paragraph, paragraphLines.join(" "));
    nodes.push(paragraph);
  }
  return nodes;
}

function appendInlineMarkdown(parent, input) {
  const text = String(input || "");
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|\[[^\]\n]+\]\(https?:\/\/[^)\s]+\))/gi;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > offset) parent.append(document.createTextNode(text.slice(offset, match.index)));
    const token = match[0];
    if (token.startsWith("`")) parent.append(createElement("code", "", token.slice(1, -1)));
    else if (token.startsWith("**")) parent.append(createElement("strong", "", token.slice(2, -2)));
    else if (token.startsWith("*")) parent.append(createElement("em", "", token.slice(1, -1)));
    else {
      const link = token.match(/^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/i);
      if (link) {
        const anchor = createElement("a", "", link[1]);
        try {
          const url = new URL(link[2]);
          if ((url.protocol === "http:" || url.protocol === "https:") && !url.username && !url.password) {
            anchor.href = url.href;
            anchor.target = "_blank";
            anchor.rel = "noopener noreferrer";
            anchor.referrerPolicy = "no-referrer";
            parent.append(anchor);
          } else parent.append(document.createTextNode(token));
        } catch (_error) {
          parent.append(document.createTextNode(token));
        }
      } else parent.append(document.createTextNode(token));
    }
    offset = match.index + token.length;
  }
  if (offset < text.length) parent.append(document.createTextNode(text.slice(offset)));
}

function buildTable(headers, rows) {
  const wrap = createElement("div", "table-wrap");
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  headers.forEach((value) => { const cell = document.createElement("th"); appendInlineMarkdown(cell, value); headRow.append(cell); });
  head.append(headRow);
  const body = document.createElement("tbody");
  rows.forEach((row) => {
    const tableRow = document.createElement("tr");
    headers.forEach((_, cellIndex) => { const cell = document.createElement("td"); appendInlineMarkdown(cell, row[cellIndex] || ""); tableRow.append(cell); });
    body.append(tableRow);
  });
  table.append(head, body);
  wrap.append(table);
  return wrap;
}

function startsBlock(line, nextLine = "") {
  const value = String(line || "").trim();
  return !value || /^#{1,4}\s+/.test(value) || value.startsWith("```") || /^>\s?/.test(value) || /^[-*+]\s+/.test(value) || /^\d+[.)]\s+/.test(value) || (value.includes("|") && isTableSeparator(nextLine));
}

function isTableSeparator(line) {
  return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(String(line || ""));
}

function tableCells(line) {
  return String(line).trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
}

function normalizeReportParts(value) {
  if (typeof value === "string" && value.trim()) return [{ title: "", content: value }];
  if (Array.isArray(value)) return value.flatMap((item) => {
    if (typeof item === "string" && item.trim()) return [{ title: "", content: item }];
    if (item && typeof item === "object" && typeof (item.content || item.text) === "string") return [{ title: plain(item.title, "Báo cáo"), content: item.content || item.text }];
    return [];
  });
  if (!value || typeof value !== "object") return [];
  return Object.entries(value).flatMap(([key, item]) => {
    if (typeof item === "string" && item.trim()) return [{ title: humanize(key), content: item }];
    return [];
  });
}

function normalizeProgress(value) {
  if (Array.isArray(value)) return value.map((row) => ({ label: plain(row?.label || row?.stage || row?.name, "Stage"), status: normalizeStatus(row?.status) }));
  if (value && typeof value === "object") return Object.entries(value).map(([stage, status]) => ({ label: humanize(stage), status: normalizeStatus(status) }));
  return [];
}

function normalizeDebates(value) {
  if (Array.isArray(value)) return value.flatMap((item) => item && typeof item.content === "string" ? [{ title: plain(item.title, "Tranh luận"), content: item.content }] : []);
  if (!value || typeof value !== "object") return [];
  const labels = {
    bull: "Quan điểm Bull Researcher", bull_history: "Quan điểm Bull Researcher",
    bear: "Quan điểm Bear Researcher", bear_history: "Quan điểm Bear Researcher",
    manager: "Kết luận Research Manager", judge_decision: "Kết luận Research Manager",
    aggressive: "Risk Analyst chủ động", aggressive_history: "Risk Analyst chủ động",
    neutral: "Risk Analyst trung lập", neutral_history: "Risk Analyst trung lập",
    conservative: "Risk Analyst bảo toàn", conservative_history: "Risk Analyst bảo toàn",
    portfolio: "Kết luận Portfolio Manager", history: "Toàn bộ tranh luận",
  };
  const entries = [];
  const appendObject = (group, prefix) => {
    if (!group || typeof group !== "object") return;
    Object.entries(group).forEach(([key, content]) => {
      if (typeof content === "string" && content.trim() && labels[key]) entries.push({ title: `${prefix}${labels[key]}`, content });
    });
  };
  appendObject(value.investment, "");
  appendObject(value.risk, "");
  Object.entries(value).forEach(([key, content]) => {
    if (typeof content === "string" && content.trim() && labels[key]) entries.push({ title: labels[key], content });
  });
  return entries;
}

function normalizeSources(value) {
  if (Array.isArray(value)) return value.filter((item) => item && typeof item === "object");
  if (!value || typeof value !== "object") return [];
  const result = [];
  Object.entries(value).forEach(([stage, metadata]) => {
    if (stage === "stages" && metadata && typeof metadata === "object" && !Array.isArray(metadata)) {
      Object.entries(metadata).forEach(([nestedStage, nestedMetadata]) => result.push({ stage: nestedStage, ...(nestedMetadata && typeof nestedMetadata === "object" ? nestedMetadata : {}) }));
    } else {
      result.push({ stage, ...(metadata && typeof metadata === "object" ? metadata : {}) });
    }
  });
  return result;
}

function flattenProvenance(value, prefix, rows, warnings, depth) {
  if (depth > 6 || !value || typeof value !== "object") return;
  if (Array.isArray(value)) {
    value.forEach((item, index) => flattenProvenance(item, prefix || `Nguồn ${index + 1}`, rows, warnings, depth + 1));
    return;
  }
  if (Array.isArray(value.warnings)) value.warnings.forEach((warning) => { if (typeof warning === "string" && warning.trim()) warnings.push(warning.trim()); });
  const detail = [];
  provenanceScalars.forEach((key) => {
    const scalar = value[key];
    if (scalar === null || scalar === undefined || scalar === "") return;
    if (typeof scalar === "string" || typeof scalar === "number" || typeof scalar === "boolean") detail.push(`${humanize(key)}: ${String(scalar)}`);
    else if (Array.isArray(scalar)) detail.push(`${humanize(key)}: ${scalar.filter((item) => ["string", "number", "boolean"].includes(typeof item)).join(", ")}`);
  });
  if (detail.length) rows.push({ name: plain(value.provider || value.vendor || value.category, prefix ? humanize(prefix) : "Nguồn nội bộ"), detail: detail.join(" · ") });
  provenanceContainers.forEach((key) => {
    if (!(key in value)) return;
    const nextPrefix = prefix ? `${prefix} / ${key}` : key;
    flattenProvenance(value[key], nextPrefix, rows, warnings, depth + 1);
  });
}

function statusPill(status, className) {
  const normalized = normalizeStatus(status, "partial");
  return createElement("span", `${className} ${normalized}`, statusLabels[normalized] || normalized);
}

function normalizeStatus(value, fallback = "not_run") {
  const candidate = plain(value, fallback);
  return allowedStatuses.has(candidate) ? candidate : fallback;
}

function createElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function plain(value, fallback = "") {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

function positiveInteger(value, fallback) {
  const number = Number(value);
  return Number.isInteger(number) && number > 0 ? number : fallback;
}

function humanize(value) {
  return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function modeLabel(value) {
  return value === "live" ? "Live · as-of-now" : value === "close" ? "Close · 15:00" : plain(value, "—");
}

function formatDate(value, includeTime = false) {
  if (!value) return "—";
  if (!includeTime && /^\d{4}-\d{2}-\d{2}$/.test(String(value))) {
    const [year, month, day] = String(value).split("-");
    return `${day}/${month}/${year}`;
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return plain(value, "—");
  return new Intl.DateTimeFormat("vi-VN", {
    day: "2-digit", month: "2-digit", year: "numeric",
    ...(includeTime ? { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZoneName: "short" } : {}),
    timeZone: "Asia/Ho_Chi_Minh",
  }).format(date);
}

function formatPrice(value, currency) {
  if (value === null || value === undefined || value === "") return "Unavailable";
  const number = Number(value);
  const normalizedCurrency = String(currency || "");
  return Number.isFinite(number) && /^[A-Z]{2,12}$/.test(normalizedCurrency)
    ? `${new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 2 }).format(number)} ${normalizedCurrency}`
    : "Unavailable";
}

function markSelectedListItem() {
  document.querySelectorAll(".history-item").forEach((button) => button.classList.toggle("active", button.dataset.historyId === state.selectedId));
}

function clearViewer() {
  state.selectedId = null;
  viewer.classList.add("is-hidden");
  viewerEmpty.classList.remove("is-hidden");
}

function fragmentHistoryId() {
  const value = window.location.hash.slice(1);
  return historyIdPattern.test(value) ? value : null;
}

function setFragment(historyId) {
  if (fragmentHistoryId() === historyId) return;
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${historyId}`);
}

function clearFragment() {
  if (!window.location.hash) return;
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
}

function initializeTheme() {
  let saved = "";
  try { saved = localStorage.getItem("tradingagents-gx-theme") || ""; } catch (_error) { saved = ""; }
  const preferred = window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  document.documentElement.dataset.theme = saved === "dark" || saved === "light" ? saved : preferred;
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("tradingagents-gx-theme", next); } catch (_error) { /* Theme persistence is optional. */ }
}

function showNotice(message) {
  notice.textContent = message;
  notice.classList.remove("is-hidden");
}

function hideNotice() {
  notice.textContent = "";
  notice.classList.add("is-hidden");
}

async function apiFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-TradingAgents-UI-Token", uiToken);
  if (options.body) headers.set("Content-Type", "application/json");
  return fetch(path, { ...options, headers, credentials: "same-origin" });
}
