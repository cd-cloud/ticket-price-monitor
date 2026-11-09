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
document.getElementById("providerFilter").addEventListener("change", () => renderTasks(dashboardState.data.tasks || []));
document.getElementById("cabinFilter").addEventListener("change", () => renderTasks(dashboardState.data.tasks || []));
document.getElementById("routeSearchInput").addEventListener("input", () => renderTasks(dashboardState.data.tasks || []));
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
  if (!document.hidden && workspaceState.activeTab === "tail" && tailState.results.loaded && !tailBusy()) {
    renderTailResults(tailState.results.items);
  }
}, 60000);
