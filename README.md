# Workday Job Alert

Watches job postings across **Workday, Greenhouse, Lever, and Rippling**
career sites and pings a Discord channel the moment a new posting shows up
matching your keywords.

It works by calling each platform's own public job-search API directly (no
browser automation, headless Chrome, or HTML scraping) — fast and cheap to
run frequently.

| Platform | How it works | Reliability |
|---|---|---|
| Workday | `myworkdayjobs.com` CXS API | High — but tenant/site slugs must be discovered (not guessable) |
| Greenhouse | `boards-api.greenhouse.io` Job Board API | High — single request, slug is almost always the plain company name |
| Lever | `api.lever.co` Postings API | High — single request, slug is almost always the plain company name |
| Rippling | `ats.rippling.com` board API | High — same pattern as Lever/Greenhouse |
| iCIMS | *(not implemented)* | No public API exists; would require fragile HTML scraping that breaks across iCIMS versions. Ask if you still want an experimental version. |

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

To seed `seen_jobs.json` from the current postings without sending alerts (for
historical backfills), run:

```bash
python poll_workday.py --backfill
```

Posts are now also filtered by location against each company's `locations`
entries, using the location text returned by each ATS API. The built-in
`location_keywords` example in `config.yaml` targets the USA and Germany.

## Adding companies on Greenhouse, Lever, or Rippling

These are simpler than Workday — no host/site-slug guessing, just one
identifier per platform. Add a block to `config.yaml` like:

```yaml
  - name: "Some Company"
    ats: "greenhouse"
    board_token: "somecompany"
    keywords: *role_keywords

  - name: "Some Other Company"
    ats: "lever"
    company: "someothercompany"
    keywords: *role_keywords

  - name: "A Third Company"
    ats: "rippling"
    board_id: "athirdcompany"
    keywords: *role_keywords
```

To find the right value: visit the company's careers page. If the URL looks
like `boards.greenhouse.io/companyname`, `jobs.lever.co/companyname`, or
`ats.rippling.com/companyname/jobs`, that `companyname` segment is exactly
what goes in `board_token` / `company` / `board_id`.

Easiest way to find a lot of these at once: run the **"Discover
Greenhouse/Lever/Rippling Companies"** GitHub Action (Actions tab → select
it → Run workflow). It tests `candidates.yaml` against all three
platforms' real APIs and auto-merges whatever's confirmed into
`config.yaml` — same idea as the Workday discovery tool, just faster since
these platforms don't need the host/slug-pattern brute force Workday does.

## Scaling to 150+ Workday companies (auto-discovery)

Workday site-slugs aren't guessable from a company name — Nike's is `nke`,
not `NikeCareers`; PayPal's is `jobs`, not `JobSearch`. Guessing wrong just
fails silently. So instead of guessing, this repo includes a **discovery
tool** that actually tests real combinations against the live API and only
keeps what's confirmed to work.

1. `candidates.yaml` — 150+ well-known companies (tech, finance, pharma,
   healthcare, industrials, retail) with a best-guess tenant slug each.
2. `discover_workday.py` — for each candidate, tries real Workday hosts
   (wd1, wd3, wd5, wd10, wd12) × common site-slug patterns, in parallel,
   and stops as soon as it finds a combination that returns actual job
   postings. Writes results to `discovered_companies.yaml`, sorted into
   `confirmed` / `uncertain` (200 OK, 0 jobs — could be legit-but-empty or
   a near-miss) / `not_found`.
3. `merge_discovered.py` — appends every `confirmed` company into
   `config.yaml` (with the shared `*role_keywords` filter already
   attached), skipping anything already present.

**Easiest way to run this: the "Discover Workday Companies" GitHub Action**
(Actions tab → select it → Run workflow). It runs discovery, auto-merges
confirmed companies into `config.yaml`, and commits the result — because
this makes a lot of requests, your sandbox/local network may get
throttled, but GitHub Actions' runner won't be.

Or run it locally:
```bash
python discover_workday.py          # scan all candidates
python discover_workday.py --limit 20   # just the first 20, for a quick test
python merge_discovered.py          # fold confirmed results into config.yaml
```

Not every candidate will resolve — many large companies (Google, Amazon,
Microsoft, Apple, JPMorgan, Goldman Sachs, etc.) simply don't use Workday
for their public career site, so `not_found` is expected and not a bug.
Check `discovered_companies.yaml` afterward to see exactly what happened
with each one. Feel free to add more names to `candidates.yaml` and re-run.

## Notes / limitations

- Some Workday tenants rate-limit or occasionally return non-200 responses;
  the script retries 3x per request. If a company consistently fails, double
  check the tenant/wd_host/site values.
- `max_jobs_per_company` in `config.yaml` caps how many postings are scanned
  per run — raise it for companies with very large job boards.
- This only works for companies whose careers site is hosted directly on
  Workday's `myworkdayjobs.com` domain (not every company uses Workday).
