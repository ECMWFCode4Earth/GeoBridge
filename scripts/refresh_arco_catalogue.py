#!/usr/bin/env python3
"""
refresh_arco_catalogue.py — generate arco_snapshot.yaml from ECMWF's
machine-readable ARCO STAC catalogue.

DO NOT RUN THIS AS A USER. This script is a maintenance tool for the
GeoBridge team only. End users get the snapshot bundled with the library
release they install.

What it does
------------
Crawls a three-level STAC hierarchy:

    https://arco.datastores.ecmwf.int/cadl-metadata/metadata/catalog.stac.json
      ├── {dataset}/product.stac.json        (collection-level metadata)
      │     └── {subset}/dataset.stac.json   (asset URLs + cube schema)

For each (dataset, subset) pair it captures:

  * The Zarr URLs (timeChunked, geoChunked, downsampled*)
  * The WMTS endpoint if available
  * The variable list with units, value ranges, and colormap hints
  * The cube dimensions (spatial bbox + resolution, temporal extent)
  * Provider info, descriptions, licences

Result is written to ``geobridge/semantic/arco_snapshot.yaml``.

When to run it
--------------
- Periodically (monthly is fine)
- Whenever ECMWF announces a new ARCO dataset
- Before publishing a new GeoBridge release


Usage
-----
    python scripts/refresh_arco_catalogue.py
    python scripts/refresh_arco_catalogue.py --limit 3              # quick test
    python scripts/refresh_arco_catalogue.py --output /tmp/test.yaml
    python scripts/refresh_arco_catalogue.py -v                     # verbose
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CATALOG_URL = (
    "https://arco.datastores.ecmwf.int/cadl-metadata/metadata/catalog.stac.json"
)
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parent.parent
    / "geobridge" / "semantic" / "arco_snapshot.yaml"
)

# Be polite to the catalogue host
REQUEST_DELAY_SECONDS = 0.1

# Truncate long descriptions in the snapshot to keep diffs readable
MAX_DESCRIPTION_CHARS = 800

logger = logging.getLogger("refresh_arco_catalogue")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _fetch_json(url: str, timeout: int = 30) -> dict:
    """GET *url* and return the parsed JSON body."""
    logger.debug("GET %s", url)
    req = urllib.request.Request(
        url, headers={
            "Accept": "application/json",
            "User-Agent": "geobridge-arco-refresh/0.1",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _resolve(base_url: str, href: str) -> str:
    """Resolve a STAC link href against its parent URL."""
    return urllib.parse.urljoin(base_url, href)


# ---------------------------------------------------------------------------
# Service detection from provider metadata
# ---------------------------------------------------------------------------

def _infer_service(product: dict) -> str:
    """Infer C3S / CAMS / CEMS from the product STAC metadata.

    Uses ``properties.providerMetadata.source`` when present
    (values: c3s, cams, cems), otherwise the dataset id prefix.
    """
    src = (
        product.get("properties", {})
        .get("providerMetadata", {})
        .get("source", "")
        .lower()
    )
    if src == "cams":
        return "CAMS"
    if src == "cems":
        return "CEMS"
    if src == "c3s":
        return "C3S"
    # Fallback to id prefix
    ds_id = product.get("id", "").lower()
    if ds_id.startswith("cams"):
        return "CAMS"
    if ds_id.startswith("cems"):
        return "CEMS"
    if ds_id.startswith("satellite"):
        return "C3S"   # satellite products are typically C3S-hosted
    return "C3S"


# ---------------------------------------------------------------------------
# Truncation / cleanup
# ---------------------------------------------------------------------------

def _trim_description(text: str) -> str:
    """Trim a long description to a single sensible paragraph."""
    text = (text or "").strip()
    if len(text) <= MAX_DESCRIPTION_CHARS:
        return text
    cut = text[:MAX_DESCRIPTION_CHARS]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(",.;:") + " …"


# ---------------------------------------------------------------------------
# Per-subset extraction
# ---------------------------------------------------------------------------

def _extract_zarr_assets(assets: dict) -> dict:
    """Pull out the Zarr asset URLs by their canonical names."""
    out: dict[str, str] = {}
    for key, asset in (assets or {}).items():
        a_type = (asset or {}).get("type", "")
        if a_type == "application/vnd+zarr":
            out[key] = asset.get("href", "")
    return out


def _extract_wmts_asset(assets: dict) -> Optional[str]:
    """Return the WMTS GetCapabilities URL if exposed as an asset."""
    wmts = (assets or {}).get("wmts") or (assets or {}).get("WMTS")
    if wmts and wmts.get("href"):
        return wmts["href"]
    return None


def _extract_thumbnail(assets: dict) -> str:
    """Return the thumbnail URL if present, else empty string."""
    thumb = (assets or {}).get("thumbnail")
    if thumb and thumb.get("href"):
        return thumb["href"]
    return ""


def _extract_variables(cube_vars: dict) -> dict:
    """Summarise the cube:variables block into a compact form."""
    out: dict[str, dict] = {}
    for short_name, info in (cube_vars or {}).items():
        if info.get("type") != "data":
            continue
        name_en = (info.get("name") or {}).get("en", short_name)
        out[short_name] = {
            "name": name_en,
            "unit": info.get("unit") or "",
            "standard_name": info.get("standardName") or "",
            "value_min": info.get("valueMin"),
            "value_max": info.get("valueMax"),
            "colormap": info.get("colormapId") or "",
            "colormap_invert": bool(info.get("colormapInvert")),
            "colormap_diff": info.get("colormapDiffId") or "",
            "log_scale": bool(info.get("hasLogScale")),
        }
    return out


def _extract_dimensions(cube_dims: dict) -> dict:
    """Summarise the cube:dimensions block into a compact form."""
    out: dict[str, dict] = {}
    for axis_name, info in (cube_dims or {}).items():
        d_type = info.get("type")
        if d_type == "spatial":
            out[axis_name] = {
                "kind": "spatial",
                "axis": info.get("axis"),
                "extent": info.get("extent"),
                "step": info.get("step"),
                "crs_epsg": info.get("reference_system"),
            }
        elif d_type == "temporal":
            out[axis_name] = {
                "kind": "temporal",
                "extent": info.get("extent"),
                "step": info.get("step"),
            }
    return out


def _summarise_subset(
    product_id: str, subset_id: str, item: dict, item_url: str,
) -> dict:
    """Reduce a dataset.stac.json item to the fields GeoBridge uses."""
    props = item.get("properties", {})
    return {
        "subset_id": subset_id,
        "title": (props.get("title") or
                  props.get("Title") or
                  props.get("admp_title") or
                  subset_id),
        "bbox": item.get("bbox"),
        "time_start": props.get("start_datetime"),
        "time_end": props.get("end_datetime"),
        "sample_period": props.get("admp_sample_period"),
        "in_preparation": bool(props.get("admp_in_preparation")),
        "zarr": _extract_zarr_assets(item.get("assets", {})),
        "wmts": _extract_wmts_asset(item.get("assets", {})),
        "dimensions": _extract_dimensions(props.get("cube:dimensions", {})),
        "variables": _extract_variables(props.get("cube:variables", {})),
        "item_url": item_url,
    }


# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------

def _list_root_children(root_url: str) -> list[tuple[str, str]]:
    """Return [(dataset_id, product_stac_url), ...] from the root catalogue."""
    root = _fetch_json(root_url)
    out: list[tuple[str, str]] = []
    for link in root.get("links", []):
        if link.get("rel") != "child":
            continue
        href = link.get("href")
        if not href:
            continue
        # The dataset id is the first path component in the href
        ds_id = href.split("/", 1)[0]
        out.append((ds_id, _resolve(root_url, href)))
    return out


def _fetch_subsets(product_url: str) -> list[tuple[str, str]]:
    """Return [(subset_id, dataset_stac_url), ...] for one product."""
    product = _fetch_json(product_url)
    out: list[tuple[str, str]] = []
    for link in product.get("links", []):
        if link.get("rel") != "item":
            continue
        href = link.get("href")
        if not href:
            continue
        subset_id = href.split("/", 1)[0]
        out.append((subset_id, _resolve(product_url, href)))
    return out, product


def _process_dataset(
    ds_id: str, product_url: str,
) -> Optional[dict]:
    """Fetch the product + all its subsets and assemble the snapshot entry."""
    try:
        subset_links, product = _fetch_subsets(product_url)
    except Exception as exc:
        logger.warning("  product fetch failed for %s: %s", ds_id, exc)
        return None

    subsets: dict[str, dict] = {}
    for subset_id, item_url in subset_links:
        try:
            item = _fetch_json(item_url)
            subsets[subset_id] = _summarise_subset(ds_id, subset_id, item, item_url)
            time.sleep(REQUEST_DELAY_SECONDS)
        except Exception as exc:
            logger.warning("  subset %s/%s fetch failed: %s", ds_id, subset_id, exc)

    return {
        "id": ds_id,
        "title": product.get("title", ds_id).strip(),
        "description": _trim_description(product.get("description", "")),
        "service": _infer_service(product),
        "license": product.get("license", "unknown"),
        "providers": [p.get("name") for p in product.get("providers", []) if p.get("name")],
        "thumbnail": _extract_thumbnail(product.get("assets", {})),
        "provider_url": (
            product.get("properties", {})
            .get("providerMetadata", {})
            .get("url", "")
        ),
        "product_url": product_url,
        "subsets": subsets,
    }


# ---------------------------------------------------------------------------
# Snapshot writing
# ---------------------------------------------------------------------------

def _write_snapshot(entries: list[dict], output_path: Path) -> None:
    """Write the snapshot to *output_path* as readable YAML."""
    try:
        import yaml
    except ImportError:
        logger.error("PyYAML not installed. Install with: pip install pyyaml")
        sys.exit(1)

    # Sort datasets alphabetically for stable diffs
    entries = sorted(entries, key=lambda e: e["id"])

    payload = {
        "_meta": {
            "generator": "scripts/refresh_arco_catalogue.py",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": CATALOG_URL,
            "dataset_count": len(entries),
            "subset_count": sum(len(e["subsets"]) for e in entries),
        },
        "datasets": {entry["id"]: entry for entry in entries},
    }

    # Disable YAML anchors so git diff stays readable
    class _NoAnchorsDumper(yaml.SafeDumper):
        def ignore_aliases(self, data):  # noqa: ARG002
            return True

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fp:
        yaml.dump(
            payload, fp,
            Dumper=_NoAnchorsDumper,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
            width=110,
        )
    logger.info("Wrote %d datasets / %d subsets to %s",
                len(entries),
                sum(len(e["subsets"]) for e in entries),
                output_path)


def _diff_summary(old_path: Path, new_entries: list[dict]) -> str:
    """Produce a text diff between the previous snapshot and *new_entries*."""
    if not old_path.exists():
        return f"  No previous snapshot at {old_path}.\n  All {len(new_entries)} entries are new."
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
    lines = [
        f"  Old snapshot:   {len(old_ids)} datasets",
        f"  New snapshot:   {len(new_ids)} datasets",
        f"  Unchanged ids:  {len(new_ids & old_ids)}",
        f"  Added:          {len(added)}",
        f"  Removed:        {len(removed)}",
    ]
    if added:
        lines.append("\n  Newly added datasets:")
        lines.extend(f"    + {dsid}" for dsid in added)
    if removed:
        lines.append("\n  Removed datasets:")
        lines.extend(f"    - {dsid}" for dsid in removed)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help=f"Output YAML path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process only the first N datasets (for quick testing)",
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

    logger.info("Step 1: list ARCO datasets")
    children = _list_root_children(CATALOG_URL)
    logger.info("  found %d datasets at root", len(children))

    if args.limit:
        logger.info("Limiting to first %d datasets (--limit)", args.limit)
        children = children[: args.limit]

    logger.info("Step 2: crawl product + subset metadata for %d datasets",
                len(children))
    entries: list[dict] = []
    for i, (ds_id, product_url) in enumerate(children, start=1):
        logger.info("  [%d/%d] %s", i, len(children), ds_id)
        entry = _process_dataset(ds_id, product_url)
        if entry is not None:
            entries.append(entry)
        time.sleep(REQUEST_DELAY_SECONDS)

    logger.info("Step 3: diff against previous snapshot")
    print("\nDiff summary:\n" + _diff_summary(args.output, entries) + "\n")

    logger.info("Step 4: write snapshot")
    _write_snapshot(entries, args.output)

    print("\nDone. ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
