#!/usr/bin/env python3
"""
Workday Discovery Tool
-----------------------
Takes candidates.yaml (company name + best-guess tenant slug) and tries
real combinations of Workday hosts + site-slug patterns against each one,
using the actual CXS jobs API. Anything that returns a real result (HTTP
200 + at least one job posting, or a nonzero `total`) gets written to
discovered_companies.yaml in the exact format config.yaml expects.

This exists because Workday site-slugs are NOT guessable from a company
name (see Nike's "nke" vs the obvious "NikeCareers", or PayPal's "jobs" vs
"JobSearch") — the only reliable way to find them is to actually query the
API and see what responds. Run this from GitHub Actions (unrestricted
network) rather than locally if your own network blocks myworkdayjobs.com.

Usage:
    python discover_workday.py                  # scan all candidates
    python discover_workday.py --limit 20        # scan first 20 only
    python discover_workday.py --tenant nike      # scan a single tenant guess
"""

import argparse
import sys
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
CANDIDATES_PATH = ROOT / "candidates.yaml"
OUTPUT_PATH = ROOT / "discovered_companies.yaml"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; WorkdayDiscovery/1.0)",
}

# Workday shards observed in the wild. New tenants get assigned to one of
# these; a company's site could be on any of them.
WD_HOSTS = [
    "wd1.myworkdayjobs.com",
    "wd3.myworkdayjobs.com",
    "wd5.myworkdayjobs.com",
    "wd10.myworkdayjobs.com",
    "wd12.myworkdayjobs.com",
]

# Generic site-slug patterns companies commonly use for their public
# career site (based on confirmed real examples: Visa, Workday, Salesforce,
# Adobe, PayPal, Nike, Micron, Motorola Solutions).
GENERIC_SITE_SLUGS = [
    "External",
    "Careers",
    "External_Career_Site",
    "CorporateCareers",
    "Global_Careers",
    "GlobalCareers",
    "External_Careers",
    "ExternalCareers",
    "Search",
    "jobs",
    "Jobs",
    "External_Experienced",
    "external_experienced",
    "Experienced",
    "Career",
    "CareerSite",
]


def site_slug_candidates(tenant):
    """Generate slug guesses for a tenant: generic patterns plus
    tenant-prefixed variants (observed pattern, e.g. Cisco -> Cisco_Careers,
    S&P Global/spgi -> SPGI_Careers)."""
    slugs = list(GENERIC_SITE_SLUGS)
    cap = tenant.capitalize()
    upper = tenant.upper()
    slugs += [
        tenant,                 # e.g. tenant name itself as the site (Visa, Workday)
        cap,
        upper,
        f"{cap}_Careers",
        f"{upper}_Careers",
        f"{cap}Careers",
        f"{upper}Careers",
        f"{cap}_Career_Site",
        f"{cap}_External_Career_Site",
    ]
    # de-dupe while preserving order
    seen = set()
    out = []
    for s in slugs:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


REQUEST_TIMEOUT = 10
MAX_WORKERS = 8  # concurrent requests per tenant scan


def try_combination(tenant, host, site):
    url = f"https://{tenant}.{host}/wday/cxs/{tenant}/{site}/jobs"
    body = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
    try:
        resp = requests.post(url, headers=HEADERS, json=body, timeout=REQUEST_TIMEOUT)
    except Exception:
        return None

    if resp.status_code != 200:
        return None

    try:
        data = resp.json()
    except Exception:
        return None

    total = data.get("total")
    if total is None:
        return None
    # total == 0 is ambiguous (could be a valid-but-empty site, or a
    # not-quite-right site slug that still returns 200). Treat >0 as a
    # confident match; total == 0 gets reported separately as "uncertain".
    return total


def discover(tenant):
    """Try host x site combinations for a tenant, in parallel batches,
    stopping as soon as a confirmed (nonzero-job) match is found. Returns
    list of (host, site, total) matches found before stopping, best first.
    """
    import concurrent.futures

    combos = [(host, site) for host in WD_HOSTS for site in site_slug_candidates(tenant)]
    matches = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_combo = {
            executor.submit(try_combination, tenant, host, site): (host, site)
            for host, site in combos
        }
        found_confirmed = False
        for future in concurrent.futures.as_completed(future_to_combo):
            host, site = future_to_combo[future]
            try:
                total = future.result()
            except Exception:
                total = None
            if total is not None:
                matches.append((host, site, total))
                if total > 0:
                    found_confirmed = True
            if found_confirmed:
                # Cancel remaining not-yet-started futures; ones already
                # in flight will still complete but we stop waiting.
                for f in future_to_combo:
                    f.cancel()
                break

    matches.sort(key=lambda m: m[2], reverse=True)
    return matches


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Only scan the first N candidates")
    parser.add_argument("--tenant", type=str, default=None, help="Scan only this single tenant guess")
    args = parser.parse_args()

    with open(CANDIDATES_PATH) as f:
        candidates = yaml.safe_load(f)["candidates"]

    if args.tenant:
        candidates = [c for c in candidates if c["tenant"] == args.tenant]
        if not candidates:
            print(f"No candidate with tenant '{args.tenant}' in candidates.yaml")
            sys.exit(1)
    elif args.limit:
        candidates = candidates[: args.limit]

    confirmed = []
    empty = []
    not_found = []

    for i, cand in enumerate(candidates, 1):
        name, tenant = cand["name"], cand["tenant"]
        print(f"[{i}/{len(candidates)}] {name} (tenant guess: {tenant})...", end=" ", flush=True)
        matches = discover(tenant)

        nonzero = [m for m in matches if m[2] > 0]
        zero = [m for m in matches if m[2] == 0]

        if nonzero:
            host, site, total = nonzero[0]
            print(f"FOUND -> {tenant}.{host}/{site} ({total} jobs)")
            confirmed.append({"name": name, "tenant": tenant, "wd_host": host, "site": site, "jobs_seen": total})
        elif zero:
            host, site, total = zero[0]
            print(f"uncertain (200 OK but 0 jobs) -> {tenant}.{host}/{site}")
            empty.append({"name": name, "tenant": tenant, "wd_host": host, "site": site})
        else:
            print("not found")
            not_found.append(name)

    with open(OUTPUT_PATH, "w") as f:
        f.write("# Auto-discovered by discover_workday.py — review before merging into config.yaml\n")
        f.write("# 'confirmed' entries returned real job postings and are safe to trust.\n")
        f.write("# 'uncertain' entries returned HTTP 200 with 0 jobs — could be a genuinely\n")
        f.write("#   empty career site, or a near-miss slug. Double check manually.\n")
        f.write("# 'not_found' — no combination of host/site tried returned a valid response.\n")
        f.write("#   This company likely isn't on Workday, or uses a slug pattern not in\n")
        f.write("#   SITE_SLUG_TEMPLATES — check their careers page manually if you care about it.\n\n")
        yaml.dump(
            {
                "confirmed": confirmed,
                "uncertain": empty,
                "not_found": not_found,
            },
            f,
            sort_keys=False,
            default_flow_style=False,
        )

    print()
    print(f"Done. {len(confirmed)} confirmed, {len(empty)} uncertain, {len(not_found)} not found.")
    print(f"Results written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
