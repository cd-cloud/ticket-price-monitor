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
  const hasDetails = (task.lowest_flight_details || []).length || task.lowest_flight_no || task.lowest_departure_time || task.latest_price;
  if (!hasDetails) return '<span class="muted">暂无详情</span>';
  return `
    <button class="ghost compact" type="button" data-task-action="show-detail" data-key="${escapeHtml(task.route_key)}" data-provider="${escapeHtml(task.provider)}">
      详情 / 走势
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

function renderPriceHistorySparkline(points, targetPrice = null) {
  const valid = (points || []).filter((item) => Number.isFinite(Number(item.price)));
  if (!valid.length) {
    return '<p class="muted">暂无历史价格快照。</p>';
  }
  const prices = valid.map((item) => Number(item.price));
  const minVal = Math.min(...prices, ...(Number.isFinite(Number(targetPrice)) ? [Number(targetPrice)] : []));
  const maxVal = Math.max(...prices, ...(Number.isFinite(Number(targetPrice)) ? [Number(targetPrice)] : []));
  const span = Math.max(maxVal - minVal, 1);
  const width = 460;
  const height = 136;
  const padX = 16;
  const padY = 16;
  const innerW = width - padX * 2;
  const innerH = height - padY * 2;
  const coords = valid.map((item, idx) => {
    const x = valid.length === 1 ? width / 2 : padX + (idx / (valid.length - 1)) * innerW;
    const y = padY + innerH - ((Number(item.price) - minVal) / span) * innerH;
    return { x: Number(x.toFixed(1)), y: Number(y.toFixed(1)), item };
  });
  const polyline = coords.map((pt) => `${pt.x},${pt.y}`).join(" ");
  const dots = coords.map((pt) => `
    <circle cx="${pt.x}" cy="${pt.y}" r="3.5" class="history-chart-dot">
      <title>${escapeHtml(formatQueryTime(pt.item.observed_at))} · ${escapeHtml(pt.item.currency || "CNY")} ${escapeHtml(pt.item.price)}</title>
    </circle>
  `).join("");
  let targetLine = "";
  if (targetPrice != null && Number.isFinite(Number(targetPrice))) {
    const ty = Number((padY + innerH - ((Number(targetPrice) - minVal) / span) * innerH).toFixed(1));
    targetLine = `<line x1="${padX}" y1="${ty}" x2="${width - padX}" y2="${ty}" class="history-target-line" />`;
  }
  const currency = valid[valid.length - 1].currency || "CNY";
  const recentRows = valid.slice(-10).reverse().map((item) => `
    <tr>
      <td>${escapeHtml(formatQueryTime(item.observed_at))}</td>
      <td><strong>${escapeHtml(item.currency || currency)} ${escapeHtml(item.price)}</strong></td>
      <td>${escapeHtml(item.flight_no || "-")} ${escapeHtml(item.departure_time || "")}</td>
    </tr>
  `).join("");
  return `
    <div class="history-chart-wrap">
      <svg class="history-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="价格历史趋势图">
        ${targetLine}
        ${coords.length > 1 ? `<polyline fill="none" class="history-chart-line" points="${polyline}" />` : ""}
        ${dots}
      </svg>
      <div class="history-chart-axis">
        <span>${escapeHtml(formatQueryDate(valid[0].observed_at))}</span>
        <span>区间 ${escapeHtml(currency)} ${Math.min(...prices).toFixed(0)} - ${Math.max(...prices).toFixed(0)}${targetPrice != null ? ` · 目标 ≤ ${escapeHtml(targetPrice)}` : ""}</span>
        <span>${escapeHtml(formatQueryDate(valid[valid.length - 1].observed_at))}</span>
      </div>
    </div>
    <table class="history-table">
      <thead><tr><th>查询时间</th><th>价格</th><th>航班</th></tr></thead>
      <tbody>${recentRows}</tbody>
    </table>
  `;
}

let taskDetailRequestId = 0;

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
  const targetMeta = task.target_price != null
    ? ` · 目标价 ≤ ${task.currency} ${task.target_price}${task.target_price_hit ? "（已达标）" : ""}`
    : "";
  const details = formatFlightDetails(task) || '<p class="muted">暂无可展示的航班分段详情。</p>';

  if (drawer.hidden) drawer.returnFocus = document.activeElement;
  document.getElementById("drawerTitle").textContent = cleanDisplayText(title);
  document.getElementById("drawerBody").innerHTML = `
    <div class="detail-kpis">
      <div><span>最新价格</span><strong>${escapeHtml(price)}</strong></div>
      <div><span>最近查询</span><strong>${escapeHtml(queryTime)}</strong></div>
      <div><span>平台</span><strong>${escapeHtml(providerLabel(task.provider))}</strong></div>
    </div>
    <div class="detail-meta">
      <p>${escapeHtml(cabinLabel(task.cabin))} · ${escapeHtml(transferPolicy)} · ${escapeHtml(airlines)}${escapeHtml(targetMeta)}</p>
      <p>${escapeHtml(task.itinerary_summary || task.departure_date || "-")}</p>
    </div>
    <section class="detail-history-section">
      <div class="detail-section-head">
        <h3>历史价格走势</h3>
        <span id="drawerHistorySummary" class="muted">正在加载历史记录…</span>
      </div>
      <div id="drawerHistoryBody" class="detail-history-body"></div>
    </section>
    ${details}
  `;
  drawer.hidden = false;
  drawer.querySelector("button[data-drawer-close]").focus({ preventScroll: true });

  const reqId = ++taskDetailRequestId;
  fetchApi(`/api/routes/${encodeURIComponent(routeKey)}/history?provider=${encodeURIComponent(provider)}&limit=60`, { timeoutMs: 10000 })
    .then(async (response) => {
      if (!response.ok) throw new Error(await responseErrorMessage(response, "历史记录加载失败"));
      return response.json();
    })
    .then((history) => {
      if (reqId !== taskDetailRequestId) return;
      const summaryEl = document.getElementById("drawerHistorySummary");
      const bodyEl = document.getElementById("drawerHistoryBody");
      if (!summaryEl || !bodyEl) return;
      const stats = history.stats || {};
      summaryEl.textContent = stats.count
        ? `共 ${stats.count} 条 · 最低 ${stats.currency || task.currency} ${stats.min_price} · 均价 ${stats.avg_price}`
        : "暂无历史记录";
      bodyEl.innerHTML = renderPriceHistorySparkline(history.points || [], history.target_price ?? task.target_price);
    })
    .catch((error) => {
      if (reqId !== taskDetailRequestId) return;
      const summaryEl = document.getElementById("drawerHistorySummary");
      if (summaryEl) summaryEl.textContent = error.message || "历史记录加载失败";
    });
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
      <input type="checkbox" name="providers" value="${escapeHtml(provider)}" checked />
      <span>${escapeHtml(provider)}</span>
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

function taskComparisonKey(task) {
  const required = ["currency", "passengers", "cabin", "departure_date", "route_type", "transfer_policy"];
  if (required.some((field) => task[field] === null || task[field] === undefined || task[field] === "")) return null;
  if (!Array.isArray(task.preferred_airlines)) return null;
  const terms = task.quote_terms;
  if (!terms || typeof terms !== "object") return null;
  const termFields = ["price_basis", "tax_included", "priced_passengers"];
  if (termFields.some((field) => terms[field] === null || terms[field] === undefined || terms[field] === "" || terms[field] === "unknown")) return null;
  const passengers = Number(task.passengers);
  const pricedPassengers = Number(terms.priced_passengers);
  if (!Number.isInteger(passengers) || !Number.isInteger(pricedPassengers)) return null;
  return [
    String(task.currency).toUpperCase(), passengers, String(task.cabin).toLowerCase(),
    String(task.departure_date), String(task.return_date || ""), String(task.route_type),
    String(task.transfer_policy), task.preferred_airlines.map((item) => String(item).toUpperCase()).sort().join(","),
    String(terms.price_basis), terms.tax_included, pricedPassengers,
  ].join("|");
}

function summarizeTaskGroup(group) {
  const tasks = group.tasks || [];
  const priced = tasks.filter((task) => {
    if (task.latest_price === null || task.latest_price === undefined || task.latest_price === "") return false;
    return Number.isFinite(Number(task.latest_price));
  });
  const comparisonKeys = priced.map(taskComparisonKey);
  const comparable = priced.length > 0
    && comparisonKeys.every((key) => key !== null)
    && new Set(comparisonKeys).size === 1;
  const best = comparable
    ? priced.reduce((winner, task) => Number(task.latest_price) < Number(winner.latest_price) ? task : winner)
    : null;
  const errors = tasks.filter((task) => task.last_error);
  const expired = tasks.length > 0 && tasks.every((task) => task.is_expired);
  const autoEnabled = tasks.filter((task) => task.auto_query_enabled).length;
  const dates = Array.from(new Set(tasks.map((task) => task.departure_date).filter(Boolean))).sort();
  const origins = Array.from(new Set(tasks.map((task) => task.origin).filter(Boolean))).sort();
  const destinations = Array.from(new Set(tasks.map((task) => task.destination).filter(Boolean))).sort();
  return {
    tasks, priced, best, comparable, comparisonKeys, errors, expired, autoEnabled,
    dates, origins, destinations,
  };
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
  if (summary.priced.length && !summary.comparable) return statusBadge("warning");
  if (summary.best) return statusBadge("ok");
  if (summary.expired) return statusBadge("expired");
  if (summary.autoEnabled) return statusBadge("scheduled");
  return statusBadge("idle");
}

function renderTargetPricePill(task) {
  if (task.target_price == null) return "";
  if (task.target_price_hit) {
    return `<span class="price-change-pill target-hit">已低于目标 ${escapeHtml(task.target_price)}</span>`;
  }
  return `<span class="muted target-hint">目标 ≤ ${escapeHtml(task.target_price)}</span>`;
}

function toggleTaskGroupExpanded(groupKey) {
  if (!groupKey) return;
  if (!(dashboardState.expandedTaskGroups instanceof Set)) {
    dashboardState.expandedTaskGroups = new Set();
  }
  if (dashboardState.expandedTaskGroups.has(groupKey)) {
    dashboardState.expandedTaskGroups.delete(groupKey);
  } else {
    dashboardState.expandedTaskGroups.add(groupKey);
  }
  dashboardState.taskRenderSignature = "";
  renderTasks(dashboardState.data.tasks || []);
}

function renderTaskRow(task) {
  const row = document.createElement("tr");
  if (task.target_price_hit) row.className = "target-hit-row";
  const transferPolicy = transferPolicyLabel(task.transfer_policy || "any");
  const airlines = task.preferred_airlines?.length ? task.preferred_airlines.join(", ") : "不限";
  const dateText = task.route_type === "multi_city"
    ? (task.itinerary_summary || task.departure_date)
    : task.departure_date;
  const groupLine = task.group_label ? `<br><span class="muted">${escapeHtml(task.group_label)}</span>` : "";
  const runDisabled = task.is_expired ? "disabled" : "";
  const runTitle = task.is_expired ? 'title="航线日期较早，请更新日期后查询"' : "";
  const deleteAction = task.group_id
    ? `<button class="ghost compact danger" type="button" data-task-action="delete-group" data-group-id="${escapeHtml(task.group_id)}">删除整组</button>`
    : `<button class="ghost compact danger" type="button" data-task-action="delete-route" data-key="${escapeHtml(task.route_key)}">删除</button>`;
  row.innerHTML = `
    <td>${statusBadge(task.status)}</td>
    <td>${escapeHtml(providerLabel(task.provider))}</td>
    <td>${escapeHtml(taskRouteDisplay(task))}${groupLine}</td>
    <td>${escapeHtml(dateText)}</td>
    <td>${escapeHtml(cabinLabel(task.cabin))}<br><span class="muted">${escapeHtml(transferPolicy)} · ${escapeHtml(airlines)}</span></td>
    <td>
      <div class="price-cell">
        <strong>${task.latest_price ? `${escapeHtml(task.currency)} ${escapeHtml(task.latest_price)}` : "暂无"}</strong>
        ${renderPriceChangePill(task)}
        ${renderTargetPricePill(task)}
      </div>
    </td>
    <td>${escapeHtml(formatQueryTime(task.last_query_time))}</td>
    <td>
      <div class="task-flight-mini">
        <span>${escapeHtml(taskFlightSummary(task))}</span>
        ${detailButton(task)}
      </div>
    </td>
    <td>${formatAutoQueryStatus(task)}</td>
    <td>
      <div class="task-action-stack">
        <button class="ghost compact" type="button" data-task-action="run-route" data-key="${escapeHtml(task.route_key)}" data-provider="${escapeHtml(task.provider)}" ${runDisabled} ${runTitle}>
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
  const bestLabel = best
    ? `${escapeHtml(best.currency)} ${escapeHtml(best.latest_price)}`
    : summary.priced.length ? "口径不同，无法比价" : "暂无";
  const runDisabled = summary.expired ? "disabled" : "";
  const runTitle = summary.expired ? 'title="航线日期较早，请更新日期后查询"' : "";
  const routePreview = summary.tasks.slice(0, 4).map(taskRouteDisplay).join(" / ");
  const expanded = dashboardState.expandedTaskGroups instanceof Set && dashboardState.expandedTaskGroups.has(group.key);
  const row = document.createElement("tr");
  row.className = `multi-city-group-row${expanded ? " is-expanded" : ""}`;
  row.innerHTML = `
    <td>${groupStatusBadge(summary)}</td>
    <td>${escapeHtml(providerLabel(group.provider))}</td>
    <td>
      <strong>${escapeHtml(group.group_label)}</strong>
      <br><span class="muted">${summary.tasks.length} 条具体航线${routePreview ? ` · ${escapeHtml(routePreview)}${summary.tasks.length > 4 ? " ..." : ""}` : ""}</span>
    </td>
    <td>${escapeHtml(summary.dates.join(" / ") || "-")}</td>
    <td>${escapeHtml(cabinLabel(sampleTask.cabin))}<br><span class="muted">${escapeHtml(transferPolicyLabel(sampleTask.transfer_policy || "any"))} · ${summary.origins.length} 个出发地 / ${summary.destinations.length} 个目的地</span></td>
    <td>${bestLabel}<br><span class="muted">${summary.priced.length}/${summary.tasks.length} 已有价格</span></td>
    <td>${escapeHtml(formatQueryTime(best?.last_query_time || sampleTask.last_query_time))}</td>
    <td>
      <button class="ghost compact" type="button" data-task-action="toggle-group-expand" data-group-key="${escapeHtml(group.key)}" aria-expanded="${expanded ? "true" : "false"}">
        ${expanded ? "收起具体航线" : `展开 ${summary.tasks.length} 条航线`}
      </button>
    </td>
    <td>${summary.autoEnabled ? `${summary.autoEnabled}/${summary.tasks.length} 开启` : "未开启"}</td>
    <td>
      <div class="task-action-stack">
        <button class="ghost compact" type="button" data-task-action="run-group" data-group-id="${escapeHtml(group.group_id)}" data-provider="${escapeHtml(group.provider)}" ${runDisabled} ${runTitle}>查询本平台组</button>
        <button class="ghost compact danger" type="button" data-task-action="delete-group" data-group-id="${escapeHtml(group.group_id)}">删除整组</button>
      </div>
    </td>
  `;
  return row;
}

function renderTaskGroupDetailRow(group) {
  const summary = summarizeTaskGroup(group);
  const detailRows = summary.tasks.map((task) => `
    <div class="multi-city-detail-row${task.target_price_hit ? " target-hit-row" : ""}">
      <div>
        <strong>${escapeHtml(taskRouteDisplay(task))}</strong>
        <span>${escapeHtml(task.itinerary_summary || task.departure_date || "-")}</span>
      </div>
      <div>
        <strong>${task.latest_price ? `${escapeHtml(task.currency)} ${escapeHtml(task.latest_price)}` : "暂无"}</strong>
        ${renderPriceChangePill(task)}
      </div>
      <div>${escapeHtml(formatQueryTime(task.last_query_time))}</div>
      <div>${detailButton(task)}</div>
      <div>
        <button class="ghost compact" type="button" data-task-action="run-route" data-key="${escapeHtml(task.route_key)}" data-provider="${escapeHtml(task.provider)}" ${task.is_expired ? "disabled" : ""}>查询</button>
      </div>
    </div>
  `).join("");
  const detailRow = document.createElement("tr");
  detailRow.className = "multi-city-group-detail-tr";
  detailRow.innerHTML = `<td colspan="10"><div class="multi-city-detail-list">${detailRows}</div></td>`;
  return detailRow;
}

function renderTasks(tasks) {
  const filtered = filteredTasks(tasks);
  const displayRows = collapseTaskGroups(filtered);
  const body = document.getElementById("taskTableBody");
  renderTaskViewTabs(tasks);
  renderTaskSummary(filtered);
  const expandedKeys = dashboardState.expandedTaskGroups instanceof Set
    ? Array.from(dashboardState.expandedTaskGroups).sort()
    : [];
  const signature = JSON.stringify({
    view: dashboardState.activeTaskView,
    provider: document.getElementById("providerFilter")?.value || "",
    cabin: document.getElementById("cabinFilter")?.value || "",
    search: document.getElementById("routeSearchInput")?.value || "",
    expanded: expandedKeys,
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
    if (rowItem.type === "task_group") {
      body.appendChild(renderTaskGroupRow(rowItem));
      if (dashboardState.expandedTaskGroups instanceof Set && dashboardState.expandedTaskGroups.has(rowItem.key)) {
        body.appendChild(renderTaskGroupDetailRow(rowItem));
      }
    } else {
      body.appendChild(renderTaskRow(rowItem.task));
    }
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
    const autoEnabledCount = group.routes.filter((item) => item.auto_query_enabled).length;
    const groupAutoAllOn = group.routes.length > 1 && autoEnabledCount === group.routes.length;
    const autoQueryStatus = group.routes.length > 1
      ? (autoEnabledCount
        ? `已开启 ${autoEnabledCount}/${group.routes.length} 条（12h 错峰）`
        : "未开启自动查询")
      : (route.auto_query_enabled
        ? `已开启，每 ${route.auto_query_interval_hours || 12} 小时一次；下次：${formatQueryTime(route.auto_query_next_run_at)}`
        : "未开启自动查询");
    const targetLine = route.target_price != null
      ? `<p>目标价提醒：≤ ${escapeHtml(route.target_price)}</p>`
      : "";
    const segments = (route.segments || [])
      .map((item) => `${item.origin}->${item.destination} ${item.departure_date}`)
      .join(" / ");
    const groupSummary = group.routes.length > 1
      ? `<p class="route-group-summary">${group.routes.length} 条候选 · ${escapeHtml(group.routes.slice(0, 6).map(routeLabel).join(" / "))}${group.routes.length > 6 ? " ..." : ""}</p>`
      : "";
    const groupProviderButtons = groupProviderNames(group.routes)
      .map((provider) => `
        <button class="ghost" type="button" data-action="run-group" data-group-id="${escapeHtml(group.group_id)}" data-provider="${escapeHtml(provider)}">
          查询${escapeHtml(providerLabel(provider))}
        </button>
      `)
      .join("");
    const routeActions = group.routes.length > 1
      ? `
        ${groupProviderButtons}
        <button class="ghost" type="button" data-action="run-group" data-group-id="${escapeHtml(group.group_id)}">
          查询整组
        </button>
        <button class="ghost" type="button" data-action="toggle-group-auto" data-enabled="${groupAutoAllOn ? "true" : "false"}" data-group-id="${escapeHtml(group.group_id)}">
          ${groupAutoAllOn ? "关闭整组自动查询" : "开启整组 12h 自动"}
        </button>
        <button class="ghost" type="button" data-action="edit-group" data-group-id="${escapeHtml(group.group_id)}">编辑整组</button>
        <button class="ghost danger" type="button" data-action="delete-group" data-group-id="${escapeHtml(group.group_id)}">删除整组</button>
      `
      : `
        <button class="ghost" type="button" data-action="toggle-auto" data-enabled="${route.auto_query_enabled ? "true" : "false"}" data-key="${escapeHtml(route.route_key)}">
          ${route.auto_query_enabled ? "关闭自动查询" : "开启 12h 自动查询"}
        </button>
        <button class="ghost" type="button" data-action="edit" data-key="${escapeHtml(route.route_key)}">编辑</button>
        <button class="ghost danger" type="button" data-action="delete" data-key="${escapeHtml(route.route_key)}">删除</button>
      `;
    const dateLine = route.route_type === "multi_city"
      ? segments
      : `${route.departure_date}${route.return_date ? ` / ${route.return_date}` : ""}`;
    card.innerHTML = `
      <div>
        <h3>${escapeHtml(group.group_label)}</h3>
        ${groupSummary}
        <p>${escapeHtml(dateLine)}</p>
        <p>类型：${route.route_type === "multi_city" ? "多程" : "单程/往返"}</p>
        <p>舱位：${escapeHtml(cabinLabel(route.cabin))}，${escapeHtml(transferPolicy)}，人数：${escapeHtml(route.passengers)}</p>
        <p>平台：${escapeHtml(providers)}</p>
        <p>航司偏好：${escapeHtml(airlines)}</p>
        ${targetLine}
        <p>自动查询：${escapeHtml(autoQueryStatus)}</p>
        <p>后台错峰：至少间隔 10 分钟</p>
      </div>
      <div class="card-actions">
        ${routeActions}
      </div>
    `;
    container.appendChild(card);
  }
}

function renderGlobalRunStatus(progress = {}) {
  const badge = document.getElementById("globalRunStatus");
  if (!badge) return;
  const tailRunning = typeof tailState !== "undefined"
    && (tailState.job?.items || []).some((job) => ACTIVE_TAIL_JOB_STATUSES.includes(job.status));
  if (progress.running) {
    badge.className = "status-badge scheduled";
    badge.textContent = "航线查询中";
  } else if (tailRunning) {
    badge.className = "status-badge scheduled";
    badge.textContent = "甩尾任务运行中";
  } else if ((progress.errors || []).length) {
    badge.className = "status-badge warning";
    badge.textContent = `最近有 ${progress.errors.length} 项异常`;
  } else {
    badge.className = "status-badge ok";
    badge.textContent = "就绪";
  }
}

function renderDashboard(data) {
  dashboardState.data = data;
  const optionsSignature = JSON.stringify({
    providers: data.provider_options || [],
    cabins: data.cabin_options || [],
    registry: data.provider_registry || [],
  });
  if (optionsSignature !== dashboardState.optionsSignature) {
    dashboardState.optionsSignature = optionsSignature;
    renderProviderOptions(data.provider_options || []);
    renderCabinOptions(data.cabin_options || []);
    if (typeof syncProviderCapabilities === "function") {
      syncProviderCapabilities();
    }
  }
  renderTasks(data.tasks || []);
  renderRunProgressSummary(data.run_feedback?.progress || {});
  renderGlobalRunStatus(data.run_feedback?.progress || {});
  renderResultChangeSummary(data.run_feedback || {});
  renderRoutes(data.routes || []);
  renderSessionHints(data.session_hints || []);
  updateWorkspaceTabs();
}

async function fetchDashboard() {
  const currentProvider = document.getElementById("providerFilter").value;
  const currentCabin = document.getElementById("cabinFilter").value;
  const currentSearch = document.getElementById("routeSearchInput").value;
  const response = await fetchApi("/api/dashboard", { timeoutMs: 12000 });
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response, "读取控制台数据失败"));
  }
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
  try {
    const response = await fetchApi("/api/log");
    if (!response.ok) throw new Error("log request failed");
    const data = await response.json();
    logBox.textContent = data.lines?.join("\n") || "暂无日志";
  } catch {
    logBox.textContent = "日志加载失败，请点击“刷新日志”重试。";
  }
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
  if (typeof document !== "undefined" && document.hidden) {
    scheduleDashboardPolling(10000);
    return;
  }
  const onDashboardTab = typeof workspaceState === "undefined"
    || workspaceState.activeTab === "routes"
    || workspaceState.activeTab === "tasks"
    || workspaceState.activeTab === "insights";
  const runningNow = Boolean(dashboardState.data.last_run?.running);
  if (!onDashboardTab && !runningNow) {
    scheduleDashboardPolling(15000);
    return;
  }
  dashboardState.pollInFlight = true;
  try {
    await fetchDashboard();
    const running = Boolean(dashboardState.data.last_run?.running);
    if (running || (typeof workspaceState !== "undefined" && workspaceState.activeTab === "insights")) {
      await fetchLog();
    }
    scheduleDashboardPolling(running ? 1500 : 8000);
  } catch (_error) {
    scheduleDashboardPolling(6000);
  } finally {
    dashboardState.pollInFlight = false;
  }
}
