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

async function fetchApi(url, options = {}) {
  const { timeoutMs, ...fetchOptions } = options;
  if (!timeoutMs) return fetch(url, fetchOptions);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...fetchOptions, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}
