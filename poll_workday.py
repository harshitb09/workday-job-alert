#!/usr/bin/env python3
"""
Job Alert (multi-ATS)
----------------------
Polls each company's public applicant-tracking-system API — Workday,
Greenhouse, Lever, or Rippling — for each company in config.yaml, diffs
against a "seen jobs" state file, and posts new postings to a Discord
webhook.

(Filename stays poll_workday.py to avoid breaking existing GitHub Actions
workflow references, even though it now handles multiple ATS platforms.)

Designed to run on a schedule (cron / GitHub Actions). State is stored in
seen_jobs.json so repeated runs only alert on genuinely new postings.

Every fetch_jobs_<ats>() function returns a normalized list of dicts:
    {"id": ..., "title": ..., "url": ..., "posted": ...}
so the rest of the pipeline (dedup, keyword filtering, Discord alerts)
doesn't need to know which ATS a company is on.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

MAX_POSTING_AGE_HOURS = 24  # only alert on postings posted within this window


def _parse_iso(dt_str):
    """Parse an ISO-8601 timestamp (Greenhouse's updated_at, sometimes
    Rippling's postedAt). Returns a tz-aware datetime or None."""
    try:
        s = str(dt_str).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _parse_epoch_ms(value):
    """Parse a millisecond epoch timestamp (Lever's createdAt)."""
    try:
        ms = float(value)
        # Sanity check: epoch millis for any date since ~2001 is a large
        # number; anything smaller is probably seconds, not millis.
        if ms < 10_000_000_000:
            return None
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except Exception:
        return None


def _parse_workday_relative(text):
    """Parse Workday's relative postedOn text ('Posted Today', 'Posted 3
    Days Ago', 'Posted 30+ Days Ago') into an age in hours, or None if the
    text doesn't match a known pattern."""
    if not text:
        return None
    t = text.lower().strip()
    if "today" in t:
        return 0.0
    if "yesterday" in t:
        return 24.0
    m = re.search(r"(\d+)\+?\s*days?\s*ago", t)
    if m:
        return float(m.group(1)) * 24
    return None


def posting_age_hours(job, ats):
    """Best-effort age of a posting in hours. Returns None if the
    timestamp couldn't be parsed for this platform/format."""
    posted = job.get("posted")
    if not posted:
        return None

    if ats == "workday":
        return _parse_workday_relative(str(posted))

    if ats == "lever":
        dt = _parse_epoch_ms(posted)
        if dt is None:
            dt = _parse_iso(posted)
    else:
        # Greenhouse and Rippling both typically use ISO-8601 strings.
        dt = _parse_iso(posted)
        if dt is None:
            dt = _parse_epoch_ms(posted)

    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def is_recent(job, ats, max_age_hours=MAX_POSTING_AGE_HOURS):
    """True if the posting is within the alerting window. If the age can't
    be determined (unparseable/missing timestamp), defaults to True rather
    than silently dropping a possibly-genuine new posting — the tradeoff
    being an occasional older posting slipping through on platforms with
    inconsistent date formats, which is preferable to losing real alerts."""
    age = posting_age_hours(job, ats)
    if age is None:
        return True
    return age <= max_age_hours

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "seen_jobs.json"

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; JobAlertBot/1.0)",
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


def _request_with_retries(method, url, **kwargs):
    """Shared retry wrapper for all ATS fetchers. Returns parsed JSON or
    None if every attempt failed."""
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(RETRY_SLEEP)
    print(f"  [!] Request failed after {RETRIES} attempts: {url} ({last_err})")
    return None


def company_key(company):
    """Unique state-file key for a company entry, ATS-agnostic.

    Workday keeps its original key format (no ats prefix) so existing
    seen_jobs.json entries from before multi-ATS support keep matching —
    otherwise every existing Workday company would silently re-seed on
    the next run instead of picking up where it left off."""
    ats = company.get("ats", "workday")
    if ats == "workday":
        return f"{company['tenant']}::{company['site']}"
    elif ats == "greenhouse":
        return f"greenhouse::{company['board_token']}"
    elif ats == "lever":
        return f"lever::{company['company']}"
    elif ats == "rippling":
        return f"rippling::{company['board_id']}"
    else:
        raise ValueError(f"Unknown ats type: {ats}")


# --------------------------------------------------------------------------
# Workday
# --------------------------------------------------------------------------

def fetch_jobs_workday(company, page_size, max_jobs):
    """Paginate through a company's Workday job postings (CXS API, POST)."""
    url = f"https://{company['tenant']}.{company['wd_host']}/wday/cxs/{company['tenant']}/{company['site']}/jobs"
    base = f"https://{company['tenant']}.{company['wd_host']}/{company['site']}"
    normalized = []
    offset = 0

    while offset < max_jobs:
        body = {"appliedFacets": {}, "limit": page_size, "offset": offset, "searchText": ""}
        data = _request_with_retries("POST", url, headers=HEADERS, json=body)
        if data is None:
            break

        postings = data.get("jobPostings", [])
        total = data.get("total", 0)
        if not postings:
            break

        for job in postings:
            job_id = job.get("bulletFields", [None])[0] or job.get("externalPath")
            if not job_id:
                continue
            normalized.append({
                "id": job_id,
                "title": job.get("title", ""),
                "url": base + (job.get("externalPath") or ""),
                "posted": job.get("postedOn", ""),
            })

        offset += page_size
        if offset >= total:
            break

    return normalized


# --------------------------------------------------------------------------
# Greenhouse
# --------------------------------------------------------------------------

def fetch_jobs_greenhouse(company, page_size, max_jobs):
    """Single-request fetch — Greenhouse's public Job Board API returns
    everything in one response, no pagination needed."""
    token = company["board_token"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false"
    data = _request_with_retries("GET", url, headers=HEADERS)
    if data is None:
        return []

    normalized = []
    for job in data.get("jobs", []):
        job_id = job.get("id")
        if job_id is None:
            continue
        normalized.append({
            "id": str(job_id),
            "title": job.get("title", ""),
            "url": job.get("absolute_url", ""),
            "posted": job.get("updated_at", ""),
        })
    return normalized[:max_jobs]


# --------------------------------------------------------------------------
# Lever
# --------------------------------------------------------------------------

def fetch_jobs_lever(company, page_size, max_jobs):
    """Single-request fetch — Lever's public Postings API returns
    everything in one response, no pagination needed."""
    slug = company["company"]
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    data = _request_with_retries("GET", url, headers=HEADERS)
    if data is None:
        return []
    if not isinstance(data, list):
        # Lever returns a JSON error object (not a list) for unknown/disabled sites
        print(f"  [!] Unexpected Lever response shape for '{slug}' — site may not exist or public postings may be disabled")
        return []

    normalized = []
    for job in data:
        job_id = job.get("id")
        if not job_id:
            continue
        normalized.append({
            "id": job_id,
            "title": job.get("text", ""),
            "url": job.get("hostedUrl", ""),
            "posted": job.get("createdAt", ""),
        })
    return normalized[:max_jobs]


# --------------------------------------------------------------------------
# Rippling
# --------------------------------------------------------------------------

def fetch_jobs_rippling(company, page_size, max_jobs):
    """Paginate through a company's Rippling job board API."""
    board_id = company["board_id"]
    base_job_url = f"https://ats.rippling.com/{board_id}/jobs"
    normalized = []
    page = 0
    page_size_rippling = min(page_size, 50)  # Rippling's API caps around 50/page

    while len(normalized) < max_jobs:
        url = f"https://ats.rippling.com/api/v2/board/{board_id}/jobs"
        params = {"page": page, "pageSize": page_size_rippling}
        data = _request_with_retries("GET", url, headers=HEADERS, params=params)
        if data is None:
            break

        items = data.get("items", [])
        if not items:
            break

        for job in items:
            job_id = job.get("id") or job.get("jobId")
            if not job_id:
                continue
            title = job.get("title") or job.get("name") or ""
            # Rippling listings don't always include a direct job URL; fall
            # back to the board's jobs page with the id as an anchor guess.
            url_field = job.get("applyUrl") or job.get("url") or f"{base_job_url}/{job_id}"
            normalized.append({
                "id": str(job_id),
                "title": title,
                "url": url_field,
                "posted": job.get("postedAt") or job.get("createdAt") or "",
            })

        total_pages = data.get("totalPages", 1)
        page += 1
        if page >= total_pages:
            break

    return normalized[:max_jobs]


ATS_FETCHERS = {
    "workday": fetch_jobs_workday,
    "greenhouse": fetch_jobs_greenhouse,
    "lever": fetch_jobs_lever,
    "rippling": fetch_jobs_rippling,
}


def fetch_jobs(company, page_size, max_jobs):
    ats = company.get("ats", "workday")
    fetcher = ATS_FETCHERS.get(ats)
    if fetcher is None:
        print(f"  [!] Unknown ats type '{ats}' for {company.get('name')} — skipping")
        return []
    return fetcher(company, page_size, max_jobs)


def matches_keywords(title, keywords):
    if not keywords:
        return True
    title_lower = title.lower()
    return any(kw.lower() in title_lower for kw in keywords)


def send_discord_alert(company, job):
    """Attempts to send a Discord alert. Returns True on confirmed success,
    False if all retries failed (caller should NOT mark the job as seen in
    that case, so it gets retried on the next run instead of being lost)."""
    if not DISCORD_WEBHOOK_URL:
        print(f"  [new] {company['name']}: {job.get('title')} -> {job.get('url')}  (no webhook set, printed only)")
        return True  # nothing to retry — this is expected/intentional, not a failure

    embed = {
        "title": job.get("title") or "New Job Posting",
        "url": job.get("url") or None,
        "color": 5814783,
        "fields": [
            {"name": "Company", "value": company["name"], "inline": True},
        ],
    }
    if job.get("posted"):
        embed["fields"].append({"name": "Posted", "value": str(job["posted"]), "inline": True})

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


def main():
    config = load_config()
    companies = config.get("companies", [])
    page_size = config.get("page_size", 20)
    max_jobs = config.get("max_jobs_per_company", 300)

    state = load_state()
    total_new = 0

    for company in companies:
        key = company_key(company)
        seen_ids = set(state.get(key, []))
        ats = company.get("ats", "workday")

        print(f"Checking {company['name']} [{ats}] ({key})...")
        postings = fetch_jobs(company, page_size, max_jobs)
        print(f"  fetched {len(postings)} postings")

        current_ids = set()
        unseen_count = 0
        keyword_match_count = 0
        to_alert = []
        skipped_titles = []

        for job in postings:
            job_id = job["id"]
            current_ids.add(job_id)

            if job_id in seen_ids:
                continue
            unseen_count += 1

            if not matches_keywords(job.get("title", ""), company.get("keywords") or []):
                skipped_titles.append(job.get("title", "(no title)"))
                continue
            keyword_match_count += 1

            # Alert on anything new-to-us that's also recently posted
            # (within MAX_POSTING_AGE_HOURS) — covers both "brand new since
            # last check" and "posted up to a day ago, just discovered".
            # Once alerted (or skipped as too old), it's marked seen either
            # way below, so nothing is ever alerted on twice.
            if is_recent(job, ats):
                to_alert.append(job)

        print(f"  {unseen_count} new-to-us, {keyword_match_count} match keywords, "
              f"{len(to_alert)} within {MAX_POSTING_AGE_HOURS}h -> alerting {len(to_alert)}")
        if skipped_titles:
            print(f"  skipped (new but didn't match keywords): {skipped_titles}")

        failed_ids = set()
        for job in to_alert:
            success = send_discord_alert(company, job)
            if success:
                total_new += 1
            else:
                # Don't record this job as "seen" — leaving it out of
                # current_ids means next run will treat it as new again
                # and retry the Discord send, instead of silently losing
                # it the way a failed send used to.
                failed_ids.add(job["id"])
            time.sleep(1)  # be gentle with Discord rate limits

        # Everything fetched this run gets marked seen — including jobs
        # that didn't match keywords or were too old to alert on — so
        # nothing is ever re-evaluated or re-alerted on a later run.
        # Failed sends are the one exception: left out so they retry.
        state[key] = sorted(current_ids - failed_ids)

    save_state(state)
    print(f"Done. {total_new} new job(s) alerted.")


if __name__ == "__main__":
    sys.exit(main())
