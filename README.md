# LinkLoom

LinkLoom is a lightweight, self-hosted bookmark manager that keeps team bookmarks synchronized between a Flask + SQLite backend and a browser extension. It combines rich import/search/dead-link tooling with a guaranteed-safe sync protocol so you can backup or centralize all of your browser bookmarks without losing metadata or folder structure.

## What LinkLoom Does

- Keeps every bookmark, folder, tag, and note together so you can find, recover, or clear things without hunting across browsers.
- Fetches each URL to extract content and store it within the bookmark's notes to make it searchable.
- Lets a trusted admin start the app, invite others, and hand out tokens so everyone keeps working on their own bookmarks.
- Reads browser bookmark exports, keeps the folder nesting, and stores page text so the search box shows meaningful results.
- Makes it easy to browse, edit, or delete bookmarks in the web UI while catching dead links and refreshing content in the background.
- Links to the Firefox extension so bookmarks flow between your browser and the server without losing their folders.

## Sync Workflow

Sync keeps your Firefox bookmarks and LinkLoom in step with two phases: the first sync you run once, and the steady updates that follow.

### First Sync

- Pick how you want to sync (overwrite either side or merge) and follow the prompts. LinkLoom double-checks by asking you to type a confirmation phrase so you cannot accidentally wipe anything.
- The first sync is skipped if one side has nothing yet, so you won’t lose data when only one location has bookmarks.
- Folder order is preserved, so bookmarks land inside the same parent folders after the apply step.

### Ongoing Sync

- After the first sync, the Firefox extension quietly sends new bookmarks/folders to LinkLoom and regularly checks for updates to keep both sides matched.
- Folder changes stay in place, and background jobs refresh bookmark notes and link status without interrupting your work.

## How to Use LinkLoom

1. **Start the app** with one of the installers below and visit the web interface. The first visit walks you through making the admin account.
2. **Install a browser extension:** load the extension, point it at your LinkLoom URL, and paste a token from `/tokens` so bookmarks move both ways.
3. **Do a first sync:** you can either sync your browser bookmarks into the server (after a first install), sync the server's bookmarks into your browser (if you reinstalled your web browser), or do a 2-way sync.
4. **Boommarks will keep in sync:** When you add or modify bookmarks on your browser, it will update the server copy, and vice-versa.

## Browser Extension Integration

The extension keeps your browser bookmarks and LinkLoom in sync without forcing you to leave the browser. You can either load it temporarily for testing or install a release package for daily use.

1. For manual installation, view the instructions in `extension/firefox/` or `extension/chrome/`.
2. To build the distributable, run `./extension/<browser>/package-release.sh`; the script drops `extension/<browser>/dist/linkloom-<browser>-<version>.xpi` (plus a checksum) that you can drag into `about:addons` to install.
3. In the extension options, set `App URL` to your LinkLoom address and paste a token from `/tokens`, then run the guided first sync. Once the sync is done, the extension watches for changes and keeps both sides aligned automatically.

## Installation & Running LinkLoom

### Running via Docker Compose (recommended)

```bash
export SECRET_KEY=$(openssl rand -hex 32)
docker compose up --build
```

This builds the multi-stage Alpine image, mounts a named `linkloom-data` volume at `/data`, and exposes the Flask process. Compose maps host `5000` to container `5000` by default, but the Flask command inside the image still binds to port `8072`, so you can either visit `http://localhost:8072` or override `FLASK_RUN_PORT=5000` in the compose service.

### Running via `uv` (recommended if you don't want to use Docker)

1. If you haven't already, install uv (https://docs.astral.sh/uv/getting-started/installation/)
2. Install and run the app:
```bash
git clone https://github.com/jrmdev/linkloom.git && cd linkloom
uv add -r requirements.txt
uv run run.py --host 0.0.0.0 --port 8072
```

`uv` packages uvloop/Trio-powered execution and can be useful if you want slightly faster concurrency than the default Flask dev server. The Flask app stays the same; only the runner changes.

### Running via pip

### 1. Prepare the environment

```bash
git clone https://github.com/jrmdev/linkloom.git && cd linkloom
python3 -m venv .venv
source .venv/bin/activate
pip3 install -r requirements.txt
```

Set `SECRET_KEY` environment variables if you need to customize storage or scheduler behaviors.

### 2. Run the server

```bash
python3 run.py
```

`run.py` binds to `0.0.0.0:8072` by default, so browse to `http://localhost:8072` to finish the bootstrap flow. Logs appear on stdout, and the scheduler runs dead-link/import jobs automatically unless you set `SCHEDULER_ENABLED=0`.

### Running via Tailscale

If your tailnet has Tailscale Services enabled, you can expose LinkLoom at a private *.ts.net HTTPS URL and let Tailscale handle TLS.

1. In the Tailscale admin console, create a service (for example, linkloom) in Services.
2. Run LinkLoom locally on the host (for example on 127.0.0.1:8072).
3. On the LinkLoom host, advertise it as the backend for that service:

    `tailscale serve --service=svc:linkloom --https=443 http://127.0.0.1:8072`

4. Approve the service host advertisement in the Tailscale admin console.
5. After approval, LinkLoom will be reachable inside your tailnet at a URL like:

    `https://linkloom.<your-tailnet>.ts.net/`

6. Keep LinkLoom running on port 8072; Tailscale proxies HTTPS traffic to it over your Tailnet.

## Tech Stack

- Python 3.13 + Flask 3.1.1
- SQLite (via Flask-SQLAlchemy + Flask-Migrate)
- APScheduler for background sync/dead-link jobs
- BeautifulSoup, lxml, Trafilatura, httpx for import/content extraction
- RapidFuzz for fuzzy search and scoring
