function tailQuoteFresh(item) {
  return Boolean(item.expires_at) && Date.parse(item.expires_at) > Date.now();
}

function tailQuoteVerified(item) {
  return tailQuoteFresh(item) && item.validation?.status === "valid";
}

function prepareTailResults(results) {
  let prepared = [...(results || [])];
  if (document.getElementById("tailLatestOnlyInput")?.checked) {
    const latest = new Map();
    for (const item of prepared) {
      const key = `${item.provider}|${item.itinerary_key || [item.provider, item.origin, item.transfer, item.destination,
        item.departure_date, item.cabin, item.passengers || 1, JSON.stringify(item.flight_details || [])].join("|")}`;
      const current = latest.get(key);
      if (!current || Date.parse(item.observed_at) > Date.parse(current.observed_at)
          || (item.observed_at === current.observed_at && Number(item.id) > Number(current.id))) latest.set(key, item);
    }
    prepared = [...latest.values()];
  }
  const date = document.getElementById("tailDateFilter")?.value || "";
  const max = Number(document.getElementById("tailMaxPriceInput")?.value || 0);
  const trust = document.getElementById("tailTrustInput")?.value || "all";
  const provider = document.getElementById("tailResultProviderFilter")?.value || "";
  return prepared.filter(item => (!date || item.departure_date === date)
    && (!provider || item.provider === provider)
    && (!max || Number(item.price) <= max)
    && (trust === "all" || (trust === "verified" ? tailQuoteVerified(item)
      : trust === "stale" ? !tailQuoteFresh(item) : tailQuoteFresh(item) && !tailQuoteVerified(item))))
    .sort(compareTailResults);
}

function renderTailResults(results) {
  const container = document.getElementById("tailResults");
  if (!container) return;
  const panel = document.getElementById("tailDiscoveryPanel");
  if (panel) panel.dataset.hasResults = String(tailState.results.items.length > 0);
  updateTailResultTools();
  const visible = prepareTailResults(results);
  renderTailSummary(visible, results);
  if (!visible.length) {
    let title, message, action = "";
    if (results.length) {
      title = "已有报价，但被列表筛选隐藏了";
      message = "调整上方日期、价格或报价状态即可，不需要重新搜索。";
      action = '<button type="button" class="ghost compact" data-tail-action="clear-filters">清除列表筛选</button>';
    } else if (tailState.results.view === "current" && !tailState.job.selectedId) {
      title = "先选择我要乘坐的行程";
      message = "填写出发地、首段抵达城市和日期，开始查询；也可以切到历史报价。";
    } else if (!tailState.results.loaded) {
      title = "正在读取已保存结果"; message = "这一步不会重新查询平台。";
    } else if (tailState.results.view === "current" && tailState.job.activeId === tailState.job.selectedId) {
      title = "正在查找，已完成的报价会逐步出现";
      message = tailState.connection.unknown ? "连接中断不代表任务已停止，正在重连。" : "你可以先查看任务进度，不必反复点击搜索。";
    } else {
      title = tailState.results.view === "history" ? "还没有已保存的历史报价" : "本次尚未确认符合条件的报价";
      message = tailState.results.view === "history" ? "开始一次搜索后，保存的结果会出现在这里。"
        : "不代表没有航班。查看上方任务原因，再决定重试或调整条件。旧版任务若无检查点，可在历史报价中查看。";
      action = '<button type="button" class="ghost compact" data-tail-action="edit">查看查询条件</button>';
    }
    replaceTailMarkup(container, `<div class="tail-empty-state"><strong>${title}</strong><span>${message}</span>${action}</div>`);
  } else {
    const groups = [
      ["已校验且未过期", visible.filter(tailQuoteVerified), "verified"],
      ["信息待核实 · 未过期", visible.filter(item => tailQuoteFresh(item) && !tailQuoteVerified(item)), "review"],
      ["已过期 / 时间未知", visible.filter(item => !tailQuoteFresh(item)), "stale"],
    ];
    replaceTailMarkup(container, groups.filter(([, rows]) => rows.length).map(([label, rows, state]) => {
      const content = groupTailResultsByRoute(rows).map(group => renderTailRouteGroup({...group, state})).join("");
      if (state !== "stale") return `<section class="tail-state-section"><h3>${label} <span>${rows.length}</span></h3>${content}</section>`;
      const open = document.getElementById("tailTrustInput")?.value === "stale" ? " open" : "";
      return `<details class="tail-state-section tail-stale-group" data-disclosure="stale-results"${open}>
        <summary data-focus-key="stale-results">${label} <span>${rows.length}</span><small>点击展开</small></summary>${content}</details>`;
    }).join(""));
    if (document.getElementById("tailTrustInput")?.value === "stale") {
      const staleGroup = container.querySelector(".tail-stale-group");
      if (staleGroup) staleGroup.open = true;
      tailState.results.disclosureState.set("stale-results", true);
    }
  }
  if (typeof updateTailBusyControls === "function") updateTailBusyControls();
  updateWorkspaceTabs();
}

function groupTailResultsByRoute(results) {
  const routeGroups = new Map();
  for (const item of results || []) {
    const routeKey = [item.origin || "", item.transfer || ""].join("|");
    if (!routeGroups.has(routeKey)) {
      routeGroups.set(routeKey, { origin: item.origin, transfer: item.transfer, results: [] });
    }
    routeGroups.get(routeKey).results.push(item);
  }
  return Array.from(routeGroups.values()).map((group) => {
    const byDate = new Map();
    for (const item of group.results) {
      const date = item.departure_date || "未标日期";
      if (!byDate.has(date)) byDate.set(date, []);
      byDate.get(date).push(item);
    }
    const dateGroups = Array.from(byDate.entries())
      .map(([date, items]) => ({
        date,
        items: items.sort(compareTailResults),
      }))
      .sort((left, right) => String(left.date).localeCompare(String(right.date)));
    const best = [...group.results].sort(compareTailResults)[0] || null;
    return { ...group, dateGroups, best };
  }).sort((left, right) => {
    const comparison = compareTailResults(left.best, right.best);
    if (comparison !== 0) return comparison;
    return `${left.origin || ""}${left.transfer || ""}`.localeCompare(`${right.origin || ""}${right.transfer || ""}`);
  });
}

function safeTailReviewUrl(value) {
  try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) ? url.href : ""; }
  catch { return ""; }
}

function tailQuoteTermsLabel(terms) {
  terms = terms || {};
  const basis = {per_person: "每人", party_total: "全体乘客总价"}[terms.price_basis] || "每人/总价未确认";
  const tax = terms.tax_included === true ? "含税" : terms.tax_included === false ? "不含税" : "税费未确认";
  return `${basis} · ${tax} · 请求 ${terms.requested_passengers || 1} 人 · ${terms.inventory_verified ? "人数库存已校验" : "人数库存未核实"}`;
}

function tailComparableBaseline(item, key) {
  const baseline = item.baselines?.[key], terms = item.quote_terms || {}, other = baseline?.quote_terms || {};
  if (!tailQuoteVerified(item) || baseline?.status !== "comparable" || baseline.reasons?.length
      || baseline.savings_amount == null || !Number.isFinite(Number(baseline.savings_amount))
      || !Number.isFinite(Number(baseline.price)) || Number(baseline.price) <= 0
      || !Number.isFinite(Number(item.price)) || Number(item.price) <= 0) return null;
  // Public metadata from legacy rows may be incomplete. Never promote its
  // top-level savings_amount alone into a confirmed, current comparison.
  for (const field of ["currency", "price_basis", "tax_included", "priced_passengers"]) {
    if (terms[field] == null || terms[field] === "unknown" || terms[field] !== other[field]) return null;
  }
  if (!terms.currency || terms.currency !== item.currency || Number(terms.priced_passengers) <= 0) return null;
  if (Number(item.passengers || 1) > 1 && (!terms.inventory_verified || !other.inventory_verified
      || Number(terms.priced_passengers) !== Number(item.passengers))) return null;
  if (key === "same_flight" && (!terms.fare_conditions || terms.fare_conditions !== other.fare_conditions)) return null;
  if (Math.abs(Number(baseline.price) - Number(item.price) - Number(baseline.savings_amount)) > .011) return null;
  return baseline;
}

function tailDecisionDifference(item) {
  if (!tailQuoteFresh(item)) return "历史报价，先复核再比较";
  for (const [key, label] of [["same_flight", "同首段单买"], ["market", "符合筛选条件的直飞价"]]) {
    const baseline = tailComparableBaseline(item, key);
    if (!baseline) continue;
    const amount = Number(baseline.savings_amount);
    return amount === 0 ? `与${label}相同 · 同口径参考`
      : `比${label}${amount > 0 ? "低" : "高"} ${item.currency} ${Math.abs(amount).toFixed(2).replace(/\.00$/, "")} · 同口径参考`;
  }
  return "无法判断能省多少";
}

function renderTailBaselines(item) {
  return [["market", "符合条件的直飞最低价"], ["same_flight", "同首段航班单买价"]].map(([key, label]) => {
    const value = item.baselines?.[key];
    if (!value || value.price === null) return `<small>${label}：暂无可比基准</small>`;
    const detail = tailComparableBaseline(item, key) ? `可比价差 ${Number(value.savings_amount).toFixed(2)}` : "仅供参考，价差待复核";
    return `<small>${label}：${escapeHtml(item.currency || "CNY")} ${Number(value.price).toFixed(0)} · ${detail}</small>
      <small>${escapeHtml((value.reasons || []).join("；"))}</small>`;
  }).join("");
}

function tailFullRouteLabel(item) {
  const details = item.flight_details || [];
  const stops = [];
  for (const leg of details) {
    const from = leg.departure_airport || leg.origin || "待核实";
    const to = leg.arrival_airport || leg.destination || "待核实";
    if (stops[stops.length - 1] !== from) stops.push(from);
    stops.push(to);
  }
  return details.length > 1 ? stops.map(tailPlaceLabel).join(" → ")
    : `${tailPlaceLabel(item.origin)} → ${tailPlaceLabel(item.transfer)} → ${tailPlaceLabel(item.destination)}`;
}

function tailItineraryKey(item) {
  const legs = item.flight_details || [];
  const confirmed = legs.length > 1 && legs.every(leg => leg.flight_no
    && (leg.departure_airport || leg.origin) && (leg.arrival_airport || leg.destination));
  const identity = confirmed ? legs.map(leg => [
    String(leg.flight_no).toUpperCase(),
    String(leg.departure_airport || leg.origin).toUpperCase(),
    String(leg.arrival_airport || leg.destination).toUpperCase(),
    leg.departure_date || item.departure_date || "", leg.departure_time || "",
    leg.arrival_time || "", leg.cabin || item.cabin || "",
  ].join(":")).join("|") : item.itinerary_key || item.id || `${item.provider}|${item.destination}|${JSON.stringify(legs)}`;
  return [identity, item.currency, item.passengers || 1, item.quote_terms?.price_basis || "unknown"].join("|");
}

function renderTailItineraryGroups(items) {
  const groups = new Map();
  for (const item of items) {
    const key = tailItineraryKey(item);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  return [...groups.entries()].map(([groupKey, rows]) => {
    rows.sort(compareTailResults);
    const first = rows[0];
    const details = first.flight_details || [];
    const valid = (first.validation?.status || first.raw_payload?.validation?.status) === "valid";
    const providers = [...new Set(rows.map(item => item.provider))].sort();
    const featured = providers.map(provider => rows.find(item => item.provider === provider));
    const alternatives = rows.filter(item => !featured.includes(item));
    const otherProvider = first.provider === "ctrip" ? "feizhu" : "ctrip";
    const dual = tailState.job.selected?.payload?.provider === "both" || providers.length > 1;
    const key = escapeHtml(groupKey);
    return `<section class="tail-itinerary-option ${providers.length > 1 ? "has-multiple-providers" : ""}" data-itinerary-key="${key}">
      <div class="tail-itinerary-heading"><div><small>${valid && details.length > 1 ? "完整购票行程" : "候选购票行程 · 待核实"}</small>
        <strong>${escapeHtml(tailFullRouteLabel(first))}</strong></div>
        <span>${providers.length > 1 ? "同行程平台独立报价" : escapeHtml(providerLabel(first.provider))}</span></div>
      ${dual && providers.length === 1 ? `<p class="tail-missing-provider">${escapeHtml(providerLabel(otherProvider))}暂无同行程报价</p>` : ""}
      <div class="tail-itinerary-quotes">${featured.map((item, index) => renderTailResultCard(item, index + 1, false)).join("")}</div>
      ${alternatives.length ? `<details class="tail-itinerary-more" data-disclosure="itinerary-${key}"><summary data-focus-key="itinerary-${key}">同行程其他 ${alternatives.length} 条已存报价</summary>
        ${alternatives.map((item, index) => renderTailResultCard(item, index + featured.length + 1, false)).join("")}</details>` : ""}
    </section>`;
  });
}

function renderTailFlightGroups(items, state = "") {
  const groups = new Map();
  for (const item of items) {
    const leg = (item.flight_details || [])[0] || {};
    const key = [state, item.departure_date, item.first_leg_key || item.itinerary_key || item.id,
      item.currency, item.passengers || 1, item.quote_terms?.price_basis || "unknown"].join("|");
    if (!groups.has(key)) groups.set(key, {leg, items:[], key});
    groups.get(key).items.push(item);
  }
  return [...groups.values()].map(group => {
    group.items.sort(compareTailResults);
    const leg = group.leg;
    const options = renderTailItineraryGroups(group.items);
    const key = escapeHtml(group.key);
    return `<section class="tail-flight-group" data-stable-key="${key}">
      <div class="tail-flight-heading">
        <div><p class="tail-first-leg-label">我要乘坐 · ${escapeHtml(tailPlaceLabel(group.items[0].origin))} → ${escapeHtml(tailPlaceLabel(group.items[0].transfer))}</p>
        <h4>${escapeHtml(leg.flight_no || "航班号待确认")} <span>${escapeHtml(leg.departure_time || "--:--")} → ${escapeHtml(leg.arrival_time || "--:--")}</span></h4>
        <p class="muted">${escapeHtml(airportCodeDisplayText(leg.departure_airport || leg.origin))} → ${escapeHtml(airportCodeDisplayText(leg.arrival_airport || leg.destination))}</p></div>
        <div class="tail-flight-best"><small>${options.length} 条购票路线 · ${group.items.length} 条平台报价</small><span>按完整行程比较</span></div>
      </div>
      ${options[0] || ""}
      ${options.length > 1 ? `<details class="tail-alternatives" data-disclosure="alternatives-${key}"><summary data-focus-key="alternatives-${key}">比较其他 ${options.length - 1} 条购票路线</summary>
      ${options.slice(1).join("")}</details>` : ""}
    </section>`;
  }).join("");
}

function renderTailResultCard(item, index, showRoute = true) {
  const key = escapeHtml(String(item.id || item.itinerary_key || `${item.destination}-${index}`));
  const reviewUrl = safeTailReviewUrl(item.review_url || item.search_url || "");
  const valid = (item.validation?.status || item.raw_payload?.validation?.status) === "valid";
  const fresh = tailQuoteFresh(item);
  const scope = item.filter_scope === "first_leg" ? "舱位／航司仅约束我要乘坐的航段"
    : item.filter_scope === "all_legs" ? "舱位／航司约束完整购票行程" : "";
  const legs = (item.flight_details || []).map((leg, i) => `
    <div class="tail-leg-row"><span class="tail-leg-index">${i+1}</span><div>
      <strong>${escapeHtml(airportCodeDisplayText(leg.departure_airport || leg.origin))} → ${escapeHtml(airportCodeDisplayText(leg.arrival_airport || leg.destination))}</strong>
      <span>${escapeHtml(leg.departure_date || item.departure_date)} · ${escapeHtml(leg.departure_time || "--:--")} → ${escapeHtml(leg.arrival_time || "--:--")} · ${escapeHtml(leg.flight_no || "航班号待确认")}</span>
      <span>${escapeHtml(cabinLabel(leg.cabin || item.cabin))}${leg.transfer_layover ? ` · 中转停留 ${escapeHtml(leg.transfer_layover)}` : ""}</span></div></div>`).join("");
  const difference = tailDecisionDifference(item);
  const details = item.flight_details || [];
  const status = !fresh ? "已过期 / 时间未知" : valid ? "航段已核对 · 价格未复核" : "航段或报价待核实";
  const passengers = item.quote_terms?.requested_passengers || item.passengers;
  const terms = `${{per_person:"每人", party_total:"全体乘客总价"}[item.quote_terms?.price_basis] || "每人/总价未确认"} · ${item.quote_terms?.tax_included === true ? "含税" : item.quote_terms?.tax_included === false ? "不含税" : "税费未确认"} · ${passengers ? `${passengers} 人` : "人数未知"} · ${item.quote_terms?.inventory_verified ? "人数库存已校验" : "人数库存未核实"}`;
  return `<article class="tail-result-card" data-provider="${escapeHtml(item.provider || "unknown")}">
    <div class="tail-result-summary">
      <div class="tail-result-main">${showRoute
        ? `<small>${valid && details.length > 1 ? "实际购票行程" : "候选购票行程 · 待核实"} · ${escapeHtml(providerLabel(item.provider))}</small><strong>${escapeHtml(tailFullRouteLabel(item))}</strong>`
        : `<small>报价平台</small><strong>${escapeHtml(providerLabel(item.provider))}</strong>`}
        <span class="muted">${escapeHtml(cabinLabel(item.cabin))}</span>
        ${item.suitability?.airport_change ? '<span class="tail-warning">涉及换机场</span>' : ""}</div>
      <div class="tail-price"><strong>${escapeHtml(item.currency || "CNY")} ${Number(item.price || 0).toFixed(0)}</strong>
        <small>${item.provider === "feizhu" ? "平台参考价 · 总价未核实" : "组合参考报价"}</small>
        <small>${escapeHtml(terms)}</small>
      </div>
    </div>
    <div class="tail-result-decision">
      <span class="tail-decision-difference">${escapeHtml(difference)}</span>
      <div class="tail-quote-state"><span class="tail-status ${valid && fresh ? "verified" : "review"}">${status}</span>
        <small>${item.observed_at ? `采集 ${escapeHtml(formatQueryTime(item.observed_at))}` : "采集时间未知"}</small>
      </div>
      <div class="tail-review-actions">
        ${item.id ? `<button type="button" class="ghost compact" data-tail-action="recheck" data-result-id="${key}" data-focus-key="recheck-${key}">重新查此行程</button>` : ""}
        ${reviewUrl ? `<a class="ghost compact tail-review-link" href="${escapeHtml(reviewUrl)}" target="_blank" rel="noopener noreferrer">去${escapeHtml(providerLabel(item.provider))}核实</a>` : ""}
      </div>
    </div>
    ${item.recheck_outcome?.status === "failed" ? "<p class='tail-recheck-note'>最近复核失败，旧价待确认</p>" : ""}
    ${["inconclusive", "not_found"].includes(item.recheck_outcome?.status) ? "<p class='tail-recheck-note'>最近复核无法确认原行程，可重试；不代表已下架</p>" : ""}
    <p class="tail-card-caveat">价格及出票需复核 · 行李、票规及后续航段影响未核实</p>
    <details class="tail-details" data-disclosure="quote-${key}">
      <summary data-focus-key="quote-details-${key}">完整行程、价差与待核实事项</summary>
      <p>${escapeHtml(tailPlaceLabel(item.origin))} → ${escapeHtml(tailPlaceLabel(item.transfer))} → ${escapeHtml(tailPlaceLabel(item.destination))} · ${escapeHtml(scope)}</p>
      ${item.search_metadata?.system_message ? `<p>平台提示：${escapeHtml(item.search_metadata.system_message)}</p>` : ""}
      ${item.provider === "feizhu" ? `<p>平台 ${escapeHtml(item.quote_terms?.source_price_field || "ticketPrice")} · 整段总价未核实。CLI 返回结果不保证完整；未返回不代表无航线。</p>` : ""}
      <div class="tail-baselines">${renderTailBaselines(item)}
        <p>${escapeHtml(difference)}（非购买承诺）</p>
        ${!tailComparableBaseline(item, "market") && !tailComparableBaseline(item, "same_flight") ? "<p>口径未确认，不计算节省金额；历史报价须重新复核。</p>" : ""}</div>
      <p>${escapeHtml(tailQuoteTermsLabel(item.quote_terms))}</p>
      <p>行李：未核实；票规：未核实；后续行程影响：未核实。搜索结果不代表可出票。</p>
      <div class="tail-leg-list">${legs || "暂无可确认的航段明细"}</div>
    </details>
  </article>`;
}

function tailResultConfidence(item) {
  const validationStatus = item.validation?.status || item.raw_payload?.validation?.status || "";
  if (validationStatus === "invalid") return { level: "low", label: "硬校验失败" };
  if (validationStatus === "needs_review") return { level: "low", label: "需复核" };
  const score = tailResultConfidenceScore(item);
  if (score >= 80) return { level: "high", label: "高可信" };
  if (score >= 55) return { level: "medium", label: "中可信" };
  return { level: "low", label: "需复核" };
}

function tailResultConfidenceScore(item) {
  let score = 35;
  const details = item.flight_details || [];
  const source = item.detail_source || item.raw_payload?.detail_source || "";
  const quality = item.detail_quality || item.raw_payload?.detail_quality || "";
  if (source === "network_exact") score += 35;
  if (source === "dom_transit_card") score += 25;
  if (source === "text_fallback") score += 10;
  if (quality === "complete") score += 20;
  if (details.length >= 2) score += 15;
  if (details.some((leg) => leg.flight_no)) score += 8;
  if (details.some((leg) => String(leg.tail_transfer || "").toUpperCase() === String(item.transfer || "").toUpperCase())) score += 8;
  if (item.date_evidence?.matched_source) score += 5;
  if ((item.validation?.status || item.raw_payload?.validation?.status) === "valid") score += 12;
  if ((item.validation?.status || item.raw_payload?.validation?.status) === "needs_review") score -= 20;
  return Math.max(0, Math.min(score, 100));
}

function renderTailRouteGroup(group) {
  return `<section class="tail-route-group">
    <div class="tail-route-group-head"><strong>${escapeHtml(tailPlaceLabel(group.origin))} → ${escapeHtml(tailPlaceLabel(group.transfer))}</strong>
    <span>${group.results.length} 条组合报价</span></div>
    ${group.dateGroups.map(date => `<div class="tail-date-group"><h4>${escapeHtml(date.date)}</h4>
      ${renderTailFlightGroups(date.items, group.state || "")}</div>`).join("")}
  </section>`;
}

function renderTailSummary(visibleResults, allResults) {
  const summary = document.getElementById("tailSummary");
  if (!summary) return;
  const dateFilter = document.getElementById("tailDateFilter")?.value || "";
  const maxPrice = Number(document.getElementById("tailMaxPriceInput")?.value || 0);
  if (!visibleResults.length) {
    summary.textContent = allResults.length ? "当前筛选下暂无结果"
      : tailState.results.view === "history" ? "暂无已保存的历史报价"
        : !tailState.job.selectedId ? "尚未开始查询"
          : !tailState.results.loaded ? "正在读取已保存结果"
            : tailState.job.activeId === tailState.job.selectedId ? "查询进行中，尚未保存符合条件的报价"
              : "本次暂未保存符合条件的报价";
    return;
  }
  const groups = groupTailResultsByRoute(visibleResults);
  const destinations = new Set(visibleResults.map((item) => item.destination));
  const filters = [
    dateFilter ? `日期 ${dateFilter}` : "",
    maxPrice > 0 ? `价格≤¥${maxPrice.toFixed(0)}` : "",
  ].filter(Boolean).join(" · ");
  summary.textContent = `${filters ? `${filters} · ` : ""}${groups.length} 条主航线，${visibleResults.length} 条结果 / ${destinations.size} 个目的地`;
}
