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
  const response = await fetchApi("/api/browser-profiles");
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || "读取浏览器档案失败");
  }
  browserProfileState.data = data;
  renderBrowserProfiles(data);
  updateWorkspaceTabs();
}

async function postBrowserProfileAction(provider, action, payload = {}) {
  const response = await fetchApi(`/api/browser-profiles/${encodeURIComponent(provider)}/${action}`, {
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
