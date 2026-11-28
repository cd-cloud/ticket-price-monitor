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
  warning: ["口径不同", "warning"],
  expired: ["日期较早", "expired"],
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

function confirmAction(message, options = {}) {
  if (typeof document === "undefined" || !document.body) {
    return Promise.resolve(typeof window !== "undefined" && typeof window.confirm === "function"
      ? window.confirm(message)
      : true);
  }
  return new Promise((resolve) => {
    let dialog = document.getElementById("appConfirmDialog");
    if (!dialog) {
      dialog = document.createElement("div");
      dialog.id = "appConfirmDialog";
      dialog.className = "app-confirm-overlay";
      dialog.hidden = true;
      dialog.innerHTML = `
        <div class="app-confirm-backdrop" data-confirm-cancel></div>
        <section class="app-confirm-panel" role="alertdialog" aria-modal="true" aria-labelledby="appConfirmTitle" aria-describedby="appConfirmMessage">
          <h3 id="appConfirmTitle">请确认操作</h3>
          <p id="appConfirmMessage"></p>
          <div class="app-confirm-actions">
            <button type="button" class="ghost" data-confirm-cancel>取消</button>
            <button type="button" class="primary" data-confirm-ok>确认</button>
          </div>
        </section>
      `;
      document.body.appendChild(dialog);
    }
    const previousFocus = document.activeElement;
    const titleEl = dialog.querySelector("#appConfirmTitle");
    const msgEl = dialog.querySelector("#appConfirmMessage");
    const okBtn = dialog.querySelector("[data-confirm-ok]");
    const cancelBtn = dialog.querySelector("button[data-confirm-cancel]");
    titleEl.textContent = options.title || "请确认操作";
    msgEl.textContent = message;
    okBtn.textContent = options.confirmText || "确认";
    okBtn.className = options.danger ? "primary danger-fill" : "primary";
    dialog.hidden = false;

    const finish = (accepted) => {
      dialog.hidden = true;
      dialog.removeEventListener("click", onClick);
      document.removeEventListener("keydown", onKeydown);
      if (previousFocus && previousFocus.isConnected && typeof previousFocus.focus === "function") {
        previousFocus.focus({ preventScroll: true });
      }
      resolve(accepted);
    };
    const onClick = (event) => {
      if (event.target.closest("[data-confirm-ok]")) finish(true);
      else if (event.target.closest("[data-confirm-cancel]")) finish(false);
    };
    const onKeydown = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        finish(false);
      }
    };
    dialog.addEventListener("click", onClick);
    document.addEventListener("keydown", onKeydown);
    (options.danger ? cancelBtn : okBtn).focus({ preventScroll: true });
  });
}

function cleanVisibleText(_root = document.body) {
  // Retained as a no-op compatibility helper; display text is normalized at render time.
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
