function organizeDashboardLayout() {
  document.getElementById("taskTableBody")?.closest("section")?.classList.add("task-workspace");
  ensureTaskDetailDrawer();
  ensureTailDiscoveryPanel();
  ensureBrowserProfilePanel();
  ensureWorkspaceTabs();
  setWorkspaceTab(workspaceState.activeTab, { persist: false });
}

function workspacePanelMap() {
  return {
    routes: document.querySelector(".route-grid"),
    tasks: document.getElementById("taskTableBody")?.closest("section"),
    tail: document.getElementById("tailDiscoveryPanel"),
    profiles: document.getElementById("browserProfilePanel"),
    insights: document.querySelector(".log-workspace"),
    experiments: document.getElementById("airchinaExperimentPanel"),
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
    <button type="button" data-workspace-tab="experiments">国航实验</button>
    <label class="workspace-tab-select"><span>当前功能</span><select id="workspaceTabSelect" aria-label="当前功能">
      <option value="routes" data-workspace-option="routes">航线配置</option>
      <option value="tasks" data-workspace-option="tasks">查询任务</option>
      <option value="tail" data-workspace-option="tail">甩尾发现</option>
      <option value="profiles" data-workspace-option="profiles">浏览器档案</option>
      <option value="insights" data-workspace-option="insights">日志</option>
      <option value="experiments" data-workspace-option="experiments">国航实验</option>
    </select></label>
  `;
  anchor.insertAdjacentElement("afterend", tabs);
  tabs.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-workspace-tab]");
    if (!button) return;
    setWorkspaceTab(button.dataset.workspaceTab);
  });
  tabs.addEventListener("change", (event) => {
    if (event.target.matches("#workspaceTabSelect")) setWorkspaceTab(event.target.value);
  });
  Object.values(workspacePanelMap()).forEach((panel) => panel?.classList.add("workspace-panel"));
  updateWorkspaceTabs();
}

function setWorkspaceTab(tab, options = {}) {
  const panels = workspacePanelMap();
  const nextTab = panels[tab] ? tab : "routes";
  workspaceState.activeTab = nextTab;
  document.body.dataset.workspace = nextTab;
  if (options.persist !== false) {
    localStorage.setItem("workspaceState.activeTab", nextTab);
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
  const select = document.getElementById("workspaceTabSelect");
  if (select) select.value = nextTab;
}

function updateWorkspaceTabs() {
  const tabs = document.getElementById("workspaceTabs");
  if (!tabs) return;
  const routes = dashboardState.data.routes || [];
  const tasks = dashboardState.data.tasks || [];
  const tailRunning = tailState.job.items.some((job) => ACTIVE_TAIL_JOB_STATUSES.includes(job.status));
  const profileWarnings = (browserProfileState.data.providers || []).filter((item) => item.chrome_profile && !item.chrome_profile.ok).length;
  const counts = {
    routes: routes.length ? String(routes.length) : "",
    tasks: tasks.length ? String(tasks.length) : "",
    tail: tailRunning ? "运行中" : (tailState.results.items.length ? String(tailState.results.items.length) : ""),
    profiles: profileWarnings ? String(profileWarnings) : "",
  };
  Object.entries(counts).forEach(([key, value]) => {
    const badge = tabs.querySelector(`[data-workspace-count="${key}"]`);
    if (!badge) return;
    badge.textContent = value;
    badge.hidden = !value;
    const button = tabs.querySelector(`[data-workspace-tab="${key}"]`);
    const option = tabs.querySelector(`[data-workspace-option="${key}"]`);
    if (button && option) option.textContent = `${button.firstChild.textContent.trim()}${value ? ` · ${value}` : ""}`;
  });
}
