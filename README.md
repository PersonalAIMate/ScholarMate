# ScholarMate

**Live site:** https://scholarmate.org

ScholarMate recommends the latest, most relevant papers from across the web for
you — based on your Google Scholar profile or a set of keywords — and can email
them to you on a schedule. Each recommendation is tagged with its **source
venue/platform** (e.g. *Nature*, *IEEE Robotics & Automation Letters*,
*NeurIPS*, *arXiv*). It ships in two forms that share the same recommendation
logic:

- **Web app** (Flask, deployed on Vercel) — accounts, dashboard, scheduled email digests.
- **Chrome extension** — popup + options, fetches Scholar from *your* browser to
  avoid server-side rate limits, with background alarms for periodic refresh.

---

## How it works

1. **Keywords** — from your Scholar profile (research interests + frequent words
   in your paper titles) or a manual comma-separated list.
2. **Search** — queries several free, keyless scholarly APIs **concurrently** for
   recent papers matching those keywords: [arXiv](https://info.arxiv.org/help/api/),
   [OpenAlex](https://openalex.org/), [Crossref](https://www.crossref.org/),
   [Semantic Scholar](https://www.semanticscholar.org/product/api), and
   [Europe PMC](https://europepmc.org/). These aggregators index Nature, Science,
   IEEE, ACM, the major conferences, etc., so the real venue is shown as each
   paper's source badge. Each source is **best-effort** — if one is rate-limited
   or down, the others still return results.
3. **Merge & rank** — results are de-duplicated by title (a paper found on
   multiple platforms shows e.g. `arXiv · Nature`), then scored by **TF-IDF**
   over the keyword set (rarer keywords weigh more), plus a title-match bonus,
   and the top *K* are returned.

> Google Scholar has no public API and blocks scraping, so it is **not** a fetch
> source — your Scholar *profile URL* is only used to derive keywords. The web
> app also throttles Refresh (serving cache within `REFRESH_MIN_INTERVAL_SEC`)
> and falls back to your last cached results when sources are unavailable.

> The ranking is implemented twice — `arxiv_client.py` (`rank_papers`) for the
> web app and `background.js` (`rankPapers`) for the extension. They are kept
> deliberately in sync; **if you change one, change the other.**

---

## Project layout

```
app.py            Flask app: auth, dashboard, /api/papers, /api/cron/digest
paper_sources.py  Multi-source fetch (arXiv/OpenAlex/Crossref/S2/EuropePMC) + merge
arxiv_client.py   Scholar keyword extraction + arXiv search + TF-IDF ranking (web)
db_adapter.py     DB layer: SQLite locally, Neon Postgres on Vercel
emailer.py        SMTP email sending + digest rendering
templates/        Jinja templates (base, login, register, dashboard)
static/           CSS, favicons, logo
background.js     Extension service worker (mirrors arxiv_client logic)
popup.js / popup.html, options.js / options.html, manifest.json   Extension
vercel.json       Vercel build, routing, and cron schedule
```

---

## Local development (web app)

Requires Python 3.9+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Optional: set a stable session key (otherwise an ephemeral one is used)
export SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

python app.py            # http://localhost:5000
```

With no `POSTGRES_URL` set, it uses a local SQLite file (`scholarmate.db`).

---

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `SECRET_KEY` | **Yes in production** | Signs session cookies. App refuses to boot on Vercel without it. |
| `POSTGRES_URL` | Prod | Neon/Postgres connection string (Vercel integration sets this). Falls back to `DATABASE_URL` / `POSTGRES_URL_NON_POOLING`, else SQLite. |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASS` | For email | SMTP credentials. Any provider (Gmail, Resend, SendGrid, SES…). Email features are no-ops until all three are set. |
| `SMTP_PORT` | No | SMTP port (default `587`). |
| `SMTP_FROM` | No | From address (default = `SMTP_USER`). |
| `SMTP_USE_TLS` | No | `1` (default) for STARTTLS, `0` to disable. |
| `CRON_SECRET` | For digests | Shared secret protecting `/api/cron/digest`. Vercel sends it as a Bearer token. |
| `SENTRY_DSN` | No | Enables Sentry error monitoring when set. |
| `SENTRY_TRACES_SAMPLE_RATE` | No | Sentry performance sample rate (default `0.0`). |
| `LOG_LEVEL` | No | Logging level (default `INFO`). |
| `SOURCES` | No | Comma list of enabled paper sources (default `arxiv,openalex,crossref,semantic_scholar,europepmc`). |
| `RECENCY_DAYS` | No | "Latest" window in days for date-bounded sources (default `365`). |
| `CONTACT_EMAIL` | No | Contact passed to OpenAlex/Crossref for polite, higher-rate API access. |
| `REFRESH_MIN_INTERVAL_SEC` | No | Min seconds between live fetches per user; Refresh serves cache within this window (default `60`). |

---

## Deployment (Vercel)

The repo is wired for Vercel via `vercel.json` (Python build, catch-all route to
`app.py`). Pushing to `main` deploys when the project's Git integration is
connected.

1. Set the environment variables above in **Project → Settings → Environment Variables**
   (at minimum `SECRET_KEY`, plus `POSTGRES_URL` via the Neon integration).
2. Database tables are created automatically on cold start (`init_db()`), so no
   manual migration is needed.

### Scheduled email digests

`vercel.json` defines a weekly cron (Mondays 08:00 UTC) hitting
`/api/cron/digest`. To activate end-to-end:

1. Configure the `SMTP_*` variables.
2. Set `CRON_SECRET` (Vercel injects it into the cron request's Authorization header).

Until both are set the endpoint is a safe no-op. You can trigger it manually for
testing with `GET /api/cron/digest?key=<CRON_SECRET>`.

---

## Analytics

`base.html` includes the Vercel Web Analytics script. It only collects data once
Analytics is enabled for the project in the Vercel dashboard (Project →
Analytics → Enable).

---

## Chrome extension

Load unpacked from the repo root (`chrome://extensions` → Developer mode → Load
unpacked). Configure email/keywords/Scholar URL/schedule in the options page.
The extension fetches Scholar from your own browser, so it isn't subject to the
server-IP rate limits the web app can hit.
