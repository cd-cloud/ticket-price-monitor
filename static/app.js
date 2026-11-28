function handleUiError(error, fallback = "操作失败") {
  console.error(error);
  notifyComplete(error?.message || fallback, { kind: "warning", durationMs: 5200 });
}

document.getElementById("logoutBtn")?.addEventListener("click", async () => {
  try {
    await fetchApi("/api/session/logout", { method: "POST" });
    window.location.replace("/");
  } catch (error) {
    handleUiError(error, "退出登录失败");
  }
});

document.getElementById("runAllBtn")?.addEventListener("click", () => {
  runAsyncQuery("/api/run-once", "已开始查询全部航线", "启动全部查询失败")
    .catch((error) => handleUiError(error, "启动全部查询失败"));
});
document.getElementById("reportBtn")?.addEventListener("click", () => {
  runAction("/api/report", "报告已刷新")
    .catch((error) => handleUiError(error, "刷新报告失败"));
});
document.getElementById("reloadBtn")?.addEventListener("click", () => {
  fetchDashboard().catch((error) => handleUiError(error, "刷新数据失败"));
});
document.getElementById("reloadLogBtn")?.addEventListener("click", fetchLog);
document.getElementById("resetRouteBtn").addEventListener("click", resetRouteForm);
document.getElementById("routeForm").addEventListener("submit", (event) => {
  submitRouteForm(event).catch((error) => handleUiError(error, "保存航线失败"));
});
document.getElementById("routeTypeSelect").addEventListener("change", syncRouteTypeMode);
document.getElementById("departureDateInput").addEventListener("change", () => {
  syncRoundTripDateBounds("departure");
  syncProviderCapabilities();
});
document.getElementById("returnDateInput").addEventListener("change", () => {
  syncRoundTripDateBounds("return");
  syncProviderCapabilities();
});
document.getElementById("passengersInput").addEventListener("input", syncProviderCapabilities);
document.getElementById("transferPolicySelect").addEventListener("change", syncProviderCapabilities);
document.getElementById("airlinesInput").addEventListener("input", syncProviderCapabilities);
document.getElementById("addSegmentBtn").addEventListener("click", () => addSegmentRow({}));
document.getElementById("routeGroupEnabled").addEventListener("change", () => {
  syncRouteFormVisibility();
  syncRouteFormRequirements();
  syncProviderCapabilities();
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
document.getElementById("providerFilter").addEventListener("change", () => renderTasks(dashboardState.data.tasks || []));
document.getElementById("cabinFilter").addEventListener("change", () => renderTasks(dashboardState.data.tasks || []));
document.getElementById("routeSearchInput").addEventListener("input", () => renderTasks(dashboardState.data.tasks || []));
document.getElementById("taskTableBody").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-task-action]");
  if (!button) return;
  const action = button.dataset.taskAction;
  if (action === "show-detail") {
    showTaskDetail(button.dataset.key, button.dataset.provider);
    return;
  }
  if (action === "toggle-group-expand") {
    toggleTaskGroupExpanded(button.dataset.groupKey);
    return;
  }
  if (action === "run-route") {
    button.disabled = true;
    try {
      await runSingleRoute(button.dataset.key, button.dataset.provider);
    } catch (error) {
      handleUiError(error, "查询此航线失败");
    } finally {
      button.disabled = false;
    }
    return;
  }
  if (action === "run-group") {
    button.disabled = true;
    try {
      await runRouteGroup(button.dataset.groupId, button.dataset.provider || "");
    } catch (error) {
      handleUiError(error, "查询航线组失败");
    } finally {
      button.disabled = false;
    }
    return;
  }
  if (action === "delete-route") {
    const ok = await confirmAction("确定删除这条航线监控配置？历史快照仍保留在本地数据库中。", {
      title: "删除航线",
      confirmText: "确认删除",
      danger: true,
    });
    if (!ok) return;
    button.disabled = true;
    try {
      await deleteRoute(button.dataset.key);
    } catch (error) {
      handleUiError(error, "删除航线失败");
      button.disabled = false;
    }
    return;
  }
  if (action === "delete-group") {
    const ok = await confirmAction("确定删除整个航线组及其全部候选航线？历史快照仍保留在本地数据库中。", {
      title: "删除整组航线",
      confirmText: "确认删除",
      danger: true,
    });
    if (!ok) return;
    button.disabled = true;
    try {
      await deleteRouteGroup(button.dataset.groupId);
    } catch (error) {
      handleUiError(error, "删除航线组失败");
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
  if (action === "edit-group") {
    fillRouteGroupForm(button.dataset.groupId);
    return;
  }
  if (action === "delete") {
    const ok = await confirmAction("确定删除这条航线监控配置？", {
      title: "删除航线",
      confirmText: "确认删除",
      danger: true,
    });
    if (!ok) return;
    button.disabled = true;
    try {
      await deleteRoute(routeKey);
    } catch (error) {
      handleUiError(error, "删除航线失败");
      button.disabled = false;
    }
    return;
  }
  if (action === "delete-group") {
    const ok = await confirmAction("确定删除整个航线组及其全部候选航线？", {
      title: "删除整组航线",
      confirmText: "确认删除",
      danger: true,
    });
    if (!ok) return;
    button.disabled = true;
    try {
      await deleteRouteGroup(button.dataset.groupId);
    } catch (error) {
      handleUiError(error, "删除航线组失败");
      button.disabled = false;
    }
    return;
  }
  if (action === "run-group") {
    button.disabled = true;
    try {
      await runRouteGroup(button.dataset.groupId, button.dataset.provider || "");
    } catch (error) {
      handleUiError(error, "查询航线组失败");
    } finally {
      button.disabled = false;
    }
    return;
  }
  if (action === "toggle-auto") {
    button.disabled = true;
    try {
      await toggleAutoQuery(routeKey, button.dataset.enabled !== "true");
    } catch (error) {
      handleUiError(error, "更新自动查询失败");
    } finally {
      button.disabled = false;
    }
    return;
  }
  if (action === "toggle-group-auto") {
    button.disabled = true;
    try {
      await toggleGroupAutoQuery(button.dataset.groupId, button.dataset.enabled !== "true");
    } catch (error) {
      handleUiError(error, "更新整组自动查询失败");
    } finally {
      button.disabled = false;
    }
  }
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    scheduleDashboardPolling(300);
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
  if (!document.hidden && workspaceState.activeTab === "tail" && tailState.results.loaded && !tailBusy()) {
    renderTailResults(tailState.results.items);
  }
}, 60000);
