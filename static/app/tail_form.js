function tailFormFieldIds() {
  return ["tailOriginInput", "tailTransferInput", "tailDateInput", "tailProviderSelect",
    "tailFeizhuScopeInput",
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
      fields, manual: tailState.form.candidatesManual, candidates: selectedTailCandidates(),
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
    tailState.form.candidatesManual = saved.manual === true;
    tailState.form.candidateProfileOverride = document.getElementById("tailCandidateProfileSelect").value;
    if (tailState.form.candidatesManual && Array.isArray(saved.candidates)) tailState.form.savedCandidates = saved.candidates;
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
  if (settings) settings.textContent = value("tailProviderSelect") === "both"
    ? `携程 ${selectedTailCandidates().length} 城市 · 飞猪${{open:"开放发现", hybrid:"开放 + 补查", candidates:"同城市查询"}[value("tailFeizhuScopeInput")] || "开放发现"}`
    : value("tailSearchScopeInput") === "open" ? "开放发现 · 非全量"
    : `${value("tailSearchScopeInput") === "hybrid" ? "开放 + 定向补查 · " : ""}已选 ${selectedTailCandidates().length} 城市`;
  const budgetSummary = document.getElementById("tailBudgetSummary");
  if (budgetSummary) {
    const adjusted = ["tailBudgetCitiesInput", "tailBudgetMinutesInput", "tailTargetInput",
      "tailFollowupBudgetInput", "tailCallBudgetInput", "tailBaselineBudgetInput"]
      .filter(id => {
        const input = document.getElementById(id);
        return input && !input.disabled && input.value !== input.defaultValue;
      }).length;
    budgetSummary.textContent = adjusted ? `${adjusted} 项已调整` : "默认预算";
  }
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
  if (saved.provider !== "both" && draft.provider !== "both") ignored.add("feizhu_search_scope");
  if (![saved.search_scope, draft.search_scope,
    saved.provider === "both" ? saved.feizhu_search_scope : "",
    draft.provider === "both" ? draft.feizhu_search_scope : ""].includes("hybrid")) ignored.add("max_followup_candidates");
  if (!["feizhu", "both"].includes(saved.provider) && !["feizhu", "both"].includes(draft.provider)) {
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

function updateTailRunAction() {
  const button = document.getElementById("tailRunBtn");
  const hint = document.getElementById("tailRunHint");
  if (!button) return;
  const busy = tailBusy();
  const job = tailState.job.selected?.job_id === tailState.job.selectedId ? tailState.job.selected : null;
  const differs = Boolean(job && document.getElementById("tailOriginInput")
    && tailQueryDiffers(job.payload || {}, buildTailQueryPayload()));
  button.classList.toggle("primary", !job);
  button.classList.toggle("ghost", Boolean(job));
  button.textContent = busy ? "查询进行中" : !job ? "开始查询"
    : differs ? "使用新条件查询" : "使用表单条件重新查询";
  const view = document.getElementById("tailViewResultsBtn");
  if (view) {
    const emphasize = !busy && tailState.results.items.length > 0
      && (tailState.results.view === "history" || !job || job.status === "finished");
    view.classList.toggle("primary", emphasize);
    view.classList.toggle("ghost", !emphasize);
    view.textContent = "查看方案 ↓";
  }
  if (!hint) return;
  hint.textContent = tailState.connection.unknown
    ? "连接中断，后台状态待确认；请勿重复发起。"
    : busy ? "已完成的结果会逐步出现；停止查询会保留已保存结果。"
      : !job ? "填写我要乘坐的行程后开始查询；条件只保存在本机。"
        : tailState.results.view === "history" ? "当前浏览历史报价；新查询使用上方表单条件，不改变历史结果。"
          : differs ? "将使用已修改的条件开始新查询；下方仍为原任务结果。"
            : "使用上方表单条件开始新查询；已保存结果仍会保留。";
}

function updateTailResultContext() {
  const context = document.getElementById("tailResultIdentity");
  const notice = document.getElementById("tailDraftNotice");
  const snapshot = document.getElementById("tailTaskConditions");
  if (!context || !notice || !snapshot) return;
  updateTailRunAction();
  notice.hidden = true;
  snapshot.textContent = "";
  const taskSummary = document.getElementById("tailTaskContextSummary");
  if (tailState.results.view === "history") {
    context.textContent = "正在查看：历史报价 · 跨任务的已保存结果";
    if (taskSummary) taskSummary.textContent = "查看历史报价范围";
    notice.textContent = "上方条件仅用于新查询，不会改变历史报价；请使用列表筛选。";
    notice.hidden = false;
    return;
  }
  const job = tailState.job.selected?.job_id === tailState.job.selectedId ? tailState.job.selected : null;
  if (!job) {
    context.textContent = tailState.job.selectedId ? "正在读取所选任务条件…" : "尚未开始查询";
    if (taskSummary) taskSummary.textContent = tailState.job.selectedId ? "正在读取任务" : "选择已有任务";
    return;
  }
  const p = job.payload || {};
  context.textContent = `当前结果：${tailConditionLabel(p)}`;
  if (taskSummary) taskSummary.textContent = job.status ? tailJobStatusLabel(job.status) : "当前任务";
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
  if (p.search_scope) parts.push(`${p.provider === "both" ? "携程检索" : "检索方式"}：${{candidates:"定向城市", open:"开放发现（非全量）", hybrid:"开放发现 + 定向补查"}[p.search_scope] || p.search_scope}`);
  if (p.provider === "both") parts.push(`飞猪：${{candidates:"同城市查询", open:"开放发现（非全量）", hybrid:"开放 + 定向补查"}[p.feizhu_search_scope || "open"]}；平台分别计量预算，实际子任务方式见平台进度`);
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
    notice.textContent = `${tailState.form.touched ? "查询条件已修改" : "上方待提交条件与此任务不同"}，下方仍为所选任务的结果。点击“使用新条件查询”才会使用新条件。`;
    notice.hidden = false;
  }
}

function updateTailResultTools() {
  const summary = document.getElementById("tailResultFilterSummary");
  if (summary) {
    const count = Number(Boolean(document.getElementById("tailDateFilter")?.value))
      + Number(Boolean(document.getElementById("tailResultProviderFilter")?.value))
      + Number(Number(document.getElementById("tailMaxPriceInput")?.value || 0) > 0)
      + Number((document.getElementById("tailTrustInput")?.value || "all") !== "all")
      + Number(document.getElementById("tailLatestOnlyInput")?.checked === false);
    summary.textContent = count ? `筛选 · ${count} 项已调整` : "筛选";
  }
  const jump = document.getElementById("tailViewResultsBtn");
  if (jump) jump.hidden = !tailState.results.items.length;
  const quickFilter = document.getElementById("tailProviderQuickFilter");
  if (quickFilter) {
    const providers = new Set(tailState.results.items.map(item => item.provider).filter(Boolean));
    const selected = document.getElementById("tailResultProviderFilter")?.value || "";
    quickFilter.hidden = providers.size < 2;
    quickFilter.querySelectorAll("[data-tail-provider]").forEach(button => {
      const provider = button.dataset.tailProvider;
      const count = provider ? tailState.results.items.filter(item => item.provider === provider).length : tailState.results.items.length;
      button.textContent = `${{ "": "全部", ctrip: "携程", feizhu: "飞猪" }[provider]} ${count}`;
      button.setAttribute("aria-pressed", String(provider === selected));
    });
  }
  updateTailResultContext();
}


function filterTailCandidateList() {
  const keyword = document.getElementById("tailCandidateSearch").value.trim().toLowerCase();
  document.querySelectorAll("#tailCandidateList label").forEach(label => {
    label.hidden = !label.textContent.toLowerCase().includes(keyword);
  });
}


function clearTailResultFilters() {
  for (const [id, value] of [["tailDateFilter", ""], ["tailMaxPriceInput", ""], ["tailTrustInput", "all"], ["tailResultProviderFilter", ""]]) {
    const input = document.getElementById(id);
    if (input) input.value = value;
  }
  document.getElementById("tailLatestOnlyInput").checked = true;
  renderTailResults(tailState.results.items);
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
      <p class="muted">先选实际乘坐的航班，再比较包含额外尾段的购票组合。例如想坐北京 → 上海，上海是实际乘坐的到达城市，不一定是购票行程终点。搜索报价不代表可出票。</p></div>
    </div>
    <p id="tailActionError" role="alert" hidden></p>
    <form id="tailDiscoveryForm" class="tail-form">
      <details id="tailQueryEditor" class="tail-query-editor" open>
        <summary><span>我要乘坐的行程</span><span id="tailQuerySummary" class="muted">先选出发地、要乘坐的城市和日期</span></summary>
        <fieldset id="tailQueryFields">
          <div class="tail-basic-grid">
            <label>出发地<input id="tailOriginInput" data-airport-input placeholder="北京 (BJS)" autocomplete="off" required /></label>
            <label>实际乘坐到哪里 <small>例如：北京 → 上海的上海</small><input id="tailTransferInput" data-airport-input placeholder="上海 (SHA)" autocomplete="off" required /></label>
            <label>出发日期<input id="tailDateInput" type="date" required /></label>
            <label>查询平台<select id="tailProviderSelect"><option value="both">携程 + 飞猪同时查</option><option value="ctrip">仅携程</option><option value="feizhu">仅飞猪（实验）</option></select></label>
          </div>
          <p id="tailBothQuickNote" class="muted" hidden>一次搜索，并行查询携程和飞猪。预算按平台分别计算；某平台不可用时仍保留另一平台结果。</p>
          <div class="tail-ui-accordion">
            <div class="tail-options-grid accordion">
            <details class="tail-settings accordion-item" id="tailFilters">
              <summary class="accordion-button"><span class="tail-settings-title">报价与航班限制</span> <span id="tailFilterSummary" class="muted"></span></summary>
              <div class="tail-settings-grid accordion-body">
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
                <label class="tail-wide">舱位 / 航司适用范围<select id="tailFilterScopeInput"><option value="first_leg">仅实际乘坐的航段</option><option value="all_legs">购票行程的每一段</option></select></label>
              </div>
              <p class="muted">仅约束实际乘坐航段时，仍核对完整购票行程，但不限制尾段舱位和航司。携程跨组混舱覆盖不保证；人数库存须另行复核。</p>
            </details>
            <details class="tail-settings accordion-item" id="tailSearchSettings">
              <summary class="accordion-button"><span class="tail-settings-title">搜索范围</span> <span id="tailSearchSummary" class="muted"></span></summary>
              <input id="tailModeInput" type="hidden" value="explore" />
              <div class="tail-settings-grid accordion-body">
                <label class="tail-wide" id="tailScopeField">检索方式<select id="tailSearchScopeInput"><option value="candidates">按已选尾段城市查询</option><option value="open">开放目的地发现（非全量）</option><option value="hybrid">开放发现 + 历史引导补查</option></select></label>
                <label class="tail-wide" id="tailFeizhuScopeField" hidden>飞猪检索方式<select id="tailFeizhuScopeInput"><option value="open">开放目的地发现（非全量）</option><option value="candidates">与携程相同的候选城市</option><option value="hybrid">开放发现 + 历史引导补查</option></select></label>
                <p id="tailBothNotice" class="muted tail-wide" hidden>双平台并行，失败互不影响。携程按已选城市查询；飞猪按下方方式查询。时间、城市及首段目标按平台分别计量，调用上限只约束飞猪；飞猪需已启用并配置官方凭据。</p>
                <p id="tailOpenScopeNotice" class="muted tail-wide" hidden>一次发现多个尾段城市，不代表全量。不使用城市数及首段数量预算；继续已完成任务不会翻页，再次搜索才会重新查询。</p>
                <p id="tailHybridScopeNotice" class="muted tail-wide" hidden>先开放取样，再从所选城市与历史线索定向补查。补查城市上限按原任务累计，调用上限按本轮计算；历史路线不代表当前可售。</p>
              </div>
              <details id="tailAdvancedBudgets" class="tail-advanced-budgets accordion-item">
                <summary class="accordion-button"><span class="tail-settings-title">高级控制 <span id="tailBudgetSummary" class="muted">默认预算</span></summary>
                <div class="tail-settings-grid accordion-body">
                  <label>查询城市上限<input id="tailBudgetCitiesInput" type="number" min="0" max="500" placeholder="不限" /><small>留空或 0 表示不限</small></label>
                  <label>运行时间上限（分钟）<input id="tailBudgetMinutesInput" type="number" min="0" max="1440" placeholder="不限" /><small>留空或 0 表示不限</small></label>
                  <label>已校验首段达到几个后停止<input id="tailTargetInput" type="number" min="0" max="100" placeholder="不限" /><small>留空或 0 表示不限，不等于确认省钱</small></label>
                  <label>定向补查城市上限<input id="tailFollowupBudgetInput" type="number" min="0" max="50" value="5" /><small>0 表示关闭</small></label>
                  <label>本轮调用上限<input id="tailCallBudgetInput" type="number" min="0" max="500" value="20" /><small>含基准查询，0 表示不限；不等于计费次数</small></label>
                  <label>同首段航班基准补查次数<input id="tailBaselineBudgetInput" type="number" min="0" max="20" value="3" /><small>每轮计数，不含初始基准；0 表示关闭</small></label>
                </div>
              </details>
              <div class="tail-candidates accordion-body">
                <div class="tail-candidate-head">
                  <label>尾段城市来源<select id="tailCandidateProfileSelect"><option value="">按航司推荐</option><option value="ALL_DOMESTIC">国内航点</option><option value="DEFAULT">基础目的地</option></select></label>
                  <div id="tailCandidateSummary" class="tail-candidate-summary" role="status">正在加载候选城市…</div>
                </div>
                <details class="tail-candidate-box accordion-item">
                  <summary class="accordion-button"><span class="tail-settings-title">调整尾段城市</summary>
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
          </div>
        </fieldset>
      </details>
      <div class="tail-run-actions">
        <button id="tailRunBtn" class="primary" type="submit">开始查询</button>
        <button id="tailCancelBtn" class="ghost danger" type="button" hidden disabled>停止查询</button>
        <button id="tailViewResultsBtn" class="ghost" type="button" hidden>查看方案 ↓</button>
        <span id="tailRunHint" class="muted">填写我要乘坐的行程后开始查询；条件只保存在本机。</span>
      </div>
    </form>
    <p id="tailConnectionStatus" role="status" hidden></p>
    <div id="tailJobSummary" class="tail-job-summary" hidden></div>
    <section class="tail-results-workspace" aria-label="甩尾查询结果">
      <div class="tail-results-heading">
        <h3 id="tailResultsHeading" tabindex="-1">航班方案</h3>
        <div class="tail-view-tabs" role="group" aria-label="结果来源">
          <button type="button" data-tail-view="current" aria-pressed="true">任务报价</button>
          <button type="button" data-tail-view="history" aria-pressed="false">历史报价</button>
        </div>
        <button id="tailRefreshBtn" class="ghost compact" type="button" title="只重新读取已保存的结果，不查询平台">刷新已存结果</button>
      </div>
      <div class="tail-result-context">
        <p id="tailResultIdentity"></p>
        <p id="tailDraftNotice" class="tail-draft-notice" role="status" hidden></p>
        <details id="tailTaskContextDetails">
          <summary><span>切换任务 / 查询条件</span><span id="tailTaskContextSummary" class="muted">选择已有任务</span></summary>
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
            <label class="tail-control">报价平台<select id="tailResultProviderFilter"><option value="">全部平台</option><option value="ctrip">携程</option><option value="feizhu">飞猪</option></select></label>
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
        <div id="tailProviderQuickFilter" class="tail-provider-tabs" role="group" aria-label="按已加载报价筛选平台" hidden>
          <span>报价平台</span>
          <button type="button" data-tail-provider="" aria-pressed="true">全部</button>
          <button type="button" data-tail-provider="ctrip" aria-pressed="false">携程</button>
          <button type="button" data-tail-provider="feizhu" aria-pressed="false">飞猪</button>
        </div>
      </div>
      <div class="tail-results-meta">
        <span id="tailSummary" role="status">尚未搜索</span>
      </div>
      <p id="tailListError" class="tail-warning" role="alert" hidden></p>
      <p class="tail-safety-note">这里只显示已加载的部分报价，没查到不代表没有。不同行程或计价口径不直接比价；航段核对不代表价格有效或可出票。</p>
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
    tailState.form.touched = true;
    if (event.target.id === "tailCandidateSearch") filterTailCandidateList();
    updateTailFormSummary();
    saveTailForm();
  });
  form.addEventListener("change", event => {
    tailState.form.touched = true;
    const id = event.target.id;
    if (id === "tailAirlineSelect") document.getElementById("tailAirlineInput").value = event.target.value;
    if (event.target.matches('#tailCandidateList input')) tailState.form.candidatesManual = true;
    if (id === "tailCandidateProfileSelect") {
      tailState.form.candidateProfileOverride = event.target.value;
      tailState.form.candidatesManual = false; // Explicit source change, not silent route-input reset.
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
    if (key) tailState.results.disclosureState.set(key, event.target.open);
  }, true);
  panel.addEventListener("click", async event => {
    const choice = event.target.closest("[data-candidates]");
    if (choice) {
      tailState.form.touched = true;
      const action = choice.dataset.candidates;
      tailState.form.candidatesManual = action !== "recommended";
      if (action === "recommended") applyTailAirlineDefaults();
      else document.querySelectorAll('#tailCandidateList input').forEach(input => {
        if (action === "none") input.checked = false;
        else if (!input.disabled && !input.closest("label").hidden) input.checked = true;
      });
      updateTailCandidateSummary(); saveTailForm(); return;
    }
    const view = event.target.closest("[data-tail-view]");
    if (view) { setTailResultView(view.dataset.tailView); return; }
    const provider = event.target.closest("button[data-tail-provider]");
    if (provider) {
      document.getElementById("tailResultProviderFilter").value = provider.dataset.tailProvider;
      renderTailResults(tailState.results.items);
      return;
    }
    const button = event.target.closest("button[data-tail-action]");
    if (!button) return;
    const action = button.dataset.tailAction;
    if (action === "clear-filters") { clearTailResultFilters(); return; }
    if (action === "edit") {
      document.getElementById("tailQueryEditor").open = true;
      document.getElementById("tailOriginInput").focus(); return;
    }
    if (tailState.connection.submitting || tailState.connection.unknown || (tailBusy() && action !== "continue")) return;
    button.disabled = true;
    showTailError("");
    try {
      if (action === "recheck") {
        const data = await tailPostJob(`/api/tail-discovery/results/${encodeURIComponent(button.dataset.resultId)}/recheck`);
        startTailJobPolling(data);
      } else await handleTailJobAction(action, button.dataset.jobId);
    } catch (error) { showTailError(error.message); }
    finally { button.disabled = Boolean(tailState.job.activeId); }
  });
  document.getElementById("tailCancelBtn").addEventListener("click", () => cancelActiveTailJob().catch(error => showTailError(error.message)));
  document.querySelector("#tailQueryEditor > summary").addEventListener("click", () => { tailState.form.touched = true; });
  document.getElementById("tailRefreshBtn").addEventListener("click", () => fetchTailResults().catch(() => {}));
  document.getElementById("tailViewResultsBtn").addEventListener("click", () => {
    const heading = document.getElementById("tailResultsHeading");
    heading.focus({preventScroll:true});
    heading.scrollIntoView({block:"start", behavior:"instant"});
  });
  document.getElementById("tailLoadMoreBtn").addEventListener("click", () => fetchTailResults({append:true}).catch(() => {}));
  document.getElementById("tailClearFiltersBtn").addEventListener("click", clearTailResultFilters);
  document.getElementById("tailJobPicker").addEventListener("change", event => selectTailJob(event.target.value).catch(error => showTailError(error.message)));
  for (const id of ["tailTrustInput", "tailSortSelect", "tailDateFilter", "tailLatestOnlyInput", "tailMaxPriceInput", "tailResultProviderFilter"]) {
    document.getElementById(id).addEventListener(id === "tailMaxPriceInput" ? "input" : "change", () => renderTailResults(tailState.results.items));
  }
}


async function fetchTailCandidates() {
  const container = document.getElementById("tailCandidateList");
  if (!container) return;
  const response = await fetchApi("/api/tail-discovery/candidates");
  if (!response.ok) throw new Error("候选城市加载失败，请刷新页面重试");
  const data = await response.json();
  tailState.form.safeBatchSize = Number(data.safe_batch_size || 3);
  tailState.form.defaultProfiles = data.default_profiles || {};
  tailState.form.candidateNames = Object.fromEntries((data.candidates || []).map((item) => [String(item.code || "").toUpperCase(), item.name]));
  window.tailCandidateNames = tailState.form.candidateNames;
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

  if (tailState.form.savedCandidates) {
    const saved = new Set(tailState.form.savedCandidates);
    container.querySelectorAll("input").forEach(input => { input.checked = saved.has(input.value); });
    tailState.form.savedCandidates = null;
  }
  applyTailAirlineDefaults();
  tailState.form.candidatesLoaded = true;
  if (tailState.results.items.length) renderTailResults(tailState.results.items);
}

function selectedTailCandidates() {
  const excluded = excludedTailDestinationCodes();
  return Array.from(document.querySelectorAll('#tailCandidateList input[type="checkbox"]:checked'))
    .filter((input) => !excluded.has(input.value))
    .map((input) => input.value);
}

function tailDefaultCodesForAirlines(value) {
  if (tailState.form.candidateProfileOverride) {
    return tailState.form.defaultProfiles[tailState.form.candidateProfileOverride] || [];
  }
  const codes = new Set(String(value || "")
    .split(",")
    .map((item) => normalizeCode(item))
    .filter(Boolean));
  if (!codes.size) return tailState.form.defaultProfiles.DEFAULT || [];
  if (["CA", "ZH", "TV", "SC", "KY", "NX"].every((code) => codes.has(code))) {
    return tailState.form.defaultProfiles.AIR_CHINA_GROUP || [];
  }
  for (const code of ["CA", "CZ", "MU"]) {
    if (codes.has(code)) return tailState.form.defaultProfiles[code] || [];
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
  if (tailState.form.candidatesManual || !selectedCodes.length) {
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
      <small>${tailState.form.candidatesManual ? "自定义选择，不随航司自动重置" : "自动推荐"} · 已排除出发地和抵达城市</small>`;
    updateTailFormSummary();
  }
}

function updateTailSearchScope() {
  const scope = document.getElementById("tailSearchScopeInput");
  if (!scope) return;
  const both = document.getElementById("tailProviderSelect")?.value === "both";
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

  const feizhu = both || document.getElementById("tailProviderSelect")?.value === "feizhu";
  const bothNotice = document.getElementById("tailBothNotice");
  if (bothNotice) bothNotice.hidden = !both;
  const quickNote = document.getElementById("tailBothQuickNote");
  if (quickNote) quickNote.hidden = !both;
  const setField = (id, enabled) => {
    const input = document.getElementById(id);
    if (!input) return;
    input.disabled = !enabled;
    const label = input.closest?.("label");
    if (label) label.hidden = !enabled;
  };
  setField("tailSearchScopeInput", allowed);
  setField("tailFeizhuScopeInput", both);
  setField("tailFollowupBudgetInput", hybrid || both && document.getElementById("tailFeizhuScopeInput")?.value === "hybrid");
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
    ...(document.getElementById("tailProviderSelect")?.value === "both"
      ? {feizhu_search_scope: document.getElementById("tailFeizhuScopeInput")?.value || "open"} : {}),
    cabin: document.getElementById("tailCabinSelect").value,
    max_price: Number(document.getElementById("tailQueryMaxPriceInput")?.value || 0) || null,
    departure_time_start: document.getElementById("tailDepartureTimeStartInput")?.value || "",
    departure_time_end: document.getElementById("tailDepartureTimeEndInput")?.value || "",
    preferred_airlines: document.getElementById("tailAirlineInput")?.value || "",
    candidate_profile: tailState.form.candidateProfileOverride,
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
    max_provider_calls: ["feizhu", "both"].includes(document.getElementById("tailProviderSelect")?.value)
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
  tailState.connection.submitting = true;
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
    tailState.connection.submitting = false;
    updateTailBusyControls();
    if (!tailState.job.activeId) {
      updateTailCancelButton(null);
    }
  }
}

async function cancelActiveTailJob() {
  if (!tailState.job.activeId) return;
  const cancelButton = document.getElementById("tailCancelBtn");
  if (cancelButton) cancelButton.disabled = true;
  await handleTailJobAction("cancel", tailState.job.activeId);
  pollTailDiscoveryJob(tailState.job.activeId, true);
}
