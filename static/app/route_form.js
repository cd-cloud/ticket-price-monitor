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
  const route = (dashboardState.data.routes || []).find((item) => item.route_key === routeKey);
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

  const response = await fetchApi("/api/routes", {
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
  const response = await fetchApi(`/api/routes/${encodeURIComponent(routeKey)}`, { method: "DELETE" });
  if (!response.ok) {
    const error = await response.text();
    throw new Error(error || "删除航线失败");
  }
  await fetchDashboard();
  notifyComplete("航线已删除");
}

async function deleteRouteGroup(groupId) {
  const response = await fetchApi(`/api/route-groups/${encodeURIComponent(groupId)}`, { method: "DELETE" });
  if (!response.ok) {
    const error = await response.text();
    throw new Error(error || "删除查询组失败");
  }
  await fetchDashboard();
  notifyComplete("航线组已删除");
}

async function runRouteGroup(groupId, provider = "") {
  const params = provider ? `?provider=${encodeURIComponent(provider)}` : "";
  const response = await fetchApi(`/api/route-groups/${encodeURIComponent(groupId)}/run-once${params}`, { method: "POST" });
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
  const response = await fetchApi(`/api/routes/${encodeURIComponent(routeKey)}/auto-query`, {
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
