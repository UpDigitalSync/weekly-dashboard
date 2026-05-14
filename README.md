# Weekly Strategy Dashboard

Autonomous AI agent that pulls data from 3 pillars every Monday at 08:00 UTC,
runs it through Claude, and publishes an HTML dashboard to GitHub Pages.

**Pillars:**
1. **TikTok** — `@routebites`, `@neuro_dispenza`, `@undermapped` via Apify
2. **SEO** — triptiplist.com via GA4 Data API
3. **Knowledge Engine** — X trends via TwitterAPI.io

## Files

```
weekly-dashboard/
├── agent.py                    # main script — fetch, synthesize, render
├── template.html               # Jinja2 template for index.html
├── requirements.txt
├── .env.example                # copy to .env for local runs
├── .gitignore
├── .github/workflows/weekly.yml
├── data/                       # raw JSON per run (auto-created)
└── archive/                    # week-YYYY-MM-DD.html (auto-created)
```

## Credentials required

Five values. Put them in `.env` for local runs, or in **Settings → Secrets and
variables → Actions** in the GitHub repo for production.

| Name | Where to get it | What it's for |
|---|---|---|
| `APIFY_TOKEN` | console.apify.com → Settings → Integrations | TikTok scraping |
| `GA4_PROPERTY_ID` | GA4 → Admin → Property Settings (9-digit number) | triptiplist.com traffic |
| `GOOGLE_SA_JSON` | Google Cloud → IAM → Service Accounts (download JSON, paste full contents) | GA4 auth |
| `TWITTER_API_KEY` | twitterapi.io dashboard | X trends scan |
| `DEEPSEEK_API_KEY` | platform.deepseek.com/api_keys | DeepSeek synthesis (deepseek-chat, OpenAI-compatible) |

### GA4 one-time setup
1. Google Cloud Console → APIs & Services → **Enable Google Analytics Data API**
2. Create a Service Account → **download JSON key**
3. GA4 Admin → Property Access Management → **add the service account email**
   (`name@project.iam.gserviceaccount.com`) as **Viewer**
4. Paste the full JSON file contents into `GOOGLE_SA_JSON` (it stays on one line)

## Local run

```sh
cp .env.example .env
# fill in real values
pip install -r requirements.txt
python agent.py
```

Output: `index.html` (latest) + `archive/week-YYYY-MM-DD.html` (this run).
Raw API responses land in `data/YYYY-MM-DD.json` for debugging.

The script degrades gracefully — if one source is missing credentials it
returns an error in that section but the dashboard still renders for the
others. So you can start with just `APIFY_TOKEN` + `ANTHROPIC_API_KEY` if
GA4 and TwitterAPI.io aren't ready yet.

## Production deploy

1. Create a public GitHub repo: `UpDigitalSync/weekly-dashboard`
2. Push these files to it
3. **Settings → Pages** → Source: `Deploy from a branch`, `main` / root
4. **Settings → Secrets and variables → Actions** → add all 5 secrets above
5. **Actions** tab → run "Weekly Strategy Report" once manually to verify
6. Optional: set up `dashboard.triptiplist.com` CNAME

The Action runs every Monday 08:00 UTC automatically, commits a fresh
`index.html` + a new archive entry, and pushes to `main`. GitHub Pages
redeploys on push.

## Cost

| Component | Cost / week |
|---|---|
| GitHub Actions | free (well under 2000 min/mo) |
| GitHub Pages | free |
| Apify TikTok scrape (3 profiles) | ~$0.25–0.50 |
| GA4 Data API | free |
| TwitterAPI.io | per current plan |
| DeepSeek API (deepseek-chat, ~1 call/week, ~30K tokens) | ~$0.01 |
| **Total** | **~$2–3 / month** |
