let latestPreflight = null;

function selectedMode() {
  const checked = document.querySelector("input[name='mode']:checked");
  return checked ? checked.value : "replace_server_with_local";
}

async function send(action, payload = {}) {
  const response = await browser.runtime.sendMessage({ action, ...payload });
  if (!response || !response.ok) {
    throw new Error((response && response.error) || "Unknown error");
  }
  return response;
}

function setFlash(text, isError = false) {
  const flash = document.getElementById("flash");
  flash.style.color = isError ? "#b93838" : "#4b5563";
  flash.textContent = text || "";
}

function formatStatus(state) {
  const parts = [];
  parts.push(`<span class="pill">Initialized: ${state.sync.initialized ? "yes" : "no"}</span>`);
  parts.push(`<span class="pill">Sync enabled: ${state.settings.syncEnabled ? "yes" : "no"}</span>`);
  parts.push(`<span class="pill">Cursor: ${state.sync.cursor || 0}</span>`);
  if (state.sync.lastSyncAt) {
    parts.push(`<span class="pill">Last sync: ${new Date(state.sync.lastSyncAt).toLocaleString()}</span>`);
  }
  if (state.sync.lastNoOpReason) {
    parts.push(`<span class="pill">Last no-op: ${state.sync.lastNoOpReason}</span>`);
  }
  if (state.sync.lastError) {
    parts.push(`<span class="pill" style="border-color:#f1b5b5;color:#9f2b2b;">Error: ${state.sync.lastError}</span>`);
  }
  return parts.join(" ");
}

function renderState(state) {
  document.getElementById("appUrl").value = state.settings.appUrl || "";
  document.getElementById("token").value = state.settings.token || "";
  document.getElementById("pollMinutes").value = Number(state.settings.pollMinutes || 3);
  document.getElementById("syncEnabled").checked = Boolean(state.settings.syncEnabled);
  document.getElementById("status").innerHTML = formatStatus(state);
}

function renderPreflight(preflight) {
  const result = document.getElementById("preflightResult");
  const text = document.getElementById("preflightText");
  const confirmWrap = document.getElementById("confirmWrap");
  const requiredPhrase = document.getElementById("requiredPhrase");

  const impact = preflight.impact || {};
  const lines = [
    preflight.warning,
    `Local bookmarks: ${preflight.local_bookmark_count}`,
    `Server bookmarks: ${preflight.server_bookmark_count}`,
    `Local adds: ${impact.local_additions || 0}`,
    `Local deletes: ${impact.local_deletions || 0}`,
    `Server adds: ${impact.server_additions || 0}`,
    `Server deletes: ${impact.server_deletions || 0}`,
  ];
  if (preflight.would_noop) {
    lines.push(`No-op safeguard: ${preflight.no_op_reason || "enabled"}`);
  }
  text.innerHTML = lines.map((line) => `<div>${line}</div>`).join("");

  requiredPhrase.textContent = preflight.required_phrase || "";
  document.getElementById("typedPhrase").value = "";
  document.getElementById("confirmChecked").checked = false;

  result.hidden = false;
  confirmWrap.hidden = false;
}

async function refreshState() {
  const response = await send("getState");
  renderState(response.state);
}

async function saveSettings() {
  const payload = {
    appUrl: document.getElementById("appUrl").value,
    token: document.getElementById("token").value,
    pollMinutes: document.getElementById("pollMinutes").value,
    syncEnabled: document.getElementById("syncEnabled").checked,
  };
  const response = await send("saveSettings", payload);
  renderState(response.state);
  setFlash("Settings saved.");
}

async function runPreflight() {
  const mode = selectedMode();
  const response = await send("preflightFirstSync", { mode });
  latestPreflight = response.preflight;
  renderPreflight(response.preflight);
  setFlash("Preflight complete.");
}

async function applyFirstSync() {
  if (!latestPreflight || !latestPreflight.confirmation_token) {
    throw new Error("Run preflight first.");
  }
  const mode = selectedMode();
  const typedPhrase = document.getElementById("typedPhrase").value;
  const confirmChecked = document.getElementById("confirmChecked").checked;
  const response = await send("applyFirstSync", {
    mode,
    confirmationToken: latestPreflight.confirmation_token,
    typedPhrase,
    confirmChecked,
  });
  const result = response.result || {};
  if (result.status === "no_op") {
    setFlash(`First sync no-op: ${result.reason || "no changes"}`);
  } else {
    setFlash(`First sync applied (${result.status || "ok"}).`);
  }
  await refreshState();
}

async function syncNow() {
  await send("syncNow");
  setFlash("Sync completed.");
  await refreshState();
}

async function resetState() {
  await send("resetSyncState");
  latestPreflight = null;
  document.getElementById("preflightResult").hidden = true;
  setFlash("Sync state reset. Run first sync again.");
  await refreshState();
}

function bindEvents() {
  document.getElementById("saveSettings").addEventListener("click", async () => {
    try {
      await saveSettings();
    } catch (err) {
      setFlash(err.message || String(err), true);
    }
  });

  document.getElementById("preflight").addEventListener("click", async () => {
    try {
      await runPreflight();
    } catch (err) {
      setFlash(err.message || String(err), true);
    }
  });

  document.getElementById("applyFirstSync").addEventListener("click", async () => {
    try {
      await applyFirstSync();
    } catch (err) {
      setFlash(err.message || String(err), true);
    }
  });

  document.getElementById("syncNow").addEventListener("click", async () => {
    try {
      await syncNow();
    } catch (err) {
      setFlash(err.message || String(err), true);
    }
  });

  document.getElementById("resetState").addEventListener("click", async () => {
    try {
      await resetState();
    } catch (err) {
      setFlash(err.message || String(err), true);
    }
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  bindEvents();
  try {
    await refreshState();
  } catch (err) {
    setFlash(err.message || String(err), true);
  }
});
