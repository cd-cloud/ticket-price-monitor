// Read-only DOM projection. No React state, storage, cookies, network, or clicks.
// Class stems observed on the public one-way page, 2026-10-30.
() => {
  const visible = el => {
    if (!el || !el.getClientRects().length) return false;
    for (let p = el; p; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (s.display === "none" || s.visibility === "hidden" || s.opacity === "0") return false;
    }
    return true;
  };
  const all = (root, selector) => Array.from(root.querySelectorAll(selector)).filter(visible);
  const text = el => el ? el.innerText.trim() : null;
  const one = (root, selector) => {
    const found = all(root, selector);
    return found.length === 1 ? found[0] : null;
  };
  const read = (root, stem) => text(one(root, `[class*="${stem}"]`));
  const cabinTypes = ["first", "business", "superEconomy", "economy"];
  const body = document.body.innerText;
  const headers = all(document, '[class*="style_resultInfoBox__"]');
  const header = headers.length === 1 ? headers[0] : null;
  return {
    schema: "airchina_visible_oneway_v1",
    url: location.origin + location.pathname, // Never capture tokenized URL fields.
    verification_visible: /安全验证|请点击上图中的|请完成验证|滑动滑块/.test(body),
    blocked_visible: /访问过于频繁|访问异常|访问被拒绝/.test(body),
    stale_visible: /停留时间过长/.test(body) && /价格/.test(body),
    loading_visible: /正在加载中，请稍候/.test(body),
    notice_confirmation_visible: /确认，查看下一个提示/.test(body),
    header_count: headers.length,
    route_text: header ? text(one(header, '[class*="style_cityTest__"]')) : null,
    date_text: header ? read(header, "style_dateBox__") : null,
    trip_type: header ? read(header, "style_tripType__") : null,
    passengers_text: header ? read(header, "style_passengerBox__") : null,
    price_notice: read(document, "searchTitle_peiceTips__"),
    cabin_headers: Object.fromEntries(cabinTypes.map(cabin => [
      cabin, read(document, `cabinTitle_${cabin}__`)
    ])),
    rows: all(document, '[class*="style_singleFightWrapper__"]').map(row => ({
      flight_numbers: all(row, '[class*="shared-flights_number__"]').map(text),
      departure_time: read(row, "baseContent_departure__"),
      arrival_time: read(row, "baseContent_arrive__"),
      departure_airport: read(row, "baseContent_airport-departure__"),
      arrival_airport: read(row, "baseContent_airport-arrive__"),
      duration: read(row, "baseContent_totalTime__"),
      between_text: read(row, "baseContent_between__"),
      cabins: Object.fromEntries(cabinTypes.map(cabin => {
        const cell = one(row, `[class*="cabinContent_${cabin}__"]`);
        return [cabin, cell ? {
          price_text: read(cell, "cabinContent_price__"),
          cell_text: text(cell),
        } : null];
      })),
      expanded_cabins: cabinTypes.filter(cabin => {
        const cell = one(row, `[class*="cabinContent_${cabin}__"]`);
        return cell && cell.className.includes("cabinContent_activeCabin__");
      }),
      products: all(row, '[class*="cabinDetails_singleDetail__"]').map(product => ({
        name: read(product, "cabinDetails_priceCode__"),
        seat_class_text: read(product, "cabinDetails_cabinClass__"),
        price_text: read(product, "cabinDetails_price__"),
        price_block_text: read(product, "cabinDetails_price-wrap__"),
        summaries: all(product, '[class*="cabinDetails_cabinInfo__"]').map(info => ({
          label: read(info, "cabinDetails_des__"),
          value: read(info, "cabinDetails_value__"),
        })),
        // The live page has a transparent "剩余0张" layout placeholder.
        // AX/innerText includes it: never interpret it as stock evidence.
        seat_text: all(product, '[class*="cabinDetails_seatWrapper__"] span')
          .filter(el => !el.className.includes("seatPlaceholder")
            && !/rgba\([^)]*,\s*0\)/.test(getComputedStyle(el).color)
            && getComputedStyle(el).color !== "transparent")
          .map(text).filter(Boolean).join(" ") || null,
      })),
    })),
  };
}
