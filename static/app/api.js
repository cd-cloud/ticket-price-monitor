async function responseErrorMessage(response, fallback) {
  const text = await response.text();
  if (!text) return fallback;
  try {
    const data = JSON.parse(text);
    return data.detail || data.error || fallback;
  } catch (_error) {
    return text;
  }
}

function resolveUiToken() {
  if (typeof window === "undefined") return "";
  try {
    const queryToken = new URLSearchParams(window.location?.search || "").get("token");
    if (queryToken) {
      try {
        window.sessionStorage?.setItem("flight_tracker_ui_token", queryToken);
      } catch (_error) {
        // Storage may be unavailable in restricted contexts.
      }
      return queryToken;
    }
    return window.sessionStorage?.getItem("flight_tracker_ui_token") || "";
  } catch (_error) {
    return "";
  }
}

function withUiTokenOptions(options) {
  const token = resolveUiToken();
  if (!token) return options;
  const headers = { ...(options.headers || {}) };
  const hasTokenHeader = Object.keys(headers).some(
    (key) => key.toLowerCase() === "x-flight-tracker-token",
  );
  if (!hasTokenHeader) {
    headers["X-Flight-Tracker-Token"] = token;
  }
  return { ...options, headers };
}

async function fetchApi(url, options = {}) {
  const { timeoutMs, ...rawFetchOptions } = options;
  const fetchOptions = withUiTokenOptions(rawFetchOptions);
  if (!timeoutMs) return fetch(url, fetchOptions);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...fetchOptions, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}
