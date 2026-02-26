const searchInput = document.getElementById("searchInput");
const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results");
const openOptionsButton = document.getElementById("openOptions");

let debounceTimer = null;
let requestCounter = 0;

async function send(action, payload = {}) {
  const response = await browser.runtime.sendMessage({ action, ...payload });
  if (!response || !response.ok) {
    throw new Error((response && response.error) || "Unknown error");
  }
  return response;
}

function setStatus(text, isError = false) {
  statusEl.style.color = isError ? "#b93838" : "#566174";
  statusEl.textContent = text || "";
}

function clearResults() {
  resultsEl.textContent = "";
}

function renderResults(items) {
  clearResults();
  for (const item of items) {
    const li = document.createElement("li");
    const link = document.createElement("a");
    link.href = item.url || "";
    link.target = "_blank";
    link.rel = "noopener noreferrer";

    const title = document.createElement("div");
    title.className = "title";
    title.textContent = item.title || "(untitled)";

    const url = document.createElement("div");
    url.className = "url";
    url.textContent = item.url || "";

    link.appendChild(title);
    link.appendChild(url);
    li.appendChild(link);
    resultsEl.appendChild(li);
  }
}

async function runSearch() {
  const query = (searchInput.value || "").trim();
  if (!query) {
    clearResults();
    setStatus("Type to search your LinkLoom bookmarks.");
    return;
  }

  requestCounter += 1;
  const currentRequest = requestCounter;
  setStatus("Searching...");

  try {
    const response = await send("searchBookmarks", { q: query, limit: 20 });
    if (currentRequest !== requestCounter) {
      return;
    }
    const items = Array.isArray(response.items) ? response.items : [];
    renderResults(items);
    if (!items.length) {
      setStatus(`No results for "${query}".`);
    } else {
      setStatus(`${items.length} result${items.length === 1 ? "" : "s"}.`);
    }
  } catch (err) {
    if (currentRequest !== requestCounter) {
      return;
    }
    clearResults();
    setStatus(err && err.message ? err.message : String(err), true);
  }
}

async function initialize() {
  openOptionsButton.addEventListener("click", () => {
    void browser.runtime.openOptionsPage();
  });

  searchInput.disabled = true;
  clearResults();

  try {
    const response = await send("getState");
    const state = response.state || {};
    const appUrl = state.settings && state.settings.appUrl;
    const token = state.settings && state.settings.token;
    const configured = Boolean(appUrl && token);

    if (!configured) {
      setStatus("Configure App URL and API token in Options.", true);
      return;
    }

    searchInput.disabled = false;
    setStatus("Type to search your LinkLoom bookmarks.");
    searchInput.focus();
  } catch (err) {
    setStatus(err && err.message ? err.message : String(err), true);
    return;
  }

  searchInput.addEventListener("input", () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      void runSearch();
    }, 180);
  });
}

void initialize();
