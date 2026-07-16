# Workday Job Alert

Watches Workday-hosted career sites (companies like Nike, Salesforce, Adobe,
etc. that use Workday for their careers page) and pings a Discord channel
the moment a new job posting shows up.

It works by calling Workday's own internal job-search API (the same one the
careers page JavaScript calls), which returns clean JSON — no browser
automation, headless Chrome, or scraping HTML needed. That makes it fast and
cheap to run frequently.

## 1. Set up the Discord webhook

1. In your Discord server: **Server Settings → Integrations → Webhooks → New Webhook**
2. Name it, pick the channel, copy the **Webhook URL**.

## 2. Create a GitHub repo

1. Push everything in this folder to a new GitHub repo (public or private, either works).
2. Go to **Settings → Secrets and variables → Actions → New repository secret**
3. Name: `DISCORD_WEBHOOK_URL`, Value: the URL you copied above.

## 3. Edit `config.yaml`

Add the companies you want to track. For each one you need three values
pulled straight out of that company's careers URL:

```
https://{tenant}.{wd_host}/{site}
```

Example: `https://nike.wd1.myworkdayjobs.com/NikeCareers`
- tenant: `nike`
- wd_host: `wd1.myworkdayjobs.com`
- site: `NikeCareers` (case-sensitive)

Tip: open the careers page, open DevTools → Network tab, filter for
`jobs`, and reload / search — you'll see a POST request to
`.../wday/cxs/{tenant}/{site}/jobs`, which confirms the exact values.

Optionally set `keywords: ["software engineer", "backend"]` per company to
only get alerted on titles matching those terms. Leave `keywords: []` to
get every new posting.

## 4. Turn it on

The workflow in `.github/workflows/check-jobs.yml` runs every 15 minutes
automatically once pushed to GitHub (edit the cron line to change frequency
— 5 minutes is roughly the practical minimum GitHub honors reliably).

You can also trigger it manually anytime from the **Actions** tab →
"Check Workday Jobs" → **Run workflow**, which is the easiest way to test
your config before waiting for the schedule.

## How "new" is detected

- On the very first run for a company, it silently records every job
  currently posted (no alert spam for the whole backlog) and saves that
  list to `seen_jobs.json`.
- On every run after that, any posting not in `seen_jobs.json` is new and
  gets sent to Discord.
- `seen_jobs.json` is committed back to the repo by the workflow after each
  run, so state persists between scheduled runs.

## Running locally (optional)

```bash
pip install -r requirements.txt
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python poll_workday.py
```

Without `DISCORD_WEBHOOK_URL` set, it just prints what it would have sent —
useful for testing your `config.yaml` without spamming Discord.

## Notes / limitations

- Some Workday tenants rate-limit or occasionally return non-200 responses;
  the script retries 3x per request. If a company consistently fails, double
  check the tenant/wd_host/site values.
- `max_jobs_per_company` in `config.yaml` caps how many postings are scanned
  per run — raise it for companies with very large job boards.
- This only works for companies whose careers site is hosted directly on
  Workday's `myworkdayjobs.com` domain (not every company uses Workday).
