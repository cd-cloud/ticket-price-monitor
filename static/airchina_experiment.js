/* Independent reference-only workspace. No integration with quote ranking. */
(() => {
  const panel = document.getElementById("airchinaExperimentPanel");
  if (!panel) return;
  const form = document.getElementById("airchinaExperimentForm");
  const select = document.getElementById("airchinaExperimentJobs");
  const status = document.getElementById("airchinaExperimentStatus");
  const results = document.getElementById("airchinaExperimentResults");
  const error = document.getElementById("airchinaExperimentError");
  const resume = document.getElementById("airchinaExperimentContinue");
  const cancel = document.getElementById("airchinaExperimentCancel");
  const submit = form.querySelector("[type=submit]");
  const active = new Set(["queued", "running", "waiting_user", "cancelling"]);
  const labels = {
    queued: "已排队", running: "读取中", waiting_user: "等待你处理",
    cancelling: "取消中", cancelled: "已取消", timed_out: "已超时", failed: "失败",
    interrupted: "应用重启中断", completed_reference: "已取得参考票价", inconclusive: "未取得可靠结果",
  };
  let jobs = [], selected = "", generation = 0, busy = false, uncertain = true, timer;
  submit.disabled = true;
  const base = "/api/experiments/airchina";
  const today = new Date();
  form.elements.departure_date.min = `${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,"0")}-${String(today.getDate()).padStart(2,"0")}`;

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

  function render(job) {
    results.replaceChildren();
    const running = jobs.some(j => active.has(j.status)) || (job && active.has(job.status));
    submit.disabled = busy || uncertain || running;
    resume.hidden = !job || job.status !== "waiting_user";
    cancel.hidden = !job || !active.has(job.status);
    resume.disabled = cancel.disabled = busy || uncertain || job?.status === "cancelling";
    if (!job) { status.textContent = "尚未选择任务。"; return; }
    status.textContent = `${labels[job.status] || job.status} · ${job.request.origin} → ${job.request.destination} · ${job.request.departure_date}\n${job.message}\n更新时间：${new Date(job.updated_at).toLocaleString()}${active.has(job.status) ? `；截止：${new Date(job.deadline_at).toLocaleTimeString()}` : ""}`;
    if (job.status !== "completed_reference") return;
    const report = job.result || {};
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
      jobs = data.jobs;
      uncertain = false;
      if (!jobs.some(j => j.job_id === selected)) selected = jobs[0]?.job_id || "";
      select.replaceChildren();
      for (const job of jobs) {
        const option = new Option(`${job.request.origin} → ${job.request.destination} ${job.request.departure_date} · ${labels[job.status] || job.status} · ${new Date(job.created_at).toLocaleTimeString()}`, job.job_id);
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
      resume.disabled = cancel.disabled = submit.disabled = true;
      // Do not silently infer completion or issue another POST.
    }
  }

  async function mutate(url, payload) {
    if (busy) return;
    busy = true; ++generation; clearTimeout(timer);
    submit.disabled = resume.disabled = cancel.disabled = true;
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
      resume.disabled = cancel.disabled = submit.disabled = true;
    } finally { busy = false; }
  }

  form.addEventListener("submit", event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const payload = Object.fromEntries(new FormData(form));
    if (!payload.flight_no) delete payload.flight_no;
    mutate(base, payload);
  });
  select.addEventListener("change", () => {
    selected = select.value;
    render(jobs.find(j => j.job_id === selected));
  });
  document.getElementById("airchinaExperimentRefresh").addEventListener("click", refresh);
  resume.addEventListener("click", () => mutate(`${base}/${encodeURIComponent(selected)}/continue`));
  cancel.addEventListener("click", () => mutate(`${base}/${encodeURIComponent(selected)}/cancel`));
  refresh();
})();
