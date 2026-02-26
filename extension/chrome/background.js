const browser = globalThis.browser || globalThis.chrome;

const STORAGE_KEY = "linkloom_sync_state";
const SYNC_ALARM = "linkloom_periodic_sync";
const PLATFORM = "chrome-extension";
const DEFAULT_POLL_MINUTES = 3;
const ROOT_ID = "0";
const BOOKMARK_TOOLBAR_ID = "1";
const DEFAULT_ROOT_ID = "2";
const MOBILE_BOOKMARKS_ID = "3";

const SPECIAL_ROOT_NAME_TO_ID = {
  "bookmarks menu": DEFAULT_ROOT_ID,
  "bookmarks toolbar": BOOKMARK_TOOLBAR_ID,
  "bookmarks bar": BOOKMARK_TOOLBAR_ID,
  "other bookmarks": DEFAULT_ROOT_ID,
  "unsorted bookmarks": DEFAULT_ROOT_ID,
  "mobile bookmarks": MOBILE_BOOKMARKS_ID,
};

let remoteApplyDepth = 0;
let flushInProgress = false;

function nowIso() {
  return new Date().toISOString();
}

function randomId() {
  if (crypto && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function normalizeUrl(raw) {
  const value = String(raw || "").trim();
  if (!value) {
    return "";
  }
  try {
    const url = new URL(value);
    if (!url.pathname) {
      url.pathname = "/";
    }
    url.hash = "";
    const params = [...url.searchParams.entries()].sort((a, b) => {
      if (a[0] === b[0]) {
        return a[1].localeCompare(b[1]);
      }
      return a[0].localeCompare(b[0]);
    });
    const clean = new URL(`${url.protocol}//${url.host}${url.pathname}`);
    for (const [key, val] of params) {
      clean.searchParams.append(key, val);
    }
    return clean.toString();
  } catch (_err) {
    return value;
  }
}

function normalizeServerBookmarkTitle(value) {
  if (value === null || value === undefined) {
    return "";
  }
  return String(value).trim();
}

function normalizeFolderName(value) {
  return String(value || "").trim().toLowerCase().replace(/\s+/g, " ");
}

function toInt(value, fallback) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.max(1, Math.floor(parsed));
}

function buildDefaultState() {
  return {
    settings: {
      appUrl: "",
      token: "",
      pollMinutes: DEFAULT_POLL_MINUTES,
      clientId: "",
      syncEnabled: false,
    },
    sync: {
      initialized: false,
      cursor: 0,
      lastSyncAt: null,
      lastError: null,
      lastNoOpReason: null,
    },
    maps: {
      bookmarkLocalToServer: {},
      bookmarkServerToLocal: {},
      folderLocalToServer: {},
      folderServerToLocal: {},
    },
    outbox: [],
  };
}

function applyStateDefaults(value) {
  const base = buildDefaultState();
  const state = value || {};
  return {
    settings: {
      ...base.settings,
      ...(state.settings || {}),
      pollMinutes: toInt(state.settings && state.settings.pollMinutes, DEFAULT_POLL_MINUTES),
      syncEnabled: Boolean(state.settings && state.settings.syncEnabled),
    },
    sync: {
      ...base.sync,
      ...(state.sync || {}),
      initialized: Boolean(state.sync && state.sync.initialized),
      cursor: Number((state.sync && state.sync.cursor) || 0) || 0,
    },
    maps: {
      ...base.maps,
      ...(state.maps || {}),
      bookmarkLocalToServer: { ...base.maps.bookmarkLocalToServer, ...((state.maps && state.maps.bookmarkLocalToServer) || {}) },
      bookmarkServerToLocal: { ...base.maps.bookmarkServerToLocal, ...((state.maps && state.maps.bookmarkServerToLocal) || {}) },
      folderLocalToServer: { ...base.maps.folderLocalToServer, ...((state.maps && state.maps.folderLocalToServer) || {}) },
      folderServerToLocal: { ...base.maps.folderServerToLocal, ...((state.maps && state.maps.folderServerToLocal) || {}) },
    },
    outbox: Array.isArray(state.outbox) ? state.outbox.slice() : [],
  };
}

async function readState() {
  const row = await browser.storage.local.get(STORAGE_KEY);
  return applyStateDefaults(row[STORAGE_KEY]);
}

async function writeState(state) {
  await browser.storage.local.set({ [STORAGE_KEY]: state });
}

function setBookmarkMapping(state, localId, serverId) {
  if (!localId || !serverId) {
    return;
  }
  const localKey = String(localId);
  const serverKey = String(serverId);
  state.maps.bookmarkLocalToServer[localKey] = Number(serverId);
  state.maps.bookmarkServerToLocal[serverKey] = localKey;
}

function clearBookmarkMapping(state, localId) {
  if (!localId) {
    return;
  }
  const localKey = String(localId);
  const serverId = state.maps.bookmarkLocalToServer[localKey];
  delete state.maps.bookmarkLocalToServer[localKey];
  if (serverId !== undefined && serverId !== null) {
    delete state.maps.bookmarkServerToLocal[String(serverId)];
  }
}

function setFolderMapping(state, localId, serverId) {
  if (!localId || !serverId) {
    return;
  }
  const localKey = String(localId);
  const serverKey = String(serverId);
  state.maps.folderLocalToServer[localKey] = Number(serverId);
  state.maps.folderServerToLocal[serverKey] = localKey;
}

function clearFolderMapping(state, localId) {
  if (!localId) {
    return;
  }
  const localKey = String(localId);
  const serverId = state.maps.folderLocalToServer[localKey];
  delete state.maps.folderLocalToServer[localKey];
  if (serverId !== undefined && serverId !== null) {
    delete state.maps.folderServerToLocal[String(serverId)];
  }
}

function isConfigured(state) {
  return Boolean(state.settings.appUrl && state.settings.token);
}

function isSyncReady(state) {
  return isConfigured(state) && state.settings.syncEnabled && state.sync.initialized;
}

function sanitizeBaseUrl(value) {
  const raw = String(value || "").trim();
  if (!raw) {
    return "";
  }
  return raw.replace(/\/+$/, "");
}

function buildApiUrl(baseUrl, path) {
  const prefix = sanitizeBaseUrl(baseUrl);
  if (!prefix) {
    throw new Error("App URL is not configured.");
  }
  return `${prefix}/api/v1${path}`;
}

async function apiRequest(state, path, options = {}) {
  const method = options.method || "GET";
  const headers = {
    Authorization: `Bearer ${state.settings.token}`,
  };
  const init = {
    method,
    headers,
  };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(buildApiUrl(state.settings.appUrl, path), init);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = payload && payload.error ? payload.error : `${response.status} ${response.statusText}`;
    throw new Error(message);
  }
  return payload;
}

async function ensureClientId(state) {
  if (state.settings.clientId) {
    return state.settings.clientId;
  }
  state.settings.clientId = `ch-${randomId()}`;
  await writeState(state);
  return state.settings.clientId;
}

async function ensureClientRegistered(state) {
  if (!isConfigured(state)) {
    return;
  }
  const clientId = await ensureClientId(state);
  await apiRequest(state, "/sync/register-client", {
    method: "POST",
    body: {
      client_id: clientId,
      platform: PLATFORM,
    },
  });
}

async function scheduleSyncAlarm(state) {
  await browser.alarms.clear(SYNC_ALARM);
  if (!state.settings.syncEnabled || !isConfigured(state)) {
    return;
  }
  await browser.alarms.create(SYNC_ALARM, {
    periodInMinutes: toInt(state.settings.pollMinutes, DEFAULT_POLL_MINUTES),
  });
}

function traverseNodeForSnapshot(node, parentId, folderPath, folders, bookmarks) {
  if (node.url) {
    bookmarks.push({
      id: String(node.id),
      url: node.url,
      title: (node.title || "").trim() || null,
      parent_id: parentId,
      folder_local_id: parentId,
      folder_path: folderPath,
      updated_at: nowIso(),
    });
    return;
  }

  const title = (node.title || "").trim() || "Untitled Folder";
  folders.push({
    id: String(node.id),
    parent_id: parentId,
    title,
  });

  const nextPath = folderPath.concat([title]);
  for (const child of node.children || []) {
    traverseNodeForSnapshot(child, String(node.id), nextPath, folders, bookmarks);
  }
}

async function collectLocalSnapshot() {
  const tree = await browser.bookmarks.getTree();
  const root = tree[0] || {};
  const folders = [];
  const bookmarks = [];
  for (const child of root.children || []) {
    traverseNodeForSnapshot(child, null, [], folders, bookmarks);
  }
  return { folders, bookmarks };
}

async function getDefaultLocalParentId() {
  const tree = await browser.bookmarks.getTree();
  const root = tree[0] || {};
  const children = root.children || [];
  const preferred = children.find((row) => row.id === DEFAULT_ROOT_ID);
  if (preferred) {
    return preferred.id;
  }
  if (children.length > 0) {
    return children[0].id;
  }
  return null;
}

async function getLocalSpecialRootLookup() {
  const tree = await browser.bookmarks.getTree();
  const root = tree[0] || {};
  const children = root.children || [];
  const lookup = {};

  for (const child of children) {
    const childId = String(child.id);
    if ([DEFAULT_ROOT_ID, BOOKMARK_TOOLBAR_ID, MOBILE_BOOKMARKS_ID].includes(childId)) {
      lookup[childId] = childId;
    }
    const normalizedTitle = normalizeFolderName(child.title);
    const expectedRootId = SPECIAL_ROOT_NAME_TO_ID[normalizedTitle];
    if (expectedRootId && !lookup[expectedRootId]) {
      lookup[expectedRootId] = childId;
    }
  }

  return lookup;
}

function resolveSpecialRootLocalId(folder, localSpecialRoots) {
  if (!folder) {
    return null;
  }
  const title = normalizeFolderName(folder.name || folder.title || "");
  const expectedRootId = SPECIAL_ROOT_NAME_TO_ID[title];
  if (!expectedRootId) {
    return null;
  }
  return localSpecialRoots[expectedRootId] || null;
}

async function nodeExists(id) {
  try {
    const rows = await browser.bookmarks.get(String(id));
    return rows && rows.length ? rows[0] : null;
  } catch (_err) {
    return null;
  }
}

async function removeNodeRecursive(node) {
  if (!node) {
    return;
  }
  try {
    if (node.url) {
      await browser.bookmarks.remove(String(node.id));
    } else {
      await browser.bookmarks.removeTree(String(node.id));
    }
  } catch (_err) {
    // Ignore node-not-found races.
  }
}

async function clearAllLocalBookmarks() {
  const tree = await browser.bookmarks.getTree();
  const root = tree[0] || {};
  for (const bucket of root.children || []) {
    const children = await browser.bookmarks.getChildren(String(bucket.id));
    for (const node of children) {
      await removeNodeRecursive(node);
    }
  }
}

async function reserveAppendIndex(parentId, nextIndexByParent) {
  if (parentId === null || parentId === undefined) {
    return null;
  }
  const parentKey = String(parentId);
  if (nextIndexByParent && Object.prototype.hasOwnProperty.call(nextIndexByParent, parentKey)) {
    const index = nextIndexByParent[parentKey];
    nextIndexByParent[parentKey] = index + 1;
    return index;
  }
  const children = await browser.bookmarks.getChildren(parentKey);
  const startIndex = children.length;
  if (nextIndexByParent) {
    nextIndexByParent[parentKey] = startIndex + 1;
  }
  return startIndex;
}

async function createFolderAppended(parentId, title, nextIndexByParent) {
  const payload = { parentId, title };
  const index = await reserveAppendIndex(parentId, nextIndexByParent);
  if (index !== null) {
    payload.index = index;
  }
  return browser.bookmarks.create(payload);
}

async function createBookmarkAppended(parentId, title, url, nextIndexByParent) {
  const payload = { parentId, title, url };
  const index = await reserveAppendIndex(parentId, nextIndexByParent);
  if (index !== null) {
    payload.index = index;
  }
  return browser.bookmarks.create(payload);
}

async function ensureFolderMappingsFromSnapshot(state, folders, createMissing) {
  const pending = new Map();
  const nextIndexByParent = {};
  for (const folder of folders || []) {
    const sid = String(folder.id);
    pending.set(sid, folder);
  }
  const defaultParent = await getDefaultLocalParentId();
  const localSpecialRoots = await getLocalSpecialRootLookup();

  while (pending.size > 0) {
    let progressed = false;
    for (const [serverId, folder] of [...pending.entries()]) {
      const serverParentId = folder.parent_id === null || folder.parent_id === undefined
        ? null
        : String(folder.parent_id);
      if (serverParentId && !state.maps.folderServerToLocal[serverParentId]) {
        continue;
      }

      if (!serverParentId) {
        const specialLocalId = resolveSpecialRootLocalId(folder, localSpecialRoots);
        if (specialLocalId) {
          setFolderMapping(state, specialLocalId, serverId);
          pending.delete(serverId);
          progressed = true;
          continue;
        }
      }

      const existingLocalId = state.maps.folderServerToLocal[serverId];
      let localNode = existingLocalId ? await nodeExists(existingLocalId) : null;
      if (!localNode && !createMissing) {
        pending.delete(serverId);
        progressed = true;
        continue;
      }

      const parentLocalId = serverParentId
        ? state.maps.folderServerToLocal[serverParentId]
        : defaultParent;
      const title = (folder.name || folder.title || "").trim() || "Untitled Folder";

      if (!localNode) {
        localNode = await createFolderAppended(parentLocalId, title, nextIndexByParent);
      } else {
        if ((localNode.title || "") !== title) {
          localNode = await browser.bookmarks.update(String(localNode.id), { title });
        }
        if (parentLocalId && localNode.parentId !== parentLocalId) {
          localNode = await browser.bookmarks.move(String(localNode.id), { parentId: parentLocalId });
        }
      }

      setFolderMapping(state, String(localNode.id), serverId);
      pending.delete(serverId);
      progressed = true;
    }

    if (!progressed) {
      for (const [serverId, folder] of [...pending.entries()]) {
        const serverParentId = folder.parent_id === null || folder.parent_id === undefined
          ? null
          : String(folder.parent_id);
        if (!serverParentId) {
          const specialLocalId = resolveSpecialRootLocalId(folder, localSpecialRoots);
          if (specialLocalId) {
            setFolderMapping(state, specialLocalId, serverId);
            pending.delete(serverId);
            continue;
          }
        }
        const title = (folder.name || folder.title || "").trim() || "Untitled Folder";
        const localNode = await createFolderAppended(defaultParent, title, nextIndexByParent);
        setFolderMapping(state, String(localNode.id), serverId);
        pending.delete(serverId);
      }
    }
  }
}

function localBookmarksByNormalized(snapshot) {
  const grouped = {};
  for (const row of snapshot.bookmarks) {
    const normalized = normalizeUrl(row.url);
    if (!normalized) {
      continue;
    }
    if (!grouped[normalized]) {
      grouped[normalized] = [];
    }
    grouped[normalized].push(String(row.id));
  }
  return grouped;
}

async function upsertLocalBookmarkForServer(state, payload) {
  const serverId = String(payload.id);
  const title = normalizeServerBookmarkTitle(payload.title);
  const serverFolderId = payload.folder_id === null || payload.folder_id === undefined
    ? null
    : String(payload.folder_id);
  const parentId = serverFolderId
    ? state.maps.folderServerToLocal[serverFolderId]
    : await getDefaultLocalParentId();

  let localId = state.maps.bookmarkServerToLocal[serverId];
  let localNode = localId ? await nodeExists(localId) : null;
  if (!localNode) {
    localNode = await createBookmarkAppended(parentId, title, payload.url);
    setBookmarkMapping(state, String(localNode.id), serverId);
    return;
  }

  if ((localNode.title || "") !== title || (localNode.url || "") !== payload.url) {
    localNode = await browser.bookmarks.update(String(localNode.id), {
      title,
      url: payload.url,
    });
  }
  if (parentId && localNode.parentId !== parentId) {
    await browser.bookmarks.move(String(localNode.id), { parentId });
  }
  setBookmarkMapping(state, String(localNode.id), serverId);
}

async function applyReplaceLocalSnapshot(state, response) {
  state.maps.bookmarkLocalToServer = {};
  state.maps.bookmarkServerToLocal = {};
  state.maps.folderLocalToServer = {};
  state.maps.folderServerToLocal = {};

  await clearAllLocalBookmarks();
  await ensureFolderMappingsFromSnapshot(state, response.folders || [], true);

  const defaultParent = await getDefaultLocalParentId();
  const nextIndexByParent = {};
  for (const row of response.bookmarks || []) {
    if (row.deleted_at) {
      continue;
    }
    const serverFolderId = row.folder_id === null || row.folder_id === undefined
      ? null
      : String(row.folder_id);
    const parentId = serverFolderId
      ? state.maps.folderServerToLocal[serverFolderId]
      : defaultParent;
    const created = await createBookmarkAppended(
      parentId,
      normalizeServerBookmarkTitle(row.title),
      row.url,
      nextIndexByParent,
    );
    setBookmarkMapping(state, String(created.id), String(row.id));
  }
}

async function applyMergeSnapshot(state, response) {
  const mapping = response.mapping || {};
  const folderMap = mapping.local_folder_id_to_server_id || {};
  const bookmarkMap = mapping.local_bookmark_id_to_server_id || {};
  for (const [localId, serverId] of Object.entries(folderMap)) {
    setFolderMapping(state, localId, serverId);
  }
  for (const [localId, serverId] of Object.entries(bookmarkMap)) {
    setBookmarkMapping(state, localId, serverId);
  }

  await ensureFolderMappingsFromSnapshot(state, response.folders || [], true);
  const localSnapshot = await collectLocalSnapshot();
  const localByNormalized = localBookmarksByNormalized(localSnapshot);
  const consumedLocalIds = new Set();

  for (const row of response.bookmarks || []) {
    if (row.deleted_at) {
      continue;
    }
    const serverId = String(row.id);
    const mappedLocalId = state.maps.bookmarkServerToLocal[serverId];
    const mappedNode = mappedLocalId ? await nodeExists(mappedLocalId) : null;
    if (mappedNode) {
      await upsertLocalBookmarkForServer(state, row);
      consumedLocalIds.add(String(mappedNode.id));
      continue;
    }

    const normalized = normalizeUrl(row.url);
    const candidates = localByNormalized[normalized] || [];
    let matchedLocalId = null;
    for (const candidate of candidates) {
      if (consumedLocalIds.has(candidate)) {
        continue;
      }
      matchedLocalId = candidate;
      break;
    }

    if (matchedLocalId) {
      consumedLocalIds.add(matchedLocalId);
      setBookmarkMapping(state, matchedLocalId, serverId);
      await upsertLocalBookmarkForServer(state, row);
      continue;
    }

    await upsertLocalBookmarkForServer(state, row);
  }
}

async function applyResponseMappings(state, response) {
  const mapping = response.mapping || {};
  for (const [localId, serverId] of Object.entries(mapping.local_folder_id_to_server_id || {})) {
    setFolderMapping(state, localId, serverId);
  }
  for (const [localId, serverId] of Object.entries(mapping.local_bookmark_id_to_server_id || {})) {
    setBookmarkMapping(state, localId, serverId);
  }
}

async function withRemoteApply(fn) {
  remoteApplyDepth += 1;
  try {
    await fn();
  } finally {
    remoteApplyDepth = Math.max(0, remoteApplyDepth - 1);
  }
}

function isApplyingRemote() {
  return remoteApplyDepth > 0;
}

async function enqueueOperations(operations, mutateState) {
  if (!operations.length) {
    return;
  }
  const state = await readState();
  if (!isSyncReady(state)) {
    return;
  }
  if (typeof mutateState === "function") {
    mutateState(state);
  }
  state.outbox.push(...operations);
  await writeState(state);
  void flushOutbox();
}

async function flushOutbox() {
  if (flushInProgress) {
    return;
  }
  flushInProgress = true;
  try {
    while (true) {
      const state = await readState();
      if (!isSyncReady(state) || state.outbox.length === 0) {
        return;
      }
      await ensureClientRegistered(state);

      const op = state.outbox[0];
      const response = await apiRequest(state, "/sync/push", {
        method: "POST",
        body: {
          client_id: state.settings.clientId,
          operations: [op],
        },
      });
      const result = (response.results || [])[0] || {};

      if (op.entity_type === "bookmark" && op.op === "create" && result.bookmark_id && op.local_id) {
        setBookmarkMapping(state, String(op.local_id), result.bookmark_id);
      }
      if (op.entity_type === "folder" && op.op === "create" && result.folder_id && op.local_id) {
        setFolderMapping(state, String(op.local_id), result.folder_id);
      }
      if (op.entity_type === "folder" && op.op === "create" && result.status === "exists" && result.folder_id && op.local_id) {
        setFolderMapping(state, String(op.local_id), result.folder_id);
      }

      state.outbox.shift();
      state.sync.cursor = Math.max(Number(state.sync.cursor || 0), Number(response.cursor || 0));
      state.sync.lastError = null;
      await writeState(state);
    }
  } catch (err) {
    const state = await readState();
    state.sync.lastError = err && err.message ? err.message : String(err);
    await writeState(state);
  } finally {
    flushInProgress = false;
  }
}

async function applyFolderEvent(state, event) {
  const payload = event.payload || {};
  const serverId = String(payload.id || event.entity_id || "");
  if (!serverId) {
    return;
  }
  if (event.action === "delete") {
    const localId = state.maps.folderServerToLocal[serverId];
    if (localId && localId !== ROOT_ID) {
      const node = await nodeExists(localId);
      if (node) {
        await removeNodeRecursive(node);
      }
    }
    if (localId) {
      clearFolderMapping(state, localId);
    }
    return;
  }

  const serverParentId = payload.parent_id === null || payload.parent_id === undefined
    ? null
    : String(payload.parent_id);
  if (!serverParentId) {
    const localSpecialRoots = await getLocalSpecialRootLookup();
    const specialLocalId = resolveSpecialRootLocalId(payload, localSpecialRoots);
    if (specialLocalId) {
      setFolderMapping(state, specialLocalId, serverId);
      return;
    }
  }
  const parentLocalId = serverParentId
    ? state.maps.folderServerToLocal[serverParentId]
    : await getDefaultLocalParentId();
  const title = (payload.name || payload.title || "").trim() || "Untitled Folder";

  const existingLocalId = state.maps.folderServerToLocal[serverId];
  let localNode = existingLocalId ? await nodeExists(existingLocalId) : null;
  if (!localNode) {
    localNode = await browser.bookmarks.create({
      parentId: parentLocalId,
      title,
    });
  } else {
    if ((localNode.title || "") !== title) {
      localNode = await browser.bookmarks.update(String(localNode.id), { title });
    }
    if (parentLocalId && localNode.parentId !== parentLocalId) {
      localNode = await browser.bookmarks.move(String(localNode.id), { parentId: parentLocalId });
    }
  }

  setFolderMapping(state, String(localNode.id), serverId);
}

async function applyBookmarkDeleteEvent(state, serverId) {
  const localId = state.maps.bookmarkServerToLocal[String(serverId)];
  if (!localId) {
    return;
  }
  const node = await nodeExists(localId);
  if (node) {
    await removeNodeRecursive(node);
  }
  clearBookmarkMapping(state, localId);
}

async function applyBookmarkEvent(state, event) {
  const payload = event.payload || {};
  const serverId = String(payload.id || event.entity_id || "");
  if (!serverId) {
    return;
  }
  if (event.action === "delete" || event.action === "purge" || payload.deleted_at) {
    await applyBookmarkDeleteEvent(state, serverId);
    return;
  }
  await upsertLocalBookmarkForServer(state, payload);
}

async function pullFromServer() {
  let state = await readState();
  if (!isSyncReady(state)) {
    return;
  }
  await ensureClientRegistered(state);

  let cursor = Number(state.sync.cursor || 0);
  let hasMore = true;
  while (hasMore) {
    const payload = await apiRequest(state, `/sync/pull?since=${cursor}&limit=200`);
    const events = Array.isArray(payload.events) ? payload.events : [];
    if (events.length) {
      await withRemoteApply(async () => {
        for (const event of events) {
          if (event.entity_type === "folder") {
            await applyFolderEvent(state, event);
          } else if (event.entity_type === "bookmark") {
            await applyBookmarkEvent(state, event);
          }
        }
      });
    }

    cursor = Number(payload.cursor || cursor);
    state.sync.cursor = cursor;
    state.sync.lastSyncAt = nowIso();
    state.sync.lastError = null;
    await writeState(state);

    hasMore = Boolean(payload.has_more);
  }

  await apiRequest(state, "/sync/ack", {
    method: "POST",
    body: {
      client_id: state.settings.clientId,
      cursor,
    },
  });
}

async function runSyncCycle() {
  const state = await readState();
  if (!isSyncReady(state)) {
    return;
  }
  await flushOutbox();
  await pullFromServer();
}

async function searchBookmarks(query, limit) {
  const state = await readState();
  if (!isConfigured(state)) {
    throw new Error("Set App URL and token first.");
  }
  const q = String(query || "").trim();
  if (!q) {
    return { items: [] };
  }
  const parsedLimit = Number(limit);
  const safeLimit = Number.isFinite(parsedLimit)
    ? Math.max(1, Math.min(100, Math.floor(parsedLimit)))
    : 20;
  return apiRequest(state, `/search?q=${encodeURIComponent(q)}&limit=${safeLimit}`);
}

async function buildBookmarkCreateOperation(state, localId, node) {
  if (!node || !node.url) {
    return null;
  }
  const parentServerId = node.parentId
    ? state.maps.folderLocalToServer[String(node.parentId)] || null
    : null;
  return {
    op_id: randomId(),
    entity_type: "bookmark",
    op: "create",
    local_id: String(localId),
    bookmark: {
      url: node.url,
      title: (node.title || "").trim() || null,
      notes: null,
      tags: [],
      folder_id: parentServerId,
      updated_at: nowIso(),
    },
  };
}

async function buildBookmarkUpdateOperation(state, localId) {
  const rows = await browser.bookmarks.get(String(localId));
  const node = rows && rows.length ? rows[0] : null;
  if (!node || !node.url) {
    return null;
  }
  const serverId = state.maps.bookmarkLocalToServer[String(localId)];
  if (!serverId) {
    return buildBookmarkCreateOperation(state, localId, node);
  }
  const parentServerId = node.parentId
    ? state.maps.folderLocalToServer[String(node.parentId)] || null
    : null;
  return {
    op_id: randomId(),
    entity_type: "bookmark",
    op: "update",
    id: Number(serverId),
    local_id: String(localId),
    bookmark: {
      id: Number(serverId),
      url: node.url,
      title: (node.title || "").trim() || null,
      folder_id: parentServerId,
      updated_at: nowIso(),
    },
  };
}

function buildBookmarkDeleteOperation(serverId, localId) {
  return {
    op_id: randomId(),
    entity_type: "bookmark",
    op: "delete",
    id: Number(serverId),
    local_id: String(localId),
    updated_at: nowIso(),
  };
}

async function buildFolderCreateOperation(state, localId, node) {
  if (!node || node.url) {
    return null;
  }
  const parentServerId = node.parentId
    ? state.maps.folderLocalToServer[String(node.parentId)] || null
    : null;
  return {
    op_id: randomId(),
    entity_type: "folder",
    op: "create",
    local_id: String(localId),
    folder: {
      name: (node.title || "").trim() || "Untitled Folder",
      parent_id: parentServerId,
      updated_at: nowIso(),
    },
  };
}

async function buildFolderUpdateOperation(state, localId) {
  const rows = await browser.bookmarks.get(String(localId));
  const node = rows && rows.length ? rows[0] : null;
  if (!node || node.url) {
    return null;
  }
  const serverId = state.maps.folderLocalToServer[String(localId)];
  if (!serverId) {
    return buildFolderCreateOperation(state, localId, node);
  }
  const parentServerId = node.parentId
    ? state.maps.folderLocalToServer[String(node.parentId)] || null
    : null;
  return {
    op_id: randomId(),
    entity_type: "folder",
    op: "update",
    id: Number(serverId),
    local_id: String(localId),
    folder: {
      id: Number(serverId),
      name: (node.title || "").trim() || "Untitled Folder",
      parent_id: parentServerId,
      updated_at: nowIso(),
    },
  };
}

function buildFolderDeleteOperation(serverId, localId) {
  return {
    op_id: randomId(),
    entity_type: "folder",
    op: "delete",
    id: Number(serverId),
    local_id: String(localId),
    updated_at: nowIso(),
  };
}

function collectFolderRemovalOperations(node, state, operations) {
  if (!node) {
    return;
  }
  if (node.url) {
    const serverId = state.maps.bookmarkLocalToServer[String(node.id)];
    if (serverId) {
      operations.push(buildBookmarkDeleteOperation(serverId, node.id));
    }
    clearBookmarkMapping(state, String(node.id));
    return;
  }

  for (const child of node.children || []) {
    collectFolderRemovalOperations(child, state, operations);
  }

  const serverFolderId = state.maps.folderLocalToServer[String(node.id)];
  if (serverFolderId) {
    operations.push(buildFolderDeleteOperation(serverFolderId, node.id));
  }
  clearFolderMapping(state, String(node.id));
}

async function handleBookmarkCreated(localId, node) {
  if (isApplyingRemote()) {
    return;
  }
  const state = await readState();
  if (!isSyncReady(state)) {
    return;
  }
  const operation = node.url
    ? await buildBookmarkCreateOperation(state, localId, node)
    : await buildFolderCreateOperation(state, localId, node);
  if (!operation) {
    return;
  }
  await enqueueOperations([operation]);
}

async function handleBookmarkChanged(localId) {
  if (isApplyingRemote()) {
    return;
  }
  const state = await readState();
  if (!isSyncReady(state)) {
    return;
  }
  const rows = await browser.bookmarks.get(String(localId)).catch(() => []);
  const node = rows && rows.length ? rows[0] : null;
  if (!node) {
    return;
  }
  const operation = node.url
    ? await buildBookmarkUpdateOperation(state, localId)
    : await buildFolderUpdateOperation(state, localId);
  if (!operation) {
    return;
  }
  await enqueueOperations([operation]);
}

async function handleBookmarkMoved(localId) {
  await handleBookmarkChanged(localId);
}

async function handleBookmarkRemoved(localId, removeInfo) {
  if (isApplyingRemote()) {
    return;
  }
  const state = await readState();
  if (!isSyncReady(state)) {
    return;
  }

  const operations = [];
  const node = removeInfo && removeInfo.node ? removeInfo.node : null;
  if (node && !node.url) {
    collectFolderRemovalOperations(node, state, operations);
  } else {
    const serverId = state.maps.bookmarkLocalToServer[String(localId)];
    if (serverId) {
      operations.push(buildBookmarkDeleteOperation(serverId, localId));
    }
    clearBookmarkMapping(state, String(localId));
  }

  await enqueueOperations(operations, (mutableState) => {
    mutableState.maps = state.maps;
  });
}

async function preflightFirstSync(mode) {
  const state = await readState();
  if (!isConfigured(state)) {
    throw new Error("Set App URL and token first.");
  }
  await ensureClientRegistered(state);
  const snapshot = await collectLocalSnapshot();
  const payload = await apiRequest(state, "/sync/first/preflight", {
    method: "POST",
    body: {
      client_id: state.settings.clientId,
      platform: PLATFORM,
      mode,
      local_folders: snapshot.folders,
      local_bookmarks: snapshot.bookmarks,
    },
  });
  return payload;
}

async function applyFirstSync(mode, confirmationToken, typedPhrase, confirmChecked) {
  const state = await readState();
  if (!isConfigured(state)) {
    throw new Error("Set App URL and token first.");
  }

  await ensureClientRegistered(state);
  const snapshot = await collectLocalSnapshot();
  const response = await apiRequest(state, "/sync/first/apply", {
    method: "POST",
    body: {
      client_id: state.settings.clientId,
      mode,
      confirmation_token: confirmationToken,
      typed_phrase: typedPhrase,
      confirm_checked: Boolean(confirmChecked),
      local_folders: snapshot.folders,
      local_bookmarks: snapshot.bookmarks,
    },
  });

  if (response.status === "no_op") {
    state.sync.lastNoOpReason = response.reason || "no_op";
    state.sync.lastError = null;
    await writeState(state);
    return response;
  }

  await withRemoteApply(async () => {
    if (mode === "replace_local_with_server") {
      await applyReplaceLocalSnapshot(state, response);
    } else if (mode === "two_way_merge") {
      await applyMergeSnapshot(state, response);
    } else {
      await applyResponseMappings(state, response);
    }
  });

  state.sync.initialized = true;
  state.settings.syncEnabled = true;
  state.sync.cursor = Number(response.cursor || 0);
  state.sync.lastSyncAt = nowIso();
  state.sync.lastError = null;
  state.sync.lastNoOpReason = null;
  await writeState(state);
  await scheduleSyncAlarm(state);
  await runSyncCycle();
  return response;
}

async function saveSettings(payload) {
  const state = await readState();
  state.settings.appUrl = sanitizeBaseUrl(payload.appUrl);
  state.settings.token = String(payload.token || "").trim();
  state.settings.pollMinutes = toInt(payload.pollMinutes, DEFAULT_POLL_MINUTES);
  state.settings.syncEnabled = Boolean(payload.syncEnabled);
  await ensureClientId(state);
  await writeState(state);
  await scheduleSyncAlarm(state);
  if (state.settings.syncEnabled && state.sync.initialized) {
    await runSyncCycle();
  }
  return state;
}

async function resetSyncState() {
  const state = await readState();
  state.sync = {
    initialized: false,
    cursor: 0,
    lastSyncAt: null,
    lastError: null,
    lastNoOpReason: null,
  };
  state.outbox = [];
  state.maps = {
    bookmarkLocalToServer: {},
    bookmarkServerToLocal: {},
    folderLocalToServer: {},
    folderServerToLocal: {},
  };
  state.settings.syncEnabled = false;
  await writeState(state);
  await scheduleSyncAlarm(state);
  return state;
}

async function initializeRuntime() {
  const state = await readState();
  await ensureClientId(state);
  await writeState(state);
  await scheduleSyncAlarm(state);
  if (isSyncReady(state)) {
    await runSyncCycle();
  }
}

browser.runtime.onInstalled.addListener(() => {
  void initializeRuntime();
});

browser.runtime.onStartup.addListener(() => {
  void initializeRuntime();
});

browser.alarms.onAlarm.addListener((alarm) => {
  if (alarm && alarm.name === SYNC_ALARM) {
    void runSyncCycle();
  }
});

browser.bookmarks.onCreated.addListener((id, node) => {
  void handleBookmarkCreated(id, node);
});

browser.bookmarks.onChanged.addListener((id, _changeInfo) => {
  void handleBookmarkChanged(id);
});

browser.bookmarks.onMoved.addListener((id, _moveInfo) => {
  void handleBookmarkMoved(id);
});

browser.bookmarks.onRemoved.addListener((id, removeInfo) => {
  void handleBookmarkRemoved(id, removeInfo);
});

browser.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  const action = message && message.action;

  const handle = async () => {
    if (!action) {
      return { ok: false, error: "missing action" };
    }
    if (action === "getState") {
      return { ok: true, state: await readState() };
    }
    if (action === "saveSettings") {
      const state = await saveSettings(message);
      return { ok: true, state };
    }
    if (action === "preflightFirstSync") {
      const preflight = await preflightFirstSync(message.mode);
      return { ok: true, preflight };
    }
    if (action === "applyFirstSync") {
      const result = await applyFirstSync(
        message.mode,
        message.confirmationToken,
        message.typedPhrase,
        message.confirmChecked
      );
      return { ok: true, result };
    }
    if (action === "syncNow") {
      await runSyncCycle();
      return { ok: true };
    }
    if (action === "resetSyncState") {
      const state = await resetSyncState();
      return { ok: true, state };
    }
    if (action === "searchBookmarks") {
      const payload = await searchBookmarks(message.q, message.limit);
      return { ok: true, items: payload.items || [] };
    }
    return { ok: false, error: "unknown action" };
  };

  void handle()
    .then((result) => {
      sendResponse(result);
    })
    .catch((err) => {
      sendResponse({
        ok: false,
        error: err && err.message ? err.message : String(err),
      });
    });

  return true;
});

void initializeRuntime();
