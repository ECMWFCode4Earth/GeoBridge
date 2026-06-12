"""
GeoBridge — free-text search → dataset details + all constructed URLs.

Usage:
    python examples/search_and_urls.py "urban heat island"
    python examples/search_and_urls.py "air quality pm2.5 europe"
    python examples/search_and_urls.py "precipitation flood"

What it does (one module per step):
    1. semantic.engine   — resolve free text to ranked use-case matches
    2. modules.discover  — build full LayerDescriptor for each matched dataset
    3. modules.form      — fetch the CDS form schema (available variables,
                           product types, date ranges) from the live CDS API
    4. modules.wmts      — construct WMTS GetTile / GetCapabilities URLs
    5. modules.style     — show which QGIS / ESRI style preset would be used
"""

from __future__ import annotations

import sys
import textwrap
from datetime import datetime, timezone

import geobridge as gb


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEP  = "=" * 72
_SEP2 = "-" * 72


def _indent(text: str, n: int = 4) -> str:
    return textwrap.indent(str(text), " " * n)


def _print_section(title: str) -> None:
    print(f"\n  [{title}]")


def _print_kv(key: str, value: str, indent: int = 6) -> None:
    prefix = " " * indent
    print(f"{prefix}{key}: {value}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(query: str) -> None:

    print(_SEP)
    print(f"  GeoBridge search:  \"{query}\"")
    print(_SEP)

    # ------------------------------------------------------------------
    # Step 1 — Semantic engine: resolve free text → use-case matches
    # ------------------------------------------------------------------
    _print_section("1 · semantic_search  (semantic/engine.py)")
    matches = gb.semantic_search(query, max_results=3)
    if not matches:
        print("    No semantic matches found. Try a different query.")
        return

    for i, m in enumerate(matches, 1):
        print(f"\n    Match #{i}  confidence={m.confidence:.2f}")
        _print_kv("use_case",   m.use_case_label)
        _print_kv("theme",      m.theme_label)
        _print_kv("dataset_id", m.dataset_id)
        _print_kv("variable",   m.variable)
        _print_kv("access",     m.recommended_access)
        if m.requires_fusion:
            _print_kv("note", "⚠ requires fusion of multiple datasets")

    # Work with the top match from here on
    top = matches[0]
    dataset_id = top.dataset_id
    variable   = top.variable

    print(f"\n  → working with top match: {dataset_id!r}  variable={variable!r}")

    # ------------------------------------------------------------------
    # Step 2 — discover_one: build LayerDescriptor
    # ------------------------------------------------------------------
    print(f"\n{_SEP2}")
    _print_section("2 · discover_one  (modules/discover.py)")

    ds = gb.discover_one(dataset_id)
    if ds is None:
        # semantic engine may return a CDS id with hyphens; try underscore form
        ds = gb.discover_one(dataset_id.replace("-", "_"))
    if ds is None:
        print(f"    Dataset {dataset_id!r} not found in catalogue snapshots.")
        return

    print(f"\n    {ds!r}")
    _print_kv("title",     ds.title)
    _print_kv("service",   ds.service)
    _print_kv("bbox",      f"W={ds.bbox[0]}  S={ds.bbox[1]}  E={ds.bbox[2]}  N={ds.bbox[3]}")
    _print_kv("time_range",
              f"{ds.time_range[0].strftime('%Y-%m-%d')} → "
              f"{ds.time_range[1].strftime('%Y-%m-%d')}")
    _print_kv("variables",  ", ".join(ds.variables[:10]) + ("…" if len(ds.variables) > 10 else ""))
    _print_kv("has_zarr",  str(ds.has_zarr))
    _print_kv("has_wmts",  str(ds.has_wmts))
    _print_kv("has_cds",   str(ds.has_cds_retrieve))

    # ------------------------------------------------------------------
    # Step 3 — form.py: fetch live CDS form schema
    # ------------------------------------------------------------------
    print(f"\n{_SEP2}")
    _print_section("3 · fetch_form / fetch_constraints  (modules/form.py)")

    # Use the CDS hyphenated id for the form API
    cds_id = dataset_id.replace("_", "-")

    form = None
    if ds.cds_form_url:
        try:
            form = gb.fetch_form(cds_id)
        except Exception as exc:
            print(f"    fetch_form skipped: {exc}")

    if form:
        print(f"\n    FormSchema for {cds_id!r}")
        _print_kv("widgets", str(len(form.widgets)))
        for w in form.widgets[:6]:
            vals = w.value_list
            options_preview = ", ".join(vals[:5])
            if len(vals) > 5:
                options_preview += " …"
            _print_kv(f"  {w.name} ({w.widget_type})", options_preview or "(no values)")

        # valid variables for the first available product type
        try:
            pt_widget = form.product_type_widget
            if pt_widget:
                pt_values = pt_widget.value_list
                if pt_values:
                    pt = pt_values[0]
                    valid_vars = gb.valid_variables_for_product_type(cds_id, pt)
                    _print_kv("valid_variables_for_product_type",
                               f"{pt!r} → {valid_vars[:6]} {'…' if len(valid_vars) > 6 else ''}")
        except Exception:
            pass
    else:
        print("    (form metadata not available for this dataset)")

    # ------------------------------------------------------------------
    # Step 4 — wmts.py: construct tile URLs
    # ------------------------------------------------------------------
    print(f"\n{_SEP2}")
    _print_section("4 · wmts_layer  (modules/wmts.py)")

    # Use a recent date well within most dataset coverage
    demo_date = "2023-07-15T12:00:00Z"

    wmts = None
    if ds.has_zarr or ds.has_wmts:
        try:
            wmts = gb.wmts_layer(
                dataset=dataset_id,
                variable=variable,
                datetime=demo_date,
            )
        except Exception as exc:
            print(f"    wmts_layer skipped: {exc}")

    if wmts:
        print(f"\n    WmtsLayer  layer_name={wmts.layer_name!r}")
        print()
        _print_kv("GetTile URL (Leaflet/OL template)", "")
        print(_indent(wmts.url, 8))
        print()
        _print_kv("GetCapabilities URL", "")
        print(_indent(wmts.get_capabilities_url, 8))
        print()
        _print_kv("QGIS XYZ uri", "")
        qgis = wmts.to_qgis()
        print(_indent(qgis["uri"], 8))
        print()
        _print_kv("Leaflet config", "")
        leaf = wmts.to_leaflet()
        for k, v in leaf["options"].items():
            print(_indent(f"{k}: {v}", 10))
    else:
        print("    WMTS not available for this dataset.")

    # ------------------------------------------------------------------
    # ARCO Zarr download URLs
    # ------------------------------------------------------------------
    print(f"\n{_SEP2}")
    _print_section("5a · ARCO Zarr URLs  (fast, synchronous — modules/extract.py)")

    if ds.has_zarr:
        print()
        if ds.zarr_time_chunked:
            _print_kv("time_chunked  (spatial maps, short time windows)", "")
            print(_indent(ds.zarr_time_chunked, 8))
        if ds.zarr_geo_chunked:
            print()
            _print_kv("geo_chunked   (long time series at a point)", "")
            print(_indent(ds.zarr_geo_chunked, 8))
        print()
        print("    Usage:  gb.zarr_to_geotiff(")
        print(f"                dataset={dataset_id!r},")
        print(f"                variable={variable!r},")
        print( "                bbox=(west, south, east, north),")
        print( "                time_range=('2023-07-01', '2023-07-31'),")
        print( "            )")
    else:
        print("    ARCO Zarr store not available for this dataset.")

    # ------------------------------------------------------------------
    # CDS API download URLs
    # ------------------------------------------------------------------
    print(f"\n{_SEP2}")
    _print_section("5b · CDS API URLs  (async, queued — modules/cds_download.py)")

    if ds.has_cds_retrieve:
        print()
        _print_kv("retrieve endpoint  (POST to queue a download job)", "")
        print(_indent(ds.cds_retrieve_url, 8))
        if ds.cds_form_url:
            print()
            _print_kv("form schema JSON  (describes valid request parameters)", "")
            print(_indent(ds.cds_form_url, 8))
        if ds.cds_constraints_url:
            print()
            _print_kv("constraints JSON  (valid parameter combinations)", "")
            print(_indent(ds.cds_constraints_url, 8))

        # Build a minimal example POST body from the form schema
        print()
        _print_kv("example POST body", "")
        sample_pt = "reanalysis"
        if form:
            pt_widget = form.product_type_widget
            if pt_widget and pt_widget.value_list:
                sample_pt = pt_widget.value_list[0]
        import json
        sample_body = {
            "inputs": {
                "product_type": sample_pt,
                "variable": variable,
                "year": "2023",
                "month": "07",
                "day": "15",
                "time": "12:00",
                "format": "netcdf",
            }
        }
        print(_indent(json.dumps(sample_body, indent=2), 8))
        print()
        print("    Usage:  gb.cds_to_geotiff(")
        print(f"                dataset={cds_id!r},")
        print(f"                request={{'variable': {variable!r}, 'year': '2023', ...}},")
        print( "            )")
    else:
        print("    CDS retrieve URL not available for this dataset.")

    # ------------------------------------------------------------------
    # Step 5 — style.py: show which style preset applies
    # ------------------------------------------------------------------
    print(f"\n{_SEP2}")
    _print_section("6 · style presets  (modules/style.py)")

    print(f"\n    Colormap hint from LayerDescriptor:  {ds.colormap}")
    print(f"\n    to_qgis_style() / to_esri_lyrx() accept:")
    _print_kv("dataset", dataset_id)
    _print_kv("variable", variable)
    print("    (call with output_path= to write a .qml or .lyrx file)")

    print(f"\n{_SEP}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python examples/search_and_urls.py \"<free text query>\"")
        sys.exit(1)
    main(" ".join(sys.argv[1:]))
