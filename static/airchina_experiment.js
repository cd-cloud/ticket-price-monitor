/* Independent reference-only workspace. No integration with quote ranking. */
(() => {
  const panel = document.getElementById("airchinaExperimentPanel");
  if (!panel) return;
  const form = document.getElementById("airchinaExperimentForm");
  const select = document.getElementById("airchinaExperimentJobs");
  const status = document.getElementById("airchinaExperimentStatus");
  const results = document.getElementById("airchinaExperimentResults");
  const notices = document.getElementById("airchinaExperimentNotices");
  const error = document.getElementById("airchinaExperimentError");
  const resume = document.getElementById("airchinaExperimentContinue");
  const cancel = document.getElementById("airchinaExperimentCancel");
  const closeSession = document.getElementById("airchinaSessionClose");
  const prepareSession = document.getElementById("airchinaSessionPrepare");
  const restoreSession = document.getElementById("airchinaSessionRestore");
  const readySession = document.getElementById("airchinaSessionReady");
  const cancelPrepare = document.getElementById("airchinaSessionCancelPrepare");
  const deleteBackup = document.getElementById("airchinaSessionBackupDelete");
  const backupInput = document.getElementById("airchinaSessionBackup");
  const sessionStatus = document.getElementById("airchinaSessionStatus");
  const submit = form.querySelector("[type=submit]");
  const active = new Set(["queued", "running", "waiting_user", "cancelling"]);
  const preparationActive = new Set(["preparing", "opening", "in_use", "waiting_user", "validating", "cancelling"]);
  const labels = {
    queued: "已排队", running: "读取中", waiting_user: "等待你处理",
    cancelling: "取消中", cancelled: "已取消", timed_out: "已超时", failed: "失败",
    interrupted: "应用重启中断", completed_reference: "已取得参考票价", inconclusive: "未取得可靠结果",
  };
  const reasons = {
    page_reports_no_results: "官网显示“查询无结果，可尝试更换日期或稍后重试”；不等于确认无票",
    no_supported_visible_offer: "没有符合当前解析规则及筛选条件的航班报价",
    starting_fares_not_final_quotes: "已识别每成人不含税起价（非最终总价）",
    results_loading: "官网仍显示加载中",
    ambiguous_header: "查询摘要缺失、不唯一或不是单程",
    visible_route_mismatch: "可见航线缺失或与请求不符",
    visible_date_mismatch: "可见日期缺失或与请求不符",
    visible_passengers_mismatch: "可见人数不是 1 成人",
    unknown_price_scope: "未确认每成人不含税票价口径",
    unknown_cabin_columns: "舱等列标题缺失或变化",
    missing_rows: "未识别航班列表结构",
    access_unconfirmed: "访问状态无法确认",
    stale_results: "官网提示结果过期",
    manual_notice_confirmation_required: "仍有提示需要处理",
    needs_notice_confirmation: "仍有提示需要处理",
    needs_verification: "需要人工验证",
    blocked: "访问受限",
    unexpected_page: "页面偏离预期航线",
    invalid_snapshot: "页面结构无法校验",
    unknown_schema: "页面结构无法校验",
  };
  const contextLabels = {route_text:"航线", date_text:"日期", trip_type:"行程类型",
    passengers_text:"人数", price_notice:"价格口径"};
  const guardLabels = {verification_visible:"验证", blocked_visible:"访问限制",
    stale_visible:"结果过期", notice_confirmation_visible:"待确认通知",
    loading_visible:"加载中", no_results_visible:"查询无结果"};
  const entryLabels = {direct:"直达结果页", form:"官网表单", results:"结果页续查"};
  const interactionLabels = {stop:"无人值守，遇阻即停", wait:"等待人工"};
  const engineLabels = {native:"原生国航实验", flyclaw:"FlyClaw 国航 Source"};
  const sessionLabels = {fresh:"每任务重启", reuse:"短时复用", persistent:"常驻会话"};
  const entryStages = {opening_home:"打开官网", filling_form:"填写表单",
    selecting_origin:"选择出发机场", selecting_destination:"选择到达机场",
    selecting_date:"选择日期", selecting_passengers:"设置人数", checking_form:"核对条件",
    submitting_form:"提交查询", result_page:"到达结果页", stopped:"已停止",
    waiting_verification:"等待人工验证", verification_cleared:"验证已解除",
    checking_retained_results:"检查保留的旧列表", opening_results_form:"展开页内查询表单",
    observing_refresh:"观察列表刷新"};
  const entryReasons = {not_submitted:"尚未提交", submit_attempted:"已开始一次提交尝试",
    result_page_observed:"已确认结果页地址", submit_outcome_unknown:"提交结果未知，不重提",
    form_wait_timeout:"等待控件或条件确认超时", form_conditions_mismatch:"表单条件不匹配",
    form_changed_before_submit:"提交前条件发生变化", date_unavailable:"日期不可选择",
    unknown_home_dialog:"首页存在未识别弹窗", verification:"需要验证", blocked:"访问受限",
    verification_cleared:"人工验证已解除，不代表报价有效",
    passenger_control_disabled:"人数控件不可操作", calendar_navigation_limit:"已达到翻月上限",
    retained_results_mismatch:"保留页不是同条件的有效参考列表",
    results_form_not_ready:"结果页查询表单未就绪",
    airport_filling_input_timeout:"机场输入未完成",
    airport_opening_picker_timeout:"机场选择器未能展开",
    airport_waiting_picker_timeout:"机场选择器尚未就绪，未填写机场码",
    airport_waiting_candidates_timeout:"未等到容器内精确匹配的机场候选项",
    airport_clicking_candidate_timeout:"机场候选项点击未完成",
    airport_confirming_selection_timeout:"点击后未确认机场选中",
    airport_locating_input_timeout:"机场输入框定位超时",
    airport_selection_unconfirmed:"机场选中状态未确认",
    refresh_not_observed:"未观察到列表刷新，不能把旧票价作为新结果",
    visible_refresh_observed:"已观察到列表刷新过程（不证明服务端重新查价）"};
  let jobs = [], selected = "", generation = 0, busy = false, uncertain = true, timer;
  let sessionInfo = {state:"closed", backup:{}}, sessionBusy = false, sessionTimer, queryRunning = false;
  let queryPolicyInfo = null, lastNonResultsOptions = null, diagnosticOpen = new Set();
  submit.disabled = closeSession.disabled = true;
  const base = "/api/experiments/airchina";
  const workflow = document.getElementById("airchinaWorkflow");
  const currentStatus = document.getElementById("airchinaCurrentStatus");
  const sessionBadge = document.getElementById("airchinaSessionBadge");
  const taskBadge = document.getElementById("airchinaTaskBadge");
  const sessionNext = document.getElementById("airchinaSessionNext");
  const nextAction = document.getElementById("airchinaNextAction");
  const formHint = document.getElementById("airchinaFormHint");
  const formReset = document.getElementById("airchinaFormReset");
  const formStorageKey = "airchina_experiment_form_v1";
  const today = new Date();
  const dateText = value => `${value.getFullYear()}-${String(value.getMonth()+1).padStart(2,"0")}-${String(value.getDate()).padStart(2,"0")}`;
  form.elements.departure_date.min = dateText(today);
  const maxDate = new Date(today);
  maxDate.setDate(maxDate.getDate() + 365);
  form.elements.departure_date.max = dateText(maxDate);

  async function request(url, options = {}) {
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(url, {...options, signal: controller.signal});
      const body = await response.json();
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "请求失败，请刷新任务记录。");
      return body;
    } finally { clearTimeout(deadline); }
  }

  function selectedJob() {
    return jobs.find(job => job.job_id === selected) || null;
  }

  function setBadge(element, text, tone) {
    if (!element) return;
    element.textContent = text;
    element.className = `status-badge ${tone}`;
  }

  function diagnosticDetails(key, title, open = false) {
    const detail = document.createElement("details");
    detail.className = "airchina-diagnostics diagnostic-details";
    detail.dataset.diagnostic = key;
    detail.open = open;
    const summary = document.createElement("summary");
    summary.textContent = title;
    detail.appendChild(summary);
    return detail;
  }

  results.addEventListener("toggle", event => {
    const detail = event.target.closest("details.diagnostic-details");
    if (!detail) return;
    if (detail.open) diagnosticOpen.add(detail.dataset.diagnostic);
    else diagnosticOpen.delete(detail.dataset.diagnostic);
  }, true);

  function updateWorkflow(job) {
    const activeJob = job || selectedJob();
    const preparing = preparationActive.has(sessionInfo.state);
    const running = activeJob && active.has(activeJob.status);
    let step = "prepare";
    if (preparing || running) step = "run";
    else if (activeJob?.status === "completed_reference") step = "results";
    else if (sessionInfo.state === "ready" || activeJob) step = "query";
    const order = ["prepare", "query", "run", "results"];
    for (const item of workflow.querySelectorAll("li")) {
      const current = order.indexOf(item.dataset.step);
      item.classList.toggle("active", item.dataset.step === step);
      item.classList.toggle("done", current < order.indexOf(step));
    }
    const sessionStates = {
      closed:"未准备", preparing:"准备中", opening:"打开中", in_use:"使用中",
      waiting_user:"等待人工", validating:"确认中", ready:"可复用", retained:"可复用", error:"准备失败",
    };
    setBadge(sessionBadge, sessionStates[sessionInfo.state] || "未准备",
      sessionInfo.state === "ready" ? "ready" : preparing ? "active" : sessionInfo.state === "error" ? "error" : "idle");
    setBadge(currentStatus, activeJob ? (labels[activeJob.status] || activeJob.status) : (sessionStates[sessionInfo.state] || "未开始"),
      activeJob?.status === "completed_reference" ? "ready" : running || preparing ? "active" : "idle");
    setBadge(taskBadge, activeJob ? (labels[activeJob.status] || activeJob.status) : "尚未选择任务",
      activeJob?.status === "completed_reference" ? "ready" : running ? "active" : "idle");

    if (queryPolicyInfo?.paused) {
      nextAction.textContent = "查询保护已暂停：请先在项目 Chrome 中人工检查，再解除本地暂停。";
    } else if (activeJob?.status === "waiting_user") {
      nextAction.textContent = activeJob.stage === "form_inspection"
        ? "表单未能确认：请检查项目 Chrome，完成后点击“结束检查并关闭”。"
        : "结果页需要人工处理：请在项目 Chrome 完成验证，再点击“继续读取”。";
    } else if (running) {
      nextAction.textContent = "查询正在进行；如需停止，请使用“取消任务”，已完成内容会保留。";
    } else if (sessionInfo.state === "waiting_user") {
      nextAction.textContent = "请在项目 Chrome 完成登录或验证，再点击“我已完成登录和验证”。";
    } else if (sessionInfo.state === "ready") {
      nextAction.textContent = activeJob?.status === "completed_reference"
        ? "已取得参考结果，可展开诊断详情；也可修改条件发起新查询。"
        : "登录态已准备，填写机场和日期后即可开始实验查询。";
    } else {
      nextAction.textContent = "先准备登录态，再开始实验查询。";
    }
    sessionNext.textContent = sessionInfo.state === "waiting_user"
      ? "项目 Chrome 已打开，请完成登录或验证后点击确认。"
      : preparing ? "正在准备项目 Chrome，请稍候。"
      : sessionInfo.state === "ready" ? "登录态已准备，可开始配置查询。"
      : "先准备登录态，再在项目 Chrome 中完成登录或验证。";
  }

  const recommendedForm = {
    origin: "", destination: "", departure_date: "", cabin: "economy",
    query_engine: "native", entry_mode: "form", interaction_mode: "wait",
    session_mode: "persistent", flight_no: "", auto_confirm_notices: true,
  };
  const formFields = Object.keys(recommendedForm);

  function saveForm() {
    const payload = {};
    for (const name of formFields) {
      const field = form.elements[name];
      payload[name] = field.type === "checkbox" ? field.checked : String(field.value || "").slice(0, 128);
    }
    try { localStorage.setItem(formStorageKey, JSON.stringify(payload)); } catch (_) { /* Storage is optional. */ }
  }

  function restoreForm() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(formStorageKey) || "null"); } catch (_) { /* Ignore invalid local data. */ }
    const values = {...recommendedForm};
    if (saved && typeof saved === "object") {
      for (const name of formFields) {
        const field = form.elements[name];
        const value = saved[name];
        if (field.type === "checkbox" && typeof value === "boolean") values[name] = value;
        else if (field.type !== "checkbox" && typeof value === "string" && value.length <= 128) {
          if (!field.matches("select") || [...field.options].some(option => option.value === value)) values[name] = value;
        }
      }
    }
    for (const name of formFields) {
      const field = form.elements[name];
      if (field.type === "checkbox") field.checked = values[name];
      else field.value = values[name];
    }
    lastNonResultsOptions = {...values};
    syncEntryModeConstraints();
  }

  function syncEntryModeConstraints() {
    const resultsMode = form.elements.entry_mode.value === "results";
    if (resultsMode) {
      lastNonResultsOptions = lastNonResultsOptions || {
        entry_mode: form.elements.entry_mode.value,
        interaction_mode: form.elements.interaction_mode.value,
        session_mode: form.elements.session_mode.value,
      };
      form.elements.interaction_mode.value = "stop";
      form.elements.session_mode.value = "reuse";
      formHint.textContent = "结果页续查需要“短时复用＋无人值守”，已自动设置；请确保已有同条件成功参考列表。";
    } else {
      const values = lastNonResultsOptions || recommendedForm;
      form.elements.interaction_mode.value = values.interaction_mode;
      form.elements.session_mode.value = values.session_mode;
      formHint.textContent = "默认使用官网表单、等待人工和常驻会话。";
    }
    form.elements.interaction_mode.disabled = resultsMode;
    form.elements.session_mode.disabled = resultsMode;
  }

  form.addEventListener("input", saveForm);
  form.addEventListener("change", saveForm);
  form.elements.entry_mode.addEventListener("change", () => {
    if (form.elements.entry_mode.value !== "results") lastNonResultsOptions = null;
    syncEntryModeConstraints();
    saveForm();
  });
  for (const name of ["origin", "destination"]) {
    form.elements[name].addEventListener("blur", event => {
      event.target.value = event.target.value.trim().toUpperCase();
      saveForm();
    });
  }
  formReset.addEventListener("click", () => {
    for (const name of formFields) {
      const field = form.elements[name];
      if (field.type === "checkbox") field.checked = recommendedForm[name];
      else field.value = recommendedForm[name];
    }
    lastNonResultsOptions = null;
    syncEntryModeConstraints();
    saveForm();
    form.elements.origin.focus();
  });
  restoreForm();

  let sessionVersion = 0;

  function renderSessionControls() {
    const preparing = preparationActive.has(sessionInfo.state);
    const disabled = busy || uncertain || sessionBusy || queryRunning || preparing;
    prepareSession.disabled = restoreSession.disabled = disabled;
    const backupUnavailable = !sessionInfo.backup?.available || sessionInfo.backup?.encryption_available === false;
    restoreSession.disabled = disabled || backupUnavailable;
    deleteBackup.disabled = sessionBusy || queryRunning || preparing || !sessionInfo.backup?.available;
    readySession.hidden = sessionInfo.state !== "waiting_user";
    cancelPrepare.hidden = !preparing || sessionInfo.state === "cancelling";
    closeSession.disabled = busy || uncertain || sessionBusy || queryRunning || preparing;
    submit.disabled = busy || uncertain || sessionBusy || queryRunning || preparing;
    const states = {
      closed:"会话已关闭", preparing:"正在准备项目 Chrome", opening:"正在打开项目 Chrome",
      waiting_user:"请在项目 Chrome 中完成登录或验证",
      validating:"正在确认页面状态", ready:"会话可复用", retained:"会话可复用", in_use:"会话正在查询",
      cancelling:"正在取消准备", error:"会话准备失败",
    };
    const backupStates = {
      encryption_unavailable:"未配置项目主密钥，未保存明文备份", invalid_backup:"备份无法校验",
      environment_changed_restart_required:"浏览器环境已变化，请关闭会话并重启服务后使用对应环境的备份",
      invalid_version:"备份版本不支持", backup_failed:"备份写入失败",
    };
    const backup = sessionInfo.backup || {};
    const backupText = backup.error_code
      ? `；备份：${backupStates[backup.error_code] || backup.error_code}`
      : backup.available && backup.encryption_available === false
        ? "；备份存在，但未配置项目主密钥，不能恢复"
        : backup.available ? `；本机加密备份：${backup.saved_at ? new Date(backup.saved_at).toLocaleString() : "已保存"}` : "；无本机备份";
    const retentionLabels = {
      reference_validated:"已校验参考结果", manual_verification_cleared:"人工验证已解除",
      access_validated:"访问状态可复用", manual_session_prepared:"人工准备登录态",
    };
    const retentionText = retentionLabels[sessionInfo.retention_reason] || "";
    sessionStatus.textContent = `${states[sessionInfo.state] || sessionInfo.state || "状态未知"}${sessionInfo.reused ? " · 正在复用同一页面" : ""}${retentionText ? ` · ${retentionText}` : ""}${backupText}。登录态和验证码是否有效以官网实际响应为准。`;
    if (sessionInfo.error_code === "environment_changed_restart_required") {
      sessionStatus.textContent += ` ${backupStates.environment_changed_restart_required}。`;
    }
    updateWorkflow();
  }

  async function refreshSession() {
    const version = ++sessionVersion;
    clearTimeout(sessionTimer);
    try {
      const data = await request(`${base}/session`);
      if (version !== sessionVersion) return;
      sessionInfo = data;
      renderSessionControls();
    } catch (exc) {
      if (version === sessionVersion) sessionStatus.textContent = `会话状态读取失败：${exc.message}；不会自动重复提交。`;
    } finally {
      if (version === sessionVersion && preparationActive.has(sessionInfo.state)) {
        sessionTimer = setTimeout(refreshSession, 1500);
      }
    }
  }

  async function sessionAction(path, options = {}) {
    if (sessionBusy) return;
    sessionBusy = true;
    renderSessionControls();
    try {
      sessionInfo = await request(`${base}${path}`, options);
      await refreshSession();
    } catch (exc) {
      sessionStatus.textContent = `${exc.message} 请先刷新会话状态，勿重复提交。`;
    } finally {
      sessionBusy = false;
      renderSessionControls();
    }
  }

  function render(job) {
    updateWorkflow(job);
    results.replaceChildren();
    const openNotices = new Set([...notices.querySelectorAll("details[open]")].map(el => el.dataset.noticeId));
    notices.replaceChildren();
    if (job?.notices?.length) {
      const heading = document.createElement("h3");
      heading.textContent = `官网提示记录（${job.notices.length} 条）`;
      notices.appendChild(heading);
      const explanation = document.createElement("p");
      explanation.textContent = "以下为官网提示的采集记录，并非对乘机资格或可出票性的确认。任务失败或超时仍保留；链接参数及疑似个人号码已隐藏。";
      notices.appendChild(explanation);
      const states = {recorded:"已记录", click_pending:"已记录，准备点击", click_sent:"已点击，等待页面确认",
        auto_confirmed:"已自动确认", manual_required:"需人工处理", unconfirmed:"未确认处理结果"};
      for (const entry of job.notices) {
        const detail = document.createElement("details");
        detail.dataset.noticeId = entry.notice_id;
        detail.open = openNotices.has(entry.notice_id);
        const summary = document.createElement("summary");
        summary.textContent = `${states[entry.status] || "处理状态未知"} · ${entry.title || "未识别提示"}`;
        const body = document.createElement("pre");
        body.textContent = entry.body || "未能读取正文，未自动确认。";
        const meta = document.createElement("p");
        meta.textContent = `${new Date(entry.observed_at).toLocaleString()} · 按钮：${entry.button || "未知"}${entry.truncated ? " · 正文过长，已截断，未自动确认" : ""}`;
        detail.append(summary, body, meta);
        notices.appendChild(detail);
      }
    }
    const running = jobs.some(j => active.has(j.status)) || (job && active.has(job.status));
    queryRunning = Boolean(running);
    renderSessionControls();
    resume.hidden = !job || job.status !== "waiting_user";
    resume.textContent = job?.stage === "form_inspection" ? "结束检查并关闭" : "我已手动处理，继续读取";
    cancel.hidden = !job || !active.has(job.status);
    resume.disabled = cancel.disabled = busy || uncertain || job?.status === "cancelling";
    if (!job) {
      status.textContent = "尚未选择任务。";
      const empty = document.createElement("p");
      empty.className = "airchina-muted";
      empty.textContent = "暂无查询结果。先准备登录态，再开始实验查询。";
      results.appendChild(empty);
      return;
    }
    status.textContent = `${labels[job.status] || job.status} · ${job.request.origin} → ${job.request.destination} · ${job.request.departure_date} · ${engineLabels[job.request.query_engine || "native"] || "未知引擎"} · ${entryLabels[job.request.entry_mode || "direct"] || "未知入口"} · ${interactionLabels[job.request.interaction_mode || "wait"] || "未知处理方式"} · ${sessionLabels[job.request.session_mode || "fresh"] || "未知会话模式"}\n${job.message}\n更新时间：${new Date(job.updated_at).toLocaleString()}${active.has(job.status) ? `；截止：${new Date(job.deadline_at).toLocaleTimeString()}` : ""}`;
    if (job.access_events?.length) {
      const box = diagnosticDetails("access", "访问阻塞记录（历史观察，不代表当前官网状态）", diagnosticOpen.has("access"));
      const phases = {home_form:"首页表单", result_page:"结果页", reading_results:"列表读取", reading_products:"产品读取"};
      const actions = {wait:"当时进入人工等待", stop:"当时停止读取"};
      for (const event of job.access_events) {
        const p = document.createElement("p");
        p.textContent = `${new Date(event.observed_at).toLocaleString()} · ${phases[event.phase] || "未知阶段"} · ${reasons[event.state] || "未知阻塞"} · ${actions[event.action] || "未知处理"}。`;
        box.appendChild(p);
      }
      results.appendChild(box);
    }
    const report = job.result || {};
    if (report.query_policy) {
      const policy = report.query_policy;
      const info = document.createElement("p");
      info.textContent = policy.source === "cache"
        ? `缓存参考（未重新查价）· 原采集时间：${new Date(policy.observed_at).toLocaleString()} · 命中时距采集 ${policy.age_seconds} 秒 · 缓存有效期 ${policy.ttl_seconds} 秒。当前展示可能已过期，请勿视作实时票价。`
        : policy.source === "paused" ? "国航查询保护已暂停；本次没有打开浏览器或提交查询。"
        : `本次执行官网查询流程 · 结果记录时间：${new Date(policy.observed_at).toLocaleString()}${policy.paused_after_query ? " · 后续国航查询已暂停，需人工确认恢复。" : ""}`;
      results.appendChild(info);
    }
    const network = job.network_diagnostics;
    if (network?.schema === "airchina_network_diagnostics_v1") {
      const box = diagnosticDetails("network", "单次查询网络诊断（历史记录）", diagnosticOpen.has("network"));
      const add = text => {
        const p = document.createElement("p");
        p.textContent = text;
        box.appendChild(p);
      };
      const env = network.environment || {}, counts = network.counts || {};
      const yesNo = value => value === true ? "是" : value === false ? "否" : "未知";
      const roles = {primary:"国航主档案", recovery:"国航恢复档案", unknown:"未确认"};
      const phases = {...entryStages, starting_browser:"启动浏览器", navigating:"打开页面",
        reading_notices:"查询通知", reading_results:"读取列表", reading_products:"读取产品",
        waiting_user:"等待人工"};
      add(`浏览器：${env.backend || "未知"}；持久档案：${yesNo(env.persistent_profile)}（${roles[env.profile_role] || "未确认"}）；已加载保存状态：${yesNo(env.storage_state_loaded)}。这些是加载方式，不证明登录或验证有效。`);
      add(`监听：${network.attached ? "已挂接" : "未成功挂接"}；${network.finished ? "已结束" : "记录中"}；耗时 ${(Number(network.elapsed_ms || 0)/1000).toFixed(1)} 秒。请求 ${counts.requests || 0}，响应 ${counts.responses || 0}，网络失败 ${counts.failed || 0}，页面脚本异常 ${counts.page_errors || 0}。`);
      add(`疑似查询请求 ${counts.query_candidates || 0}（仅根据同站点路径关键词判断，不证明查询已提交或返回票价）；快照时未结束请求 ${network.pending_at_snapshot || 0}。`);
      if (network.lifecycle) {
        const life = network.lifecycle;
        add(`页面生命周期：主文档请求 ${life.main_document_requests}；DOMContentLoaded ${life.dom_content_loaded}；主框架导航 ${life.main_frame_navigations}（含页内地址变化，不等于整页刷新次数）。只统计本任务监听期间，零计数不证明验证状态有效。`);
      }
      const statuses = Object.entries(network.http_statuses || {}).map(([code, count]) => `${code} × ${count}`);
      add(`HTTP 状态：${statuses.join("；") || "尚无已记录响应"}。HTTP 错误与网络失败分别统计；外部资源错误不等于查价接口错误。`);
      const failures = {aborted:"请求取消", timeout:"超时", dns:"域名解析", connection:"连接失败",
        tls:"证书/TLS", network_other:"其他网络失败"};
      if (Object.keys(network.failure_types || {}).length)
        add(`网络失败分类：${Object.entries(network.failure_types).map(([key,n]) => `${failures[key] || "未知"} × ${n}`).join("；")}。`);
      if (network.script_error_groups?.length) {
        const categories = {chunk_load:"脚本分块加载异常", hydration:"页面初始化/水合异常",
          undefined_access:"读取或设置空值属性", not_callable:"调用非函数", undefined_symbol:"标识符未定义",
          syntax:"语法异常", other:"未分类"};
        for (const g of network.script_error_groups) {
          add(`脚本异常：${g.kind} · ${categories[g.category] || "未分类"} × ${g.count}；首次 ${phases[g.first_phase] || "未知阶段"} ${(g.first_at_ms/1000).toFixed(1)}s，最近 ${phases[g.last_phase] || "未知阶段"} ${(g.last_at_ms/1000).toFixed(1)}s。`);
        }
        add("按错误类型和特征类别合并，不是精确去重或根因结论；不保存异常原文或调用栈，不能据此认定来自查价核心组件。");
      }
      add(`阶段：${(network.phases || []).map(p => `${phases[p.phase] || "未知"} ${(p.at_ms/1000).toFixed(1)}s`).join(" → ") || "尚未记录"}。`);
      if (network.audit_incomplete || counts.events_dropped || counts.requests_untracked)
        add(`诊断不完整或已截断：省略事件 ${counts.events_dropped || 0}，未跟踪请求 ${counts.requests_untracked || 0}。不能用零计数断定没有请求或错误。`);
      const interesting = (network.events || []).filter(e =>
        e.event === "failed" || (e.event === "response" && (e.status >= 400 || e.query_candidate)));
      if (interesting.length) {
        const detail = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = "响应与失败摘要（有界、已脱敏）";
        detail.appendChild(summary);
        const scopes = {official:"官网主站", airchina_subdomain:"国航子域", external:"外部资源"};
        for (const e of interesting) {
          const p = document.createElement("p");
          p.textContent = `#${e.id} · ${phases[e.phase] || "未知阶段"} → ${phases[e.event_phase] || "未知阶段"} · ${scopes[e.scope] || "未知域"} · ${e.resource} · ${e.event === "failed" ? failures[e.failure] || "网络失败" : `HTTP ${e.status ?? "未知"}`} · ${e.elapsed_ms}ms`;
          detail.appendChild(p);
        }
        box.appendChild(detail);
      }
      add("不保存 URL、请求头、请求/响应正文、Cookie、token 或脚本错误原文。尚未解析国航业务响应；HTTP 200 不代表查询成功。");
      results.appendChild(box);
    }
    const session = job.session_evidence;
    if (session) {
      const p = document.createElement("p");
      const states = {opening:"正在打开", in_use:"任务使用中", retained:"任务结束时保留",
        closed:"已关闭", cleanup_uncertain:"清理未确认，需重启应用"};
      const reasons = {first_use:"尚无可复用会话", fresh_requested:"选择每任务重启",
        same_page_context:"复用同一页面与上下文", configuration_changed:"配置改变，重新打开",
        expired:"会话过期，重新打开", page_closed:"页面已关闭，重新打开"};
      const retentionLabels = {
        reference_validated:"可靠参考结果", manual_verification_cleared:"人工验证已解除",
        access_validated:"访问状态可复用", manual_session_prepared:"人工准备登录态",
      };
      p.textContent = `会话记录：${sessionLabels[session.mode] || "未知模式"} · ${session.reused ? "本次已复用" : "本次新建"} · ${reasons[session.reason] || "原因未知"} · ${states[session.state] || "状态未知"}${session.use_number ? ` · 本会话第 ${session.use_number} 次任务` : ""}${session.retention_reason ? ` · 保留原因：${retentionLabels[session.retention_reason] || session.retention_reason}` : ""}。这是任务记录，不保证浏览器目前仍打开，也不代表验证状态或票价有效。`;
      results.appendChild(p);
    }
    const entry = job.entry_evidence || report.entry_evidence;
    if (entry) {
      const summary = document.createElement("p");
      summary.textContent = `表单入口：${entry.result_page_observed ? "已到达预期结果页（非查价成功）" : entry.submit_attempted ? "已尝试提交，未确认到达结果页；不会重提" : "尚未提交"} · 阶段：${entryStages[entry.stage] || entry.stage} · 原因：${entryReasons[entry.reason] || entry.reason}${entry.failed_stage ? ` · 停止于：${entryStages[entry.failed_stage] || entry.failed_stage}` : ""}`;
      results.appendChild(summary);
      if (entry.airport_step) {
        const steps = {locating_input:"定位输入框", opening_picker:"展开机场选择器",
          waiting_picker:"等待机场选择器就绪", filling_input:"填写机场码",
          waiting_candidates:"等待精确候选项", clicking_candidate:"点击候选项",
          confirming_selection:"核对选中状态", selection_confirmed:"已确认选中"};
        const a = entry.airport_observation || {};
        const bool = v => v === true ? "是" : v === false ? "否" : "未知";
        const count = v => Number.isInteger(v) && v >= 0 ? String(v) : "未知";
        const p = document.createElement("p");
        p.textContent = `机场诊断：${a.side === "origin" ? "出发" : a.side === "destination" ? "到达" : "未知"} · 最后步骤：${steps[entry.airport_step] || "未知"}；最近采样步骤：${steps[a.step] || "未知"}。输入等于目标机场码：${bool(a.input_matches)}；焦点在输入框：${bool(a.input_focused)}；可编辑：${bool(a.input_enabled)}；已选中目标：${bool(a.selected_matches)}。候选（容器内/整页）：${count(a.scoped_candidates)}/${count(a.page_candidates)}；精确匹配：${count(a.scoped_exact)}/${count(a.page_exact)}。计数截断：${bool(a.counts_capped)}；验证提示：${bool(a.verification)}；访问限制：${bool(a.blocked)}；弹窗：${bool(a.dialog_present)}。仅统计已知结构，整页匹配仅用于诊断，不扩大点击范围。`;
        results.appendChild(p);
        const readiness = document.createElement("p");
        readiness.textContent = `机场控件就绪诊断：匹配容器 ${count(a.root_count)} 个；可见输入框 ${count(a.input_count)} 个；选择器已展开且有候选数据：${bool(a.picker_ready)}。只观察页面公开控件，不读取内部状态。`;
        results.appendChild(readiness);
      }
      if (entry.mode === "results") {
        const detail = document.createElement("p");
        const signals = {loading_visible:"出现加载提示", previous_rows_cleared:"旧列表已清空"};
        detail.textContent = `结果页续查：旧列表校验${entry.baseline_validated ? "通过（仅作基线）" : "未通过"}；${entry.refresh_observed ? signals[entry.refresh_signal] || "已观察刷新" : "未观察到刷新"}。未调用回首页或刷新；官网自身是否更换文档参见生命周期记录。`;
        results.appendChild(detail);
      }
      const checkNames = {one_way:"单程", origin:"出发机场", destination:"到达机场", departure_date:"日期",
        passengers:"普通 1 成人", special_passengers:"无特殊乘客", overlays_closed:"选择控件已关闭", submit:"查询按钮"};
      if (Object.keys(entry.checks || {}).length) {
        const checks = document.createElement("p");
        checks.textContent = `提交前核对：${Object.entries(entry.checks).map(([k,v]) => `${checkNames[k] || k} ${v === true ? "✓" : "未通过"}`).join("；")}。`;
        results.appendChild(checks);
      }
    }
    const evidence = report.wait_evidence || job.diagnostics;
    if (evidence && Object.hasOwn(evidence, "last_observation")) {
      const box = diagnosticDetails("reading", "读取诊断（最近采样，不代表当前官网状态）", diagnosticOpen.has("reading"));
      const add = text => {
        const p = document.createElement("p");
        p.textContent = text;
        box.appendChild(p);
      };
      const last = evidence.last_observation;
      add(`采样 ${evidence.polls} 次 · 连续稳定 ${evidence.stable_samples ?? 0}/2 次 · 列表等待 ${((evidence.elapsed_ms || 0)/1000).toFixed(1)} 秒 / 上限 ${(evidence.budget_ms || 0)/1000} 秒`);
      if (last) {
        add(`判定：${reasons[last.parser_reason] || last.parser_reason}。`);
        add(`可见航班行：${last.visible_rows ?? "未识别"}；候选参考价：${last.accepted_offers}；拒绝行：${last.rejected_rows}；筛除报价：${last.filtered_offers}；冲突报价：${last.conflicting_offers}。`);
        const missing = Object.entries(last.context_present || {}).filter(([,v]) => !v)
          .map(([key]) => contextLabels[key] || key);
        if (missing.length) add(`缺失的可见证据：${missing.join("、")}。不会用搜索条件补填。`);
        const guards = Object.entries(last.guards || {}).filter(([,v]) => v !== false)
          .map(([key,v]) => `${guardLabels[key] || key}（${v === true ? "出现" : "未确认"}）`);
        if (guards.length) add(`页面信号：${guards.join("、")}。`);
        const rejected = Object.entries(last.row_rejection_reasons || {});
        if (rejected.length) add(`拒绝原因代码：${rejected.map(([key,v]) => `${key} × ${v}`).join("；")}。`);
      } else {
        add("尚未取得可分析的 DOM 采样。");
      }
      add("本区仅保留 DOM 原因代码、计数与状态；不采集整页正文或网络响应正文。日历价格不作为航班票价。");
      results.appendChild(box);
    }
    if (job.status !== "completed_reference") return;
    const products = report.product_preview?.products || [];
    const rows = products.length ? products : (report.reference_preview?.offers || []);
    const notice = document.createElement("p");
    notice.textContent = "采样参考 · 每成人 CNY 不含税 · 非全量 · 未确认最终库存或可出票性 · 不用于正式比价";
    results.appendChild(notice);
    const table = document.createElement("table");
    const header = table.createTHead().insertRow();
    ["航班", "出发 / 到达", "舱等 / 产品", products.length ? "基础票价（不含税）" : "起价（不含税）"].forEach(label => {
      const th = document.createElement("th"); th.textContent = label; header.appendChild(th);
    });
    const body = table.createTBody();
    for (const row of rows) {
      const tr = body.insertRow();
      [row.flight_no, `${row.departure_time} / ${row.arrival_time}`,
        `${row.cabin === "business" ? "公务舱" : "经济舱"}${row.product_name ? ` · ${row.product_name} ${row.seat_class}` : ""}`,
        products.length ? `¥${row.base_fare}` : `¥${row.reference_fare} 起`,
      ].forEach(text => { tr.insertCell().textContent = text; });
    }
    results.appendChild(table);
  }

  async function refresh() {
    const version = ++generation;
    clearTimeout(timer);
    try {
      const data = await request(base);
      if (version !== generation) return;
      const gate = data.query_policy;
      queryPolicyInfo = gate || null;
      updateWorkflow();
      const policyText = document.getElementById("airchinaQueryPolicy");
      const policyResume = document.getElementById("airchinaQueryResume");
      if (gate && policyText && policyResume) {
        const reason = {needs_verification:"验证码", blocked:"访问受限", rate_limited:"服务端限流", interrupted:"上次查询异常中断"};
        policyText.textContent = `${gate.paused ? `已暂停：${reason[gate.pause_reason] || "需人工检查"}` : "查询保护已启用"} · 同一运行目录串行查询 · 最小启动间隔 ${gate.min_interval_seconds} 秒 · 缓存 ${gate.cache_ttl_seconds} 秒 · 查询流程启动 ${gate.browser_runs} 次 / 缓存命中 ${gate.cache_hits} 次${gate.retry_after_seconds ? ` · 至少还需等待 ${gate.retry_after_seconds} 秒` : ""}。流程启动次数不等于官网成功提交次数；这些是本地保护参数，不是官网免验证规则。`;
        policyResume.hidden = !gate.paused;
        policyResume.disabled = Boolean(gate.active || gate.retry_after_seconds || busy);
      }
      jobs = data.jobs;
      uncertain = false;
      if (!jobs.some(j => j.job_id === selected)) selected = jobs[0]?.job_id || "";
      select.replaceChildren();
      for (const job of jobs) {
        const option = new Option(`${job.request.origin} → ${job.request.destination} ${job.request.departure_date} · ${entryLabels[job.request.entry_mode || "direct"] || "未知入口"} · ${sessionLabels[job.request.session_mode || "fresh"] || "未知会话模式"} · ${labels[job.status] || job.status} · ${new Date(job.created_at).toLocaleTimeString()}`, job.job_id);
        select.add(option);
      }
      select.value = selected;
      render(jobs.find(j => j.job_id === selected));
      error.textContent = "";
      if (jobs.some(j => active.has(j.status))) timer = setTimeout(refresh, 1500);
    } catch (exc) {
      if (version !== generation) return;
      uncertain = true;
      error.textContent = `连接状态未知：${exc.message} 不会自动重复提交查询，请刷新记录确认。`;
      resume.disabled = cancel.disabled = submit.disabled = closeSession.disabled = true;
      // Do not silently infer completion or issue another POST.
    }
  }

  async function mutate(url, payload) {
    if (busy) return;
    busy = true; ++generation; clearTimeout(timer);
    submit.disabled = resume.disabled = cancel.disabled = closeSession.disabled = true;
    try {
      const job = await request(url, {method: "POST", headers: {"Content-Type": "application/json"},
        ...(payload ? {body: JSON.stringify(payload)} : {})});
      selected = job.job_id;
      busy = false;
      await refresh();
    } catch (exc) {
      uncertain = true;
      error.textContent = `${exc.message} 请先刷新记录确认任务是否已受理，勿重复提交。`;
      // Keep mutations disabled until GET reconciles the unknown outcome.
      resume.disabled = cancel.disabled = submit.disabled = closeSession.disabled = true;
    } finally { busy = false; }
  }

  form.addEventListener("submit", event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    if (form.elements.entry_mode.value === "results" &&
        (form.elements.session_mode.value !== "reuse" || form.elements.interaction_mode.value !== "stop")) {
      document.getElementById("airchinaExperimentError").textContent =
        "结果页续查需要选择“短时复用”和“无人值守”；请先通过官网表单取得并保留同条件的参考结果。";
      return;
    }
    const payload = Object.fromEntries(new FormData(form));
    payload.interaction_mode = form.elements.interaction_mode.value;
    payload.session_mode = form.elements.session_mode.value;
    payload.auto_confirm_notices = form.elements.auto_confirm_notices.checked;
    if (!payload.flight_no) delete payload.flight_no;
    mutate(base, payload);
  });
  select.addEventListener("change", () => {
    selected = select.value;
    render(jobs.find(j => j.job_id === selected));
  });
  document.getElementById("airchinaExperimentRefresh").addEventListener("click", refresh);
  document.getElementById("airchinaQueryResume")?.addEventListener("click", async () => {
    if (busy || !window.confirm("确认已人工检查国航访问状态？解除本地暂停不保证验证码已消失，也不会自动提交查询。")) return;
    busy = true;
    try {
      await request(`${base}/policy/resume`, {method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({confirmed:true})});
    } catch (exc) {
      error.textContent = `恢复未确认：${exc.message}。请刷新状态。`;
    } finally { busy = false; }
    await refresh();
  });
  closeSession.addEventListener("click", () => sessionAction("/session/close", {method:"POST"}));
  prepareSession.addEventListener("click", () => sessionAction("/session/prepare", {method:"POST"}));
  restoreSession.addEventListener("click", () => sessionAction("/session/restore", {method:"POST"}));
  readySession.addEventListener("click", () => sessionAction("/session/ready", {
    method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({backup:backupInput.checked}),
  }));
  cancelPrepare.addEventListener("click", () => sessionAction("/session/cancel-prepare", {method:"POST"}));
  deleteBackup.addEventListener("click", () => {
    if (window.confirm("只删除本机加密登录态备份，不关闭浏览器、不清除 Chrome 持久档案。确定删除？")) {
      sessionAction("/session/backup", {method:"DELETE"});
    }
  });
  resume.addEventListener("click", () => mutate(`${base}/${encodeURIComponent(selected)}/continue`));
  cancel.addEventListener("click", () => mutate(`${base}/${encodeURIComponent(selected)}/cancel`));
  refresh();
  refreshSession();
})();
