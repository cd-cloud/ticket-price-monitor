const AIRPORT_SUGGESTIONS = window.AIRPORT_SUGGESTIONS || [];

let dashboardData = {
  tasks: [],
  routes: [],
  provider_options: [],
  cabin_options: [],
  last_run: { running: false, saved: 0, errors: [] },
};
let activeTaskView = "all";
let dashboardPollTimer = null;
let dashboardPollInFlight = false;
let tailDiscoveryResults = [];
let tailSafeBatchSize = 3;
let tailDefaultProfiles = {};
let tailCandidateProfileOverride = "";
let activeTailJobId = "";
let activeTailJobTimer = null;
let tailDiscoveryJobs = [];
let browserProfileData = { providers: [] };
var tailCandidateNames = window.tailCandidateNames || {};
window.tailCandidateNames = tailCandidateNames;
let lastTaskRenderSignature = "";
let lastRouteRenderSignature = "";
let lastDashboardOptionsSignature = "";
let lastSessionHintSignature = "";
let activeWorkspaceTab = localStorage.getItem("activeWorkspaceTab") || "routes";

const CABIN_LABELS = {
  economy_plus: "经济舱/超级经济舱",
  business_first: "商务舱/头等舱",
  economy: "经济舱",
  premium_economy: "超级经济舱",
  business: "商务舱",
  first: "头等舱",
};

const STATUS_LABELS = {
  ok: ["已查询", "ok"],
  scheduled: ["已排程", "scheduled"],
  error: ["未更新", "idle"],
  expired: ["已过期", "expired"],
  idle: ["未查询", "idle"],
};

const ACTIVE_TAIL_JOB_STATUSES = ["queued", "running", "needs_verification"];

function cabinLabel(value) {
  return CABIN_LABELS[value] || value || "-";
}

const TRANSFER_POLICY_LABELS = {
  any: "可转机",
  direct: "仅直飞",
  direct_only: "仅直飞",
  transfer_only: "仅中转",
};

function transferPolicyLabel(value) {
  return TRANSFER_POLICY_LABELS[value] || value || "-";
}

function cleanDisplayText(value) {
  return String(value ?? "")
    .replaceAll("→", "->");
}

function escapeHtml(value) {
  return cleanDisplayText(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

let toastTimer = null;

function notifyComplete(message, options = {}) {
  let toast = document.getElementById("completionToast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "completionToast";
    toast.className = "completion-toast";
    toast.setAttribute("role", "status");
    toast.setAttribute("aria-live", "polite");
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  toast.classList.toggle("is-warning", options.kind === "warning");
  toast.hidden = false;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    toast.hidden = true;
  }, options.durationMs || 2200);
}

function renderSessionHints(hints = []) {
  const loginHints = hints.filter((hint) => hint?.needs_login && hint.message);
  const signature = JSON.stringify(loginHints.map((hint) => [hint.provider, hint.reason, hint.message]));
  if (!loginHints.length || signature === lastSessionHintSignature) return;
  lastSessionHintSignature = signature;
  notifyComplete(loginHints[0].message, { kind: "warning", durationMs: 5200 });
}

async function responseErrorMessage(response, fallback) {
  const text = await response.text();
  if (!text) return fallback;
  try {
    const data = JSON.parse(text);
    return data.detail || data.error || fallback;
  } catch (_error) {
    return text;
  }
}

function cleanVisibleText(root = document.body) {
  if (!root) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    const cleaned = cleanDisplayText(node.nodeValue);
    if (cleaned !== node.nodeValue) {
      node.nodeValue = cleaned;
    }
  }
}

function normalizeAirlines(text) {
  return text.split(",").map((item) => item.trim().toUpperCase()).filter(Boolean);
}

function normalizeCode(value) {
  return resolveAirportCode(value);
}

function parseCityList(value) {
  return String(value || "")
    .split(/[\/,，、;\s]+/)
    .map((item) => normalizeCode(item))
    .filter(Boolean)
    .filter((item, index, array) => array.indexOf(item) === index);
}

function isRouteGroupEnabled() {
  return Boolean(document.getElementById("routeGroupEnabled")?.checked);
}

function routeExpansionCount() {
  if (!isRouteGroupEnabled()) return 1;
  const routeType = document.getElementById("routeTypeSelect").value;
  if (routeType === "multi_city") {
    const counts = collectSegmentRows().map((segment) => {
      const origins = segment.origin_options?.length ? segment.origin_options : [segment.origin].filter(Boolean);
      const destinations = segment.destination_options?.length ? segment.destination_options : [segment.destination].filter(Boolean);
      return Math.max(1, origins.length) * Math.max(1, destinations.length);
    }).filter((count) => count > 0);
    if (!counts.length) return 1;
    if (document.getElementById("expansionModeSelect").value === "paired") {
      return Math.max(...counts, 1);
    }
    return counts.reduce((total, count) => total * count, 1);
  }
  const origins = parseCityList(document.getElementById("originOptionsInput").value || document.getElementById("originInput").value);
  const destinations = parseCityList(document.getElementById("destinationOptionsInput").value || document.getElementById("destinationInput").value);
  return Math.max(1, origins.length) * Math.max(1, destinations.length);
}

function routeExpansionIssue() {
  if (!isRouteGroupEnabled()) return "";
  const routeType = document.getElementById("routeTypeSelect").value;
  if (routeType !== "multi_city" || document.getElementById("expansionModeSelect").value !== "paired") {
    return "";
  }
  const optionCounts = collectSegmentRows()
    .map((segment) => {
      const origins = segment.origin_options?.length ? segment.origin_options : [segment.origin].filter(Boolean);
      const destinations = segment.destination_options?.length ? segment.destination_options : [segment.destination].filter(Boolean);
      return Math.max(0, origins.length) * Math.max(0, destinations.length);
    })
    .filter((count) => count > 1);
  return new Set(optionCounts).size > 1
    ? "，按顺序配对要求每个多选分段的候选数量一致"
    : "";
}

function renderRouteExpansionPreview() {
  const panel = document.getElementById("routeGroupPanel");
  const preview = document.getElementById("routeExpansionPreview");
  if (!panel || !preview) return;
  panel.hidden = !isRouteGroupEnabled();
  if (!isRouteGroupEnabled()) {
    preview.textContent = "";
    return;
  }
  const count = routeExpansionCount();
  const warning = routeExpansionIssue();
  const routeType = document.getElementById("routeTypeSelect").value;
  const hint = routeType === "multi_city"
    ? "，每个多程分段都可以输入多个城市，保存后展开为同一组多程查询，并按组汇总展示"
    : "，保存后会作为同一组展示";
  preview.innerHTML = `<strong>预计创建 ${count} 条查询</strong><span>${warning || hint}</span>`;
}

function collectSegmentRows() {
  return Array.from(document.querySelectorAll(".segment-row"))
    .map((row) => ({
      origin: normalizeCode(row.querySelector(".segment-origin").value),
      destination: normalizeCode(row.querySelector(".segment-destination").value),
      origin_options: parseCityList(row.querySelector(".segment-origin").value),
      destination_options: parseCityList(row.querySelector(".segment-destination").value),
      departure_date: row.querySelector(".segment-date").value,
    }))
    .filter((segment) => segment.origin || segment.destination || segment.departure_date);
}

function addSegmentRow(segment = {}) {
  const template = document.getElementById("segmentRowTemplate");
  const row = template.content.firstElementChild.cloneNode(true);
  row.querySelector(".segment-origin").value = segment.origin || "";
  row.querySelector(".segment-destination").value = segment.destination || "";
  row.querySelector(".segment-date").value = segment.departure_date || "";
  document.getElementById("segmentRows").appendChild(row);
  enhanceAirportInputs(row);
  renumberSegments();
}

function renderSegmentRows(segments = []) {
  const rows = segments.length ? segments : [{}, {}];
  document.getElementById("segmentRows").innerHTML = "";
  rows.forEach((segment) => addSegmentRow(segment));
  while (document.querySelectorAll(".segment-row").length < 2) {
    addSegmentRow({});
  }
}

function renumberSegments() {
  document.querySelectorAll(".segment-row").forEach((row, index) => {
    row.querySelector(".segment-number").textContent = index + 1;
    row.querySelector(".segment-remove").disabled = document.querySelectorAll(".segment-row").length <= 2;
  });
}

function syncRouteFormRequirements() {
  const isMultiCity = document.getElementById("routeTypeSelect").value === "multi_city";
  const groupEnabled = isRouteGroupEnabled();
  document.getElementById("originInput").required = !isMultiCity && !groupEnabled;
  document.getElementById("destinationInput").required = !isMultiCity && !groupEnabled;
  document.getElementById("departureDateInput").required = !isMultiCity;
  document.getElementById("returnDateInput").required = false;
  document.getElementById("originOptionsInput").required = !isMultiCity && groupEnabled;
  document.getElementById("destinationOptionsInput").required = !isMultiCity && groupEnabled;
}

function syncRouteFormVisibility() {
  const isMultiCity = document.getElementById("routeTypeSelect").value === "multi_city";
  const groupEnabled = isRouteGroupEnabled();
  document.querySelectorAll("[data-base-location-field]").forEach((field) => {
    field.hidden = isMultiCity || groupEnabled;
    field.querySelectorAll("input").forEach((input) => {
      input.disabled = isMultiCity || groupEnabled;
    });
  });
}

function formatDateInputValue(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function shiftDateInputValue(value, days) {
  if (!value) return "";
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(year, month - 1, day);
  if (Number.isNaN(date.getTime())) return "";
  date.setDate(date.getDate() + days);
  return formatDateInputValue(date);
}

function syncRoundTripDateBounds(changedField = "") {
  const isMultiCity = document.getElementById("routeTypeSelect").value === "multi_city";
  const departure = document.getElementById("departureDateInput");
  const returnDate = document.getElementById("returnDateInput");
  if (isMultiCity) {
    departure.min = "";
    departure.max = "";
    returnDate.min = "";
    returnDate.max = "";
    return;
  }

  const today = formatDateInputValue(new Date());
  const tomorrow = shiftDateInputValue(today, 1);
  departure.min = today;
  returnDate.min = departure.value ? shiftDateInputValue(departure.value, 1) : tomorrow;
  departure.max = returnDate.value ? shiftDateInputValue(returnDate.value, -1) : "";
  returnDate.max = "";

  if (changedField === "departure" && departure.value) {
    const earliestReturn = shiftDateInputValue(departure.value, 1);
    if (!returnDate.value || returnDate.value < earliestReturn) {
      returnDate.value = earliestReturn;
    }
  }
  if (changedField === "return" && returnDate.value) {
    if (returnDate.value < tomorrow) {
      returnDate.value = tomorrow;
    }
    const latestDeparture = shiftDateInputValue(returnDate.value, -1);
    if (!departure.value || departure.value > latestDeparture) {
      departure.value = latestDeparture;
    }
  }

  returnDate.min = departure.value ? shiftDateInputValue(departure.value, 1) : tomorrow;
  departure.max = returnDate.value ? shiftDateInputValue(returnDate.value, -1) : "";
}

function syncRouteTypeMode() {
  const isMultiCity = document.getElementById("routeTypeSelect").value === "multi_city";
  document.getElementById("multiCityBuilder").hidden = !isMultiCity;
  document.querySelectorAll("[data-single-route-field]").forEach((field) => {
    field.hidden = isMultiCity;
    field.querySelectorAll("input").forEach((input) => {
      input.disabled = isMultiCity;
      input.required = !isMultiCity && input.id !== "returnDateInput";
    });
  });
  if (isMultiCity && !document.querySelectorAll(".segment-row").length) {
    renderSegmentRows();
  }
  if (isMultiCity) {
    document.querySelectorAll('#providerCheckboxes input[type="checkbox"]').forEach((checkbox) => {
      checkbox.checked = checkbox.value === "ctrip";
    });
  }
  syncRouteFormVisibility();
  syncRouteFormRequirements();
  syncRoundTripDateBounds();
  renderRouteExpansionPreview();
}

function formatQueryTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date).replace(/\//g, "-");
}

function formatQueryDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 10) || value;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date).replace(/\//g, "-");
}

function providerLabel(value) {
  const labels = {
    ctrip: "携程",
    feizhu: "飞猪",
    priceline: "Priceline",
    airchina: "国航",
  };
  const key = String(value || "").trim().toLowerCase();
  return labels[key] || value || "-";
}

function formatTimeUntil(value) {
  if (!value) return "-";
  const target = new Date(value);
  if (Number.isNaN(target.getTime())) return "-";
  const diffMs = target.getTime() - Date.now();
  if (diffMs <= 0) return "等待执行";
  const totalMinutes = Math.ceil(diffMs / 60000);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours <= 0) return `${minutes} 分钟后`;
  if (minutes === 0) return `${hours} 小时后`;
  return `${hours} 小时 ${minutes} 分钟后`;
}

function formatAutoQueryStatus(item) {
  if (!item.auto_query_enabled) {
    return '<span class="auto-badge off">未开启</span>';
  }
  return `
    <div class="auto-status">
      <span class="auto-badge on">12h 自动</span>
      <span>${formatTimeUntil(item.auto_query_next_run_at)}</span>
      <small>${formatQueryTime(item.auto_query_next_run_at)}</small>
    </div>
  `;
}

function taskMatchesView(task) {
  if (activeTaskView === "auto") return Boolean(task.auto_query_enabled);
  if (activeTaskView === "multi_city") return task.route_type === "multi_city";
  if (activeTaskView === "unqueried") return !task.latest_price && !task.is_expired;
  return true;
}

function taskFlightSummary(task) {
  if (task.route_type === "multi_city") {
    const segmentCount = task.segment_count || task.multi_city_segments?.length || 0;
    return `${segmentCount || "-"} 段行程`;
  }
  if (task.lowest_flight_no || task.lowest_departure_time) {
    return `${task.lowest_flight_no || "-"} · ${task.lowest_departure_time || "-"} 起飞`;
  }
  return "-";
}

function routeCodeDisplay(origin, destination) {
  return `${airportCodeDisplayText(origin)} -> ${airportCodeDisplayText(destination)}`;
}

function taskRouteDisplay(task) {
  if (task.route_type === "multi_city") {
    const segments = task.multi_city_segments || [];
    if (segments.length) {
      return segments
        .map((segment) => routeCodeDisplay(segment.origin, segment.destination))
        .join(" / ");
    }
    return task.itinerary_summary || task.route_display || "-";
  }
  return routeCodeDisplay(task.origin, task.destination);
}

function detailButton(task) {
  const hasDetails = (task.lowest_flight_details || []).length || task.lowest_flight_no || task.lowest_departure_time;
  if (!hasDetails) return '<span class="muted">暂无详情</span>';
  return `
    <button class="ghost compact" data-task-action="show-detail" data-key="${escapeHtml(task.route_key)}" data-provider="${escapeHtml(task.provider)}">
      查看详情
    </button>
  `;
}

function formatFlightDetails(task) {
  const details = task.lowest_flight_details || [];
  if (!details.length) return "";
  const rows = details.map((detail, index) => {
    let segmentName = detail.origin && detail.destination
      ? `${detail.origin} → ${detail.destination}`
      : `第 ${detail.segment_index || index + 1} 段`;
    const flightNo = detail.flight_no || "待解析";
    if (detail.origin && detail.destination) {
      segmentName = `${airportCodeDisplayText(detail.origin)} -> ${airportCodeDisplayText(detail.destination)}`;
    }
    const airline = detail.airline || "-";
    const cabin = cabinLabel(detail.cabin || task.cabin);
    const departure = detail.departure_time || "-";
    const arrival = detail.arrival_time || "-";
    const departureAirport = airportCodeDisplayText(detail.departure_airport || "-");
    const arrivalAirport = airportCodeDisplayText(detail.arrival_airport || "-");
    const date = detail.departure_date || task.departure_date || "-";
    const aircraft = detail.aircraft ? `<span>${escapeHtml(detail.aircraft)}</span>` : "";
    return `
      <div class="flight-detail-row">
        <div class="flight-detail-index">${escapeHtml(detail.segment_index || index + 1)}</div>
        <div>
          <strong>${escapeHtml(segmentName)}</strong>
          <p>${escapeHtml(date)} · ${escapeHtml(airline)} · ${escapeHtml(flightNo)} · ${escapeHtml(cabin)} ${aircraft}</p>
          <p>${escapeHtml(departure)} ${escapeHtml(departureAirport)} → ${escapeHtml(arrival)} ${escapeHtml(arrivalAirport)}</p>
        </div>
      </div>
    `;
  }).join("");

  return `
    <details class="flight-details">
      <summary>展开每段航班详情</summary>
      <div class="flight-detail-list">${rows}</div>
    </details>
  `;
}

function formatBestFlight(task) {
  if (task.route_type === "multi_city") {
    const segmentCount = task.segment_count || task.multi_city_segments?.length || 0;
    const summary = taskRouteDisplay(task);
    return `
      <div class="flight-chip">
        <div class="flight-chip-top">${escapeHtml(segmentCount)} 段行程 | 携程多程总价</div>
        <div class="flight-chip-bottom">${escapeHtml(summary)}</div>
        ${formatFlightDetails(task)}
      </div>
    `;
  }
  if (!task.lowest_flight_no && !task.lowest_departure_time) return "-";
  const flightNo = task.lowest_flight_no || "-";
  const departure = task.lowest_departure_time ? `${task.lowest_departure_time} 起飞` : "-";
  const departureAirport = airportCodeDisplayText(task.lowest_departure_airport || "-");
  const arrivalTime = task.lowest_arrival_time || "-";
  const arrivalAirport = airportCodeDisplayText(task.lowest_arrival_airport || "");
  return `
    <div class="flight-chip">
      <div class="flight-chip-top">${escapeHtml(flightNo)} | ${escapeHtml(departure)}</div>
      <div class="flight-chip-bottom">${escapeHtml(departureAirport)} → ${escapeHtml(arrivalTime)} ${escapeHtml(arrivalAirport)}</div>
      ${formatFlightDetails(task)}
    </div>
  `;
}

function ensureTaskDetailDrawer() {
  if (document.getElementById("taskDetailDrawer")) return;
  const drawer = document.createElement("aside");
  drawer.id = "taskDetailDrawer";
  drawer.className = "task-detail-drawer";
  drawer.hidden = true;
  drawer.innerHTML = `
    <div class="drawer-backdrop" data-drawer-close></div>
    <section class="drawer-panel" aria-label="航班详情">
      <div class="drawer-head">
        <div>
          <p class="eyebrow">Flight Detail</p>
          <h2 id="drawerTitle">航班详情</h2>
        </div>
        <button class="ghost compact" type="button" data-drawer-close>关闭</button>
      </div>
      <div id="drawerBody" class="drawer-body"></div>
    </section>
  `;
  drawer.addEventListener("click", (event) => {
    if (event.target.closest("[data-drawer-close]")) {
      drawer.hidden = true;
    }
  });
  document.body.appendChild(drawer);
}

function showTaskDetail(routeKey, provider) {
  ensureTaskDetailDrawer();
  const drawer = document.getElementById("taskDetailDrawer");
  const task = (dashboardData.tasks || []).find((item) => item.route_key === routeKey && item.provider === provider);
  if (!drawer || !task) return;

  const title = taskRouteDisplay(task);
  const price = task.latest_price ? `${task.currency} ${task.latest_price}` : "暂无价格";
  const queryTime = formatQueryTime(task.last_query_time);
  const transferPolicy = transferPolicyLabel(task.transfer_policy || "any");
  const airlines = task.preferred_airlines?.length ? task.preferred_airlines.join(", ") : "不限";
  const details = formatFlightDetails(task) || '<p class="muted">暂无可展示的航班分段详情。</p>';

  document.getElementById("drawerTitle").textContent = cleanDisplayText(title);
  document.getElementById("drawerBody").innerHTML = `
    <div class="detail-kpis">
      <div><span>最新价格</span><strong>${escapeHtml(price)}</strong></div>
      <div><span>最近查询</span><strong>${escapeHtml(queryTime)}</strong></div>
      <div><span>平台</span><strong>${escapeHtml(task.provider)}</strong></div>
    </div>
    <div class="detail-meta">
      <p>${escapeHtml(cabinLabel(task.cabin))} · ${escapeHtml(transferPolicy)} · ${escapeHtml(airlines)}</p>
      <p>${escapeHtml(task.itinerary_summary || task.departure_date || "-")}</p>
    </div>
    ${details}
  `;
  drawer.hidden = false;
  cleanVisibleText(drawer);
}

function routeLabel(route) {
  if (route.route_type === "multi_city") {
    return (route.segments || []).map((segment) => routeCodeDisplay(segment.origin, segment.destination)).join(" / ");
  }
  return routeCodeDisplay(route.origin, route.destination);
}

function statusBadge(status, error) {
  const [label, className] = STATUS_LABELS[status] || STATUS_LABELS.idle;
  return `<span class="status-badge ${className}">${label}</span>`;
}

function renderProviderOptions(providers) {
  const container = document.getElementById("providerCheckboxes");
  const filter = document.getElementById("providerFilter");
  const selectedFilter = filter.value;
  container.innerHTML = "";
  filter.innerHTML = '<option value="">全部</option>';

  for (const provider of providers) {
    const wrapper = document.createElement("label");
    wrapper.className = "provider-pill";
    wrapper.innerHTML = `
      <input type="checkbox" name="providers" value="${provider}" checked />
      <span>${provider}</span>
    `;
    container.appendChild(wrapper);

    const option = document.createElement("option");
    option.value = provider;
    option.textContent = provider;
    filter.appendChild(option);
  }
  filter.value = selectedFilter;
}

function renderCabinOptions(cabins) {
  const select = document.getElementById("cabinSelect");
  const filter = document.getElementById("cabinFilter");
  const selected = select.value;
  const selectedFilter = filter.value;
  select.innerHTML = "";
  filter.innerHTML = '<option value="">全部</option>';

  for (const cabin of cabins) {
    const option = document.createElement("option");
    option.value = cabin;
    option.textContent = cabinLabel(cabin);
    select.appendChild(option);

    const filterOption = document.createElement("option");
    filterOption.value = cabin;
    filterOption.textContent = cabinLabel(cabin);
    filter.appendChild(filterOption);
  }
  if (selected) select.value = selected;
  filter.value = selectedFilter;
}

function filteredTasks(tasks) {
  const providerFilter = document.getElementById("providerFilter").value;
  const cabinFilter = document.getElementById("cabinFilter").value;
  const search = document.getElementById("routeSearchInput").value.trim().toLowerCase();
  return tasks.filter((task) => {
    const routeText = taskSearchText(task);
    return taskMatchesView(task) &&
      (!providerFilter || task.provider === providerFilter) &&
      (!cabinFilter || task.cabin === cabinFilter) &&
      (!search || routeText.includes(search));
  });
}

function taskSearchText(task) {
  const segments = task.route_type === "multi_city" ? (task.multi_city_segments || []) : [];
  const segmentText = segments
    .map((segment) => `${segment.origin} ${segment.destination} ${airportCodeDisplayText(segment.origin)} ${airportCodeDisplayText(segment.destination)}`)
    .join(" ");
  return [
    task.origin,
    task.destination,
    airportCodeDisplayText(task.origin),
    airportCodeDisplayText(task.destination),
    task.route_key,
    task.route_display,
    task.itinerary_summary,
    segmentText,
  ].filter(Boolean).join(" ").toLowerCase();
}

function renderTaskSummary(tasks) {
  const container = document.getElementById("taskSummary");
  const total = tasks.length;
  const queried = tasks.filter((task) => task.latest_price).length;
  const autoEnabled = tasks.filter((task) => task.auto_query_enabled).length;
  const nextRuns = tasks
    .filter((task) => task.auto_query_enabled && task.auto_query_next_run_at)
    .sort((left, right) => new Date(left.auto_query_next_run_at) - new Date(right.auto_query_next_run_at));
  const nextRunText = nextRuns.length ? `${formatTimeUntil(nextRuns[0].auto_query_next_run_at)}，${formatQueryTime(nextRuns[0].auto_query_next_run_at)}` : "-";
  container.innerHTML = `
    <div class="summary-card"><strong>${total}</strong><span>当前任务</span></div>
    <div class="summary-card"><strong>${queried}</strong><span>已有价格</span></div>
    <div class="summary-card"><strong>${autoEnabled}</strong><span>自动查询</span></div>
    <div class="summary-card wide"><strong>${nextRunText}</strong><span>最近一次自动计划</span></div>
  `;
}

function formatCurrencyAmount(currency, amount) {
  if (amount === null || amount === undefined || Number.isNaN(Number(amount))) return "-";
  return `${currency || "CNY"} ${Number(amount).toFixed(0)}`;
}

function renderPriceChangePill(task) {
  const direction = task.latest_price_delta_direction;
  const delta = Number(task.latest_price_delta_amount || 0);
  if (!direction) return "";
  if (direction === "new") {
    return '<span class="price-change-pill new">首次记录</span>';
  }
  if (direction === "same") {
    return '<span class="price-change-pill same">持平</span>';
  }
  const sign = delta > 0 ? "+" : "";
  const label = direction === "down" ? "降价" : "涨价";
  return `<span class="price-change-pill ${escapeHtml(direction)}">${label} ${sign}${delta.toFixed(0)}</span>`;
}

function progressPercent(progress) {
  const total = Number(progress?.total_targets || 0);
  const completed = Number(progress?.completed_targets || 0);
  if (!total) return 0;
  return Math.max(0, Math.min(100, Math.round((completed / total) * 100)));
}

function renderRunProgressSummary(progress = {}) {
  const container = document.getElementById("runProgressSummary");
  if (!container) return;
  const current = progress.current_target || {};
  const percent = progressPercent(progress);
  const total = Number(progress.total_targets || 0);
  const completed = Number(progress.completed_targets || 0);
  const recentSuccesses = (progress.recent_successes || []).slice(-4).reverse();
  const recentFailures = (progress.recent_failures || []).slice(-3).reverse();
  const activeLabel = progress.running
    ? `${providerLabel(current.provider || "")} · ${cleanDisplayText(current.route_display || current.route_key || "正在准备任务")}`
    : "当前没有正在运行的查询";
  const logItems = [
    ...recentSuccesses.map((item) => `
      <div class="progress-log-item">
        <strong>已保存</strong>
        <span>${escapeHtml(cleanDisplayText(item.route_display || item.route_key || "-"))}</span>
        <small>${escapeHtml(providerLabel(item.provider || ""))} · ${escapeHtml(formatCurrencyAmount("CNY", item.price))}</small>
      </div>
    `),
    ...recentFailures.map((item) => `
      <div class="progress-log-item">
        <strong>失败</strong>
        <span>${escapeHtml(cleanDisplayText(item.route_display || item.route_key || "-"))}</span>
        <small>${escapeHtml(item.error || "-")}</small>
      </div>
    `),
  ].join("");
  container.innerHTML = `
    <div class="run-progress-card ${progress.running ? "active" : ""}">
      <div class="progress-head">
        <div>
          <h3>查询进度</h3>
          <p class="muted">${escapeHtml(activeLabel)}</p>
        </div>
        <span class="progress-pill">${progress.running ? "查询进行中" : "查询空闲"}</span>
      </div>
      <div class="progress-track"><div class="progress-bar" style="width:${percent}%"></div></div>
      <div class="progress-kpis">
        <div><span>完成进度</span><strong>${completed}/${total || 0}</strong></div>
        <div><span>本轮已保存</span><strong>${Number(progress.saved || 0)}</strong></div>
        <div><span>错误数</span><strong>${(progress.errors || []).length}</strong></div>
        <div><span>最近结束</span><strong>${escapeHtml(formatQueryTime(progress.finished_at || progress.started_at))}</strong></div>
      </div>
      ${logItems ? `<div class="progress-log">${logItems}</div>` : ""}
    </div>
  `;
}

function renderResultChangeSummary(feedback = {}) {
  const container = document.getElementById("resultChangeSummary");
  if (!container) return;
  const changes = feedback.changes || {};
  const counts = changes.counts || {};
  const topDown = changes.top_down || [];
  const topUp = changes.top_up || [];
  const renderChangeList = (items, emptyText) => {
    if (!items.length) return `<div class="change-item"><span class="muted">${escapeHtml(emptyText)}</span></div>`;
    return items.map((item) => {
      const delta = Number(item.delta_amount || 0);
      const sign = delta > 0 ? "+" : "";
      return `
        <div class="change-item ${escapeHtml(item.direction || "same")}">
          <strong>${escapeHtml(cleanDisplayText(item.route_key || "-"))}</strong>
          <span>${escapeHtml(providerLabel(item.provider || ""))}</span>
          <small>${escapeHtml(formatCurrencyAmount(item.currency, item.previous_price))} -> ${escapeHtml(formatCurrencyAmount(item.currency, item.latest_price))} (${sign}${delta.toFixed(0)})</small>
        </div>
      `;
    }).join("");
  };
  container.innerHTML = `
    <div class="change-summary-card">
      <div class="change-head">
        <div>
          <h3>本轮结果变化摘要</h3>
          <p class="muted">基于最近一轮成功保存的航线，对比上一条历史快照。</p>
        </div>
      </div>
      <div class="change-kpis">
        <div><span>成功保存</span><strong>${Number(changes.total_saved_pairs || 0)}</strong></div>
        <div><span>降价</span><strong>${Number(counts.down || 0)}</strong></div>
        <div><span>涨价</span><strong>${Number(counts.up || 0)}</strong></div>
        <div><span>持平/首次</span><strong>${Number(counts.same || 0) + Number(counts.new || 0)}</strong></div>
      </div>
      <div class="change-columns">
        <div class="change-list">
          <strong>最值得关注的降价</strong>
          ${renderChangeList(topDown, "这一轮没有出现明显降价。")}
        </div>
        <div class="change-list">
          <strong>明显涨价</strong>
          ${renderChangeList(topUp, "这一轮没有出现明显涨价。")}
        </div>
      </div>
    </div>
  `;
}

function ensureTaskViewTabs() {
  if (document.getElementById("taskViewTabs")) return;
  const filters = document.querySelector(".filters");
  if (!filters) return;
  const tabs = document.createElement("div");
  tabs.id = "taskViewTabs";
  tabs.className = "task-view-tabs";
  tabs.innerHTML = `
    <button type="button" data-task-view="all">全部</button>
    <button type="button" data-task-view="auto">自动查询</button>
    <button type="button" data-task-view="multi_city">多程</button>
    <button type="button" data-task-view="unqueried">未查询</button>
  `;
  filters.parentNode.insertBefore(tabs, filters);
  tabs.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-task-view]");
    if (!button) return;
    activeTaskView = button.dataset.taskView;
    renderTasks(dashboardData.tasks || []);
  });
}

function renderTaskViewTabs(tasks) {
  ensureTaskViewTabs();
  const tabs = document.getElementById("taskViewTabs");
  if (!tabs) return;
  const counts = {
    all: tasks.length,
    auto: tasks.filter((task) => task.auto_query_enabled).length,
    multi_city: tasks.filter((task) => task.route_type === "multi_city").length,
    unqueried: tasks.filter((task) => !task.latest_price && !task.is_expired).length,
  };
  tabs.querySelectorAll("button[data-task-view]").forEach((button) => {
    const view = button.dataset.taskView;
    const label = button.textContent.replace(/\s+\d+$/, "");
    button.classList.toggle("active", view === activeTaskView);
    button.textContent = `${label} ${counts[view] || 0}`;
  });
}

function collapseTaskGroups(tasks) {
  const rows = [];
  const groups = new Map();
  for (const task of tasks) {
    if (!task.group_id) {
      rows.push({ type: "task", task });
      continue;
    }
    const key = `${task.group_id}::${task.provider}`;
    if (!groups.has(key)) {
      const group = {
        type: "task_group",
        key,
        group_id: task.group_id,
        provider: task.provider,
        group_label: task.group_label || task.route_display || task.group_id,
        tasks: [],
      };
      groups.set(key, group);
      rows.push(group);
    }
    groups.get(key).tasks.push(task);
  }
  return rows;
}

function summarizeTaskGroup(group) {
  const tasks = group.tasks || [];
  const priced = tasks.filter((task) => task.latest_price);
  const best = priced.length
    ? priced.reduce((winner, task) => Number(task.latest_price) < Number(winner.latest_price) ? task : winner)
    : null;
  const errors = tasks.filter((task) => task.last_error);
  const expired = tasks.length > 0 && tasks.every((task) => task.is_expired);
  const autoEnabled = tasks.filter((task) => task.auto_query_enabled).length;
  const dates = Array.from(new Set(tasks.map((task) => task.departure_date).filter(Boolean))).sort();
  const origins = Array.from(new Set(tasks.map((task) => task.origin).filter(Boolean))).sort();
  const destinations = Array.from(new Set(tasks.map((task) => task.destination).filter(Boolean))).sort();
  return { tasks, priced, best, errors, expired, autoEnabled, dates, origins, destinations };
}

function groupProviderNames(routes) {
  const providers = [];
  for (const route of routes || []) {
    for (const provider of route.providers || []) {
      if (provider && !providers.includes(provider)) providers.push(provider);
    }
  }
  return providers;
}

function groupStatusBadge(summary) {
  if (summary.errors.length) return statusBadge("error");
  if (summary.best) return statusBadge("ok");
  if (summary.expired) return statusBadge("expired");
  if (summary.autoEnabled) return statusBadge("scheduled");
  return statusBadge("idle");
}

function renderTaskRow(task) {
  const row = document.createElement("tr");
  const transferPolicy = transferPolicyLabel(task.transfer_policy || "any");
  const airlines = task.preferred_airlines?.length ? task.preferred_airlines.join(", ") : "不限";
  const dateText = task.route_type === "multi_city"
    ? (task.itinerary_summary || task.departure_date)
    : task.departure_date;
  const groupLine = task.group_label ? `<br><span class="muted">${escapeHtml(task.group_label)}</span>` : "";
  const runDisabled = task.is_expired ? "disabled" : "";
  const runTitle = task.is_expired ? 'title="航线日期已过期，不能查询"' : "";
  const deleteAction = task.group_id
    ? `<button class="ghost compact danger" data-task-action="delete-group" data-group-id="${escapeHtml(task.group_id)}">删除整组</button>`
    : `<button class="ghost compact danger" data-task-action="delete-route" data-key="${escapeHtml(task.route_key)}">删除</button>`;
  row.innerHTML = `
    <td>${statusBadge(task.status)}</td>
    <td>${escapeHtml(providerLabel(task.provider))}</td>
    <td>${escapeHtml(taskRouteDisplay(task))}${groupLine}</td>
    <td>${escapeHtml(dateText)}</td>
    <td>${cabinLabel(task.cabin)}<br><span class="muted">${transferPolicy} · ${airlines}</span></td>
    <td>
      <div class="price-cell">
        <strong>${task.latest_price ? `${task.currency} ${task.latest_price}` : "暂无"}</strong>
        ${renderPriceChangePill(task)}
      </div>
    </td>
    <td>${formatQueryTime(task.last_query_time)}</td>
    <td>
      <div class="task-flight-mini">
        <span>${escapeHtml(taskFlightSummary(task))}</span>
        ${detailButton(task)}
      </div>
    </td>
    <td>${formatAutoQueryStatus(task)}</td>
    <td>
      <div class="task-action-stack">
        <button class="ghost compact" data-task-action="run-route" data-key="${escapeHtml(task.route_key)}" data-provider="${escapeHtml(task.provider)}" ${runDisabled} ${runTitle}>
          查询此航线
        </button>
        ${deleteAction}
      </div>
    </td>
  `;
  return row;
}

function renderTaskGroupRow(group) {
  const summary = summarizeTaskGroup(group);
  const sampleTask = summary.tasks[0] || {};
  const best = summary.best;
  const runDisabled = summary.expired ? "disabled" : "";
  const runTitle = summary.expired ? 'title="航线日期已过期，不能查询"' : "";
  const routePreview = summary.tasks.slice(0, 4).map(taskRouteDisplay).join(" / ");
  const detailRows = summary.tasks.map((task) => `
    <div class="multi-city-detail-row">
      <div>
        <strong>${escapeHtml(taskRouteDisplay(task))}</strong>
        <span>${escapeHtml(task.itinerary_summary || task.departure_date || "-")}</span>
      </div>
      <div>${task.latest_price ? `${escapeHtml(task.currency)} ${escapeHtml(task.latest_price)}` : "暂无"}</div>
      <div>${formatQueryTime(task.last_query_time)}</div>
      <div>${detailButton(task)}</div>
      <div>
        <button class="ghost compact" data-task-action="run-route" data-key="${escapeHtml(task.route_key)}" data-provider="${escapeHtml(task.provider)}" ${task.is_expired ? "disabled" : ""}>查询</button>
      </div>
    </div>
  `).join("");
  const row = document.createElement("tr");
  row.className = "multi-city-group-row";
  row.innerHTML = `
    <td>${groupStatusBadge(summary)}</td>
    <td>${escapeHtml(providerLabel(group.provider))}</td>
    <td>
      <strong>${escapeHtml(group.group_label)}</strong>
      <br><span class="muted">${summary.tasks.length} 条具体航线${routePreview ? ` · ${escapeHtml(routePreview)}${summary.tasks.length > 4 ? " ..." : ""}` : ""}</span>
    </td>
    <td>${escapeHtml(summary.dates.join(" / ") || "-")}</td>
    <td>${cabinLabel(sampleTask.cabin)}<br><span class="muted">${transferPolicyLabel(sampleTask.transfer_policy || "any")} · ${summary.origins.length} 个出发地 / ${summary.destinations.length} 个目的地</span></td>
    <td>${best ? `${best.currency} ${best.latest_price}` : "暂无"}<br><span class="muted">${summary.priced.length}/${summary.tasks.length} 已有价格</span></td>
    <td>${formatQueryTime(best?.last_query_time || sampleTask.last_query_time)}</td>
    <td>
      <details class="multi-city-group-details">
        <summary>展开具体航线</summary>
        <div class="multi-city-detail-list">${detailRows}</div>
      </details>
    </td>
    <td>${summary.autoEnabled ? `${summary.autoEnabled}/${summary.tasks.length} 开启` : "未开启"}</td>
    <td>
      <div class="task-action-stack">
        <button class="ghost compact" data-task-action="run-group" data-group-id="${escapeHtml(group.group_id)}" data-provider="${escapeHtml(group.provider)}" ${runDisabled} ${runTitle}>查询本平台组</button>
        <button class="ghost compact danger" data-task-action="delete-group" data-group-id="${escapeHtml(group.group_id)}">删除整组</button>
      </div>
    </td>
  `;
  return row;
}

function renderTasks(tasks) {
  const filtered = filteredTasks(tasks);
  const displayRows = collapseTaskGroups(filtered);
  const body = document.getElementById("taskTableBody");
  renderTaskViewTabs(tasks);
  renderTaskSummary(filtered);
  const signature = JSON.stringify({
    view: activeTaskView,
    provider: document.getElementById("providerFilter")?.value || "",
    cabin: document.getElementById("cabinFilter")?.value || "",
    search: document.getElementById("routeSearchInput")?.value || "",
    rows: displayRows,
  });
  if (signature === lastTaskRenderSignature) return;
  lastTaskRenderSignature = signature;
  body.innerHTML = "";

  if (!filtered.length) {
    body.innerHTML = '<tr><td colspan="10" class="muted">暂无任务</td></tr>';
    return;
  }

  for (const rowItem of displayRows) {
    body.appendChild(rowItem.type === "task_group"
      ? renderTaskGroupRow(rowItem)
      : renderTaskRow(rowItem.task));
  }
}

function groupRoutesForDisplay(routes) {
  const groups = new Map();
  for (const route of routes || []) {
    const key = route.group_id || route.route_key;
    if (!groups.has(key)) {
      groups.set(key, {
        group_id: key,
        group_label: route.group_label || routeLabel(route),
        routes: [],
      });
    }
    groups.get(key).routes.push(route);
  }
  return Array.from(groups.values());
}

function renderRoutes(routes) {
  const container = document.getElementById("routeList");
  const signature = JSON.stringify(routes || []);
  if (signature === lastRouteRenderSignature) return;
  lastRouteRenderSignature = signature;
  container.innerHTML = "";

  if (!routes.length) {
    container.innerHTML = '<p class="empty-state">暂无已配置航线</p>';
    return;
  }

  for (const group of groupRoutesForDisplay(routes)) {
    const route = group.routes[0];
    const card = document.createElement("article");
    card.className = "route-card";
    if (group.routes.length > 1) card.classList.add("route-group-card");
    const providers = route.providers?.join(", ") || "-";
    const airlines = route.preferred_airlines?.join(", ") || "不限";
    const transferPolicy = transferPolicyLabel(route.transfer_policy || "any");
    const autoQueryStatus = route.auto_query_enabled
      ? `已开启，每 ${route.auto_query_interval_hours || 12} 小时一次；下次：${formatQueryTime(route.auto_query_next_run_at)}`
      : "未开启自动查询";
    const segments = (route.segments || [])
      .map((item) => `${item.origin}->${item.destination} ${item.departure_date}`)
      .join(" / ");
    const groupSummary = group.routes.length > 1
      ? `<p class="route-group-summary">${group.routes.length} 条候选 · ${escapeHtml(group.routes.slice(0, 6).map(routeLabel).join(" / "))}${group.routes.length > 6 ? " ..." : ""}</p>`
      : "";
    const groupProviderButtons = groupProviderNames(group.routes)
      .map((provider) => `
        <button class="ghost" data-action="run-group" data-group-id="${escapeHtml(group.group_id)}" data-provider="${escapeHtml(provider)}">
          查询${escapeHtml(providerLabel(provider))}
        </button>
      `)
      .join("");
    const routeActions = group.routes.length > 1
      ? `
        ${groupProviderButtons}
        <button class="ghost" data-action="run-group" data-group-id="${escapeHtml(group.group_id)}">
          查询整组
        </button>
        <button class="ghost danger" data-action="delete-group" data-group-id="${escapeHtml(group.group_id)}">删除整组</button>
      `
      : `
        <button class="ghost" data-action="toggle-auto" data-enabled="${route.auto_query_enabled ? "true" : "false"}" data-key="${route.route_key}">
          ${route.auto_query_enabled ? "关闭自动查询" : "开启 12h 自动查询"}
        </button>
        <button class="ghost" data-action="edit" data-key="${route.route_key}">编辑</button>
        <button class="ghost danger" data-action="delete" data-key="${route.route_key}">删除</button>
      `;
    card.innerHTML = `
      <div>
        <h3>${escapeHtml(group.group_label)}</h3>
        ${groupSummary}
        <p>${route.route_type === "multi_city" ? segments : `${route.departure_date}${route.return_date ? ` / ${route.return_date}` : ""}`}</p>
        <p>类型：${route.route_type === "multi_city" ? "多程" : "单程/往返"}</p>
        <p>舱位：${cabinLabel(route.cabin)}，${transferPolicy}，人数：${route.passengers}</p>
        <p>平台：${providers}</p>
        <p>航司偏好：${airlines}</p>
        <p>自动查询：${autoQueryStatus}</p>
        <p>后台错峰：至少间隔 10 分钟</p>
      </div>
      <div class="card-actions">
        ${routeActions}
      </div>
    `;
    container.appendChild(card);
  }
}

function renderDashboard(data) {
  dashboardData = data;
  const optionsSignature = JSON.stringify({
    providers: data.provider_options || [],
    cabins: data.cabin_options || [],
  });
  if (optionsSignature !== lastDashboardOptionsSignature) {
    lastDashboardOptionsSignature = optionsSignature;
    renderProviderOptions(data.provider_options || []);
    renderCabinOptions(data.cabin_options || []);
  }
  renderTasks(data.tasks || []);
  renderRunProgressSummary(data.run_feedback?.progress || {});
  renderResultChangeSummary(data.run_feedback || {});
  renderRoutes(data.routes || []);
  renderSessionHints(data.session_hints || []);
  updateWorkspaceTabs();
  syncRouteTypeMode();
  cleanVisibleText();
}

async function fetchDashboard() {
  const currentProvider = document.getElementById("providerFilter").value;
  const currentCabin = document.getElementById("cabinFilter").value;
  const currentSearch = document.getElementById("routeSearchInput").value;
  const response = await fetch("/api/dashboard");
  const data = await response.json();
  renderDashboard(data);
  document.getElementById("providerFilter").value = currentProvider;
  document.getElementById("cabinFilter").value = currentCabin;
  document.getElementById("routeSearchInput").value = currentSearch;
  renderTasks(dashboardData.tasks || []);
}

async function fetchLog() {
  const logBox = document.getElementById("logBox");
  if (!logBox) return;
  const response = await fetch("/api/log");
  const data = await response.json();
  document.getElementById("logBox").textContent = data.lines?.join("\n") || "暂无日志";
}

async function runAction(url, successMessage = "操作已完成") {
  const response = await fetch(url, { method: "POST" });
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response, "操作失败"));
  }
  await fetchDashboard();
  await fetchLog();
  notifyComplete(successMessage);
}

async function runAsyncQuery(url, successMessage, fallbackMessage = "查询启动失败") {
  const response = await fetch(url, { method: "POST" });
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response, fallbackMessage));
  }
  await fetchDashboard();
  await fetchLog();
  notifyComplete(successMessage);
  scheduleDashboardPolling(1200);
}

function resetRouteForm() {
  document.getElementById("routeForm").reset();
  document.getElementById("previousRouteKey").value = "";
  document.getElementById("previousGroupId").value = "";
  document.getElementById("routeGroupEnabled").checked = false;
  document.getElementById("routeGroupLabelInput").value = "";
  document.getElementById("originOptionsInput").value = "";
  document.getElementById("destinationOptionsInput").value = "";
  renderSegmentRows();
  for (const checkbox of document.querySelectorAll('#providerCheckboxes input[type="checkbox"]')) {
    checkbox.checked = true;
  }
  syncRouteTypeMode();
  renderRouteExpansionPreview();
  enhanceAirportInputs(document.getElementById("routeForm"));
}

function fillRouteForm(routeKey) {
  setWorkspaceTab("routes");
  const route = (dashboardData.routes || []).find((item) => item.route_key === routeKey);
  if (!route) return;
  document.getElementById("previousRouteKey").value = route.route_key;
  document.getElementById("previousGroupId").value = route.group_id || "";
  document.getElementById("routeTypeSelect").value = route.route_type || "one_way";
  document.getElementById("originInput").value = route.origin;
  document.getElementById("destinationInput").value = route.destination;
  document.getElementById("departureDateInput").value = route.departure_date;
  document.getElementById("returnDateInput").value = route.return_date || "";
  document.getElementById("cabinSelect").value = route.cabin;
  document.getElementById("transferPolicySelect").value = route.transfer_policy || "any";
  document.getElementById("passengersInput").value = route.passengers;
  document.getElementById("airlinesInput").value = (route.preferred_airlines || []).join(", ");
  document.getElementById("routeGroupEnabled").checked = Boolean(route.group_id);
  document.getElementById("routeGroupLabelInput").value = route.group_label || "";
  document.getElementById("originOptionsInput").value = route.origin;
  document.getElementById("destinationOptionsInput").value = route.destination;
  renderSegmentRows(route.segments || []);
  for (const checkbox of document.querySelectorAll('#providerCheckboxes input[type="checkbox"]')) {
    checkbox.checked = (route.providers || []).includes(checkbox.value);
  }
  syncRouteTypeMode();
  renderRouteExpansionPreview();
  enhanceAirportInputs(document.getElementById("routeForm"));
}

async function submitRouteForm(event) {
  event.preventDefault();
  const routeType = document.getElementById("routeTypeSelect").value;
  const isMultiCity = routeType === "multi_city";
  const segments = isMultiCity ? collectSegmentRows() : [];
  if (isMultiCity) {
    const completeSegments = segments.filter((item) => item.origin && item.destination && item.departure_date);
    if (completeSegments.length < 2 || completeSegments.length !== segments.length) {
      throw new Error("多程至少需要 2 段，并且每段都要填写出发地、目的地和日期。");
    }
  }
  const firstSegment = segments[0] || {};
  const lastSegment = segments[segments.length - 1] || {};
  const groupEnabled = isRouteGroupEnabled();
  if (!isMultiCity && document.getElementById("returnDateInput").value) {
    const departureDate = document.getElementById("departureDateInput").value;
    const returnDate = document.getElementById("returnDateInput").value;
    if (!departureDate || returnDate <= departureDate) {
      throw new Error("返回日期必须晚于出发日期。");
    }
  }
  if (groupEnabled && !isMultiCity) {
    const origins = parseCityList(document.getElementById("originOptionsInput").value || document.getElementById("originInput").value);
    const destinations = parseCityList(document.getElementById("destinationOptionsInput").value || document.getElementById("destinationInput").value);
    if (!origins.length || !destinations.length) {
      throw new Error("城市组合至少需要填写一个出发地和一个目的地。");
    }
  }
  const payload = {
    previous_route_key: document.getElementById("previousRouteKey").value,
    previous_group_id: document.getElementById("previousGroupId").value,
    route_batch: groupEnabled,
    group_label: document.getElementById("routeGroupLabelInput").value.trim() || null,
    expansion_mode: document.getElementById("expansionModeSelect").value,
    route_type: routeType,
    origin: isMultiCity ? firstSegment.origin : normalizeCode(document.getElementById("originInput").value),
    destination: isMultiCity ? lastSegment.destination : normalizeCode(document.getElementById("destinationInput").value),
    origin_options: groupEnabled && !isMultiCity ? parseCityList(document.getElementById("originOptionsInput").value || document.getElementById("originInput").value) : null,
    destination_options: groupEnabled && !isMultiCity ? parseCityList(document.getElementById("destinationOptionsInput").value || document.getElementById("destinationInput").value) : null,
    departure_date: isMultiCity ? firstSegment.departure_date : document.getElementById("departureDateInput").value,
    return_date: isMultiCity ? lastSegment.departure_date : (document.getElementById("returnDateInput").value || null),
    cabin: document.getElementById("cabinSelect").value,
    transfer_policy: document.getElementById("transferPolicySelect").value,
    passengers: Number(document.getElementById("passengersInput").value || 1),
    preferred_airlines: normalizeAirlines(document.getElementById("airlinesInput").value),
    segments: groupEnabled && isMultiCity
      ? segments.map((segment) => ({
          ...segment,
          origin_options: segment.origin_options?.length ? segment.origin_options : [segment.origin].filter(Boolean),
          destination_options: segment.destination_options?.length ? segment.destination_options : [segment.destination].filter(Boolean),
        }))
      : segments,
    providers: Array.from(document.querySelectorAll('#providerCheckboxes input[type="checkbox"]:checked'))
      .map((checkbox) => checkbox.value),
  };
  if (!payload.providers.length) {
    throw new Error("请至少选择一个查询平台。");
  }

  const expansionIssue = routeExpansionIssue();
  if (groupEnabled && expansionIssue) {
    throw new Error(expansionIssue.replace(/^，/, ""));
  }

  const response = await fetch("/api/routes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    throw new Error(await responseErrorMessage(response, "保存航线失败"));
  }

  resetRouteForm();
  await fetchDashboard();
  notifyComplete("航线已保存");
}

async function deleteRoute(routeKey) {
  const response = await fetch(`/api/routes/${encodeURIComponent(routeKey)}`, { method: "DELETE" });
  if (!response.ok) {
    const error = await response.text();
    throw new Error(error || "删除航线失败");
  }
  await fetchDashboard();
  notifyComplete("航线已删除");
}

async function deleteRouteGroup(groupId) {
  const response = await fetch(`/api/route-groups/${encodeURIComponent(groupId)}`, { method: "DELETE" });
  if (!response.ok) {
    const error = await response.text();
    throw new Error(error || "删除查询组失败");
  }
  await fetchDashboard();
  notifyComplete("航线组已删除");
}

async function runRouteGroup(groupId, provider = "") {
  const params = provider ? `?provider=${encodeURIComponent(provider)}` : "";
  const response = await fetch(`/api/route-groups/${encodeURIComponent(groupId)}/run-once${params}`, { method: "POST" });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || "查询整组失败");
  }
  notifyComplete(provider ? `已开始查询${providerLabel(provider)}航线组` : "已开始查询整组");
  await fetchDashboard();
  await fetchLog();
  scheduleDashboardPolling(1200);
}

async function toggleAutoQuery(routeKey, enabled) {
  const response = await fetch(`/api/routes/${encodeURIComponent(routeKey)}/auto-query`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  if (!response.ok) {
    const error = await response.text();
    throw new Error(error || "更新自动查询状态失败");
  }
  await fetchDashboard();
  notifyComplete(enabled ? "已开启自动查询" : "已关闭自动查询");
}

async function runSingleRoute(routeKey, provider) {
  const params = provider ? `?provider=${encodeURIComponent(provider)}` : "";
  await runAsyncQuery(
    `/api/routes/${encodeURIComponent(routeKey)}/run-once${params}`,
    "已开始查询此航线",
    "查询此航线失败",
  );
}

function scheduleDashboardPolling(delayMs = 4000) {
  window.clearTimeout(dashboardPollTimer);
  dashboardPollTimer = window.setTimeout(pollDashboardLoop, delayMs);
}

async function pollDashboardLoop() {
  if (dashboardPollInFlight) return;
  dashboardPollInFlight = true;
  try {
    await fetchDashboard();
    const running = Boolean(dashboardData.last_run?.running);
    if (running || activeTailJobId) {
      await fetchLog();
    }
    scheduleDashboardPolling(running ? 1500 : 8000);
  } catch (_error) {
    scheduleDashboardPolling(6000);
  } finally {
    dashboardPollInFlight = false;
  }
}

function ensureBrowserProfilePanel() {
  if (document.getElementById("browserProfilePanel")) return;
  const anchor = document.getElementById("tailDiscoveryPanel") || document.querySelector(".route-grid");
  if (!anchor) return;
  const panel = document.createElement("section");
  panel.id = "browserProfilePanel";
  panel.className = "panel browser-profile-panel";
  panel.innerHTML = `
    <div class="panel-head">
      <div>
        <h2>浏览器档案</h2>
        <p class="muted">管理 Chrome 持久登录态、备用档案和最近验证状态。</p>
      </div>
      <button id="browserProfileRefreshBtn" class="ghost" type="button">刷新状态</button>
    </div>
    <div id="browserProfileSummary" class="profile-summary"></div>
    <div id="browserProfileList" class="profile-list"></div>
  `;
  anchor.insertAdjacentElement("afterend", panel);
  document.getElementById("browserProfileRefreshBtn")?.addEventListener("click", fetchBrowserProfiles);
  panel.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-profile-action]");
    if (!button) return;
    const provider = button.dataset.provider;
    const action = button.dataset.profileAction;
    try {
      if (action === "open") {
        await postBrowserProfileAction(provider, "open", { profile: button.dataset.profile || "selected" });
        notifyComplete("已打开 Chrome 档案，请在新窗口维护登录态");
      } else if (action === "switch") {
        await postBrowserProfileAction(provider, "switch", { profile: button.dataset.profile || "primary" });
        notifyComplete(button.dataset.profile === "recovery" ? "已切到 recovery 档案" : "已切回主档案");
      } else if (action === "reset-recovery") {
        if (!window.confirm("确定重置 recovery 档案？这会删除备用 Chrome 档案里的登录态和缓存。")) return;
        await postBrowserProfileAction(provider, "reset-recovery", {});
        notifyComplete("已重置 recovery 档案");
      } else if (action === "clean") {
        const switchToRecovery = button.dataset.switchRecovery === "true";
        const targetText = button.dataset.profile === "primary" ? "主档案" : "当前档案";
        const message = switchToRecovery
          ? `确定清理 ${targetText} 的携程站点数据，并切到 recovery 档案？`
          : `确定清理 ${targetText} 的携程站点数据、缓存和 Cookie？`;
        if (!window.confirm(message)) return;
        await postBrowserProfileAction(provider, "clean", {
          profile: button.dataset.profile || "selected",
          switch_to_recovery: switchToRecovery,
        });
        notifyComplete(switchToRecovery ? "已清理并切到 recovery 档案" : "已清理 Chrome 档案污染");
      }
      await fetchBrowserProfiles();
    } catch (error) {
      notifyComplete(error.message || "浏览器档案操作失败", { kind: "warning", durationMs: 4200 });
    }
  });
}

async function fetchBrowserProfiles() {
  const panel = document.getElementById("browserProfilePanel");
  if (!panel) return;
  const response = await fetch("/api/browser-profiles");
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || "读取浏览器档案失败");
  }
  browserProfileData = data;
  renderBrowserProfiles(data);
  updateWorkspaceTabs();
}

async function postBrowserProfileAction(provider, action, payload = {}) {
  const response = await fetch(`/api/browser-profiles/${encodeURIComponent(provider)}/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || "浏览器档案操作失败");
  }
  return data;
}

function renderBrowserProfiles(data) {
  const summary = document.getElementById("browserProfileSummary");
  const list = document.getElementById("browserProfileList");
  if (!summary || !list) return;
  const providers = data.providers || [];
  summary.innerHTML = `
    <div><span>默认 backend</span><strong>${escapeHtml(data.default_backend || "-")}</strong></div>
    <div><span>fallback</span><strong>${escapeHtml((data.fallbacks || []).join(" -> ") || "-")}</strong></div>
    <div><span>Chrome 档案</span><strong>${providers.filter((item) => item.chrome_enabled).length}</strong></div>
  `;
  list.innerHTML = providers.map(renderBrowserProfileCard).join("");
}

function renderBrowserProfileCard(item) {
  const profile = item.chrome_profile || {};
  const activity = item.activity || {};
  const selected = profile.selected_profile_name || profile.profile_name || "-";
  const isRecovery = String(selected).includes("recovery");
  const profiles = (profile.profiles || []).map((entry) => `
    <div class="profile-row ${entry.name === selected ? "active" : ""}">
      <div>
        <strong>${escapeHtml(entry.name)}</strong>
        <span>${entry.exists ? "已创建" : "未创建"} · ${entry.ok ? "健康" : escapeHtml(entry.reason || "异常")}</span>
      </div>
      <code>${escapeHtml(entry.path || "")}</code>
    </div>
  `).join("");
  return `
    <article class="profile-card">
      <div class="profile-card-head">
        <div>
          <h3>${escapeHtml(item.provider)}</h3>
          <p>${escapeHtml((item.backend_order || []).join(" -> "))}</p>
        </div>
        <span class="profile-status ${profile.ok ? "ok" : "warn"}">${profile.ok ? "主档案健康" : "主档案异常"}</span>
      </div>
      <div class="profile-kpis">
        <div><span>当前使用</span><strong>${escapeHtml(selected)}</strong></div>
        <div><span>模式</span><strong>${isRecovery ? "Recovery" : "Primary"}</strong></div>
        <div><span>storage_state</span><strong>${item.storage_state_exists ? "存在" : "无"}</strong></div>
      </div>
      <div class="profile-activity">
        <span>最近成功：${escapeHtml(activity.last_success_at || "-")}</span>
        <span>最近验证：${escapeHtml(activity.last_verification_at || "-")}</span>
        <span>最近失败：${escapeHtml(activity.last_failure_at || "-")}</span>
      </div>
      <div class="profile-actions">
        <button class="ghost compact" type="button" data-profile-action="open" data-profile="selected" data-provider="${escapeHtml(item.provider)}">打开当前档案</button>
        <button class="ghost compact" type="button" data-profile-action="open" data-profile="primary" data-provider="${escapeHtml(item.provider)}">打开主档案</button>
        <button class="ghost compact" type="button" data-profile-action="switch" data-profile="primary" data-provider="${escapeHtml(item.provider)}">切回主档案</button>
        <button class="ghost compact" type="button" data-profile-action="switch" data-profile="recovery" data-provider="${escapeHtml(item.provider)}">切到 recovery</button>
        <button class="ghost compact" type="button" data-profile-action="clean" data-profile="selected" data-provider="${escapeHtml(item.provider)}">清理当前污染</button>
        <button class="ghost compact" type="button" data-profile-action="clean" data-profile="primary" data-switch-recovery="true" data-provider="${escapeHtml(item.provider)}">清主档并切 recovery</button>
        <button class="ghost compact danger" type="button" data-profile-action="reset-recovery" data-provider="${escapeHtml(item.provider)}">重置 recovery</button>
      </div>
      <div class="profile-path"><span>当前路径</span><code>${escapeHtml(profile.selected_profile_path || profile.profile_path || "")}</code></div>
      <div class="profile-rows">${profiles}</div>
    </article>
  `;
}

function ensureTailDiscoveryPanel() {
  if (document.getElementById("tailDiscoveryPanel")) return;
  const routeGrid = document.querySelector(".route-grid");
  if (!routeGrid) return;
  const panel = document.createElement("section");
  panel.id = "tailDiscoveryPanel";
  panel.className = "panel tail-discovery-panel";
  panel.innerHTML = `
    <div class="panel-head">
      <div>
        <h2>甩尾发现</h2>
        <p class="muted">输入 A 和中转点 B，系统逐个尝试候选目的地 C，并保留实际经 B 中转的最低价。</p>
      </div>
      <button id="tailRefreshBtn" class="ghost" type="button">刷新结果</button>
    </div>
    <form id="tailDiscoveryForm" class="tail-form">
      <label>
        出发地 A
        <input id="tailOriginInput" data-airport-input placeholder="北京 (BJS)" autocomplete="off" required />
      </label>
      <label>
        中转点 B
        <input id="tailTransferInput" data-airport-input placeholder="成都 (CTU)" autocomplete="off" required />
      </label>
      <label>
        出发日期
        <input id="tailDateInput" type="date" required />
      </label>
      <label>
        查询平台
        <select id="tailProviderSelect">
          <option value="ctrip">携程</option>
          <option value="feizhu">飞猪（实验）</option>
        </select>
      </label>
      <label>
        价格上限
        <input id="tailQueryMaxPriceInput" type="number" min="0" step="50" placeholder="留空不限，如 1200" />
      </label>
      <label>
        起飞时间开始
        <input id="tailDepartureTimeStartInput" type="time" />
      </label>
      <label>
        起飞时间结束
        <input id="tailDepartureTimeEndInput" type="time" />
      </label>
      <label>
        舱位
        <select id="tailCabinSelect">
          <option value="economy_plus">经济舱/超级经济舱</option>
          <option value="business_first">商务舱/头等舱</option>
          <option value="economy">经济舱</option>
          <option value="premium_economy">超级经济舱</option>
          <option value="business">商务舱</option>
          <option value="first">头等舱</option>
        </select>
      </label>
      <label>
        指定航司
        <select id="tailAirlineSelect">
          <option value="">不限航司</option>
          <option value="CA,ZH,TV,SC,KY,NX">国航系航司（CA/ZH/TV/SC/KY/NX）</option>
          <option value="CA">中国国航 CA</option>
          <option value="MU">东方航空 MU</option>
          <option value="CZ">南方航空 CZ</option>
          <option value="ZH">深圳航空 ZH</option>
          <option value="HU">海南航空 HU</option>
          <option value="MF">厦门航空 MF</option>
          <option value="SC">山东航空 SC</option>
          <option value="3U">四川航空 3U</option>
          <option value="TV">西藏航空 TV</option>
          <option value="KY">昆明航空 KY</option>
          <option value="NX">澳门航空 NX</option>
          <option value="JD">首都航空 JD</option>
          <option value="FM">上海航空 FM</option>
          <option value="HO">吉祥航空 HO</option>
          <option value="KN">中国联合航空 KN</option>
          <option value="9C">春秋航空 9C</option>
          <option value="GS">天津航空 GS</option>
          <option value="GJ">长龙航空 GJ</option>
          <option value="EU">成都航空 EU</option>
        </select>
        <input id="tailAirlineInput" placeholder="可选 ZH 或 深圳航空，多个用逗号分隔" autocomplete="off" />
      </label>
      <div class="tail-run-actions">
        <button id="tailRunBtn" class="primary" type="submit">开始发现</button>
        <button id="tailCancelBtn" class="ghost danger" type="button" hidden disabled>中止任务</button>
      </div>
      <div class="tail-candidates">
        <div class="tail-candidate-head">
          <label>
            目的地范围
            <select id="tailCandidateProfileSelect">
              <option value="">按航司推荐目的地</option>
              <option value="ALL_DOMESTIC">全面检索国内航点</option>
              <option value="DEFAULT">基础默认目的地</option>
            </select>
          </label>
          <div id="tailCandidateSummary" class="tail-candidate-summary">候选目的地加载中...</div>
        </div>
        <details class="tail-candidate-box">
          <summary>展开 / 调整候选目的地</summary>
          <div id="tailCandidateList" class="tail-candidate-list"></div>
        </details>
      </div>
    </form>
    <div class="tail-toolbar">
      <div id="tailSummary" class="tail-summary">暂无命中结果</div>
      <label class="tail-control">
        排序
        <select id="tailSortSelect">
          <option value="price">价格从低到高</option>
          <option value="time">查询时间从新到旧</option>
          <option value="confidence">可信度优先</option>
        </select>
      </label>
      <label class="tail-control">
        日期
        <select id="tailDateFilter">
          <option value="">全部日期</option>
        </select>
      </label>
      <label class="tail-control">
        价格上限
        <input id="tailMaxPriceInput" type="number" min="0" step="50" placeholder="如 1200" />
      </label>
      <label class="tail-toggle">
        <input id="tailLatestOnlyInput" type="checkbox" checked />
        每个目的地只显示最新一条
      </label>
    </div>
    <div id="tailJobSummary" class="tail-job-summary" hidden></div>
    <div id="tailResults" class="tail-results"></div>
  `;
  routeGrid.insertAdjacentElement("afterend", panel);
  enhanceAirportInputs(panel);
  document.getElementById("tailDiscoveryForm").addEventListener("submit", (event) => {
    event.preventDefault();
    runTailDiscovery().catch((error) => {
      console.error(error);
    });
  });
  document.getElementById("tailCancelBtn")?.addEventListener("click", cancelActiveTailJob);
  document.getElementById("tailRefreshBtn").addEventListener("click", fetchTailResults);
  document.getElementById("tailSortSelect").addEventListener("change", () => renderTailResults(tailDiscoveryResults));
  document.getElementById("tailDateFilter").addEventListener("change", () => renderTailResults(tailDiscoveryResults));
  document.getElementById("tailMaxPriceInput").addEventListener("input", () => renderTailResults(tailDiscoveryResults));
  document.getElementById("tailLatestOnlyInput").addEventListener("change", () => renderTailResults(tailDiscoveryResults));
  document.getElementById("tailAirlineSelect").addEventListener("change", (event) => {
    document.getElementById("tailAirlineInput").value = event.target.value || "";
    applyTailAirlineDefaults();
  });
  document.getElementById("tailAirlineInput").addEventListener("change", () => {
    applyTailAirlineDefaults();
  });
  document.getElementById("tailCandidateProfileSelect").addEventListener("change", (event) => {
    tailCandidateProfileOverride = event.target.value || "";
    applyTailAirlineDefaults();
  });
  document.getElementById("tailOriginInput").addEventListener("change", applyTailAirlineDefaults);
  document.getElementById("tailOriginInput").addEventListener("input", applyTailAirlineDefaults);
  document.getElementById("tailTransferInput").addEventListener("change", applyTailAirlineDefaults);
  document.getElementById("tailTransferInput").addEventListener("input", applyTailAirlineDefaults);
}

async function fetchTailCandidates() {
  const container = document.getElementById("tailCandidateList");
  if (!container) return;
  const response = await fetch("/api/tail-discovery/candidates");
  const data = await response.json();
  tailSafeBatchSize = Number(data.safe_batch_size || 3);
  tailDefaultProfiles = data.default_profiles || {};
  tailCandidateNames = Object.fromEntries((data.candidates || []).map((item) => [String(item.code || "").toUpperCase(), item.name]));
  window.tailCandidateNames = tailCandidateNames;
  const defaults = new Set(data.default_codes || []);
  container.innerHTML = "";
  (data.candidates || []).forEach((item) => {
    const label = document.createElement("label");
    label.className = "tail-candidate-chip";
    label.innerHTML = `
      <input type="checkbox" value="${escapeHtml(item.code)}" ${defaults.has(item.code) ? "checked" : ""} />
      <span>${escapeHtml(item.name)} ${escapeHtml(item.code)}</span>
    `;
    container.appendChild(label);
  });
  applyTailAirlineDefaults();
  if (tailDiscoveryResults.length) renderTailResults(tailDiscoveryResults);
}

function selectedTailCandidates() {
  const excluded = excludedTailDestinationCodes();
  return Array.from(document.querySelectorAll('#tailCandidateList input[type="checkbox"]:checked'))
    .filter((input) => !excluded.has(input.value))
    .map((input) => input.value);
}

function tailDefaultCodesForAirlines(value) {
  if (tailCandidateProfileOverride) {
    return tailDefaultProfiles[tailCandidateProfileOverride] || [];
  }
  const codes = new Set(String(value || "")
    .split(",")
    .map((item) => normalizeCode(item))
    .filter(Boolean));
  if (!codes.size) return tailDefaultProfiles.DEFAULT || [];
  if (["CA", "ZH", "TV", "SC", "KY", "NX"].every((code) => codes.has(code))) {
    return tailDefaultProfiles.AIR_CHINA_GROUP || [];
  }
  for (const code of ["CA", "CZ", "MU"]) {
    if (codes.has(code)) return tailDefaultProfiles[code] || [];
  }
  return [];
}

function excludedTailDestinationCodes() {
  return new Set([
    normalizeCode(document.getElementById("tailOriginInput")?.value || ""),
    normalizeCode(document.getElementById("tailTransferInput")?.value || ""),
  ].filter(Boolean));
}

function applyTailAirlineDefaults() {
  const selectedCodes = tailDefaultCodesForAirlines(document.getElementById("tailAirlineInput")?.value || "");
  const excluded = excludedTailDestinationCodes();
  if (!selectedCodes.length) {
    document.querySelectorAll('#tailCandidateList input[type="checkbox"]').forEach((input) => {
      input.disabled = excluded.has(input.value);
      if (input.disabled) input.checked = false;
    });
    updateTailCandidateSummary();
    return;
  }
  const selected = new Set(selectedCodes);
  document.querySelectorAll('#tailCandidateList input[type="checkbox"]').forEach((input) => {
    input.disabled = excluded.has(input.value);
    input.checked = !input.disabled && selected.has(input.value);
  });
  updateTailCandidateSummary();
}

function updateTailCandidateSummary() {
  const selected = selectedTailCandidates();
  const excluded = excludedTailDestinationCodes();
  const selectedPreview = selected.slice(0, 8).map(airportCodeDisplayText).join("、");
  const more = selected.length > 8 ? ` 等 ${selected.length} 个` : "";
  const excludedText = Array.from(excluded).map(airportCodeDisplayText).join("、") || "无";
  const summary = document.getElementById("tailCandidateSummary");
  if (summary) {
    summary.innerHTML = `
      <strong>${escapeHtml(String(selected.length))} 个目的地</strong>
      <span>${escapeHtml(selectedPreview || "尚未选择")}${escapeHtml(more)}</span>
      <small>已自动排除：${escapeHtml(excludedText)}</small>
    `;
  }
}

async function runTailDiscovery() {
  const button = document.getElementById("tailRunBtn");
  const cancelButton = document.getElementById("tailCancelBtn");
  const selectedCandidates = selectedTailCandidates();
  button.disabled = true;
  if (cancelButton) {
    cancelButton.hidden = true;
    cancelButton.disabled = true;
  }
  const payload = {
    origin: normalizeCode(document.getElementById("tailOriginInput").value),
    transfer: normalizeCode(document.getElementById("tailTransferInput").value),
    departure_date: document.getElementById("tailDateInput").value,
    provider: document.getElementById("tailProviderSelect")?.value || "ctrip",
    cabin: document.getElementById("tailCabinSelect").value,
    max_price: Number(document.getElementById("tailQueryMaxPriceInput")?.value || 0) || null,
    departure_time_start: document.getElementById("tailDepartureTimeStartInput")?.value || "",
    departure_time_end: document.getElementById("tailDepartureTimeEndInput")?.value || "",
    preferred_airlines: document.getElementById("tailAirlineInput")?.value || "",
    candidate_profile: tailCandidateProfileOverride,
    passengers: 1,
    candidates: selectedCandidates,
  };
  try {
    const response = await fetch("/api/tail-discovery/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "甩尾发现失败");
    }
    activeTailJobId = data.job_id || "";
    updateTailCancelButton(data);
    pollTailDiscoveryJob(activeTailJobId, true);
  } catch (error) {
    button.disabled = false;
  } finally {
    if (!activeTailJobId) {
      button.disabled = false;
      updateTailCancelButton(null);
    }
  }
}

async function cancelActiveTailJob() {
  if (!activeTailJobId) return;
  const cancelButton = document.getElementById("tailCancelBtn");
  if (cancelButton) cancelButton.disabled = true;
  await handleTailJobAction("cancel", activeTailJobId);
  pollTailDiscoveryJob(activeTailJobId, true);
}

async function pollTailDiscoveryJob(jobId, immediate = false) {
  if (!jobId) return;
  if (activeTailJobTimer) {
    clearTimeout(activeTailJobTimer);
    activeTailJobTimer = null;
  }
  const delay = immediate ? 0 : 3500;
  activeTailJobTimer = setTimeout(async () => {
    try {
      const response = await fetch(`/api/tail-discovery/jobs/${encodeURIComponent(jobId)}`);
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || "读取甩尾任务进度失败");
      }
      renderTailJobProgress(data);
      updateTailCancelButton(data);
      fetchTailJobs().catch(() => {});
      const button = document.getElementById("tailRunBtn");
      if (data.status === "needs_verification") {
        if (button) button.disabled = true;
        pollTailDiscoveryJob(jobId);
        return;
      }
      if (data.status === "finished") {
        await fetchTailResults();
        await fetchTailJobs();
        if (button) button.disabled = false;
        activeTailJobId = "";
        updateTailCancelButton(null);
        return;
      }
      if (data.status === "failed") {
        await fetchTailJobs();
        if (button) button.disabled = false;
        activeTailJobId = "";
        updateTailCancelButton(null);
        return;
      }
      if (data.status === "cancelled" || data.status === "interrupted") {
        await fetchTailJobs();
        if (button) button.disabled = false;
        activeTailJobId = "";
        updateTailCancelButton(null);
        return;
      }
      pollTailDiscoveryJob(jobId);
    } catch (error) {
      const button = document.getElementById("tailRunBtn");
      if (button) button.disabled = false;
      activeTailJobId = "";
      updateTailCancelButton(null);
    }
  }, delay);
}

function renderTailJobProgress(job) {
  updateTailCancelButton(job);
  const container = document.getElementById("tailJobSummary");
  if (!container) return;
  if (!job) {
    container.hidden = true;
    container.innerHTML = "";
    return;
  }
  const progress = job.progress || job.result?.progress || {};
  const queueItems = progress.queue?.items || job.result?.queue?.items || [];
  const attempts = progress.attempts || job.result?.report?.attempts || [];
  const attemptByDestination = new Map(
    attempts
      .filter((item) => item?.destination)
      .map((item) => [String(item.destination).toUpperCase(), item])
  );
  const mergedItems = queueItems.length
    ? queueItems.map((item) => ({ ...item, ...(attemptByDestination.get(String(item.destination || "").toUpperCase()) || {}) }))
    : attempts;
  if (!mergedItems.length && !ACTIVE_TAIL_JOB_STATUSES.includes(job.status)) {
    container.hidden = true;
    container.innerHTML = "";
    return;
  }
  const total = progress.total ?? progress.queue?.total ?? mergedItems.length;
  const attempted = progress.attempted ?? attempts.length;
  const saved = progress.saved ?? job.result?.saved ?? 0;
  const noMatch = progress.no_match ?? 0;
  const failed = progress.failed ?? 0;
  const pending = progress.pending ?? progress.queue?.pending ?? 0;
  const rows = mergedItems.slice(0, 36).map(renderTailJobCandidate).join("");
  const overflow = mergedItems.length > 36 ? `<p class="muted">还有 ${mergedItems.length - 36} 个候选未展开显示。</p>` : "";
  container.hidden = false;
  container.innerHTML = `
    <div class="tail-job-head">
      <div>
        <strong>最近甩尾任务</strong>
        <span>${escapeHtml(tailJobStatusLabel(job.status))}</span>
      </div>
      <div class="tail-job-counts">
        <span>总数 ${escapeHtml(String(total || 0))}</span>
        <span>已查 ${escapeHtml(String(attempted || 0))}</span>
        <span>命中 ${escapeHtml(String(saved || 0))}</span>
        <span>未命中 ${escapeHtml(String(noMatch || 0))}</span>
        <span>失败 ${escapeHtml(String(failed || 0))}</span>
        <span>等待 ${escapeHtml(String(pending || 0))}</span>
      </div>
    </div>
    <div class="tail-job-candidates">${rows || '<span class="muted">任务已创建，等待候选进入队列。</span>'}</div>
    ${overflow}
  `;
  cleanVisibleText(container);
}

function renderTailJobCandidate(item) {
  const destination = String(item.destination || "").toUpperCase();
  const status = String(item.status || "pending");
  const label = tailAttemptStatusLabel(status);
  const price = item.price || item.last_price;
  const detail = price ? `CNY ${price}` : tailAttemptPhaseLabel(item.phase || item.last_phase || "");
  return `
    <div class="tail-job-candidate">
      <span>${escapeHtml(airportCodeDisplayText(destination))}</span>
      <strong class="tail-job-state ${escapeHtml(status)}">${escapeHtml(label)}</strong>
      <small>${escapeHtml(detail || "")}</small>
    </div>
  `;
}

function tailJobStatusLabel(status) {
  return {
    queued: "排队中",
    running: "查询中",
    needs_verification: "等待人工验证",
    finished: "已完成",
    failed: "失败",
    cancelled: "已取消",
    interrupted: "已中断",
  }[status] || status || "-";
}

function tailAttemptStatusLabel(status) {
  return {
    matched: "已命中",
    saved: "已保存",
    no_match: "未命中",
    failed: "失败",
    retryable_failed: "可重试",
    validation_failed: "校验失败",
    running: "查询中",
    pending: "等待中",
    started: "已开始",
  }[status] || status || "-";
}

function tailAttemptPhaseLabel(phase) {
  return {
    no_transfer_match: "未找到指定中转",
    query_failed: "查询失败",
    opening_results: "打开结果页",
    parsing_network: "解析航班",
    matched: "已找到路线",
    batch_started: "本批次已开始",
    running: "查询中",
  }[phase] || phase || "";
}

function updateTailCancelButton(job) {
  const cancelButton = document.getElementById("tailCancelBtn");
  if (!cancelButton) return;
  const canCancel = Boolean(job?.job_id) && ACTIVE_TAIL_JOB_STATUSES.includes(job.status);
  cancelButton.hidden = !canCancel;
  cancelButton.disabled = !canCancel;
  cancelButton.textContent = job?.status === "needs_verification" ? "中止验证任务" : "中止任务";
}

async function fetchTailResults() {
  const container = document.getElementById("tailResults");
  if (!container) return;
  const response = await fetch("/api/tail-discovery/results?limit=200");
  const data = await response.json();
  tailDiscoveryResults = data.results || [];
  updateTailDateFilter(tailDiscoveryResults);
  renderTailResults(tailDiscoveryResults);
}

async function fetchTailJobs() {
  const response = await fetch("/api/tail-discovery/jobs?limit=8");
  const data = await response.json();
  tailDiscoveryJobs = data.jobs || [];
  renderTailJobProgress(tailDiscoveryJobs[0] || null);
  updateWorkspaceTabs();
}

async function handleTailJobAction(action, jobId) {
  if (!jobId) return;
  if (action === "cancel") {
    const response = await fetch(`/api/tail-discovery/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "取消甩尾任务失败");
    updateTailCancelButton(data);
    await fetchTailJobs();
  }
}

async function resumeLatestTailJob() {
  const response = await fetch("/api/tail-discovery/jobs/latest");
  const data = await response.json();
  const job = data.job;
  if (!job || !["queued", "running", "needs_verification"].includes(job.status)) return;
  activeTailJobId = job.job_id || "";
  const button = document.getElementById("tailRunBtn");
  if (button) button.disabled = true;
  updateTailCancelButton(job);
  pollTailDiscoveryJob(activeTailJobId);
}

function updateTailDateFilter(results) {
  const select = document.getElementById("tailDateFilter");
  if (!select) return;
  const previous = select.value;
  const dates = Array.from(new Set((results || []).map((item) => item.departure_date).filter(Boolean))).sort();
  select.innerHTML = '<option value="">全部日期</option>' + dates
    .map((date) => `<option value="${escapeHtml(date)}">${escapeHtml(date)}</option>`)
    .join("");
  select.value = dates.includes(previous) ? previous : "";
}

function prepareTailResults(results) {
  let prepared = [...(results || [])];
  const dateFilter = document.getElementById("tailDateFilter")?.value || "";
  if (dateFilter) {
    prepared = prepared.filter((item) => item.departure_date === dateFilter);
  }
  const maxPrice = Number(document.getElementById("tailMaxPriceInput")?.value || 0);
  if (maxPrice > 0) {
    prepared = prepared.filter((item) => Number(item.price || Infinity) <= maxPrice);
  }
  if (document.getElementById("tailLatestOnlyInput")?.checked) {
    const latestByKey = new Map();
    for (const item of prepared) {
      const key = [item.origin, item.transfer, item.destination, item.departure_date, item.cabin].join("|");
      const current = latestByKey.get(key);
      if (!current || String(item.observed_at || "") > String(current.observed_at || "")) {
        latestByKey.set(key, item);
      }
    }
    prepared = Array.from(latestByKey.values());
  }
  const sortMode = document.getElementById("tailSortSelect")?.value || "price";
  prepared.sort((left, right) => {
    if (sortMode === "time") {
      return String(right.observed_at || "").localeCompare(String(left.observed_at || ""));
    }
    if (sortMode === "confidence") {
      return tailResultConfidenceScore(right) - tailResultConfidenceScore(left)
        || Number(left.price || 0) - Number(right.price || 0);
    }
    return Number(left.price || 0) - Number(right.price || 0);
  });
  return prepared;
}

function renderTailResults(results) {
  const container = document.getElementById("tailResults");
  if (!container) return;
  const visibleResults = prepareTailResults(results);
  renderTailSummary(visibleResults, results);
  if (!visibleResults.length) {
    container.innerHTML = `
      <div class="tail-empty-state">
        <strong>当前筛选下没有甩尾命中结果</strong>
        <span>可以调整日期筛选、航司筛选或重新发起查询。</span>
      </div>
    `;
    return;
  }
  container.innerHTML = groupTailResultsByRoute(visibleResults)
    .map((group) => renderTailRouteGroup(group))
    .join("");
  cleanVisibleText(container);
  updateWorkspaceTabs();
}

function groupTailResultsByRoute(results) {
  const routeGroups = new Map();
  for (const item of results || []) {
    const routeKey = [item.origin || "", item.transfer || ""].join("|");
    if (!routeGroups.has(routeKey)) {
      routeGroups.set(routeKey, { origin: item.origin, transfer: item.transfer, results: [] });
    }
    routeGroups.get(routeKey).results.push(item);
  }
  return Array.from(routeGroups.values()).map((group) => {
    const byDate = new Map();
    for (const item of group.results) {
      const date = item.departure_date || "未标日期";
      if (!byDate.has(date)) byDate.set(date, []);
      byDate.get(date).push(item);
    }
    const dateGroups = Array.from(byDate.entries())
      .map(([date, items]) => ({
        date,
        items: items.sort((left, right) => Number(left.price || 0) - Number(right.price || 0)),
      }))
      .sort((left, right) => String(left.date).localeCompare(String(right.date)));
    const best = group.results.reduce((winner, item) => (
      !winner || Number(item.price || Infinity) < Number(winner.price || Infinity) ? item : winner
    ), null);
    return { ...group, dateGroups, best };
  }).sort((left, right) => {
    const leftBest = Number(left.best?.price || Infinity);
    const rightBest = Number(right.best?.price || Infinity);
    if (leftBest !== rightBest) return leftBest - rightBest;
    return `${left.origin || ""}${left.transfer || ""}`.localeCompare(`${right.origin || ""}${right.transfer || ""}`);
  });
}

function renderTailResultCard(item, index) {
  const legs = (item.flight_details || []).map((leg, legIndex) => {
    const segmentIndex = legIndex + 1;
    const from = airportCodeDisplayText(leg.departure_airport || leg.origin);
    const to = airportCodeDisplayText(leg.arrival_airport || leg.destination);
    const flight = [leg.airline, leg.flight_no].filter(Boolean).join(" ") || "携程列表未展开航班号";
    const cabin = cabinLabel(leg.cabin || item.cabin);
    const time = `${leg.departure_time || "--:--"} -> ${leg.arrival_time || "--:--"}`;
    const layover = leg.transfer_layover ? ` · 中转停留 ${leg.transfer_layover}` : "";
    return `
      <div class="tail-leg-row">
        <div class="tail-leg-index">${escapeHtml(segmentIndex)}</div>
        <div>
          <strong>${escapeHtml(from)} -> ${escapeHtml(to)}</strong>
          <span>${escapeHtml(time)} · ${escapeHtml(flight)}</span>
          <span>${escapeHtml(leg.departure_date || item.departure_date)} · ${escapeHtml(cabin)}${escapeHtml(layover)}</span>
        </div>
      </div>
    `;
  }).join("");
  const routeTitle = `${airportCodeDisplayText(item.origin)} -> ${airportCodeDisplayText(item.transfer)} -> ${airportCodeDisplayText(item.destination)}`;
  const confidence = tailResultConfidence(item);
  const reviewUrl = item.review_url || item.search_url || "";
  const metaItems = [
    item.departure_date,
    `平台 ${providerLabel(item.provider)}`,
    cabinLabel(item.cabin),
    item.detail_quality || item.raw_payload?.detail_quality || "partial",
    confidence.label,
    item.observed_at ? `查询 ${formatQueryDate(item.observed_at)}` : "",
  ].filter(Boolean);
  return `
    <article class="tail-result-card">
      <div class="tail-rank">${escapeHtml(index)}</div>
      <div class="tail-result-main">
        <div class="tail-route-title">${escapeHtml(routeTitle)}</div>
        <div class="tail-route-meta">${metaItems.map((meta) => `<span>${escapeHtml(meta)}</span>`).join("")}</div>
      </div>
      <div class="tail-price">
        <small>最低价</small>
        <strong>¥${Number(item.price || 0).toFixed(0)}</strong>
      </div>
      <div class="tail-review-actions">
        <span class="tail-confidence level-${escapeHtml(confidence.level)}">${escapeHtml(confidence.label)}</span>
        ${reviewUrl ? `<a class="ghost compact tail-review-link" href="${escapeHtml(reviewUrl)}" target="_blank" rel="noreferrer">打开携程复核</a>` : ""}
      </div>
      <details class="tail-details">
        <summary>航段详情</summary>
        <div class="tail-leg-list">${legs || "暂无航段明细"}</div>
      </details>
    </article>
  `;
}

function tailResultConfidence(item) {
  const validationStatus = item.validation?.status || item.raw_payload?.validation?.status || "";
  if (validationStatus === "invalid") return { level: "low", label: "硬校验失败" };
  if (validationStatus === "needs_review") return { level: "low", label: "需复核" };
  const score = tailResultConfidenceScore(item);
  if (score >= 80) return { level: "high", label: "高可信" };
  if (score >= 55) return { level: "medium", label: "中可信" };
  return { level: "low", label: "需复核" };
}

function tailResultConfidenceScore(item) {
  let score = 35;
  const details = item.flight_details || [];
  const source = item.detail_source || item.raw_payload?.detail_source || "";
  const quality = item.detail_quality || item.raw_payload?.detail_quality || "";
  if (source === "network_exact") score += 35;
  if (source === "dom_transit_card") score += 25;
  if (source === "text_fallback") score += 10;
  if (quality === "complete") score += 20;
  if (details.length >= 2) score += 15;
  if (details.some((leg) => leg.flight_no)) score += 8;
  if (details.some((leg) => String(leg.tail_transfer || "").toUpperCase() === String(item.transfer || "").toUpperCase())) score += 8;
  if (item.date_evidence?.matched_source) score += 5;
  if ((item.validation?.status || item.raw_payload?.validation?.status) === "valid") score += 12;
  if ((item.validation?.status || item.raw_payload?.validation?.status) === "needs_review") score -= 20;
  return Math.max(0, Math.min(score, 100));
}

function renderTailRouteGroup(group) {
  const title = `${airportCodeDisplayText(group.origin)} -> ${airportCodeDisplayText(group.transfer)} -> X`;
  const destinations = new Set(group.results.map((item) => item.destination).filter(Boolean));
  return `
    <section class="tail-route-group">
      <div class="tail-route-group-head">
        <div>
          <strong>${escapeHtml(title)}</strong>
          <span>${escapeHtml(group.dateGroups.length)} 个日期 · ${escapeHtml(destinations.size)} 个目的地</span>
        </div>
        <div class="tail-route-group-best">点击日期查看结果</div>
      </div>
      <div class="tail-date-groups">
        ${group.dateGroups.map((dateGroup) => `
          <details class="tail-date-group">
            <summary>
              <span>${escapeHtml(dateGroup.date)}</span>
              <small>${escapeHtml(dateGroup.items.length)} 条结果，点击展开</small>
            </summary>
            <div class="tail-date-result-list">
              ${dateGroup.items.map((item, itemIndex) => renderTailResultCard(item, itemIndex + 1)).join("")}
            </div>
          </details>
        `).join("")}
      </div>
    </section>
  `;
}

function renderTailSummary(visibleResults, allResults) {
  const summary = document.getElementById("tailSummary");
  if (!summary) return;
  const dateFilter = document.getElementById("tailDateFilter")?.value || "";
  const maxPrice = Number(document.getElementById("tailMaxPriceInput")?.value || 0);
  if (!visibleResults.length) {
    summary.textContent = allResults.length ? "当前筛选下暂无结果" : "暂无命中结果";
    return;
  }
  const groups = groupTailResultsByRoute(visibleResults);
  const destinations = new Set(visibleResults.map((item) => item.destination));
  const filters = [
    dateFilter ? `日期 ${dateFilter}` : "",
    maxPrice > 0 ? `价格≤¥${maxPrice.toFixed(0)}` : "",
  ].filter(Boolean).join(" · ");
  summary.textContent = `${filters ? `${filters} · ` : ""}${groups.length} 条主航线，${visibleResults.length} 条结果 / ${destinations.size} 个目的地`;
}

function organizeDashboardLayout() {
  document.getElementById("taskTableBody")?.closest("section")?.classList.add("task-workspace");
  ensureTaskDetailDrawer();
  ensureTailDiscoveryPanel();
  ensureBrowserProfilePanel();
  ensureWorkspaceTabs();
  setWorkspaceTab(activeWorkspaceTab, { persist: false });
}

function workspacePanelMap() {
  return {
    routes: document.querySelector(".route-grid"),
    tasks: document.getElementById("taskTableBody")?.closest("section"),
    tail: document.getElementById("tailDiscoveryPanel"),
    profiles: document.getElementById("browserProfilePanel"),
    insights: document.querySelector(".log-workspace"),
  };
}

function ensureWorkspaceTabs() {
  if (document.getElementById("workspaceTabs")) return;
  const anchor = document.querySelector(".hero");
  if (!anchor) return;
  const tabs = document.createElement("nav");
  tabs.id = "workspaceTabs";
  tabs.className = "workspace-tabs";
  tabs.setAttribute("aria-label", "主功能标签");
  tabs.innerHTML = `
    <button type="button" data-workspace-tab="routes">航线配置 <span data-workspace-count="routes"></span></button>
    <button type="button" data-workspace-tab="tasks">查询任务 <span data-workspace-count="tasks"></span></button>
    <button type="button" data-workspace-tab="tail">甩尾发现 <span data-workspace-count="tail"></span></button>
    <button type="button" data-workspace-tab="profiles">浏览器档案 <span data-workspace-count="profiles"></span></button>
    <button type="button" data-workspace-tab="insights">日志</button>
  `;
  anchor.insertAdjacentElement("afterend", tabs);
  tabs.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-workspace-tab]");
    if (!button) return;
    setWorkspaceTab(button.dataset.workspaceTab);
  });
  Object.values(workspacePanelMap()).forEach((panel) => panel?.classList.add("workspace-panel"));
  updateWorkspaceTabs();
}

function setWorkspaceTab(tab, options = {}) {
  const panels = workspacePanelMap();
  const nextTab = panels[tab] ? tab : "routes";
  activeWorkspaceTab = nextTab;
  if (options.persist !== false) {
    localStorage.setItem("activeWorkspaceTab", nextTab);
  }
  Object.entries(panels).forEach(([key, panel]) => {
    if (!panel) return;
    panel.hidden = key !== nextTab;
    panel.classList.toggle("active", key === nextTab);
  });
  document.querySelectorAll("[data-workspace-tab]").forEach((button) => {
    const active = button.dataset.workspaceTab === nextTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
}

function updateWorkspaceTabs() {
  const tabs = document.getElementById("workspaceTabs");
  if (!tabs) return;
  const routes = dashboardData.routes || [];
  const tasks = dashboardData.tasks || [];
  const tailRunning = tailDiscoveryJobs.some((job) => ACTIVE_TAIL_JOB_STATUSES.includes(job.status));
  const profileWarnings = (browserProfileData.providers || []).filter((item) => item.chrome_profile && !item.chrome_profile.ok).length;
  const counts = {
    routes: routes.length ? String(routes.length) : "",
    tasks: tasks.length ? String(tasks.length) : "",
    tail: tailRunning ? "运行中" : (tailDiscoveryResults.length ? String(tailDiscoveryResults.length) : ""),
    profiles: profileWarnings ? String(profileWarnings) : "",
  };
  Object.entries(counts).forEach(([key, value]) => {
    const badge = tabs.querySelector(`[data-workspace-count="${key}"]`);
    if (!badge) return;
    badge.textContent = value;
    badge.hidden = !value;
  });
}

document.getElementById("runAllBtn").addEventListener("click", () => runAsyncQuery("/api/run-once", "已开始查询全部航线", "启动全部查询失败"));
document.getElementById("reportBtn").addEventListener("click", () => runAction("/api/report", "报告已刷新"));
document.getElementById("reloadBtn").addEventListener("click", fetchDashboard);
document.getElementById("reloadLogBtn")?.addEventListener("click", fetchLog);
document.getElementById("resetRouteBtn").addEventListener("click", resetRouteForm);
document.getElementById("routeForm").addEventListener("submit", (event) => {
  submitRouteForm(event).catch((error) => {
    console.error(error);
    notifyComplete(error.message || "保存航线失败", { kind: "warning", durationMs: 5200 });
  });
});
document.getElementById("routeTypeSelect").addEventListener("change", syncRouteTypeMode);
document.getElementById("departureDateInput").addEventListener("change", () => syncRoundTripDateBounds("departure"));
document.getElementById("returnDateInput").addEventListener("change", () => syncRoundTripDateBounds("return"));
document.getElementById("addSegmentBtn").addEventListener("click", () => addSegmentRow({}));
document.getElementById("routeGroupEnabled").addEventListener("change", () => {
  syncRouteFormVisibility();
  syncRouteFormRequirements();
  renderRouteExpansionPreview();
});
document.getElementById("routeGroupLabelInput").addEventListener("input", renderRouteExpansionPreview);
document.getElementById("originOptionsInput").addEventListener("input", renderRouteExpansionPreview);
document.getElementById("destinationOptionsInput").addEventListener("input", renderRouteExpansionPreview);
document.getElementById("expansionModeSelect").addEventListener("change", renderRouteExpansionPreview);
document.getElementById("segmentRows").addEventListener("click", (event) => {
  const button = event.target.closest(".segment-remove");
  if (!button || button.disabled) return;
  button.closest(".segment-row").remove();
  renumberSegments();
  renderRouteExpansionPreview();
});
document.getElementById("segmentRows").addEventListener("input", renderRouteExpansionPreview);
document.getElementById("providerFilter").addEventListener("change", () => renderTasks(dashboardData.tasks || []));
document.getElementById("cabinFilter").addEventListener("change", () => renderTasks(dashboardData.tasks || []));
document.getElementById("routeSearchInput").addEventListener("input", () => renderTasks(dashboardData.tasks || []));
document.getElementById("taskTableBody").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-task-action]");
  if (!button) return;
  if (button.dataset.taskAction === "show-detail") {
    showTaskDetail(button.dataset.key, button.dataset.provider);
    return;
  }
  if (button.dataset.taskAction === "run-route") {
    button.disabled = true;
    try {
      await runSingleRoute(button.dataset.key, button.dataset.provider);
    } catch (error) {
      console.error(error);
    } finally {
      button.disabled = false;
    }
    return;
  }
  if (button.dataset.taskAction === "run-group") {
    button.disabled = true;
    try {
      await runRouteGroup(button.dataset.groupId, button.dataset.provider || "");
    } catch (error) {
      console.error(error);
    } finally {
      button.disabled = false;
    }
    return;
  }
  if (button.dataset.taskAction === "delete-route") {
    button.disabled = true;
    try {
      await deleteRoute(button.dataset.key);
    } catch (error) {
      console.error(error);
      button.disabled = false;
    }
    return;
  }
  if (button.dataset.taskAction === "delete-group") {
    button.disabled = true;
    try {
      await deleteRouteGroup(button.dataset.groupId);
    } catch (error) {
      console.error(error);
      button.disabled = false;
    }
  }
});
document.getElementById("routeList").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  const routeKey = button.dataset.key;
  if (action === "edit") {
    fillRouteForm(routeKey);
    return;
  }
  if (action === "delete") {
    await deleteRoute(routeKey);
    return;
  }
  if (action === "delete-group") {
    await deleteRouteGroup(button.dataset.groupId);
    return;
  }
  if (action === "run-group") {
    button.disabled = true;
    try {
      await runRouteGroup(button.dataset.groupId, button.dataset.provider || "");
    } finally {
      button.disabled = false;
    }
    return;
  }
  if (action === "toggle-auto") {
    await toggleAutoQuery(routeKey, button.dataset.enabled !== "true");
  }
});

organizeDashboardLayout();
enhanceAirportInputs(document);
renderSegmentRows();
syncRouteTypeMode();
fetchTailCandidates();
fetchTailResults();
fetchTailJobs();
fetchBrowserProfiles().catch(() => {});
resumeLatestTailJob().catch(() => {});
fetchDashboard().then(() => scheduleDashboardPolling(3000));

