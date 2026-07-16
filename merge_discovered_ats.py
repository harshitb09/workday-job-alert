#!/usr/bin/env python3
"""
Merges confirmed Greenhouse/Lever/Rippling companies from
discovered_ats_companies.yaml into config.yaml, attaching the shared
*role_keywords anchor to each one. Skips duplicates already present.

Usage:
    python merge_discovered_ats.py
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
DISCOVERED_PATH = ROOT / "discovered_ats_companies.yaml"

# Maps platform -> the field that uniquely identifies a company on that
# platform, used both for building the config block and de-duplication.
PLATFORM_ID_FIELD = {
    "greenhouse": "board_token",
    "lever": "company",
    "rippling": "board_id",
}


def main():
    with open(DISCOVERED_PATH) as f:
        discovered = yaml.safe_load(f) or {}

    with open(CONFIG_PATH) as f:
        config_text = f.read()
    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)

    existing_keys = set()
    for c in config.get("companies", []):
        ats = c.get("ats", "workday")
        if ats == "workday":
            existing_keys.add(("workday", c.get("tenant"), c.get("site")))
        else:
            id_field = PLATFORM_ID_FIELD.get(ats)
            if id_field:
                existing_keys.add((ats, c.get(id_field)))

    new_blocks = []
    added = 0
    skipped = 0

    for platform, id_field in PLATFORM_ID_FIELD.items():
        for entry in discovered.get(platform, []):
            key = (platform, entry[id_field])
            if key in existing_keys:
                skipped += 1
                continue
            block = (
                f"  - name: \"{entry['name']}\"\n"
                f"    ats: \"{platform}\"\n"
                f"    {id_field}: \"{entry[id_field]}\"\n"
                f"    keywords: *role_keywords\n"
            )
            new_blocks.append(block)
            existing_keys.add(key)
            added += 1

    if not new_blocks:
        print(f"No new companies to add ({skipped} were already present).")
        return

    marker = "companies:\n"
    idx = config_text.index(marker) + len(marker)
    insertion = "".join(new_blocks) + "\n"
    updated_text = config_text[:idx] + insertion + config_text[idx:]

    with open(CONFIG_PATH, "w") as f:
        f.write(updated_text)

    print(f"Added {added} new companies to config.yaml ({skipped} were already present).")


if __name__ == "__main__":
    main()
