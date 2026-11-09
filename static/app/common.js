const AIRPORT_SUGGESTIONS = window.AIRPORT_SUGGESTIONS || [];

const CABIN_LABELS = {
  economy_plus: "经济舱/超级经济舱",
  business_first: "商务舱/头等舱",
  economy: "经济舱",
  premium_economy: "超级经济舱",
  business: "商务舱",
  first: "头等舱",
};

const STATUS_LABELS = {
  ok: ["已查询", "ok"],
  scheduled: ["已排程", "scheduled"],
  error: ["未更新", "idle"],
  expired: ["已过期", "expired"],
  idle: ["未查询", "idle"],
};

const ACTIVE_TAIL_JOB_STATUSES = ["queued", "running", "needs_verification"];

function cabinLabel(value) {
  return CABIN_LABELS[value] || value || "-";
}

const TRANSFER_POLICY_LABELS = {
  any: "可转机",
  direct: "仅直飞",
  direct_only: "仅直飞",
  transfer_only: "仅中转",
};

function transferPolicyLabel(value) {
  return TRANSFER_POLICY_LABELS[value] || value || "-";
}

function cleanDisplayText(value) {
  return String(value ?? "")
    .replaceAll("→", "->");
}

function escapeHtml(value) {
  return cleanDisplayText(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

let toastTimer = null;

function notifyComplete(message, options = {}) {
  let toast = document.getElementById("completionToast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "completionToast";
    toast.className = "completion-toast";
    toast.setAttribute("role", "status");
    toast.setAttribute("aria-live", "polite");
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  toast.classList.toggle("is-warning", options.kind === "warning");
  toast.hidden = false;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    toast.hidden = true;
  }, options.durationMs || 2200);
}

function renderSessionHints(hints = []) {
  const loginHints = hints.filter((hint) => hint?.needs_login && hint.message);
  const signature = JSON.stringify(loginHints.map((hint) => [hint.provider, hint.reason, hint.message]));
  if (!loginHints.length || signature === dashboardState.sessionHintSignature) return;
  dashboardState.sessionHintSignature = signature;
  notifyComplete(loginHints[0].message, { kind: "warning", durationMs: 5200 });
}

function cleanVisibleText(root = document.body) {
  if (!root) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    const cleaned = cleanDisplayText(node.nodeValue);
    if (cleaned !== node.nodeValue) {
      node.nodeValue = cleaned;
    }
  }
}

function normalizeAirlines(text) {
  return text.split(",").map((item) => item.trim().toUpperCase()).filter(Boolean);
}

function normalizeCode(value) {
  return resolveAirportCode(value);
}

function parseCityList(value) {
  return String(value || "")
    .split(/[\/,，、;\s]+/)
    .map((item) => normalizeCode(item))
    .filter(Boolean)
    .filter((item, index, array) => array.indexOf(item) === index);
}
