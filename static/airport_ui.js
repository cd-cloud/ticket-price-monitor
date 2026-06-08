const AIRPORT_UI_SUGGESTIONS = window.AIRPORT_SUGGESTIONS || [];

function airportDisplayText(item) {
  const airport = item.airport && item.airport !== item.city ? ` ${item.airport}` : "";
  return `${item.city}${airport} (${item.code})`;
}

function airportInputDisplayText(item) {
  return `${item.city} (${item.code})`;
}

function airportCodeDisplayText(code) {
  const normalized = String(code || "").trim().toUpperCase();
  if (!normalized) return "-";
  const entry = AIRPORT_UI_SUGGESTIONS.find((item) => item.code === normalized);
  if (entry) return airportDisplayText(entry);
  const fallbackName = (window.tailCandidateNames || {})[normalized];
  if (fallbackName) return `${fallbackName} (${normalized})`;
  return normalized;
}

function airportSearchTokens(item) {
  return [item.code, item.city, item.airport, item.pinyin, ...(item.aliases || [])]
    .filter(Boolean)
    .map((token) => String(token).toLowerCase());
}

function matchAirportSuggestions(keyword) {
  const normalized = keyword.trim().toLowerCase();
  if (!normalized) return [];
  return AIRPORT_UI_SUGGESTIONS
    .map((item) => ({ item, tokens: airportSearchTokens(item) }))
    .filter(({ item, tokens }) => item.code.toLowerCase() === normalized || tokens.some((token) => token.includes(normalized)))
    .sort((left, right) => {
      const leftExact = airportSearchTokens(left.item).some((token) => token === normalized) ? 0 : 1;
      const rightExact = airportSearchTokens(right.item).some((token) => token === normalized) ? 0 : 1;
      if (leftExact !== rightExact) return leftExact - rightExact;
      return left.item.code.localeCompare(right.item.code);
    })
    .slice(0, 8)
    .map(({ item }) => item);
}

function resolveAirportEntry(value) {
  const normalized = value.trim();
  if (!normalized) return null;
  const upper = normalized.toUpperCase();
  const directCodeMatch = AIRPORT_UI_SUGGESTIONS.find((item) => item.code === upper);
  if (directCodeMatch) return directCodeMatch;
  const lowered = normalized.toLowerCase();
  return AIRPORT_UI_SUGGESTIONS.find((item) => airportSearchTokens(item).some((token) => token === lowered)) || null;
}

function resolveAirportCode(value) {
  const normalized = value.trim();
  if (!normalized) return "";
  const entry = resolveAirportEntry(normalized);
  if (entry) return entry.code;
  const codeMatch = normalized.match(/\(([A-Za-z]{3})\)$/);
  if (codeMatch) return codeMatch[1].toUpperCase();
  return /^[A-Za-z]{3}$/.test(normalized) ? normalized.toUpperCase() : normalized.toUpperCase();
}

function ensureAirportField(input) {
  if (!input || input.dataset.airportEnhanced === "true") return;
  input.dataset.airportEnhanced = "true";
  input.dataset.suggestionSuppressed = "false";
  input.spellcheck = false;

  const shell = document.createElement("div");
  shell.className = "airport-field";
  input.parentNode.insertBefore(shell, input);
  shell.appendChild(input);

  const meta = document.createElement("div");
  meta.className = "airport-meta";
  shell.appendChild(meta);

  const suggestionBox = document.createElement("div");
  suggestionBox.className = "airport-suggestions";
  suggestionBox.hidden = true;
  shell.appendChild(suggestionBox);

  const renderMeta = () => {
    const entry = resolveAirportEntry(input.value);
    meta.textContent = entry ? `已匹配：${airportDisplayText(entry)}` : "可输入中文城市名、机场名或三字码";
  };

  const setSuggestionVisibility = (visible) => {
    shell.classList.toggle("suggestions-locked", !visible);
    suggestionBox.hidden = !visible;
    suggestionBox.setAttribute("aria-hidden", visible ? "false" : "true");
    suggestionBox.style.display = visible ? "grid" : "none";
  };

  const hideSuggestions = () => {
    setSuggestionVisibility(false);
    suggestionBox.innerHTML = "";
  };

  const applySelection = (entry) => {
    input.dataset.suggestionLockUntil = String(Date.now() + 400);
    input.dataset.suggestionSuppressed = "true";
    shell.classList.add("suggestions-locked");
    input.value = airportInputDisplayText(entry);
    renderMeta();
    hideSuggestions();
    input.dispatchEvent(new Event("change", { bubbles: true }));
    window.requestAnimationFrame(() => {
      hideSuggestions();
      input.blur();
    });
  };

  const renderSuggestions = () => {
    const lockUntil = Number(input.dataset.suggestionLockUntil || 0);
    const suppressed = input.dataset.suggestionSuppressed === "true";
    if (suppressed || Date.now() < lockUntil) {
      hideSuggestions();
      renderMeta();
      return;
    }
    const suggestions = matchAirportSuggestions(input.value);
    if (!suggestions.length || !input.value.trim()) {
      hideSuggestions();
      renderMeta();
      return;
    }

    suggestionBox.innerHTML = "";
    suggestions.forEach((entry) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "airport-suggestion";
      button.innerHTML = `
        <span class="airport-suggestion-main">${airportDisplayText(entry)}</span>
        <span class="airport-suggestion-sub">${entry.city} / ${entry.code}</span>
      `;
      const selectSuggestion = (event) => {
        event.preventDefault();
        event.stopPropagation();
        applySelection(entry);
      };
      button.addEventListener("pointerdown", selectSuggestion);
      button.addEventListener("click", selectSuggestion);
      suggestionBox.appendChild(button);
    });
    setSuggestionVisibility(true);
    renderMeta();
  };

  input.addEventListener("input", () => {
    input.dataset.suggestionSuppressed = "false";
    shell.classList.remove("suggestions-locked");
    renderSuggestions();
  });
  input.addEventListener("focus", () => {
    if (input.dataset.suggestionSuppressed === "true") {
      hideSuggestions();
      return;
    }
    renderSuggestions();
  });
  input.addEventListener("blur", () => {
    window.setTimeout(() => {
      const entry = resolveAirportEntry(input.value);
      if (entry) {
        input.value = airportInputDisplayText(entry);
      } else if (/^[A-Za-z]{3}$/.test(input.value.trim())) {
        input.value = input.value.trim().toUpperCase();
      }
      renderMeta();
      hideSuggestions();
    }, 120);
  });

  document.addEventListener("pointerdown", (event) => {
    if (shell.contains(event.target)) return;
    input.dataset.suggestionSuppressed = "true";
    shell.classList.add("suggestions-locked");
    hideSuggestions();
  });

  renderMeta();
}

function enhanceAirportInputs(scope = document) {
  scope.querySelectorAll("input[data-airport-input]").forEach((input) => ensureAirportField(input));
}
