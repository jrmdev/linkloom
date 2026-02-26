# LinkLoom Chrome Extension

This extension syncs the full Chrome bookmarks tree with a LinkLoom server.

## Load Unpacked in Chrome

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Click **Load unpacked**.
4. Select the `extension/chrome/` directory.

## Setup

1. Open extension preferences/options.
2. Set your LinkLoom `App URL` and API token from `/tokens`.
3. Choose first-sync mode and run preflight.
4. Confirm with the required phrase, then apply.

## Build Release Package

1. From the repository root, run `./extension/chrome/package-release.sh`.
2. The script outputs a versioned ZIP at `extension/chrome/dist/linkloom-chrome-<version>.zip`.
3. A matching SHA-256 checksum file is written beside it.

## First-Sync Safeguards

- All modes require explicit confirmation.
- `replace_local_with_server` no-ops when server bookmark count is `0`.
- `replace_server_with_local` no-ops when local bookmark count is `0`.

## Ongoing Sync

- Local bookmark changes are queued and pushed to `/api/v1/sync/push`.
- Periodic pull is driven by browser alarms using `/api/v1/sync/pull` + `/api/v1/sync/ack`.
