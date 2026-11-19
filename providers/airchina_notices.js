// Read-only projection of the actual travel-notice modal observed 2026-10-30.
// Never capture inputs, iframe contents, HTML, cookies or challenge contents.
() => {
  const visible = el => {
    if (!el || !el.getClientRects().length) return false;
    for (let p = el; p; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (s.display === "none" || s.visibility === "hidden" || s.opacity === "0") return false;
    }
    return true;
  };
  const all = (root, selector) => [...root.querySelectorAll(selector)].filter(visible);
  const read = (root, selector) => {
    const nodes = all(root, selector);
    return nodes.length === 1 ? nodes[0].innerText.trim() : null;
  };
  const text = document.body.innerText;
  const verification = /安全验证|请点击上图中的|请完成验证|滑动滑块/.test(text);
  const blocked = /访问过于频繁|访问异常|访问被拒绝/.test(text);
  const stale = /停留时间过长/.test(text) && /价格/.test(text);
  const dialogs = all(document, '[class*="dialog_mainContent__"]');
  return {
    url: location.origin + location.pathname,
    verification, blocked, stale,
    dialog_count: dialogs.length,
    // A challenge may be on top of a notice; do not read its contents.
    dialogs: verification || blocked ? [] : dialogs.map(dialog => {
      const title = read(dialog, '[class*="model_header__"]');
      const body = read(dialog, '[class*="model_body__"]');
      const buttons = all(dialog, '[class*="model_footer__"] [class*="model_btn__"]');
      return {
        title: title?.slice(0, 300) ?? null,
        body: body?.slice(0, 16000) ?? null,
        truncated: (title?.length || 0) > 300 || (body?.length || 0) > 16000,
        buttons: buttons.map(b => b.innerText.trim().slice(0, 100)),
        disabled: buttons.some(b => b.hasAttribute("disabled") || b.getAttribute("aria-disabled") === "true"),
        has_inputs: all(dialog, 'input, select, textarea, [contenteditable="true"], [role="checkbox"]').length > 0,
      };
    }),
  };
}
