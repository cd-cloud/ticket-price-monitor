// Read-only form projection, based on public controls observed 2026-10-30.
// No React state, storage, cookies, network, calendar fares or account fields.
(expected = {}) => {
  const visible = el => {
    if (!el || !el.getClientRects().length) return false;
    for (let p = el; p; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (s.display === "none" || s.visibility === "hidden" || s.opacity === "0") return false;
    }
    return true;
  };
  const all = (root, selector) => [...root.querySelectorAll(selector)].filter(visible);
  const one = (root, selector) => {
    const es = all(root, selector);
    return es.length === 1 ? es[0] : null;
  };
  const text = el => el ? el.innerText.trim() : null;
  const disabled = el => !el || el.disabled === true || el.getAttribute("aria-disabled") === "true"
    || /disabled/i.test(el.className);
  const airport = (label, code) => {
    const es = all(document, '[class*="input_inputContainer__"]').filter(el =>
      text(one(el, '[class*="input_imageMargin__"]')) === label ||
      all(el, 'input').some(input => input.getAttribute("placeholder") === label));
    const root = es.length === 1 ? es[0] : null;
    const input = root && one(root, 'input');
    const candidates = all(document, 'li[class*="CitySelector_airportItem__"]');
    const inside = root ? candidates.filter(el => root.contains(el)) : [];
    const exact = es => typeof code === "string" ? es.filter(el =>
      all(el, '[class*="CitySelector_code__"]').some(e => text(e) === code)).length : 0;
    return {
      count: es.length, input_count: root ? all(root, 'input').length : 0,
      value: input ? input.value : null,
      code: root ? text(one(root, '[class*="input_text__"]')) : null,
      diagnostics: {
        root_count: es.length,
        input_count: root ? all(root, 'input').length : 0,
        picker_ready: !!root && !!input && document.activeElement === input &&
          all(root, '[class*="CitySelector_cityPickerContainer__"]').length === 1 &&
          all(root, 'li[class*="CitySelectorContent_contentCitysItem__"], li[class*="CitySelector_airportItem__"]').length > 0,
        input_matches: input && typeof code === "string" ? input.value === code : null,
        input_focused: input ? document.activeElement === input : null,
        input_enabled: input ? !disabled(input) && !input.readOnly : null,
        selected_matches: root && typeof code === "string" ?
          text(one(root, '[class*="input_text__"]')) === code : null,
        scoped_candidates: Math.min(inside.length, 100),
        page_candidates: Math.min(candidates.length, 100),
        scoped_exact: Math.min(exact(inside), 100),
        page_exact: Math.min(exact(candidates), 100),
        counts_capped: candidates.length > 100,
      },
    };
  };
  const passengerRoots = all(document, '[class*="select_container__"]');
  const passengerRoot = title => {
    const es = passengerRoots.filter(el => text(one(el, '[class*="select_title__"]')) === title);
    return es.length === 1 ? es[0] : null;
  };
  const passengers = passengerRoot("乘客类型");
  const special = passengerRoot("伤残军警乘客");
  const summary = root => root ? text(one(root, '[class*="select_contentContainer__"]')) : null;
  const current = one(document, '#tabCurrent');
  const dateInput = one(document, 'input[placeholder="出发日期"]');
  const submit = all(document, '[class*="searchCore_searchButton__"]');
  const body = document.body.innerText;
  return {
    schema: "airchina_form_v1",
    url: location.origin + location.pathname,
    verification: /安全验证|请点击上图中的|请完成验证|滑动滑块/.test(body),
    blocked: /访问过于频繁|访问异常|访问被拒绝/.test(body),
    stale: /停留时间过长/.test(body) && /价格/.test(body),
    dialogs: all(document, '[class*="dialog_mainContent__"]').length,
    one_way_count: all(document, '[data-id="oneWay"]').length,
    trip_type: current ? current.getAttribute("data-id") : null,
    origin: airport("出发机场", expected.origin), destination: airport("到达机场", expected.destination),
    departure_date: dateInput ? dateInput.value : null,
    return_date_count: all(document, 'input[placeholder="返回日期"]').length,
    passengers: summary(passengers), special_passengers: summary(special),
    passenger_popup: passengers ? all(passengers, '[class*="select_dropcontent__"]').length : 0,
    counters: passengers ? all(passengers, '[class*="select_stepContainer__"]').map(row => ({
      label: text(one(row, '[class*="select_labelWrap__"]'))?.split("\n")[0] || null,
      value: text(one(row, '[class*="stepper_inputWrapper__"]')),
      minus_disabled: disabled(all(row, '[class*="stepper_stepperBtn__"]').find(e => text(e) === "-")),
      plus_disabled: disabled(all(row, '[class*="stepper_stepperBtn__"]').find(e => text(e) === "+")),
    })) : [],
    calendars: all(document, '[class*="styles_calendarPanel__"]').map(panel => ({
      year: text(one(panel, '[class*="style_calendarYearBtn__"]')),
      month: text(one(panel, '[class*="style_calendarMonthBtn__"]')),
      days: all(panel, '[class*="styles_calendarInnerBodyDate__"]').map(el => ({
        day: text(el), selectable: !!el.closest('td') &&
          el.closest('td').className.includes('styles_calendarCellOther__') && !disabled(el.closest('td')),
      })),
    })),
    submit_count: submit.length,
    submit_ready: submit.length === 1 && text(submit[0]) === "查询" && !disabled(submit[0]),
  };
}
