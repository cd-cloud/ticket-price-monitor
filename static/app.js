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
let tailSelectedJobId = "";
let tailSelectedJob = null;
let tailResultView = "current";
let tailResultRequest = 0;
let tailResultOffset = 0;
let tailHasMore = false;
let tailCandidatesManual = false;
let tailSavedCandidates = null;
let tailPollFailures = 0;
let tailResultsLoaded = false;
let tailConnectionUnknown = false;
let tailSubmitting = false;
let tailCandidatesLoaded = false;
let tailFormTouched = false;
const tailDisclosureState = new Map();
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

function tailFormFieldIds() {
  return ["tailOriginInput", "tailTransferInput", "tailDateInput", "tailProviderSelect",
    "tailQueryMaxPriceInput", "tailDepartureTimeStartInput", "tailDepartureTimeEndInput",
    "tailCabinSelect", "tailAirlineSelect", "tailAirlineInput", "tailFilterScopeInput",
    "tailSearchScopeInput", "tailFollowupBudgetInput", "tailCallBudgetInput", "tailPassengersInput",
    "tailBudgetCitiesInput", "tailBudgetMinutesInput", "tailTargetInput", "tailBaselineBudgetInput",
    "tailCandidateProfileSelect"];
}


function saveTailForm() {
  try {
    const fields = Object.fromEntries(tailFormFieldIds().map(id => [id, document.getElementById(id)?.value || ""]));
    localStorage.setItem("tail-search-form-v1", JSON.stringify({
      fields, manual: tailCandidatesManual, candidates: selectedTailCandidates(),
    }));
  } catch { /* A private browser or full storage must not block searching. */ }
}


function restoreTailForm() {
  try {
    const saved = JSON.parse(localStorage.getItem("tail-search-form-v1") || "null");
    if (!saved || typeof saved.fields !== "object") return;
    for (const id of tailFormFieldIds()) {
      const input = document.getElementById(id);
      const value = saved.fields[id];
      if (input && typeof value === "string") input.value = value;
    }
    tailCandidatesManual = saved.manual === true;
    tailCandidateProfileOverride = document.getElementById("tailCandidateProfileSelect").value;
    if (tailCandidatesManual && Array.isArray(saved.candidates)) tailSavedCandidates = saved.candidates;
  } catch { /* Ignore incompatible saved settings, never copy arbitrary keys. */ }
}


function updateTailFormSummary() {
  const value = id => document.getElementById(id)?.value || "";
  const route = [value("tailOriginInput"), value("tailTransferInput")].filter(Boolean).join(" → ");
  const summary = document.getElementById("tailQuerySummary");
  if (summary) summary.textContent = [route, value("tailDateInput"), providerLabel(value("tailProviderSelect"))].filter(Boolean).join(" · ");
  const filters = document.getElementById("tailFilterSummary");
  if (filters) filters.textContent = [
    cabinLabel(value("tailCabinSelect")), value("tailAirlineInput") || "不限航司",
    value("tailFilterScopeInput") === "all_legs" ? "全行程" : "仅首段",
  ].join(" · ");
  const settings = document.getElementById("tailSearchSummary");
  if (settings) settings.textContent = value("tailSearchScopeInput") === "open" ? "开放发现 · 非全量"
    : `${value("tailSearchScopeInput") === "hybrid" ? "开放 + 定向补查 · " : ""}已选 ${selectedTailCandidates().length} 城市`;
  updateTailResultContext();
}

function tailConditionLabel(payload = {}) {
  return [
    payload.origin && payload.transfer ? `${tailPlaceLabel(payload.origin)} → ${tailPlaceLabel(payload.transfer)}` : "路线未记录",
    payload.departure_date || "日期未记录",
    payload.provider ? providerLabel(payload.provider) : "平台未记录",
  ].join(" · ");
}

function tailQueryDiffers(saved, draft) {
  // Compare only recorded settings: older tasks may not have a complete payload.
  const numeric = new Set(["max_price", "passengers", "max_candidates", "max_minutes",
    "target_results", "max_baseline_queries", "max_followup_candidates", "max_provider_calls"]);
  const ignored = new Set(["mode", "ignore_cache"]);
  if (saved.search_scope !== "hybrid" && draft.search_scope !== "hybrid") ignored.add("max_followup_candidates");
  if (saved.provider !== "feizhu" && draft.provider !== "feizhu") {
    for (const key of ["max_baseline_queries", "max_followup_candidates", "max_provider_calls"]) ignored.add(key);
  }
  if (saved.search_scope === "open" && draft.search_scope === "open") {
    for (const key of ["candidates", "candidate_profile", "max_candidates", "target_results"]) ignored.add(key);
  }
  if (saved.search_scope === "hybrid" && draft.search_scope === "hybrid") {
    ignored.add("max_candidates"); ignored.add("target_results");
  }
  const normalize = (key, value) => {
    if (numeric.has(key)) return Number(value || 0);
    if (["origin", "transfer"].includes(key)) return normalizeCode(value || "");
    if (["candidates", "preferred_airlines"].includes(key)) {
      const values = Array.isArray(value) ? value : String(value || "").split(/[,，、;\s]+/);
      return [...new Set(values.map(v => String(v).trim().toUpperCase()).filter(Boolean))].sort().join(",");
    }
    return String(value ?? "").trim();
  };
  return Object.keys(draft).some(key => !ignored.has(key) && Object.hasOwn(saved, key)
    && normalize(key, saved[key]) !== normalize(key, draft[key]));
}

function updateTailResultContext() {
  const context = document.getElementById("tailResultIdentity");
  const notice = document.getElementById("tailDraftNotice");
  const snapshot = document.getElementById("tailTaskConditions");
  if (!context || !notice || !snapshot) return;
  notice.hidden = true;
  snapshot.textContent = "";
  if (tailResultView === "history") {
    context.textContent = "历史报价 · 跨任务的已保存结果";
    notice.textContent = "上方条件仅用于新查询，不会改变历史报价；请使用列表筛选。";
    notice.hidden = false;
    return;
  }
  const job = tailSelectedJob?.job_id === tailSelectedJobId ? tailSelectedJob : null;
  if (!job) {
    context.textContent = tailSelectedJobId ? "正在读取所选任务条件…" : "尚未选择查询任务";
    return;
  }
  const p = job.payload || {};
  context.textContent = `结果所属：${tailConditionLabel(p)}`;
  const parts = [
    p.cabin ? cabinLabel(p.cabin) : "舱位未记录",
    Object.hasOwn(p, "preferred_airlines") ? (Array.isArray(p.preferred_airlines) ? p.preferred_airlines.join("、") : p.preferred_airlines) || "不限航司" : "航司未记录",
    p.passengers ? `${p.passengers} 人` : "人数未记录",
    p.filter_scope ? (p.filter_scope === "all_legs" ? "舱位 / 航司约束全行程" : "舱位 / 航司仅约束首段") : "筛选范围未记录",
    Object.hasOwn(p, "departure_time_start") || Object.hasOwn(p, "departure_time_end")
      ? `起飞时间：${p.departure_time_start || "不限"} — ${p.departure_time_end || "不限"}` : "起飞时间范围未记录",
    !Object.hasOwn(p, "max_price") ? "查询价格上限未记录"
      : `查询价格上限：${p.max_price || "不限"}`,
  ];
  if (p.search_scope) parts.push(`检索方式：${{candidates:"定向城市", open:"开放发现（非全量）", hybrid:"开放发现 + 定向补查"}[p.search_scope] || p.search_scope}`);
  if (Array.isArray(p.candidates) && p.search_scope !== "open") {
    parts.push(`任务候选：${p.candidates.length} 城市（${p.candidates.slice(0, 8).map(tailPlaceLabel).join("、")}${p.candidates.length > 8 ? "…" : ""}）`);
  }
  for (const [key, label, zero] of [
    ["max_minutes", "时间上限（分钟）", "不限"],
    ["max_candidates", "查询城市上限", "不限"],
    ["target_results", "已校验首段停止目标", "不限"],
    ["max_followup_candidates", "补查城市上限", "关闭"],
    ["max_provider_calls", "本轮调用上限", "不限"],
    ["max_baseline_queries", "同首段基准补查次数", "关闭"],
  ]) {
    if (!Object.hasOwn(p, key)) continue;
    if (p.provider !== "feizhu" && ["max_followup_candidates", "max_provider_calls", "max_baseline_queries"].includes(key)) continue;
    if (p.search_scope !== "hybrid" && key === "max_followup_candidates") continue;
    if (["open", "hybrid"].includes(p.search_scope) && ["max_candidates", "target_results"].includes(key)) continue;
    parts.push(`${label}：${Number(p[key]) || zero}`);
  }
  snapshot.textContent = `${parts.join(" · ")}。任务未记录的设置无法比对；修改上方条件不会改写此任务。`;
  if (tailQueryDiffers(p, buildTailQueryPayload())) {
    notice.textContent = `${tailFormTouched ? "查询条件已修改" : "上方待提交条件与此任务不同"}，下方仍为所选任务的结果。点击“开始搜索”才会使用新条件。`;
    notice.hidden = false;
  }
}

function updateTailResultTools() {
  const summary = document.getElementById("tailResultFilterSummary");
  if (summary) {
    const count = Number(Boolean(document.getElementById("tailDateFilter")?.value))
      + Number(Number(document.getElementById("tailMaxPriceInput")?.value || 0) > 0)
      + Number((document.getElementById("tailTrustInput")?.value || "all") !== "all")
      + Number(document.getElementById("tailLatestOnlyInput")?.checked === false);
    summary.textContent = count ? `筛选 · ${count} 项已调整` : "筛选";
  }
  const jump = document.getElementById("tailViewResultsBtn");
  if (jump) jump.hidden = !tailDiscoveryResults.length;
  updateTailResultContext();
}


function filterTailCandidateList() {
  const keyword = document.getElementById("tailCandidateSearch").value.trim().toLowerCase();
  document.querySelectorAll("#tailCandidateList label").forEach(label => {
    label.hidden = !label.textContent.toLowerCase().includes(keyword);
  });
}


function clearTailResultFilters() {
  for (const [id, value] of [["tailDateFilter", ""], ["tailMaxPriceInput", ""], ["tailTrustInput", "all"]]) {
    document.getElementById(id).value = value;
  }
  document.getElementById("tailLatestOnlyInput").checked = true;
  renderTailResults(tailDiscoveryResults);
}


function replaceTailMarkup(container, html) {
  if (container.innerHTML === html) return;
  const details = container.querySelectorAll?.("details[data-disclosure]") || [];
  for (const el of details) tailDisclosureState.set(el.dataset.disclosure, el.open);
  const anchors = [...(container.querySelectorAll?.("[data-stable-key]") || [])];
  const anchor = anchors.find(el => el.getBoundingClientRect().bottom > 80);
  const anchorKey = anchor?.dataset.stableKey;
  const anchorTop = anchor?.getBoundingClientRect().top;
  const focusKey = container.contains?.(document.activeElement) ? document.activeElement?.dataset.focusKey : null;
  container.innerHTML = html;
  for (const el of container.querySelectorAll?.("details[data-disclosure]") || []) {
    if (tailDisclosureState.has(el.dataset.disclosure)) el.open = tailDisclosureState.get(el.dataset.disclosure);
  }
  if (focusKey) {
    [...container.querySelectorAll("[data-focus-key]")].find(el => el.dataset.focusKey === focusKey)?.focus({preventScroll:true});
  }
  if (anchorKey && anchorTop < window.innerHeight) {
    const replacement = [...container.querySelectorAll("[data-stable-key]")].find(el => el.dataset.stableKey === anchorKey);
    if (replacement) window.scrollBy(0, replacement.getBoundingClientRect().top - anchorTop);
  }
}


async function tailGetJson(url) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(url, {signal: controller.signal});
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(data.detail || "读取失败");
      error.status = response.status;
      throw error;
    }
    return data;
  } finally { clearTimeout(timer); }
}

async function tailPostJob(url, options = {}) {
  // Disable competing actions before dispatch, not after the response arrives.
  tailSubmitting = true;
  updateTailBusyControls();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(url, {method:"POST", signal:controller.signal, ...options});
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(data.detail || "任务操作失败");
      error.authoritative = true;
      throw error;
    }
    return data;
  } catch (error) {
    if (!error.authoritative) {
      tailConnectionUnknown = true;
      document.getElementById("tailConnectionStatus").hidden = false;
      document.getElementById("tailConnectionStatus").textContent = "提交响应中断，正在确认后台是否已接受任务；不会自动重复提交。";
      setTimeout(() => initializeTailWorkspace().catch(() => {}), 3500);
    }
    throw error;
  } finally {
    clearTimeout(timer);
    tailSubmitting = false;
    updateTailBusyControls();
  }
}


function tailBusy() {
  return Boolean(activeTailJobId) || tailConnectionUnknown || tailSubmitting;
}


function updateTailBusyControls() {
  const busy = tailBusy();
  document.getElementById("tailRunBtn").disabled = busy;
  document.getElementById("tailQueryFields").disabled = busy;
  document.getElementById("tailRunBtn").textContent = busy ? "查询进行中" : "开始搜索";
  document.getElementById("tailRunHint").textContent = tailConnectionUnknown
    ? "连接中断，后台状态待确认；请勿重复发起。"
    : busy ? "已完成的结果会逐步出现；停止查询会保留已保存结果。" : "条件保存在本机。修改后点击搜索才会生效。";
  document.querySelectorAll("#tailResults [data-tail-action='recheck'], #tailJobSummary [data-tail-action]").forEach(button => {
    button.disabled = busy && button.dataset.tailAction !== "continue";
  });
}


function renderTailJobPicker() {
  const select = document.getElementById("tailJobPicker");
  const jobs = tailSelectedJob && !tailDiscoveryJobs.some(job => job.job_id === tailSelectedJob.job_id)
    ? [tailSelectedJob, ...tailDiscoveryJobs] : tailDiscoveryJobs;
  const html = jobs.length ? jobs.map(job => {
    const p = job.payload || {};
    const stamp = job.created_at ? formatQueryTime(job.created_at) : job.job_id;
    const title = `${tailPlaceLabel(p.origin || "任务")} → ${tailPlaceLabel(p.transfer || "?")} · ${p.departure_date || ""} · ${tailJobStatusLabel(job.status)} · ${stamp}`;
    return `<option value="${escapeHtml(job.job_id)}">${escapeHtml(title)}</option>`;
  }).join("") : '<option value="">尚无任务</option>';
  if (select.innerHTML !== html) select.innerHTML = html;
  select.value = tailSelectedJobId;
}


async function selectTailJob(jobId) {
  tailSelectedJobId = jobId;
  tailResultRequest++; // Invalidate stale result requests immediately.
  tailResultsLoaded = false;
  tailDiscoveryResults = [];
  tailResultOffset = 0;
  tailHasMore = false;
  tailSelectedJob = null;
  updateTailPagination();
  renderTailResults([]);
  if (jobId) {
    const job = await tailGetJson(`/api/tail-discovery/jobs/${encodeURIComponent(jobId)}`);
    if (tailSelectedJobId !== jobId) return;
    tailSelectedJob = job;
    if (!activeTailJobId) renderTailJobProgress(job);
    if (tailResultView === "history") {
      setTailResultView("current");
      return;
    }
  } else tailSelectedJob = null;
  await fetchTailResults();
}


function setTailResultView(view) {
  tailResultView = view === "history" ? "history" : "current";
  tailResultsLoaded = false;
  tailResultOffset = 0;
  tailDiscoveryResults = [];
  document.querySelectorAll("[data-tail-view]").forEach(button => {
    button.setAttribute("aria-pressed", String(button.dataset.tailView === tailResultView));
  });
  document.getElementById("tailJobPickerLabel").hidden = tailResultView === "history";
  document.getElementById("tailResultScopeNote").textContent = tailResultView === "history"
    ? "所有任务的已保存报价；筛选仅作用于已加载条目。更新列表不会重新查询平台。"
    : "仅所选任务的已保存报价（含继续任务的此前结果）；更新列表不重新查价。";
  renderTailResults([]);
  fetchTailResults().catch(() => {});
}


function updateTailPagination() {
  document.getElementById("tailLoadMoreBtn").hidden = !tailHasMore;
  document.getElementById("tailPageNote").textContent = tailHasMore
    ? `已加载 ${tailDiscoveryResults.length} 条，尚有更多。筛选和排序仅作用于已加载报价。`
    : tailDiscoveryResults.length ? `已加载 ${tailDiscoveryResults.length} 条保存报价；不代表平台全量库存。` : "";
}


async function initializeTailWorkspace() {
  // Fail closed during boot: a page reload must not allow a second job before
  // the server has confirmed whether one is still running.
  tailConnectionUnknown = true;
  updateTailBusyControls();
  try {
    if (!tailCandidatesLoaded) await fetchTailCandidates();
    await fetchTailJobs();
    await resumeLatestTailJob();
  }
  catch {
    tailConnectionUnknown = true;
    updateTailBusyControls();
    document.getElementById("tailConnectionStatus").hidden = false;
    document.getElementById("tailConnectionStatus").textContent = "暂时无法确认后台任务，正在重连；不会重复发起查询。";
    setTimeout(() => initializeTailWorkspace().catch(error => showTailError(error.message)), 10000);
  }
}


function tailPlaceLabel(code) {
  if (code === "__OPEN__") return "开放目的地";
  const entries = typeof AIRPORT_SUGGESTIONS === "undefined" ? [] : AIRPORT_SUGGESTIONS;
  const entry = entries.find(item => item.code === String(code || "").toUpperCase());
  return entry ? `${entry.city} (${entry.code})` : airportCodeDisplayText(code);
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
      <div><p class="eyebrow">Tail discovery</p><h2>找一程合适的航班</h2>
      <p class="muted">确定想抵达的城市，比较不同尾段组合。搜索报价不代表可出票。</p></div>
    </div>
    <p id="tailActionError" role="alert" hidden></p>
    <form id="tailDiscoveryForm" class="tail-form">
      <details id="tailQueryEditor" class="tail-query-editor" open>
        <summary><span>查询条件</span><span id="tailQuerySummary" class="muted">先选出发地、想抵达的城市和日期</span></summary>
        <fieldset id="tailQueryFields">
          <div class="tail-basic-grid">
            <label>出发地<input id="tailOriginInput" data-airport-input placeholder="北京 (BJS)" autocomplete="off" required /></label>
            <label>想抵达的城市 <small>行程中转点</small><input id="tailTransferInput" data-airport-input placeholder="上海 (SHA)" autocomplete="off" required /></label>
            <label>出发日期<input id="tailDateInput" type="date" required /></label>
            <label>查询平台<select id="tailProviderSelect"><option value="ctrip">携程</option><option value="feizhu">飞猪（实验）</option></select></label>
          </div>
          <div class="tail-options-grid">
            <details class="tail-settings" id="tailFilters">
              <summary>筛选条件 <span id="tailFilterSummary" class="muted"></span></summary>
              <div class="tail-settings-grid">
                <label>查询价格上限<input id="tailQueryMaxPriceInput" type="number" min="0" step="50" placeholder="不限" /></label>
                <label>人数<input id="tailPassengersInput" type="number" min="1" max="9" value="1" required /></label>
                <label>最早起飞<input id="tailDepartureTimeStartInput" type="time" /></label>
                <label>最晚起飞<input id="tailDepartureTimeEndInput" type="time" /></label>
                <label>舱位<select id="tailCabinSelect">
          <option value="economy_plus">经济舱/超级经济舱</option>
          <option value="business_first">商务舱/头等舱</option>
          <option value="economy">经济舱</option>
          <option value="premium_economy">超级经济舱</option>
          <option value="business">商务舱</option>
          <option value="first">头等舱</option>
        </select></label>
                <label>航司<select id="tailAirlineSelect">
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
        </select></label>
                <label class="tail-wide">自定义航司（覆盖上方选择）<input id="tailAirlineInput" placeholder="不限；或输入 CA、MU 等代码，逗号分隔" autocomplete="off" /></label>
                <label class="tail-wide">舱位 / 航司适用范围<select id="tailFilterScopeInput"><option value="first_leg">仅首段：出发地 → 想抵达的城市</option><option value="all_legs">全行程每段都符合</option></select></label>
              </div>
              <p class="muted">首段模式仍校验完整行程，但不限制尾段舱位和航司。携程跨组混舱覆盖不保证；人数库存须另行复核。</p>
            </details>
            <details class="tail-settings" id="tailSearchSettings">
              <summary>搜索设置 <span id="tailSearchSummary" class="muted"></span></summary>
              <input id="tailModeInput" type="hidden" value="explore" />
              <div class="tail-settings-grid">
                <label class="tail-wide" id="tailScopeField">检索方式<select id="tailSearchScopeInput"><option value="candidates">按已选尾段城市查询</option><option value="open">开放目的地发现（非全量）</option><option value="hybrid">开放发现 + 历史引导补查</option></select></label>
                <p id="tailOpenScopeNotice" class="muted tail-wide" hidden>一次发现多个尾段城市，不代表全量。不使用城市数及首段数量预算；继续已完成任务不会翻页，再次搜索才会重新查询。</p>
                <p id="tailHybridScopeNotice" class="muted tail-wide" hidden>先开放取样，再从所选城市与历史线索定向补查。补查城市上限按原任务累计，调用上限按本轮计算；历史路线不代表当前可售。</p>
                <label>查询城市上限<input id="tailBudgetCitiesInput" type="number" min="0" max="500" placeholder="不限" /><small>留空或 0 表示不限</small></label>
                <label>运行时间上限（分钟）<input id="tailBudgetMinutesInput" type="number" min="0" max="1440" placeholder="不限" /><small>留空或 0 表示不限</small></label>
                <label>已校验首段达到几个后停止<input id="tailTargetInput" type="number" min="0" max="100" placeholder="不限" /><small>留空或 0 表示不限，不等于确认省钱</small></label>
                <label>定向补查城市上限<input id="tailFollowupBudgetInput" type="number" min="0" max="50" value="5" /><small>0 表示关闭</small></label>
                <label>本轮调用上限<input id="tailCallBudgetInput" type="number" min="0" max="500" value="20" /><small>含基准查询，0 表示不限；不等于计费次数</small></label>
                <label>同首段航班基准补查次数<input id="tailBaselineBudgetInput" type="number" min="0" max="20" value="3" /><small>每轮计数，不含初始基准；0 表示关闭</small></label>
              </div>
              <div class="tail-candidates">
                <div class="tail-candidate-head">
                  <label>尾段城市来源<select id="tailCandidateProfileSelect"><option value="">按航司推荐</option><option value="ALL_DOMESTIC">国内航点</option><option value="DEFAULT">基础目的地</option></select></label>
                  <div id="tailCandidateSummary" class="tail-candidate-summary" role="status">正在加载候选城市…</div>
                </div>
                <details class="tail-candidate-box">
                  <summary>调整尾段城市</summary>
                  <label>搜索城市<input id="tailCandidateSearch" type="search" placeholder="城市名 / 三字码" /></label>
                  <div class="tail-inline-actions">
                    <button type="button" class="ghost compact" data-candidates="all">选择搜索到的城市</button>
                    <button type="button" class="ghost compact" data-candidates="none">清空选择</button>
                    <button type="button" class="ghost compact" data-candidates="recommended">恢复推荐</button>
                  </div>
                  <div id="tailCandidateList" class="tail-candidate-list"></div>
                </details>
              </div>
            </details>
          </div>
        </fieldset>
      </details>
      <div class="tail-run-actions">
        <button id="tailRunBtn" class="primary" type="submit">开始搜索</button>
        <button id="tailCancelBtn" class="ghost danger" type="button" hidden disabled>停止查询</button>
        <button id="tailViewResultsBtn" class="ghost" type="button" hidden>查看结果 ↓</button>
        <span id="tailRunHint" class="muted">条件保存在本机。修改后点击搜索才会生效。</span>
      </div>
    </form>
    <p id="tailConnectionStatus" role="status" hidden></p>
    <div id="tailJobSummary" class="tail-job-summary" hidden></div>
    <section class="tail-results-workspace" aria-label="甩尾查询结果">
      <div class="tail-results-heading">
        <h3 id="tailResultsHeading" tabindex="-1">航班方案</h3>
        <div class="tail-view-tabs" role="group" aria-label="结果来源">
          <button type="button" data-tail-view="current" aria-pressed="true">本次查询</button>
          <button type="button" data-tail-view="history" aria-pressed="false">历史报价</button>
        </div>
        <button id="tailRefreshBtn" class="ghost compact" type="button" title="只重新读取已保存的结果，不查询平台">刷新已存结果</button>
      </div>
      <div class="tail-result-context">
        <p id="tailResultIdentity"></p>
        <p id="tailDraftNotice" class="tail-draft-notice" role="status" hidden></p>
        <details id="tailTaskContextDetails">
          <summary>切换任务 / 查询条件</summary>
          <label id="tailJobPickerLabel">查看任务（最近 20 个）<select id="tailJobPicker"><option value="">尚无任务</option></select></label>
          <p id="tailTaskConditions" class="muted"></p>
          <p id="tailResultScopeNote" class="muted">本次结果仅属于所选任务；继续任务会包含此前已完成的结果。</p>
        </details>
      </div>
      <div class="tail-result-tools">
        <label class="tail-control">排序<select id="tailSortSelect"><option value="price">参考价从低到高</option><option value="savings">可比价差优先</option><option value="time">更新时间从新到旧</option><option value="confidence">行程证据优先</option></select></label>
        <details id="tailResultFilters">
          <summary id="tailResultFilterSummary">筛选</summary>
          <div class="tail-toolbar">
            <label class="tail-control">报价状态<select id="tailTrustInput"><option value="all">全部状态</option><option value="verified">已校验且未过期</option><option value="review">信息待核实（未过期）</option><option value="stale">已过期 / 时间未知</option></select></label>
            <label class="tail-control">出发日期<select id="tailDateFilter"><option value="">全部日期</option></select></label>
            <label class="tail-control">列表价格上限<input id="tailMaxPriceInput" type="number" min="0" step="50" placeholder="只筛选已加载结果" /></label>
          </div>
          <div class="tail-results-meta">
            <label class="tail-toggle"><input id="tailLatestOnlyInput" type="checkbox" checked />同一行程只看最新报价</label>
            <button type="button" class="ghost compact" id="tailClearFiltersBtn">清除列表筛选</button>
          </div>
          <p class="muted">筛选和排序仅作用于已加载报价，不会重新查询平台。</p>
        </details>
      </div>
      <div class="tail-results-meta">
        <span id="tailSummary" role="status">尚未搜索</span>
      </div>
      <p id="tailListError" class="tail-warning" role="alert" hidden></p>
      <p class="tail-safety-note">仅已加载报价，非全量库存。行程校验不代表价格有效或可出票。</p>
      <div id="tailResults" class="tail-results"></div>
      <div class="tail-pagination"><span id="tailPageNote" class="muted"></span><button id="tailLoadMoreBtn" type="button" class="ghost" hidden>加载更多已保存报价</button></div>
    </section>
  `;
  routeGrid.insertAdjacentElement("afterend", panel);
  enhanceAirportInputs(panel);

  restoreTailForm();
  updateTailSearchScope();
  updateTailFormSummary();
  const form = document.getElementById("tailDiscoveryForm");
  form.addEventListener("submit", event => {
    event.preventDefault();
    runTailDiscovery().catch(error => showTailError(error.message));
  });
  form.addEventListener("input", event => {
    tailFormTouched = true;
    if (event.target.id === "tailCandidateSearch") filterTailCandidateList();
    updateTailFormSummary();
    saveTailForm();
  });
  form.addEventListener("change", event => {
    tailFormTouched = true;
    const id = event.target.id;
    if (id === "tailAirlineSelect") document.getElementById("tailAirlineInput").value = event.target.value;
    if (event.target.matches('#tailCandidateList input')) tailCandidatesManual = true;
    if (id === "tailCandidateProfileSelect") {
      tailCandidateProfileOverride = event.target.value;
      tailCandidatesManual = false; // Explicit source change, not silent route-input reset.
    }
    if (["tailOriginInput", "tailTransferInput", "tailAirlineInput", "tailAirlineSelect", "tailCandidateProfileSelect"].includes(id)) applyTailAirlineDefaults();
    updateTailSearchScope();
    updateTailCandidateSummary();
    updateTailFormSummary();
    saveTailForm();
  });
  form.addEventListener("invalid", event => {
    for (let parent = event.target.parentElement; parent && parent !== form; parent = parent.parentElement) {
      if (parent.tagName === "DETAILS") parent.open = true;
    }
  }, true);
  panel.addEventListener("toggle", event => {
    const key = event.target.dataset?.disclosure;
    if (key) tailDisclosureState.set(key, event.target.open);
  }, true);
  panel.addEventListener("click", async event => {
    const choice = event.target.closest("[data-candidates]");
    if (choice) {
      tailFormTouched = true;
      const action = choice.dataset.candidates;
      tailCandidatesManual = action !== "recommended";
      if (action === "recommended") applyTailAirlineDefaults();
      else document.querySelectorAll('#tailCandidateList input').forEach(input => {
        if (action === "none") input.checked = false;
        else if (!input.disabled && !input.closest("label").hidden) input.checked = true;
      });
      updateTailCandidateSummary(); saveTailForm(); return;
    }
    const view = event.target.closest("[data-tail-view]");
    if (view) { setTailResultView(view.dataset.tailView); return; }
    const button = event.target.closest("button[data-tail-action]");
    if (!button) return;
    const action = button.dataset.tailAction;
    if (action === "clear-filters") { clearTailResultFilters(); return; }
    if (action === "edit") {
      document.getElementById("tailQueryEditor").open = true;
      document.getElementById("tailOriginInput").focus(); return;
    }
    if (tailSubmitting || tailConnectionUnknown || (tailBusy() && action !== "continue")) return;
    button.disabled = true;
    showTailError("");
    try {
      if (action === "recheck") {
        const data = await tailPostJob(`/api/tail-discovery/results/${encodeURIComponent(button.dataset.resultId)}/recheck`);
        startTailJobPolling(data);
      } else await handleTailJobAction(action, button.dataset.jobId);
    } catch (error) { showTailError(error.message); }
    finally { button.disabled = Boolean(activeTailJobId); }
  });
  document.getElementById("tailCancelBtn").addEventListener("click", () => cancelActiveTailJob().catch(error => showTailError(error.message)));
  document.querySelector("#tailQueryEditor > summary").addEventListener("click", () => { tailFormTouched = true; });
  document.getElementById("tailRefreshBtn").addEventListener("click", () => fetchTailResults().catch(() => {}));
  document.getElementById("tailViewResultsBtn").addEventListener("click", () => {
    const heading = document.getElementById("tailResultsHeading");
    heading.focus({preventScroll:true});
    heading.scrollIntoView({block:"start", behavior:"instant"});
  });
  document.getElementById("tailLoadMoreBtn").addEventListener("click", () => fetchTailResults({append:true}).catch(() => {}));
  document.getElementById("tailClearFiltersBtn").addEventListener("click", clearTailResultFilters);
  document.getElementById("tailJobPicker").addEventListener("change", event => selectTailJob(event.target.value).catch(error => showTailError(error.message)));
  for (const id of ["tailTrustInput", "tailSortSelect", "tailDateFilter", "tailLatestOnlyInput", "tailMaxPriceInput"]) {
    document.getElementById(id).addEventListener(id === "tailMaxPriceInput" ? "input" : "change", () => renderTailResults(tailDiscoveryResults));
  }
}


async function fetchTailCandidates() {
  const container = document.getElementById("tailCandidateList");
  if (!container) return;
  const response = await fetch("/api/tail-discovery/candidates");
  if (!response.ok) throw new Error("候选城市加载失败，请刷新页面重试");
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

  if (tailSavedCandidates) {
    const saved = new Set(tailSavedCandidates);
    container.querySelectorAll("input").forEach(input => { input.checked = saved.has(input.value); });
    tailSavedCandidates = null;
  }
  applyTailAirlineDefaults();
  tailCandidatesLoaded = true;
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
  if (tailCandidatesManual || !selectedCodes.length) {
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
    summary.innerHTML = `<strong>已选 ${selected.length} 个尾段城市</strong>
      <small>${tailCandidatesManual ? "自定义选择，不随航司自动重置" : "自动推荐"} · 已排除出发地和抵达城市</small>`;
    updateTailFormSummary();
  }
}

function updateTailSearchScope() {
  const scope = document.getElementById("tailSearchScopeInput");
  if (!scope) return;
  const allowed = document.getElementById("tailProviderSelect")?.value === "feizhu"
    && document.getElementById("tailModeInput")?.value === "explore";
  scope.disabled = !allowed;
  if (!allowed) scope.value = "candidates";
  const open = allowed && scope.value === "open";
  const hybrid = allowed && scope.value === "hybrid";
  const notice = document.getElementById("tailOpenScopeNotice");
  if (notice) notice.hidden = !open;
  const hybridNotice = document.getElementById("tailHybridScopeNotice");
  if (hybridNotice) hybridNotice.hidden = !hybrid;

  const feizhu = document.getElementById("tailProviderSelect")?.value === "feizhu";
  const setField = (id, enabled) => {
    const input = document.getElementById(id);
    if (!input) return;
    input.disabled = !enabled;
    const label = input.closest?.("label");
    if (label) label.hidden = !enabled;
  };
  setField("tailSearchScopeInput", allowed);
  setField("tailFollowupBudgetInput", hybrid);
  setField("tailCallBudgetInput", feizhu);
  setField("tailBaselineBudgetInput", feizhu);
  setField("tailBudgetCitiesInput", !open && !hybrid);
  setField("tailTargetInput", !open && !hybrid);
  const candidates = document.querySelector("#tailDiscoveryForm .tail-candidates");
  if (candidates) candidates.hidden = open;

}

function buildTailQueryPayload() {
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
    ignore_cache: false, // Retained API field; unsupported absence caches are disabled.
    passengers: Number(document.getElementById("tailPassengersInput")?.value || 1),
    mode: document.getElementById("tailModeInput")?.value || "explore",
    max_candidates: Number(document.getElementById("tailBudgetCitiesInput")?.value || 0),
    max_minutes: Number(document.getElementById("tailBudgetMinutesInput")?.value || 0),
    target_results: Number(document.getElementById("tailTargetInput")?.value || 0),
    candidates: selectedTailCandidates(),
    search_scope: document.getElementById("tailSearchScopeInput")?.value || "candidates",
    filter_scope: document.getElementById("tailFilterScopeInput")?.value || "first_leg",
    max_baseline_queries: Number(document.getElementById("tailBaselineBudgetInput")?.value ?? 3),
    max_followup_candidates: Number(document.getElementById("tailFollowupBudgetInput")?.value ?? 5),
    max_provider_calls: document.getElementById("tailProviderSelect")?.value === "feizhu"
      ? Number(document.getElementById("tailCallBudgetInput")?.value ?? 20) : 0,
  };
  if (payload.search_scope === "open") {
    payload.candidates = [];
  }
  if (["open", "hybrid"].includes(payload.search_scope)) {
    payload.max_candidates = 0;
    payload.target_results = 0;
  }
  return payload;
}

async function runTailDiscovery() {
  const button = document.getElementById("tailRunBtn");
  const cancelButton = document.getElementById("tailCancelBtn");
  if (tailBusy()) return;
  showTailError("");
  button.disabled = true;
  tailSubmitting = true;
  document.getElementById("tailQueryFields").disabled = true;
  if (cancelButton) {
    cancelButton.hidden = true;
    cancelButton.disabled = true;
  }
  const payload = buildTailQueryPayload();
  try {
    const data = await tailPostJob("/api/tail-discovery/run", {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    saveTailForm();
    startTailJobPolling(data);
  } catch (error) {
    showTailError(error.message);
  } finally {
    tailSubmitting = false;
    updateTailBusyControls();
    if (!activeTailJobId) {
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

function pollTailDiscoveryJob(jobId, immediate = false) {
  if (!jobId || activeTailJobId !== jobId) return;
  if (activeTailJobTimer) clearTimeout(activeTailJobTimer);
  activeTailJobTimer = setTimeout(async () => {
    let job;
    try {
      job = await tailGetJson(`/api/tail-discovery/jobs/${encodeURIComponent(jobId)}`);
      if (activeTailJobId !== jobId) return;
    } catch (error) {
      if (activeTailJobId !== jobId) return;
      tailPollFailures++;
      tailConnectionUnknown = true;
      const status = document.getElementById("tailConnectionStatus");
      status.hidden = false;
      status.textContent = "连接中断，后台状态待确认。已保留结果，正在自动重连；请勿重复搜索。";
      if (error.status === 404) {
        // A missing job is authoritative, unlike an offline/timeout response.
        activeTailJobId = ""; tailConnectionUnknown = false;
        status.textContent = "后台已找不到此任务。现有结果已保留，请更新列表或重新搜索。";
        updateTailCancelButton(null);
      }
      updateTailBusyControls();
      if (activeTailJobId) pollTailDiscoveryJob(jobId);
      return;
    }
    tailPollFailures = 0; tailConnectionUnknown = false;
    document.getElementById("tailConnectionStatus").hidden = true;
    if (tailSelectedJobId === jobId) tailSelectedJob = job;
    renderTailJobProgress(job);
    updateTailCancelButton(job);
    const done = ![...ACTIVE_TAIL_JOB_STATUSES, "cancelling"].includes(job.status);
    if (done) activeTailJobId = "";
    updateTailBusyControls();
    if (job.status === "failed") showTailError(job.error || "查询受阻，已完成结果已保留。");
    // Quote failures do not alter a known running job's status.
    if (tailResultView === "current" && tailSelectedJobId === jobId) {
      await fetchTailResults({background:true}).catch(() => {});
    }
    await fetchTailJobs().catch(() => {});
    if (!done) pollTailDiscoveryJob(jobId);
  }, immediate ? 0 : Math.min(30000, 3500 * 2 ** Math.min(tailPollFailures, 3)));
}

function renderTailJobProgress(job) {
  const container = document.getElementById("tailJobSummary");
  if (!container) return;
  if (!job) { container.hidden = true; return; }
  const progress = job.progress || job.result?.progress || {};
  const queue = progress.queue?.items || job.result?.queue?.items || [];
  const attempts = progress.attempts || job.result?.report?.attempts || [];
  const byCity = new Map(attempts.map(item => [item.destination, item]));
  const items = queue.length ? queue.map(item => ({...item, ...(byCity.get(item.destination) || {})})) : attempts;
  const attempted = progress.attempted ?? attempts.length;
  const total = progress.total ?? progress.queue?.total ?? items.length;
  const saved = progress.saved ?? job.result?.saved ?? 0;
  const policy = job.result?.candidate_policy || progress.candidate_policy || {};
  const scope = job.payload?.search_scope || policy.search_scope;
  const active = [...ACTIVE_TAIL_JOB_STATUSES, "cancelling"].includes(job.status);
  const running = items.find(item => ["running", "started"].includes(item.status));
  const pending = items.filter(item => ["pending", "cancelled_before_query", "running"].includes(item.status)).length;
  const failed = items.filter(item => ["failed", "retryable_failed"].includes(item.status)).length || progress.failed || 0;
  const id = escapeHtml(job.job_id);
  const action = (value, label, primary = false) => `<button type="button" class="${primary ? "primary" : "ghost"} compact"
    data-tail-action="${value}" data-job-id="${id}" data-focus-key="${id}-${value}" ${tailBusy() && value !== "continue" ? "disabled" : ""}>${label}</button>`;
  let actions = "", primaryAction = "";
  if (job.status === "needs_verification") primaryAction = action("continue", "已在浏览器完成验证，继续", true);
  else if (!active) {
    if (pending || job.status === "paused" || job.status === "interrupted" || job.status === "cancelled") primaryAction = action("resume", "继续未完成项", true);
    if (failed) {
      if (!primaryAction) primaryAction = action("retry-failed", "重试失败城市", true);
      else actions += action("retry-failed", "重试失败城市");
    }
    if (saved) actions += action("refresh", "重新查价");
    actions += action("explore", "重新查询此任务");
  }
  const p = job.payload || {};
  const condition = tailConditionLabel(p);
  const counter = scope === "open" ? `已保存 ${saved} 条报价 · 开放取样，非全量`
    : scope === "hybrid" ? `已完成 ${attempted} 个查询单元 · 保存 ${saved} 条报价 · 非全量`
      : `已查 ${attempted}${total ? ` / ${total}` : ""} 个城市 · 保存 ${saved} 条报价`;
  container.hidden = false;
  replaceTailMarkup(container, `
    <div class="tail-job-head">
      <strong>${escapeHtml(active ? "正在执行的任务" : "所选任务")} · ${escapeHtml(tailJobStatusLabel(job.status))}</strong>
      <span class="tail-job-counter" role="status">${escapeHtml(counter)}</span>
      ${primaryAction ? `<div class="tail-job-actions">${primaryAction}</div>` : ""}
    </div>
    ${active && (tailResultView === "history" || tailSelectedJobId !== job.job_id) ? `<p class="muted">执行中：${escapeHtml(condition)}；下方结果另有所属。</p>` : ""}
    ${running ? `<p>正在查询：${escapeHtml(tailPlaceLabel(running.destination))}</p>` : ""}
    ${job.status !== "finished" ? `<p class="tail-job-message">${escapeHtml(job.error || progress.message || "等待后台更新进度")}</p>` : ""}
    ${job.status === "needs_verification" ? '<p class="tail-warning">请在已打开的查询浏览器中人工完成验证，再点击继续。本页不会自动绕过验证。</p>' : ""}
    <details data-disclosure="job-${id}"><summary data-focus-key="job-details-${id}">任务详情与操作${failed ? ` · ${failed} 项待重试` : ""}</summary>
      <p class="muted">${escapeHtml(condition)}</p>
      ${job.status === "finished" ? `<p class="tail-job-message">${escapeHtml(progress.message || "任务已完成；已保存报价仍需复核。")}</p>` : ""}
      ${actions ? `<div class="tail-job-actions">${actions}</div><p class="muted">以上操作使用此任务的原条件，不使用正在编辑的新条件。</p>` : ""}
      <details data-disclosure="cities-${id}"><summary>城市进度与检索依据</summary>
        ${renderTailPriorities(policy)}
        <div class="tail-job-candidates">${items.map(renderTailJobCandidate).join("") || '<p class="muted">暂无城市明细。</p>'}</div>
      </details>
    </details>`);
  updateTailResultContext();
}

function renderTailPriorities(policy) {
  if (policy.ranking !== "contextual_history_v1") return "";
  const pending = policy.next_candidates || [];
  const rows = (pending.length ? pending : (policy.priority_trace || []).slice(-10)).slice(0, 10);
  const calls = policy.provider_calls || {};
  return `<details class="tail-priority-details" data-disclosure="priorities">
    <summary>历史优先级与调用预算（规则估计，非可售保证）</summary>
    <p class="muted">${policy.provider_calls_supported === false
      ? "携程浏览器调用数未计量，请使用城市数和运行时间预算。"
      : `本轮平台调用尝试 ${escapeHtml(String(calls.used || 0))} / ${calls.limit ? escapeHtml(String(calls.limit)) : "不限"}，含初始基准、开放/定向搜索及同航班补查；不代表平台扣费次数。`}每批更新排序，保留新城市探索机会。</p>
    <strong>${pending.length ? "待查城市的优先顺序" : "最近已查询城市的决策依据"}</strong>
    <ol>${rows.map(row => `<li><strong>${escapeHtml(airportCodeDisplayText(row.destination))}</strong>：${escapeHtml((row.reasons || []).join("；"))}</li>`).join("")}</ol>
    ${!rows.length ? '<p class="muted">暂无定向候选；开放查询未返回不代表无航线。</p>' : ""}
  </details>`;
}

function renderTailJobCandidate(item) {
  const destination = String(item.destination || "").toUpperCase();
  const status = String(item.status || "pending");
  const label = tailAttemptStatusLabel(status);
  const price = item.price || item.last_price;
  const detail = price ? `CNY ${price}` : tailAttemptPhaseLabel(item.phase || item.last_phase || "");
  return `
    <div class="tail-job-candidate">
      <span>${escapeHtml(destination === "__OPEN__" ? "开放目的地查询" : airportCodeDisplayText(destination))}</span>
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
    paused: "已停止，可继续",
    cancelling: "正在取消",
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
    recheck_inconclusive: "复核无法确认（可重试）",
    provider_call_budget: "达到平台调用预算",
    cache_skipped: "缓存跳过",
    running: "查询中",
    pending: "等待中",
    started: "已开始",
  }[status] || status || "-";
}

function tailAttemptPhaseLabel(phase) {
  return {
    no_transfer_match: "未找到指定中转",
    no_via_transfer: "有航班但不经过中转点",
    route_absent: "确认无航班",
    airline_filtered: "航司筛选不匹配",
    parse_incomplete: "结果加载或解析不完整",
    search_incomplete: "搜索覆盖不完整，可重试；不代表无航线",
    search_inconclusive: "本次未发现，覆盖未确认，可重试",
    date_mismatch: "结果日期与请求不一致",
    date_unconfirmed: "结果日期证据不足，可重试",
    negative_cache: "近期无结果缓存",
    price_filtered: "超出价格上限",
    departure_time_filtered: "不在起飞时间范围",
    query_failed: "查询失败",
    opening_results: "打开结果页",
    parsing_network: "解析航班",
    matched: "已找到路线",
    batch_started: "本批次已开始",
    running: "查询中",
  }[phase] || phase || "";
}

function updateTailCancelButton(job) {
  const button = document.getElementById("tailCancelBtn");
  if (!button) return;
  const active = Boolean(job?.job_id) && [...ACTIVE_TAIL_JOB_STATUSES, "cancelling"].includes(job.status);
  button.hidden = !active;
  button.disabled = !active || job.status === "cancelling";
  button.textContent = job?.status === "cancelling" ? "正在停止…" : "停止查询";
}

async function fetchTailResults({append = false, background = false} = {}) {
  if (!document.getElementById("tailResults")) return;
  const requestId = ++tailResultRequest;
  const view = tailResultView, jobId = tailSelectedJobId;
  if (view === "current" && !jobId) {
    tailResultsLoaded = true; tailDiscoveryResults = []; tailHasMore = false;
    renderTailResults([]); updateTailPagination(); return;
  }
  document.getElementById("tailLoadMoreBtn").disabled = true;
  try {
    let offset = append ? tailResultOffset : 0, rows = [], more = false;
    // Preserve the amount already loaded when refreshing during a running task.
    const target = !append && background ? Math.max(200, tailResultOffset) : offset + 200;
    do {
      const params = new URLSearchParams({limit:"200", offset:String(offset)});
      if (view === "current") params.set("job_id", jobId);
      const data = await tailGetJson(`/api/tail-discovery/results?${params}`);
      if (requestId !== tailResultRequest) return;
      const batch = data.results || [];
      rows.push(...batch);
      offset = data.next_offset ?? offset + batch.length;
      more = Boolean(data.has_more) && batch.length > 0;
    } while (!append && more && offset < target);
    if (requestId !== tailResultRequest || view !== tailResultView || jobId !== tailSelectedJobId) return;
    const combined = append ? [...tailDiscoveryResults, ...rows] : rows;
    tailDiscoveryResults = [...new Map(combined.map(row => [row.id, row])).values()];
    tailResultOffset = offset; tailHasMore = more; tailResultsLoaded = true;
    const listError = document.getElementById("tailListError");
    if (listError) listError.hidden = true;
    updateTailDateFilter(tailDiscoveryResults);
    renderTailResults(tailDiscoveryResults);
    updateTailPagination();
  } catch (error) {
    if (requestId === tailResultRequest) {
      const listError = document.getElementById("tailListError");
      if (listError) {
        listError.hidden = false;
        listError.textContent = "报价列表更新失败，已保留现有结果。可点“刷新已存结果”重试。";
      }
    }
    throw error;
  } finally {
    if (requestId === tailResultRequest) document.getElementById("tailLoadMoreBtn").disabled = false;
  }
}

async function fetchTailJobs() {
  const data = await tailGetJson("/api/tail-discovery/jobs?limit=20");
  tailDiscoveryJobs = data.jobs || [];
  if (!tailSelectedJobId && tailDiscoveryJobs.length) {
    tailSelectedJob = tailDiscoveryJobs[0];
    tailSelectedJobId = tailSelectedJob.job_id;
  }
  renderTailJobPicker();
  updateWorkspaceTabs();
}

function showTailError(message) {
  const el = document.getElementById("tailActionError");
  if (el) { el.textContent = message; el.hidden = !message; }
}

function startTailJobPolling(job, immediate = true) {
  tailSubmitting = false;
  activeTailJobId = job.job_id || "";
  tailSelectedJobId = activeTailJobId;
  tailSelectedJob = job;
  tailPollFailures = 0;
  tailConnectionUnknown = false;
  document.getElementById("tailConnectionStatus").hidden = true;
  document.getElementById("tailQueryEditor").open = false;
  renderTailJobPicker();
  renderTailJobProgress(job);
  updateTailCancelButton(job);
  updateTailBusyControls();
  clearTailResultFilters();
  setTailResultView("current");
  pollTailDiscoveryJob(activeTailJobId, immediate);
}

async function handleTailJobAction(action, jobId) {
  if (!jobId) return;
  const restart = ["resume", "refresh", "explore"].includes(action);
  const data = await tailPostJob(`/api/tail-discovery/jobs/${encodeURIComponent(jobId)}/${restart ? "restart" : action}`, {
    headers:{"Content-Type":"application/json"},
    ...(restart ? {body:JSON.stringify({mode:action})} : {}),
  });
  if (action !== "cancel") startTailJobPolling(data);
  else { updateTailCancelButton(data); renderTailJobProgress(data); }
  fetchTailJobs().catch(() => {});
}

async function resumeLatestTailJob() {
  const data = await tailGetJson("/api/tail-discovery/jobs/latest");
  const job = data.job;
  tailConnectionUnknown = false;
  document.getElementById("tailConnectionStatus").hidden = true;
  if (job && [...ACTIVE_TAIL_JOB_STATUSES, "cancelling"].includes(job.status)) startTailJobPolling(job);
  else {
    updateTailBusyControls();
    if (tailSelectedJob) renderTailJobProgress(tailSelectedJob);
    await fetchTailResults().catch(() => {});
    // Restore a result-first workspace once, without folding a form the user
    // already started editing while initialization was in flight.
    if (!tailFormTouched && tailDiscoveryResults.length) document.getElementById("tailQueryEditor").open = false;
  }
}

function updateTailDateFilter(results) {
  const select = document.getElementById("tailDateFilter");
  if (!select) return;
  const previous = select.value;
  const dates = Array.from(new Set((results || []).map((item) => item.departure_date).filter(Boolean))).sort();
  if (previous && !dates.includes(previous)) dates.push(previous);
  select.innerHTML = '<option value="">全部日期</option>' + dates
    .map((date) => `<option value="${escapeHtml(date)}">${escapeHtml(date)}</option>`)
    .join("");
  select.value = previous;
}

function compareTailResults(left, right) {
  if (left.currency !== right.currency) return String(left.currency || "CNY").localeCompare(String(right.currency || "CNY"));
  const sortMode = document.getElementById("tailSortSelect")?.value || "price";
  if (sortMode === "time") {
    return String(right.observed_at || "").localeCompare(String(left.observed_at || ""));
  }
  if (sortMode === "confidence") {
    return tailResultConfidenceScore(right) - tailResultConfidenceScore(left)
      || Number(left.price || 0) - Number(right.price || 0);
  }
  if (sortMode === "savings") {
    return Number(right.savings_amount ?? -Infinity) - Number(left.savings_amount ?? -Infinity)
      || Number(left.price || 0) - Number(right.price || 0);
  }
  return Number(left.price || 0) - Number(right.price || 0);
}

function tailQuoteFresh(item) {
  return Boolean(item.expires_at) && Date.parse(item.expires_at) > Date.now();
}

function tailQuoteVerified(item) {
  return tailQuoteFresh(item) && item.validation?.status === "valid";
}

function prepareTailResults(results) {
  let prepared = [...(results || [])];
  if (document.getElementById("tailLatestOnlyInput")?.checked) {
    const latest = new Map();
    for (const item of prepared) {
      const key = item.itinerary_key || [item.provider, item.origin, item.transfer, item.destination,
        item.departure_date, item.cabin, item.passengers || 1, JSON.stringify(item.flight_details || [])].join("|");
      const current = latest.get(key);
      if (!current || Date.parse(item.observed_at) > Date.parse(current.observed_at)
          || (item.observed_at === current.observed_at && Number(item.id) > Number(current.id))) latest.set(key, item);
    }
    prepared = [...latest.values()];
  }
  const date = document.getElementById("tailDateFilter")?.value || "";
  const max = Number(document.getElementById("tailMaxPriceInput")?.value || 0);
  const trust = document.getElementById("tailTrustInput")?.value || "all";
  return prepared.filter(item => (!date || item.departure_date === date)
    && (!max || Number(item.price) <= max)
    && (trust === "all" || (trust === "verified" ? tailQuoteVerified(item)
      : trust === "stale" ? !tailQuoteFresh(item) : tailQuoteFresh(item) && !tailQuoteVerified(item))))
    .sort(compareTailResults);
}

function renderTailResults(results) {
  const container = document.getElementById("tailResults");
  if (!container) return;
  updateTailResultTools();
  const visible = prepareTailResults(results);
  renderTailSummary(visible, results);
  if (!visible.length) {
    let title, message, action = "";
    if (results.length) {
      title = "已有报价，但被列表筛选隐藏了";
      message = "调整上方日期、价格或报价状态即可，不需要重新搜索。";
      action = '<button type="button" class="ghost compact" data-tail-action="clear-filters">清除列表筛选</button>';
    } else if (tailResultView === "current" && !tailSelectedJobId) {
      title = "先告诉我们你想抵达哪里";
      message = "填写出发地、抵达城市和日期，开始搜索；也可以切到历史报价。";
    } else if (!tailResultsLoaded) {
      title = "正在读取已保存结果"; message = "这一步不会重新查询平台。";
    } else if (tailResultView === "current" && activeTailJobId === tailSelectedJobId) {
      title = "正在查找，已完成的报价会逐步出现";
      message = tailConnectionUnknown ? "连接中断不代表任务已停止，正在重连。" : "你可以先查看任务进度，不必反复点击搜索。";
    } else {
      title = tailResultView === "history" ? "还没有已保存的历史报价" : "本次尚未确认符合条件的报价";
      message = tailResultView === "history" ? "开始一次搜索后，保存的结果会出现在这里。"
        : "不代表没有航班。查看上方任务原因，再决定重试或调整条件。旧版任务若无检查点，可在历史报价中查看。";
      action = '<button type="button" class="ghost compact" data-tail-action="edit">查看查询条件</button>';
    }
    replaceTailMarkup(container, `<div class="tail-empty-state"><strong>${title}</strong><span>${message}</span>${action}</div>`);
  } else {
    const groups = [
      ["已校验且未过期", visible.filter(tailQuoteVerified), "verified"],
      ["信息待核实 · 未过期", visible.filter(item => tailQuoteFresh(item) && !tailQuoteVerified(item)), "review"],
      ["已过期 / 时间未知", visible.filter(item => !tailQuoteFresh(item)), "stale"],
    ];
    replaceTailMarkup(container, groups.filter(([, rows]) => rows.length).map(([label, rows, state]) =>
      `<section class="tail-state-section"><h3>${label} <span>${rows.length}</span></h3>
       ${groupTailResultsByRoute(rows).map(group => renderTailRouteGroup({...group, state})).join("")}</section>`
    ).join(""));
  }
  if (typeof updateTailBusyControls === "function") updateTailBusyControls();
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
        items: items.sort(compareTailResults),
      }))
      .sort((left, right) => String(left.date).localeCompare(String(right.date)));
    const best = [...group.results].sort(compareTailResults)[0] || null;
    return { ...group, dateGroups, best };
  }).sort((left, right) => {
    const comparison = compareTailResults(left.best, right.best);
    if (comparison !== 0) return comparison;
    return `${left.origin || ""}${left.transfer || ""}`.localeCompare(`${right.origin || ""}${right.transfer || ""}`);
  });
}

function safeTailReviewUrl(value) {
  try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) ? url.href : ""; }
  catch { return ""; }
}

function tailQuoteTermsLabel(terms) {
  terms = terms || {};
  const basis = {per_person: "每人", party_total: "全体乘客总价"}[terms.price_basis] || "计价单位未确认";
  const tax = terms.tax_included === true ? "含税" : terms.tax_included === false ? "不含税" : "税费未确认";
  return `${basis} · ${tax} · 请求 ${terms.requested_passengers || 1} 人 · ${terms.inventory_verified ? "人数库存已校验" : "人数库存未校验"}`;
}

function tailComparableBaseline(item, key) {
  const baseline = item.baselines?.[key], terms = item.quote_terms || {}, other = baseline?.quote_terms || {};
  if (!tailQuoteVerified(item) || baseline?.status !== "comparable" || baseline.reasons?.length
      || baseline.savings_amount == null || !Number.isFinite(Number(baseline.savings_amount))
      || !Number.isFinite(Number(baseline.price)) || Number(baseline.price) <= 0
      || !Number.isFinite(Number(item.price)) || Number(item.price) <= 0) return null;
  // Public metadata from legacy rows may be incomplete. Never promote its
  // top-level savings_amount alone into a confirmed, current comparison.
  for (const field of ["currency", "price_basis", "tax_included", "priced_passengers"]) {
    if (terms[field] == null || terms[field] === "unknown" || terms[field] !== other[field]) return null;
  }
  if (!terms.currency || terms.currency !== item.currency || Number(terms.priced_passengers) <= 0) return null;
  if (Number(item.passengers || 1) > 1 && (!terms.inventory_verified || !other.inventory_verified
      || Number(terms.priced_passengers) !== Number(item.passengers))) return null;
  if (key === "same_flight" && (!terms.fare_conditions || terms.fare_conditions !== other.fare_conditions)) return null;
  if (Math.abs(Number(baseline.price) - Number(item.price) - Number(baseline.savings_amount)) > .011) return null;
  return baseline;
}

function tailDecisionDifference(item) {
  if (!tailQuoteFresh(item)) return "历史报价，请先复核价差";
  for (const [key, label] of [["same_flight", "同首段单买"], ["market", "符合条件的直飞最低价"]]) {
    const baseline = tailComparableBaseline(item, key);
    if (!baseline) continue;
    const amount = Number(baseline.savings_amount);
    return amount === 0 ? `与${label}参考价相同`
      : `比${label}${amount > 0 ? "低" : "高"} ${item.currency} ${Math.abs(amount).toFixed(2).replace(/\.00$/, "")} · 可比参考价差`;
  }
  return "暂无可比价差";
}

function renderTailBaselines(item) {
  return [["market", "符合条件的直飞最低价"], ["same_flight", "同首段航班单买价"]].map(([key, label]) => {
    const value = item.baselines?.[key];
    if (!value || value.price === null) return `<small>${label}：暂无可比基准</small>`;
    const detail = tailComparableBaseline(item, key) ? `可比价差 ${Number(value.savings_amount).toFixed(2)}` : "仅供参考，价差待复核";
    return `<small>${label}：${escapeHtml(item.currency || "CNY")} ${Number(value.price).toFixed(0)} · ${detail}</small>
      <small>${escapeHtml((value.reasons || []).join("；"))}</small>`;
  }).join("");
}

function renderTailFlightGroups(items, state = "") {
  const groups = new Map();
  for (const item of items) {
    const leg = (item.flight_details || [])[0] || {};
    const key = [state, item.departure_date, item.first_leg_key || item.itinerary_key || item.id,
      item.currency, item.passengers || 1, item.quote_terms?.price_basis || "unknown"].join("|");
    if (!groups.has(key)) groups.set(key, {leg, items:[], key});
    groups.get(key).items.push(item);
  }
  return [...groups.values()].map(group => {
    group.items.sort(compareTailResults);
    const leg = group.leg;
    const best = [...group.items].sort((a,b) => Number(a.price) - Number(b.price))[0];
    const key = escapeHtml(group.key);
    return `<section class="tail-flight-group" data-stable-key="${key}">
      <div class="tail-flight-heading">
        <div><p class="tail-first-leg-label">想乘坐的首段 · ${escapeHtml(tailPlaceLabel(group.items[0].origin))} → ${escapeHtml(tailPlaceLabel(group.items[0].transfer))}</p>
        <h4>${escapeHtml(leg.flight_no || "航班号待确认")} <span>${escapeHtml(leg.departure_time || "--:--")} → ${escapeHtml(leg.arrival_time || "--:--")}</span></h4>
        <p class="muted">${escapeHtml(airportCodeDisplayText(leg.departure_airport || leg.origin))} → ${escapeHtml(airportCodeDisplayText(leg.arrival_airport || leg.destination))}</p></div>
        <div class="tail-flight-best"><small>${group.items.length} 个尾段方案 · 最低参考</small><strong>${escapeHtml(best.currency || "CNY")} ${Number(best.price).toFixed(0)}</strong></div>
      </div>
      ${renderTailResultCard(group.items[0], 1)}
      ${group.items.length > 1 ? `<details class="tail-alternatives" data-disclosure="alternatives-${key}"><summary data-focus-key="alternatives-${key}">比较其他 ${group.items.length - 1} 个尾段方案</summary>
      ${group.items.slice(1).map((item, index) => renderTailResultCard(item,index+2)).join("")}</details>` : ""}
    </section>`;
  }).join("");
}

function renderTailResultCard(item, index) {
  const key = escapeHtml(String(item.id || item.itinerary_key || `${item.destination}-${index}`));
  const reviewUrl = safeTailReviewUrl(item.review_url || item.search_url || "");
  const valid = (item.validation?.status || item.raw_payload?.validation?.status) === "valid";
  const fresh = tailQuoteFresh(item);
  const scope = item.filter_scope === "first_leg" ? "舱位／航司仅约束首段"
    : item.filter_scope === "all_legs" ? "舱位／航司约束全行程" : "";
  const legs = (item.flight_details || []).map((leg, i) => `
    <div class="tail-leg-row"><span class="tail-leg-index">${i+1}</span><div>
      <strong>${escapeHtml(airportCodeDisplayText(leg.departure_airport || leg.origin))} → ${escapeHtml(airportCodeDisplayText(leg.arrival_airport || leg.destination))}</strong>
      <span>${escapeHtml(leg.departure_date || item.departure_date)} · ${escapeHtml(leg.departure_time || "--:--")} → ${escapeHtml(leg.arrival_time || "--:--")} · ${escapeHtml(leg.flight_no || "航班号待确认")}</span>
      <span>${escapeHtml(cabinLabel(leg.cabin || item.cabin))}${leg.transfer_layover ? ` · 中转停留 ${escapeHtml(leg.transfer_layover)}` : ""}</span></div></div>`).join("");
  const difference = tailDecisionDifference(item);
  const details = item.flight_details || [];
  const stops = [];
  for (const leg of details) {
    const from = leg.departure_airport || leg.origin || "待核实";
    const to = leg.arrival_airport || leg.destination || "待核实";
    if (stops[stops.length - 1] !== from) stops.push(from);
    stops.push(to);
  }
  const fullRoute = details.length > 1
    ? stops.map(tailPlaceLabel).join(" → ")
    : `${tailPlaceLabel(item.origin)} → ${tailPlaceLabel(item.transfer)} → ${tailPlaceLabel(item.destination)}`;
  const status = !fresh ? "已过期 / 时间未知" : valid ? "行程已校验" : "信息待核实";
  return `<article class="tail-result-card">
    <div class="tail-result-main"><small>${valid && details.length > 1 ? "完整购票行程" : "候选购票行程 · 待核实"}</small>
      <strong>${escapeHtml(fullRoute)}</strong>
      <span class="muted">${escapeHtml(providerLabel(item.provider))} · ${escapeHtml(cabinLabel(item.cabin))}</span>
      ${item.suitability?.airport_change ? '<span class="tail-warning">涉及换机场</span>' : ""}</div>
    <div class="tail-price"><strong>${escapeHtml(item.currency || "CNY")} ${Number(item.price || 0).toFixed(0)}</strong>
      <small>${item.provider === "feizhu" ? "平台参考价 · 总价未核实" : "组合参考报价"}</small>
      <small>${escapeHtml({per_person:"每人", party_total:"全体乘客总价"}[item.quote_terms?.price_basis] || "计价单位未确认")} · ${item.quote_terms?.tax_included === true ? "含税" : item.quote_terms?.tax_included === false ? "不含税" : "税费口径未确认"}</small>
      <span class="tail-decision-difference">${escapeHtml(difference)}</span>
    </div>
    <div class="tail-quote-state"><span class="tail-status ${valid && fresh ? "verified" : "review"}">${status}</span>
      <small>${item.observed_at ? `采集 ${escapeHtml(formatQueryTime(item.observed_at))}` : "采集时间未知"}</small>
      ${item.recheck_outcome?.status === "failed" ? "<small>最近复核失败，旧价待确认</small>" : ""}
      ${["inconclusive", "not_found"].includes(item.recheck_outcome?.status) ? "<small>最近复核无法确认原行程，可重试；不代表已下架</small>" : ""}
    </div>
    <div class="tail-review-actions">
      ${item.id ? `<button type="button" class="ghost compact" data-tail-action="recheck" data-result-id="${key}" data-focus-key="recheck-${key}">复核报价</button>` : ""}
      ${reviewUrl ? `<a class="ghost compact tail-review-link" href="${escapeHtml(reviewUrl)}" target="_blank" rel="noopener noreferrer">去${escapeHtml(providerLabel(item.provider))}核实</a>` : ""}
    </div>
    <p class="tail-card-caveat">价格及出票需复核 · 行李、票规及后续航段影响未核实</p>
    <details class="tail-details" data-disclosure="quote-${key}">
      <summary data-focus-key="quote-details-${key}">完整行程、价差与待核实事项</summary>
      <p>${escapeHtml(tailPlaceLabel(item.origin))} → ${escapeHtml(tailPlaceLabel(item.transfer))} → ${escapeHtml(tailPlaceLabel(item.destination))} · ${escapeHtml(scope)}</p>
      ${item.search_metadata?.system_message ? `<p>平台提示：${escapeHtml(item.search_metadata.system_message)}</p>` : ""}
      ${item.provider === "feizhu" ? `<p>平台 ${escapeHtml(item.quote_terms?.source_price_field || "ticketPrice")} · 整段总价未核实。CLI 返回结果不保证完整；未返回不代表无航线。</p>` : ""}
      <div class="tail-baselines">${renderTailBaselines(item)}
        <p>${escapeHtml(difference)}（非购买承诺）</p>
        ${!tailComparableBaseline(item, "market") && !tailComparableBaseline(item, "same_flight") ? "<p>口径未确认，不计算节省金额；历史报价须重新复核。</p>" : ""}</div>
      <p>${escapeHtml(tailQuoteTermsLabel(item.quote_terms))}</p>
      <p>行李：未核实；票规：未核实；后续行程影响：未核实。搜索结果不代表可出票。</p>
      <div class="tail-leg-list">${legs || "暂无可确认的航段明细"}</div>
    </details>
  </article>`;
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
  return `<section class="tail-route-group">
    <div class="tail-route-group-head"><strong>${escapeHtml(tailPlaceLabel(group.origin))} → ${escapeHtml(tailPlaceLabel(group.transfer))}</strong>
    <span>${group.results.length} 条组合报价</span></div>
    ${group.dateGroups.map(date => `<div class="tail-date-group"><h4>${escapeHtml(date.date)}</h4>
      ${renderTailFlightGroups(date.items, group.state || "")}</div>`).join("")}
  </section>`;
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
  document.body.dataset.workspace = nextTab;
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
initializeTailWorkspace().catch(error => showTailError(error.message));
fetchBrowserProfiles().catch(() => {});
fetchDashboard().then(() => scheduleDashboardPolling(3000));
// Expiry is a clock-based UI state, not a new platform query.
setInterval(() => {
  if (!document.hidden && activeWorkspaceTab === "tail" && tailResultsLoaded && !tailBusy()) {
    renderTailResults(tailDiscoveryResults);
  }
}, 60000);

