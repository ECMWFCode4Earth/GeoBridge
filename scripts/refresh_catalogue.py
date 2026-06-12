#!/usr/bin/env python3
"""
refresh_catalogue.py — maintainer-side CDS catalogue snapshot refresh.

DO NOT RUN THIS AS A USER.  This script is a maintenance tool for the
GeoBridge team only.  Users get the catalogue snapshot bundled with the
library when they install it from PyPI.

What it does
------------
1. Paginates through the live ECMWF STAC catalogue API at
   https://cds.climate.copernicus.eu/api/catalogue/v1/datasets
2. Fetches the rich single-collection endpoint for each dataset to
   capture link relations (form, constraints, related, retrieve URL).
3. Extracts only the fields GeoBridge needs and writes a clean YAML
   snapshot to geobridge/semantic/cds_snapshot.yaml.
4. Prints a diff summary against the previous snapshot so the maintainer
   knows whether the change is worth committing.

When to run it
--------------
- Periodically (monthly is fine)
- Whenever ECMWF announces a new dataset
- Before publishing a new GeoBridge release

After running
-------------
    git diff geobridge/semantic/cds_snapshot.yaml      # review changes
    git add geobridge/semantic/cds_snapshot.yaml
    git commit -m "Refresh CDS catalogue snapshot"
    git push

Usage
-----
    python scripts/refresh_catalogue.py
    python scripts/refresh_catalogue.py --output /tmp/test_snapshot.yaml
    python scripts/refresh_catalogue.py --limit 5      # quick test
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CATALOGUE_BASE = "https://cds.climate.copernicus.eu/api/catalogue/v1"
DATASETS_ENDPOINT = f"{CATALOGUE_BASE}/datasets"
COLLECTIONS_ENDPOINT = f"{CATALOGUE_BASE}/collections"

DEFAULT_OUTPUT = (
    Path(__file__).resolve().parent.parent
    / "geobridge" / "semantic" / "cds_snapshot.yaml"
)

# Throttle between collection requests so we are a polite client.
REQUEST_DELAY_SECONDS = 0.1

# Description text in STAC entries can be 5000+ characters — truncate so the
# snapshot file stays readable.
MAX_DESCRIPTION_CHARS = 600

# Maximum number of related-dataset links to keep per entry.
MAX_RELATED_LINKS = 10

# Link relations we care about. Anything else is dropped from the snapshot.
KEPT_LINK_RELS = {"form", "constraints", "retrieve", "costing_api",
                  "layout", "related", "qa", "license"}

logger = logging.getLogger("refresh_catalogue")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _fetch_json(url: str, timeout: int = 30) -> dict:
    """GET *url* and return the parsed JSON body."""
    logger.debug("GET %s", url)
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "geobridge-refresh/0.1"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _list_datasets(limit: int = 100) -> list[dict]:
    """Return all collection summaries via paginated listing.

    Pagination follows the STAC convention — each response has a
    ``links`` array with optional ``rel: next`` entries.
    """
    all_collections: list[dict] = []
    url: Optional[str] = f"{DATASETS_ENDPOINT}?limit={limit}"
    page = 0
    while url:
        page += 1
        logger.info("Fetching page %d from %s", page, url)
        data = _fetch_json(url)
        page_collections = data.get("collections", [])
        all_collections.extend(page_collections)
        logger.info(
            "  page %d returned %d collections (running total %d / %s)",
            page, len(page_collections), len(all_collections),
            data.get("numberMatched", "?"),
        )

        # Find the 'next' link for pagination
        next_link = next(
            (link for link in data.get("links", []) if link.get("rel") == "next"),
            None,
        )
        url = next_link["href"] if next_link else None

    return all_collections


def _fetch_collection_detail(collection_id: str) -> Optional[dict]:
    """Fetch the rich single-collection endpoint for *collection_id*."""
    url = f"{COLLECTIONS_ENDPOINT}/{collection_id}"
    try:
        return _fetch_json(url)
    except urllib.error.HTTPError as exc:
        logger.warning("  detail fetch failed for %s (HTTP %d)", collection_id, exc.code)
        return None
    except Exception as exc:
        logger.warning("  detail fetch failed for %s: %s", collection_id, exc)
        return None


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------

def _service_from_keywords(keywords: list[str]) -> str:
    """Infer service (C3S/CAMS/CEMS) from keyword tags."""
    joined = " ".join(keywords).upper()
    if "CAMS" in joined or "ATMOSPHERE MONITORING" in joined:
        return "CAMS"
    if "CEMS" in joined or "EMERGENCY" in joined or "FIRE" in joined or "FLOOD" in joined:
        return "CEMS"
    if "C3S" in joined or "CLIMATE CHANGE" in joined:
        return "C3S"
    return "C3S"  # CDS is C3S by default


def _truncate_description(text: str) -> str:
    """Trim a long description to a sensible single-paragraph summary."""
    if len(text) <= MAX_DESCRIPTION_CHARS:
        return text.strip()
    truncated = text[:MAX_DESCRIPTION_CHARS]
    # Avoid cutting mid-word: rewind to last whitespace
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0]
    return truncated.strip() + " …"


def _extract_links(links: list[dict]) -> dict:
    """Pick out the link relations we care about.

    Multiple ``related`` entries become a list; everything else is a single URL.
    """
    extracted: dict[str, Any] = {}
    related: list[dict] = []
    for link in links or []:
        rel = link.get("rel")
        if rel not in KEPT_LINK_RELS:
            continue
        href = link.get("href")
        if not href:
            continue
        if rel == "related":
            related.append({
                "id": href.rstrip("/").split("/")[-1],
                "title": link.get("title", ""),
            })
        else:
            extracted[rel] = href
    if related:
        extracted["related"] = related[:MAX_RELATED_LINKS]
    return extracted


def _summarise_collection(collection: dict, detail: Optional[dict]) -> dict:
    """Reduce a STAC collection to the fields GeoBridge actually uses."""
    # Prefer detail (richer links) over list response.
    src = detail or collection
    extent = src.get("extent", {})
    spatial = extent.get("spatial", {}).get("bbox", [[None, None, None, None]])[0]
    temporal = extent.get("temporal", {}).get("interval", [[None, None]])[0]

    # Description: pick whichever of the two is longer (sometimes the list
    # response has the full text and detail has only a snippet, or vice versa).
    list_desc = collection.get("description", "") or ""
    detail_desc = (detail.get("description", "") if detail else "") or ""
    chosen_desc = list_desc if len(list_desc) >= len(detail_desc) else detail_desc

    summary = {
        "id": src["id"],
        "title": src.get("title", "").strip(),
        "description": _truncate_description(chosen_desc),
        "service": _service_from_keywords(src.get("keywords", [])),
        "keywords": src.get("keywords", []),
        "license": src.get("license", "unknown"),
        "providers": [p.get("name") for p in src.get("providers", []) if p.get("name")],
        "spatial_bbox": spatial,
        "temporal_interval": temporal,
        "thumbnail": (
            src.get("assets", {}).get("thumbnail", {}).get("href", "")
        ),
        "links": _extract_links(src.get("links", [])),
    }
    return summary


# ---------------------------------------------------------------------------
# Snapshot writing
# ---------------------------------------------------------------------------

def _write_snapshot(entries: list[dict], output_path: Path) -> None:
    """Write the snapshot to *output_path* in stable YAML."""
    try:
        import yaml
    except ImportError:
        logger.error("PyYAML not installed.  Install with: pip install pyyaml")
        sys.exit(1)

    payload = {
        "_meta": {
            "generator": "scripts/refresh_catalogue.py",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": DATASETS_ENDPOINT,
            "dataset_count": len(entries),
        },
        "datasets": {entry["id"]: entry for entry in sorted(entries, key=lambda e: e["id"])},
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Disable YAML anchors so the snapshot is easy to read in `git diff`.
    class _NoAnchorsDumper(yaml.SafeDumper):
        def ignore_aliases(self, data):  # noqa: ARG002
            return True

    with output_path.open("w", encoding="utf-8") as fp:
        yaml.dump(
            payload, fp,
            Dumper=_NoAnchorsDumper,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
            width=100,
        )
    logger.info("Wrote %d datasets to %s", len(entries), output_path)


def _summarise_diff(old_path: Path, new_entries: list[dict]) -> str:
    """Return a short text diff between the old snapshot and *new_entries*."""
    if not old_path.exists():
        return f"  (no previous snapshot at {old_path})\n  All {len(new_entries)} entries are new."

    try:
        import yaml
    except ImportError:
        return "  (PyYAML missing — cannot diff)"

    with old_path.open(encoding="utf-8") as fp:
        old = yaml.safe_load(fp) or {}
    old_ids = set((old.get("datasets") or {}).keys())
    new_ids = {e["id"] for e in new_entries}

    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)
    unchanged = len(new_ids & old_ids)

    lines = [
        f"  Old snapshot: {len(old_ids)} datasets",
        f"  New snapshot: {len(new_ids)} datasets",
        f"  Unchanged:   {unchanged}",
        f"  Added:       {len(added)}",
        f"  Removed:     {len(removed)}",
    ]
    if added:
        lines.append("\n  Newly added datasets:")
        lines.extend(f"    + {dsid}" for dsid in added[:20])
        if len(added) > 20:
            lines.append(f"    ... and {len(added) - 20} more")
    if removed:
        lines.append("\n  Removed datasets:")
        lines.extend(f"    - {dsid}" for dsid in removed[:20])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help=f"Output YAML path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process only the first N datasets (for quick testing)",
    )
    parser.add_argument(
        "--skip-detail", action="store_true",
        help="Skip the per-dataset detail fetch (faster but missing rich links)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.info("Step 1: list all datasets")
    collections = _list_datasets()

    if args.limit:
        logger.info("Limiting to first %d datasets (--limit)", args.limit)
        collections = collections[: args.limit]

    logger.info("Step 2: fetch detail for %d collections", len(collections))
    summarised: list[dict] = []
    for i, coll in enumerate(collections, start=1):
        cid = coll["id"]
        if args.skip_detail:
            detail = None
        else:
            detail = _fetch_collection_detail(cid)
            time.sleep(REQUEST_DELAY_SECONDS)
        try:
            summarised.append(_summarise_collection(coll, detail))
        except Exception as exc:
            logger.error("  failed to summarise %s: %s", cid, exc)
        if i % 20 == 0:
            logger.info("  processed %d / %d", i, len(collections))

    logger.info("Step 3: diff against previous snapshot")
    print("\nDiff summary:\n" + _summarise_diff(args.output, summarised) + "\n")

    logger.info("Step 4: write snapshot")
    _write_snapshot(summarised, args.output)

    print("\nDone.  Review changes with:")
    print(f"    git diff {args.output}")
    print("Then commit with:")
    print(f"    git add {args.output}")
    print('    git commit -m "Refresh CDS catalogue snapshot"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
