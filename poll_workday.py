#!/usr/bin/env python3
"""
Workday Job Alert
------------------
Polls Workday-hosted career sites (the public CXS JSON API used by their
career site frontend) for each company in config.yaml, diffs against a
"seen jobs" state file, and posts new postings to a Discord webhook.

Designed to run on a schedule (cron / GitHub Actions). State is stored in
seen_jobs.json so repeated runs only alert on genuinely new postings.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "seen_jobs.json"

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; WorkdayJobAlert/1.0)",
}

REQUEST_TIMEOUT = 20
RETRIES = 3
RETRY_SLEEP = 3


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def cxs_url(company):
    return f"https://{company['tenant']}.{company['wd_host']}/wday/cxs/{company['tenant']}/{company['site']}/jobs"


def career_site_base(company):
    return f"https://{company['tenant']}.{company['wd_host']}/{company['site']}"


def fetch_jobs(company, page_size, max_jobs):
    """Paginate through a company's Workday job postings."""
    url = cxs_url(company)
    all_postings = []
    offset = 0

    while offset < max_jobs:
        body = {
            "appliedFacets": {},
            "limit": page_size,
            "offset": offset,
            "searchText": "",
        }

        last_err = None
        for attempt in range(1, RETRIES + 1):
            try:
                resp = requests.post(url, headers=HEADERS, json=body, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                last_err = None
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(RETRY_SLEEP)
        else:
            print(f"  [!] Failed to fetch {company['name']} at offset {offset}: {last_err}")
            break

        postings = data.get("jobPostings", [])
        total = data.get("total", 0)
        if not postings:
            break

        all_postings.extend(postings)
        offset += page_size

        if offset >= total:
            break

    return all_postings


def matches_keywords(title, keywords):
    if not keywords:
        return True
    title_lower = title.lower()
    return any(kw.lower() in title_lower for kw in keywords)


def send_discord_alert(company, job, link):
    """Attempts to send a Discord alert. Returns True on confirmed success,
    False if all retries failed (caller should NOT mark the job as seen in
    that case, so it gets retried on the next run instead of being lost)."""
    if not DISCORD_WEBHOOK_URL:
        print(f"  [new] {company['name']}: {job.get('title')} -> {link}  (no webhook set, printed only)")
        return True  # nothing to retry — this is expected/intentional, not a failure

    posted_on = job.get("postedOn", "")
    embed = {
        "title": job.get("title", "New Job Posting"),
        "url": link,
        "color": 5814783,
        "fields": [
            {"name": "Company", "value": company["name"], "inline": True},
        ],
    }
    if posted_on:
        embed["fields"].append({"name": "Posted", "value": posted_on, "inline": True})

    payload = {"embeds": [embed]}

    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                retry_after = resp.json().get("retry_after", 1)
                time.sleep(float(retry_after) + 0.5)
                continue
            resp.raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001
            print(f"  [!] Discord send failed (attempt {attempt}): {e}")
            time.sleep(RETRY_SLEEP)

    print(f"  [!] Giving up on '{job.get('title')}' after {RETRIES} attempts — will retry next run")
    return False


def send_discord_note(text):
    if not DISCORD_WEBHOOK_URL:
        print(f"  [note] {text}")
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": text}, timeout=REQUEST_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        print(f"  [!] Discord note failed: {e}")


def main():
    config = load_config()
    companies = config.get("companies", [])
    page_size = config.get("page_size", 20)
    max_jobs = config.get("max_jobs_per_company", 300)

    state = load_state()
    total_new = 0

    for company in companies:
        key = f"{company['tenant']}::{company['site']}"
        seen_ids = set(state.get(key, []))
        is_first_run = key not in state

        print(f"Checking {company['name']} ({key})...")
        postings = fetch_jobs(company, page_size, max_jobs)
        print(f"  fetched {len(postings)} postings")

        current_ids = set()
        new_postings = []

        for job in postings:
            job_id = job.get("bulletFields", [None])[0] or job.get("externalPath")
            if not job_id:
                continue
            current_ids.add(job_id)

            if job_id not in seen_ids:
                title = job.get("title", "")
                if matches_keywords(title, company.get("keywords") or []):
                    new_postings.append(job)

        if is_first_run:
            # Don't spam alerts for a company's entire existing job list on
            # the very first run — just seed state silently.
            print(f"  first run for {company['name']}: seeding {len(current_ids)} jobs, no alerts sent")
        else:
            failed_ids = set()
            for job in new_postings:
                link = career_site_base(company) + (job.get("externalPath") or "")
                job_id = job.get("bulletFields", [None])[0] or job.get("externalPath")
                success = send_discord_alert(company, job, link)
                if success:
                    total_new += 1
                else:
                    # Don't record this job as "seen" — leaving it out of
                    # current_ids means next run will treat it as new again
                    # and retry the Discord send, instead of silently
                    # losing it the way a failed send used to.
                    failed_ids.add(job_id)
                time.sleep(1)  # be gentle with Discord rate limits
            current_ids -= failed_ids

        state[key] = sorted(current_ids)

    save_state(state)
    print(f"Done. {total_new} new job(s) alerted.")


if __name__ == "__main__":
    sys.exit(main())
