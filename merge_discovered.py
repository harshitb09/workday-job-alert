#!/usr/bin/env python3
"""
Merges the 'confirmed' companies from discovered_companies.yaml into
config.yaml, attaching the shared *role_keywords anchor to each one.
Skips any tenant/site combo already present in config.yaml (no duplicates).

Usage:
    python merge_discovered.py
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
DISCOVERED_PATH = ROOT / "discovered_companies.yaml"


def main():
    with open(DISCOVERED_PATH) as f:
        discovered = yaml.safe_load(f)

    confirmed = discovered.get("confirmed", [])
    if not confirmed:
        print("No confirmed companies in discovered_companies.yaml — nothing to merge.")
        return

    with open(CONFIG_PATH) as f:
        config_text = f.read()

    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)

    existing_keys = {(c["tenant"], c["site"]) for c in config.get("companies", [])}

    new_blocks = []
    added = 0
    skipped = 0

    for c in confirmed:
        key = (c["tenant"], c["site"])
        if key in existing_keys:
            skipped += 1
            continue
        block = (
            f"  - name: \"{c['name']}\"\n"
            f"    tenant: \"{c['tenant']}\"\n"
            f"    wd_host: \"{c['wd_host']}\"\n"
            f"    site: \"{c['site']}\"\n"
            f"    keywords: *role_keywords\n"
        )
        new_blocks.append(block)
        existing_keys.add(key)
        added += 1

    if not new_blocks:
        print(f"All {skipped} confirmed companies are already in config.yaml. Nothing to add.")
        return

    # Insert new blocks right after the last existing company block, i.e.
    # right before the first blank/comment line that follows "companies:".
    marker = "companies:\n"
    idx = config_text.index(marker) + len(marker)
    insertion = "".join(new_blocks) + "\n"
    updated_text = config_text[:idx] + insertion + config_text[idx:]

    with open(CONFIG_PATH, "w") as f:
        f.write(updated_text)

    print(f"Added {added} new companies to config.yaml ({skipped} were already present).")
    print("Review config.yaml, then commit and push.")


if __name__ == "__main__":
    main()
