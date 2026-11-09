function replaceTailMarkup(container, html) {
  if (container.innerHTML === html) return;
  const details = container.querySelectorAll?.("details[data-disclosure]") || [];
  for (const el of details) tailState.results.disclosureState.set(el.dataset.disclosure, el.open);
  const anchors = [...(container.querySelectorAll?.("[data-stable-key]") || [])];
  const anchor = anchors.find(el => el.getBoundingClientRect().bottom > 80);
  const anchorKey = anchor?.dataset.stableKey;
  const anchorTop = anchor?.getBoundingClientRect().top;
  const focusKey = container.contains?.(document.activeElement) ? document.activeElement?.dataset.focusKey : null;
  container.innerHTML = html;
  for (const el of container.querySelectorAll?.("details[data-disclosure]") || []) {
    if (tailState.results.disclosureState.has(el.dataset.disclosure)) el.open = tailState.results.disclosureState.get(el.dataset.disclosure);
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
  const response = await fetchApi(url, {timeoutMs: 15000});
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.detail || "读取失败");
    error.status = response.status;
    throw error;
  }
  return data;
}

async function tailPostJob(url, options = {}) {
  // Disable competing actions before dispatch, not after the response arrives.
  tailState.connection.submitting = true;
  updateTailBusyControls();
  try {
    const response = await fetchApi(url, {method:"POST", ...options, timeoutMs: 20000});
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(data.detail || "任务操作失败");
      error.authoritative = true;
      throw error;
    }
    return data;
  } catch (error) {
    if (!error.authoritative) {
      tailState.connection.unknown = true;
      document.getElementById("tailConnectionStatus").hidden = false;
      document.getElementById("tailConnectionStatus").textContent = "提交响应中断，正在确认后台是否已接受任务；不会自动重复提交。";
      setTimeout(() => initializeTailWorkspace().catch(() => {}), 3500);
    }
    throw error;
  } finally {
    tailState.connection.submitting = false;
    updateTailBusyControls();
  }
}


function tailBusy() {
  return Boolean(tailState.job.activeId) || tailState.connection.unknown || tailState.connection.submitting;
}


function updateTailBusyControls() {
  const busy = tailBusy();
  document.getElementById("tailRunBtn").disabled = busy;
  document.getElementById("tailQueryFields").disabled = busy;
  updateTailRunAction();
  document.querySelectorAll("#tailResults [data-tail-action='recheck'], #tailJobSummary [data-tail-action]").forEach(button => {
    button.disabled = busy && button.dataset.tailAction !== "continue";
  });
}


function renderTailJobPicker() {
  const select = document.getElementById("tailJobPicker");
  const jobs = tailState.job.selected && !tailState.job.items.some(job => job.job_id === tailState.job.selected.job_id)
    ? [tailState.job.selected, ...tailState.job.items] : tailState.job.items;
  const html = jobs.length ? jobs.map(job => {
    const p = job.payload || {};
    const stamp = job.created_at ? formatQueryTime(job.created_at) : job.job_id;
    const title = `${tailPlaceLabel(p.origin || "任务")} → ${tailPlaceLabel(p.transfer || "?")} · ${p.departure_date || ""} · ${tailJobStatusLabel(job.status)} · ${stamp}`;
    return `<option value="${escapeHtml(job.job_id)}">${escapeHtml(title)}</option>`;
  }).join("") : '<option value="">尚无任务</option>';
  if (select.innerHTML !== html) select.innerHTML = html;
  select.value = tailState.job.selectedId;
}


async function selectTailJob(jobId) {
  tailState.job.selectedId = jobId;
  if (tailState.job.activeId && tailState.job.selected?.job_id === tailState.job.activeId) renderTailJobProgress(tailState.job.selected);
  tailState.results.request++; // Invalidate stale result requests immediately.
  tailState.results.loaded = false;
  tailState.results.items = [];
  tailState.results.offset = 0;
  tailState.results.hasMore = false;
  tailState.job.selected = null;
  updateTailPagination();
  renderTailResults([]);
  if (jobId) {
    const job = await tailGetJson(`/api/tail-discovery/jobs/${encodeURIComponent(jobId)}`);
    if (tailState.job.selectedId !== jobId) return;
    tailState.job.selected = job;
    if (!tailState.job.activeId || job.job_id === tailState.job.activeId) renderTailJobProgress(job);
    if (tailState.results.view === "history") {
      setTailResultView("current");
      return;
    }
  } else tailState.job.selected = null;
  await fetchTailResults();
}


function setTailResultView(view) {
  tailState.results.view = view === "history" ? "history" : "current";
  if (tailState.job.activeId && tailState.job.selected?.job_id === tailState.job.activeId) renderTailJobProgress(tailState.job.selected);
  tailState.results.loaded = false;
  tailState.results.offset = 0;
  tailState.results.items = [];
  document.querySelectorAll("[data-tail-view]").forEach(button => {
    button.setAttribute("aria-pressed", String(button.dataset.tailView === tailState.results.view));
  });
  document.getElementById("tailJobPickerLabel").hidden = tailState.results.view === "history";
  document.getElementById("tailResultScopeNote").textContent = tailState.results.view === "history"
    ? "所有任务的已保存报价；筛选仅作用于已加载条目。更新列表不会重新查询平台。"
    : "仅所选任务的已保存报价（含继续任务的此前结果）；更新列表不重新查价。";
  renderTailResults([]);
  fetchTailResults().catch(() => {});
}


function updateTailPagination() {
  document.getElementById("tailLoadMoreBtn").hidden = !tailState.results.hasMore;
  document.getElementById("tailPageNote").textContent = tailState.results.hasMore
    ? `已加载 ${tailState.results.items.length} 条，尚有更多。筛选和排序仅作用于已加载报价。`
    : tailState.results.items.length ? `已加载 ${tailState.results.items.length} 条保存报价；不代表平台全量库存。` : "";
}


async function initializeTailWorkspace() {
  // Fail closed during boot: a page reload must not allow a second job before
  // the server has confirmed whether one is still running.
  tailState.connection.unknown = true;
  updateTailBusyControls();
  try {
    if (!tailState.form.candidatesLoaded) await fetchTailCandidates();
    await fetchTailJobs();
    await resumeLatestTailJob();
  }
  catch {
    tailState.connection.unknown = true;
    updateTailBusyControls();
    document.getElementById("tailConnectionStatus").hidden = false;
    document.getElementById("tailConnectionStatus").textContent = "暂时无法确认后台任务，正在重连；不会重复发起查询。";
    setTimeout(() => initializeTailWorkspace().catch(error => showTailError(error.message)), 10000);
  }
}

function pollTailDiscoveryJob(jobId, immediate = false) {
  if (!jobId || tailState.job.activeId !== jobId) return;
  if (tailState.job.activeTimer) clearTimeout(tailState.job.activeTimer);
  tailState.job.activeTimer = setTimeout(async () => {
    let job;
    try {
      job = await tailGetJson(`/api/tail-discovery/jobs/${encodeURIComponent(jobId)}`);
      if (tailState.job.activeId !== jobId) return;
    } catch (error) {
      if (tailState.job.activeId !== jobId) return;
      tailState.job.pollFailures++;
      tailState.connection.unknown = true;
      const status = document.getElementById("tailConnectionStatus");
      status.hidden = false;
      status.textContent = "连接中断，后台状态待确认。已保留结果，正在自动重连；请勿重复搜索。";
      if (error.status === 404) {
        // A missing job is authoritative, unlike an offline/timeout response.
        tailState.job.activeId = ""; tailState.connection.unknown = false;
        status.textContent = "后台已找不到此任务。现有结果已保留，请更新列表或重新搜索。";
        updateTailCancelButton(null);
      }
      updateTailBusyControls();
      if (tailState.job.activeId) pollTailDiscoveryJob(jobId);
      return;
    }
    tailState.job.pollFailures = 0; tailState.connection.unknown = false;
    document.getElementById("tailConnectionStatus").hidden = true;
    if (tailState.job.selectedId === jobId) tailState.job.selected = job;
    renderTailJobProgress(job);
    updateTailCancelButton(job);
    const done = ![...ACTIVE_TAIL_JOB_STATUSES, "cancelling"].includes(job.status);
    if (done) tailState.job.activeId = "";
    updateTailBusyControls();
    if (job.status === "failed") showTailError(job.error || "查询受阻，已完成结果已保留。");
    // Quote failures do not alter a known running job's status.
    if (tailState.results.view === "current" && tailState.job.selectedId === jobId) {
      await fetchTailResults({background:true}).catch(() => {});
    }
    await fetchTailJobs().catch(() => {});
    if (!done) pollTailDiscoveryJob(jobId);
  }, immediate ? 0 : Math.min(30000, 3500 * 2 ** Math.min(tailState.job.pollFailures, 3)));
}

function tailActiveContext(job) {
  if (![...ACTIVE_TAIL_JOB_STATUSES, "cancelling"].includes(job.status)) return "";
  const elsewhere = tailState.results.view === "history" || tailState.job.selectedId !== job.job_id;
  return `<p class="tail-active-context">后台执行：${escapeHtml(tailConditionLabel(job.payload))}${elsewhere ? "；下方结果另有所属。" : ""}</p>`;
}

function renderTailJobProgress(job) {
  const container = document.getElementById("tailJobSummary");
  if (!container) return;
  if (!job) { container.hidden = true; return; }
  if (job.payload?.provider === "both") {
    renderTailBatchProgress(job, container);
    updateTailResultContext();
    return;
  }
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
  if (container.dataset) container.dataset.active = String(active);
  const running = items.find(item => ["running", "started"].includes(item.status));
  const pending = items.filter(item => ["pending", "cancelled_before_query", "running"].includes(item.status)).length;
  const failed = items.filter(item => ["failed", "retryable_failed"].includes(item.status)).length || progress.failed || 0;
  const id = escapeHtml(job.job_id);
  const action = (value, label, primary = false) => `<button type="button" class="${primary ? "primary" : "ghost"} compact"
    data-tail-action="${value}" data-job-id="${id}" data-focus-key="${id}-${value}" ${tailBusy() && value !== "continue" ? "disabled" : ""}>${label}</button>`;
  let actions = "", primaryAction = "";
  if (job.status === "needs_verification") primaryAction = action("continue", "已在浏览器完成验证，继续", true);
  else if (!active) {
    if (pending || job.status === "paused" || job.status === "interrupted" || job.status === "cancelled") primaryAction = action("resume", "继续此任务", true);
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
      <strong>${escapeHtml(active ? "正在执行的任务" : "当前查看任务")} · ${escapeHtml(tailJobStatusLabel(job.status))}</strong>
      <span class="tail-job-counter" role="status">${escapeHtml(counter)}</span>
      ${primaryAction ? `<div class="tail-job-actions">${primaryAction}</div>` : ""}
    </div>
    ${primaryAction && job.status !== "needs_verification" ? '<p class="tail-task-action-note">沿用此任务的原条件，保留已完成结果；不会使用正在编辑的新条件。</p>' : ""}
    ${tailActiveContext(job)}
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

function renderTailPriorities(policy, inline = false) {
  if (policy.ranking !== "contextual_history_v1") return "";
  const pending = policy.next_candidates || [];
  const rows = (pending.length ? pending : (policy.priority_trace || []).slice(-10)).slice(0, 10);
  const calls = policy.provider_calls || {};
  const content = `<p class="muted">${policy.provider_calls_supported === false
    ? "携程浏览器调用数未计量，请使用城市数和运行时间预算。"
    : `本轮平台调用尝试 ${escapeHtml(String(calls.used || 0))} / ${calls.limit ? escapeHtml(String(calls.limit)) : "不限"}，含初始基准、开放/定向搜索及同航班补查；不代表平台扣费次数。`}每批更新排序，保留新城市探索机会。</p>
    <strong>历史优先级与调用预算（规则估计，非可售保证）</strong>
    <strong>${pending.length ? "待查城市的优先顺序" : "最近已查询城市的决策依据"}</strong>
    <ol>${rows.map(row => `<li><strong>${escapeHtml(airportCodeDisplayText(row.destination))}</strong>：${escapeHtml((row.reasons || []).join("；"))}</li>`).join("")}</ol>
    ${!rows.length ? '<p class="muted">暂无定向候选；开放查询未返回不代表无航线。</p>' : ""}`;
  if (inline) return `<section class="tail-priority-details">${content}</section>`;
  return `<details class="tail-priority-details" data-disclosure="priorities">
    <summary>历史优先级与调用预算（规则估计，非可售保证）</summary>${content}</details>`;
}

function renderTailBatchProgress(job, container) {
  const progress = job.progress || job.result?.progress || {};
  const children = job.result?.children || progress.children || [];
  const active = [...ACTIVE_TAIL_JOB_STATUSES, "cancelling"].includes(job.status);
  if (container.dataset) container.dataset.active = String(active);
  const id = escapeHtml(job.job_id);
  const action = (value, label, primary = false) => `<button type="button" class="${primary ? "primary" : "ghost"} compact" data-tail-action="${value}"
    data-job-id="${id}" data-focus-key="${id}-${value}" ${tailBusy() && value !== "continue" ? "disabled" : ""}>${label}</button>`;
  let actions = "", primaryAction = "";
  if (job.status === "needs_verification") primaryAction = action("continue", "已完成携程人工验证，继续", true);
  const failedProviders = children.filter(child => child.status === "failed");
  const hasPending = job.status !== "finished" && (["paused", "interrupted", "cancelled"].includes(job.status)
    || children.some(child => ["paused", "interrupted", "cancelled"].includes(child.status)
      || (child.progress?.queue?.items || []).some(item => ["pending", "cancelled_before_query"].includes(item.status))
    )
  );
  if (!active) {
    if (failedProviders.length) {
      const label = failedProviders.length === 1 ? `重试${escapeHtml(providerLabel(failedProviders[0].provider))}` : "重试失败平台";
      primaryAction = action("retry-failed", label, true);
    }
    if (hasPending) {
      if (primaryAction) actions += action("resume", "继续未查项");
      else primaryAction = action("resume", "继续未查项", true);
    }
  }
  const recoveryNote = failedProviders.length && hasPending
    ? "重试只运行失败平台；继续只处理未查项。均沿用原条件并保留已存结果。"
    : failedProviders.length ? "只重试失败平台，沿用原任务条件并保留已存结果。"
      : "沿用原任务条件，继续未查项；已存结果保留。";
  container.hidden = false;
  replaceTailMarkup(container, `
    <div class="tail-job-head"><strong>${active ? "正在执行任务" : "双平台任务"} · ${escapeHtml(tailJobStatusLabel(job.status))}</strong>
      <span class="tail-job-counter" role="status">已保存 ${escapeHtml(String(progress.saved ?? job.result?.saved ?? 0))} 条报价</span>
      ${primaryAction ? `<div class="tail-job-actions">${primaryAction}</div>` : ""}</div>
    ${primaryAction && job.status !== "needs_verification" ? `<p class="tail-task-action-note">${recoveryNote}</p>` : ""}
    <p class="tail-job-message">${escapeHtml(job.error || progress.message || "正在创建携程与飞猪子任务…")}</p>
    ${tailActiveContext(job)}
    <div class="tail-provider-progress">${children.map(child => {
      const p = child.payload || {}, cp = child.progress || {};
      const scope = p.search_scope === "open" ? "开放发现 · 非全量" : p.search_scope === "hybrid" ? "开放 + 定向补查 · 非全量" : "候选城市查询";
      return `<details class="tail-provider-progress-card" data-disclosure="child-${escapeHtml(child.job_id)}">
        <summary><strong>${escapeHtml(providerLabel(child.provider))} · ${escapeHtml(tailJobStatusLabel(child.status))}</strong>
          <span>${escapeHtml(scope)} · 已保存 ${escapeHtml(String(child.saved || 0))} 条${child.reused ? " · 保留此前结果" : ""}</span>
          ${child.error ? `<span class="tail-warning">${escapeHtml(child.error)}</span>` : ""}</summary>
        <div class="tail-provider-progress-body">
          ${child.error ? "" : `<p class="muted">${escapeHtml(cp.message || "等待平台返回进度")}</p>`}
          <p>运行时间上限：${escapeHtml(String(p.max_minutes || "不限"))} 分钟；${p.provider === "feizhu"
            ? `本轮调用上限：${escapeHtml(String(p.max_provider_calls || "不限"))}`
            : `本轮城市上限：${escapeHtml(String(p.max_candidates || "不限"))}`}</p>
          ${renderTailPriorities(cp.candidate_policy || {}, true)}
          <div class="tail-job-candidates">${(cp.queue?.items || cp.attempts || []).map(renderTailJobCandidate).join("")}</div>
        </div></details>`;
    }).join("")}</div>
    ${job.status === "needs_verification" ? '<p class="tail-warning">请在携程浏览器中人工完成验证。飞猪无需等待；此按钮不会绕过平台验证。</p>' : ""}
    ${actions ? `<div class="tail-job-actions">${actions}</div>` : ""}
    ${!active ? `<details data-disclosure="job-${id}"><summary>任务操作</summary>
      <div class="tail-job-actions">${action("refresh", "重新查价")}${action("explore", "重新查询两个平台")}</div>
      <p class="muted">使用原任务条件，预算按平台分别计算；不会使用上方尚未提交的新条件。</p></details>` : ""}`);
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
    partial: "部分平台未完成",
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
  const requestId = ++tailState.results.request;
  const view = tailState.results.view, jobId = tailState.job.selectedId;
  if (view === "current" && !jobId) {
    tailState.results.loaded = true; tailState.results.items = []; tailState.results.hasMore = false;
    renderTailResults([]); updateTailPagination(); return;
  }
  document.getElementById("tailLoadMoreBtn").disabled = true;
  try {
    let offset = append ? tailState.results.offset : 0, rows = [], more = false;
    // Preserve the amount already loaded when refreshing during a running task.
    const target = !append && background ? Math.max(200, tailState.results.offset) : offset + 200;
    do {
      const params = new URLSearchParams({limit:"200", offset:String(offset)});
      if (view === "current") params.set("job_id", jobId);
      const data = await tailGetJson(`/api/tail-discovery/results?${params}`);
      if (requestId !== tailState.results.request) return;
      const batch = data.results || [];
      rows.push(...batch);
      offset = data.next_offset ?? offset + batch.length;
      more = Boolean(data.has_more) && batch.length > 0;
    } while (!append && more && offset < target);
    if (requestId !== tailState.results.request || view !== tailState.results.view || jobId !== tailState.job.selectedId) return;
    const combined = append ? [...tailState.results.items, ...rows] : rows;
    tailState.results.items = [...new Map(combined.map(row => [row.id, row])).values()];
    tailState.results.offset = offset; tailState.results.hasMore = more; tailState.results.loaded = true;
    const listError = document.getElementById("tailListError");
    if (listError) listError.hidden = true;
    updateTailDateFilter(tailState.results.items);
    renderTailResults(tailState.results.items);
    updateTailPagination();
  } catch (error) {
    if (requestId === tailState.results.request) {
      const listError = document.getElementById("tailListError");
      if (listError) {
        listError.hidden = false;
        listError.textContent = "报价列表更新失败，已保留现有结果。可点“刷新已存结果”重试。";
      }
    }
    throw error;
  } finally {
    if (requestId === tailState.results.request) document.getElementById("tailLoadMoreBtn").disabled = false;
  }
}

async function fetchTailJobs() {
  const data = await tailGetJson("/api/tail-discovery/jobs?limit=20");
  tailState.job.items = data.jobs || [];
  if (!tailState.job.selectedId && tailState.job.items.length) {
    tailState.job.selected = tailState.job.items[0];
    tailState.job.selectedId = tailState.job.selected.job_id;
  }
  renderTailJobPicker();
  updateWorkspaceTabs();
}

function showTailError(message) {
  const el = document.getElementById("tailActionError");
  if (el) { el.textContent = message; el.hidden = !message; }
  if (message) notifyComplete(message, {kind: "warning", durationMs: 5200});
}

function startTailJobPolling(job, immediate = true) {
  tailState.connection.submitting = false;
  tailState.job.activeId = job.job_id || "";
  tailState.job.selectedId = tailState.job.activeId;
  tailState.job.selected = job;
  tailState.job.pollFailures = 0;
  tailState.connection.unknown = false;
  document.getElementById("tailConnectionStatus").hidden = true;
  document.getElementById("tailQueryEditor").open = false;
  renderTailJobPicker();
  renderTailJobProgress(job);
  updateTailCancelButton(job);
  updateTailBusyControls();
  clearTailResultFilters();
  setTailResultView("current");
  pollTailDiscoveryJob(tailState.job.activeId, immediate);
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
  tailState.connection.unknown = false;
  document.getElementById("tailConnectionStatus").hidden = true;
  if (job && [...ACTIVE_TAIL_JOB_STATUSES, "cancelling"].includes(job.status)) startTailJobPolling(job);
  else {
    updateTailBusyControls();
    if (tailState.job.selected) renderTailJobProgress(tailState.job.selected);
    await fetchTailResults().catch(() => {});
    // Restore a result-first workspace once, without folding a form the user
    // already started editing while initialization was in flight.
    if (!tailState.form.touched && tailState.results.items.length) document.getElementById("tailQueryEditor").open = false;
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
