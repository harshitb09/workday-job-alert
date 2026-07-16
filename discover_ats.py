#!/usr/bin/env python3
"""
Multi-ATS Discovery Tool (Greenhouse / Lever / Rippling)
-----------------------------------------------------------
Unlike Workday, these three platforms use simple, single-request public
APIs, and their company slug is almost always just the company name
(lowercase, no spaces) — so discovery here is fast: for each candidate in
candidates.yaml, try a handful of slug variants against each platform's
one canonical endpoint. No host-guessing, no site-slug-template
combinatorics.

Usage:
    python discover_ats.py                # scan all candidates, all 3 platforms
    python discover_ats.py --limit 20
    python discover_ats.py --platform greenhouse
"""

import argparse
import concurrent.futures
import re
import sys
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
CANDIDATES_PATH = ROOT / "candidates.yaml"
OUTPUT_PATH = ROOT / "discovered_ats_companies.yaml"

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; ATSDiscovery/1.0)",
}
REQUEST_TIMEOUT = 10
MAX_WORKERS = 10


def slug_variants(name, tenant_guess):
    """Generate a handful of plausible company slugs from its display name
    and existing tenant guess (reusing candidates.yaml's tenant field)."""
    base = re.sub(r"[^a-z0-9]", "", name.lower())
    dashed = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    variants = [tenant_guess, base, dashed]
    seen = set()
    out = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def check_greenhouse(slug):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
        jobs = data.get("jobs")
        if jobs is None:
            return None
        return len(jobs)
    except Exception:
        return None


def check_lever(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, list):
            return None
        return len(data)
    except Exception:
        return None


def check_rippling(slug):
    url = f"https://ats.rippling.com/api/v2/board/{slug}/jobs"
    try:
        resp = requests.get(url, headers=HEADERS, params={"page": 0, "pageSize": 1}, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
        total = data.get("totalItems")
        if total is None:
            return None
        return total
    except Exception:
        return None


CHECKERS = {
    "greenhouse": check_greenhouse,
    "lever": check_lever,
    "rippling": check_rippling,
}


def discover_one(name, tenant_guess, platforms):
    """Try each requested platform x slug variant. Returns dict of
    platform -> (slug, job_count) for confirmed (job_count > 0) matches."""
    variants = slug_variants(name, tenant_guess)
    found = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {}
        for platform in platforms:
            checker = CHECKERS[platform]
            for slug in variants:
                future = executor.submit(checker, slug)
                future_map[future] = (platform, slug)

        for future in concurrent.futures.as_completed(future_map):
            platform, slug = future_map[future]
            try:
                count = future.result()
            except Exception:
                count = None
            if count is not None and count > 0 and platform not in found:
                found[platform] = (slug, count)

    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--platform", choices=list(CHECKERS.keys()), default=None,
                         help="Only check this one platform (default: all three)")
    args = parser.parse_args()

    platforms = [args.platform] if args.platform else list(CHECKERS.keys())

    with open(CANDIDATES_PATH) as f:
        candidates = yaml.safe_load(f)["candidates"]
    if args.limit:
        candidates = candidates[: args.limit]

    results = {p: [] for p in platforms}

    for i, cand in enumerate(candidates, 1):
        name, tenant = cand["name"], cand["tenant"]
        print(f"[{i}/{len(candidates)}] {name}...", end=" ", flush=True)
        found = discover_one(name, tenant, platforms)
        if found:
            parts = [f"{p}:{slug} ({count} jobs)" for p, (slug, count) in found.items()]
            print(", ".join(parts))
            for p, (slug, count) in found.items():
                entry = {"name": name}
                if p == "greenhouse":
                    entry["board_token"] = slug
                elif p == "lever":
                    entry["company"] = slug
                elif p == "rippling":
                    entry["board_id"] = slug
                entry["jobs_seen"] = count
                results[p].append(entry)
        else:
            print("not found on any checked platform")

    with open(OUTPUT_PATH, "w") as f:
        f.write("# Auto-discovered by discover_ats.py — review before merging into config.yaml\n")
        f.write("# Each entry returned real job postings from the platform's public API.\n\n")
        yaml.dump(results, f, sort_keys=False, default_flow_style=False)

    total = sum(len(v) for v in results.values())
    print()
    for p in platforms:
        print(f"{p}: {len(results[p])} confirmed")
    print(f"Total: {total} confirmed matches written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
