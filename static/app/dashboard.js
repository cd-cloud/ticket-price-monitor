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
    both: "携程 + 飞猪",
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
  if (dashboardState.activeTaskView === "auto") return Boolean(task.auto_query_enabled);
  if (dashboardState.activeTaskView === "multi_city") return task.route_type === "multi_city";
  if (dashboardState.activeTaskView === "unqueried") return !task.latest_price && !task.is_expired;
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

function hideTaskDetailDrawer(drawer) {
  const returnFocus = drawer.returnFocus;
  drawer.hidden = true;
  if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
}

function ensureTaskDetailDrawer() {
  if (document.getElementById("taskDetailDrawer")) return;
  const drawer = document.createElement("aside");
  drawer.id = "taskDetailDrawer";
  drawer.className = "task-detail-drawer";
  drawer.hidden = true;
  drawer.innerHTML = `
    <div class="drawer-backdrop" data-drawer-close></div>
    <section class="drawer-panel" role="dialog" aria-modal="true" aria-labelledby="drawerTitle">
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
    if (event.target.closest("[data-drawer-close]")) hideTaskDetailDrawer(drawer);
  });
  drawer.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      hideTaskDetailDrawer(drawer);
      return;
    }
    if (event.key !== "Tab") return;
    const controls = Array.from(drawer.querySelectorAll(
      "button:not([disabled]), a[href], summary, input:not([disabled]), select:not([disabled])",
    )).filter((element) => element.getClientRects().length > 0);
    if (!controls.length) {
      event.preventDefault();
      return;
    }
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && (document.activeElement === first || !drawer.contains(document.activeElement))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  document.body.appendChild(drawer);
}

function showTaskDetail(routeKey, provider) {
  ensureTaskDetailDrawer();
  const drawer = document.getElementById("taskDetailDrawer");
  const task = (dashboardState.data.tasks || []).find((item) => item.route_key === routeKey && item.provider === provider);
  if (!drawer || !task) return;

  const title = taskRouteDisplay(task);
  const price = task.latest_price ? `${task.currency} ${task.latest_price}` : "暂无价格";
  const queryTime = formatQueryTime(task.last_query_time);
  const transferPolicy = transferPolicyLabel(task.transfer_policy || "any");
  const airlines = task.preferred_airlines?.length ? task.preferred_airlines.join(", ") : "不限";
  const details = formatFlightDetails(task) || '<p class="muted">暂无可展示的航班分段详情。</p>';

  if (drawer.hidden) drawer.returnFocus = document.activeElement;
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
  drawer.querySelector("button[data-drawer-close]").focus({ preventScroll: true });
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
    dashboardState.activeTaskView = button.dataset.taskView;
    renderTasks(dashboardState.data.tasks || []);
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
    button.classList.toggle("active", view === dashboardState.activeTaskView);
    button.setAttribute("aria-pressed", String(view === dashboardState.activeTaskView));
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
    view: dashboardState.activeTaskView,
    provider: document.getElementById("providerFilter")?.value || "",
    cabin: document.getElementById("cabinFilter")?.value || "",
    search: document.getElementById("routeSearchInput")?.value || "",
    rows: displayRows,
  });
  if (signature === dashboardState.taskRenderSignature) return;
  dashboardState.taskRenderSignature = signature;
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
  if (signature === dashboardState.routeRenderSignature) return;
  dashboardState.routeRenderSignature = signature;
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
  dashboardState.data = data;
  const optionsSignature = JSON.stringify({
    providers: data.provider_options || [],
    cabins: data.cabin_options || [],
  });
  if (optionsSignature !== dashboardState.optionsSignature) {
    dashboardState.optionsSignature = optionsSignature;
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
  const response = await fetchApi("/api/dashboard");
  const data = await response.json();
  renderDashboard(data);
  document.getElementById("providerFilter").value = currentProvider;
  document.getElementById("cabinFilter").value = currentCabin;
  document.getElementById("routeSearchInput").value = currentSearch;
  renderTasks(dashboardState.data.tasks || []);
}

async function fetchLog() {
  const logBox = document.getElementById("logBox");
  if (!logBox) return;
  const response = await fetchApi("/api/log");
  const data = await response.json();
  document.getElementById("logBox").textContent = data.lines?.join("\n") || "暂无日志";
}

async function runAction(url, successMessage = "操作已完成") {
  const response = await fetchApi(url, { method: "POST" });
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response, "操作失败"));
  }
  await fetchDashboard();
  await fetchLog();
  notifyComplete(successMessage);
}

async function runAsyncQuery(url, successMessage, fallbackMessage = "查询启动失败") {
  const response = await fetchApi(url, { method: "POST" });
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response, fallbackMessage));
  }
  await fetchDashboard();
  await fetchLog();
  notifyComplete(successMessage);
  scheduleDashboardPolling(1200);
}

function scheduleDashboardPolling(delayMs = 4000) {
  window.clearTimeout(dashboardState.pollTimer);
  dashboardState.pollTimer = window.setTimeout(pollDashboardLoop, delayMs);
}

async function pollDashboardLoop() {
  if (dashboardState.pollInFlight) return;
  dashboardState.pollInFlight = true;
  try {
    await fetchDashboard();
    const running = Boolean(dashboardState.data.last_run?.running);
    if (running || tailState.job.activeId) {
      await fetchLog();
    }
    scheduleDashboardPolling(running ? 1500 : 8000);
  } catch (_error) {
    scheduleDashboardPolling(6000);
  } finally {
    dashboardState.pollInFlight = false;
  }
}
