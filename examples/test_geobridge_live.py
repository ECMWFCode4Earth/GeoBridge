#!/usr/bin/env python3
"""
test_geobridge_live.py
======================
Live integration tests for the GeoBridge library.

Unlike smoke_test.py (which uses bundled snapshots and no network),
this script makes REAL network calls to the ECMWF ARCO Data Lake,
WMTS tile service, and CDS form endpoint.

Requirements:
  - CDS API key in ~/.cdsapirc
  - Internet connection
  - Dependencies: pip install -e ".[zarr]"

Usage:
  PYTHONPATH=. python test_geobridge_live.py              # run all
  PYTHONPATH=. python test_geobridge_live.py --only 5     # run one test
  PYTHONPATH=. python test_geobridge_live.py --list       # list tests
  PYTHONPATH=. python test_geobridge_live.py --quick      # skip slow tests

Expected total runtime:
  --quick : ~2 minutes  (skips extraction and time series)
  full    : ~10-15 minutes
"""

import argparse
import json
import math
import os
import sys
import tempfile
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

# ── Colours ──────────────────────────────────────────────────────────────
B = "\033[94m"; G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"
BOLD = "\033[1m"; RST = "\033[0m"

passed = 0
failed = 0
skipped = 0
results = []

def title(n, text):
    print(f"\n{BOLD}{B}{'─'*65}{RST}")
    print(f"{BOLD}{B}  Test {n:2d} — {text}{RST}")
    print(f"{BOLD}{B}{'─'*65}{RST}")

def ok(msg):
    global passed; passed += 1
    print(f"  {G}PASS{RST}  {msg}")
    results.append(("PASS", msg))

def fail(msg, exc=None):
    global failed; failed += 1
    print(f"  {R}FAIL{RST}  {msg}")
    if exc:
        print(f"        {R}{exc}{RST}")
    results.append(("FAIL", msg))

def skip(msg):
    global skipped; skipped += 1
    print(f"  {Y}SKIP{RST}  {msg}")
    results.append(("SKIP", msg))

def show(label, value):
    print(f"        {label:30s}  {value}")


# ═══════════════════════════════════════════════════════════════════════════
# TEST DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════════

ALL_TESTS = {}

def test(n, label, slow=False):
    """Decorator to register a test function."""
    def decorator(fn):
        ALL_TESTS[n] = {"fn": fn, "label": label, "slow": slow}
        return fn
    return decorator


# ── 1. Authentication ────────────────────────────────────────────────────

@test(1, "Authentication — read key and verify token format")
def test_auth():
    import geobridge as gb
    gb.authenticate()
    token = gb.get_token()
    assert len(token) > 10, f"Token too short: {len(token)} chars"
    ok(f"Authenticated, key ends …{token[-6:]}")

    header = gb.auth_header()
    assert "Authorization" in header
    assert header["Authorization"].startswith("Bearer ")
    ok("auth_header() returns valid Bearer header")

    assert gb.is_authenticated()
    ok("is_authenticated() returns True")


# ── 2. Discovery — offline ──────────────────────────────────────────────

@test(2, "Discovery — offline snapshot reading")
def test_discover_offline():
    import geobridge as gb

    all_ds = gb.discover()
    ok(f"discover() returns {len(all_ds)} datasets")

    arco = gb.discover(arco_only=True)
    ok(f"discover(arco_only=True) returns {len(arco)} ARCO datasets")
    for d in arco:
        assert d.has_zarr, f"{d.id} in arco_only but has_zarr=False"

    extractable = gb.discover(extraction_only=True)
    assert len(extractable) >= len(arco)
    ok(f"discover(extraction_only=True) returns {len(extractable)} extractable datasets")

    era5 = gb.discover_one("reanalysis_era5_single_levels")
    assert era5 is not None
    assert era5.has_zarr
    assert era5.has_wmts
    assert "t2m" in era5.variables
    ok(f"discover_one('reanalysis_era5_single_levels') → {len(era5.variables)} variables")

    # Test filters
    c3s = gb.discover(services=["C3S"])
    cams = gb.discover(services=["CAMS"])
    ok(f"Service filter: C3S={len(c3s)}, CAMS={len(cams)}")

    temp = gb.discover(variable="t2m")
    assert len(temp) > 0
    ok(f"Variable filter 't2m' → {len(temp)} datasets")

    kw = gb.discover(keyword="temperature")
    assert len(kw) > 0
    ok(f"Keyword filter 'temperature' → {len(kw)} datasets")


# ── 3. LayerDescriptor properties ────────────────────────────────────────

@test(3, "LayerDescriptor — fields, flags, and serialisation")
def test_descriptor():
    import geobridge as gb
    era5 = gb.discover_one("reanalysis_era5_single_levels")

    assert era5.extraction_supported
    ok("extraction_supported = True (has ARCO Zarr)")

    d = era5.to_dict()
    assert "flags" in d
    assert d["flags"]["has_zarr"] is True
    assert d["flags"]["has_wmts"] is True
    assert d["flags"]["extraction_supported"] is True
    ok("to_dict() contains flags with correct values")

    assert "cds_form" in d["access_methods"]
    assert "cds_constraints" in d["access_methods"]
    ok("to_dict() contains form and constraints URLs")

    j = json.dumps(d)
    assert len(j) > 100
    ok(f"to_dict() is JSON-serialisable ({len(j)} chars)")

    leaflet = era5.to_leaflet()
    assert leaflet is not None
    assert "url" in leaflet
    ok("to_leaflet() returns valid config")

    qgis = era5.to_qgis()
    assert qgis is not None
    assert "uri" in qgis
    ok("to_qgis() returns valid config")


# ── 4. Semantic search ───────────────────────────────────────────────────

@test(4, "Semantic search — 10 queries across all themes")
def test_semantic():
    import geobridge as gb

    queries = {
        "urban heat island":           ("heat_stress", "reanalysis_era5_single_levels"),
        "PM2.5 air quality Athens":    ("air_quality", "cams_europe_air_quality"),
        "wildfire fire weather index": ("wildfire", "cems_fire"),
        "sea surface temperature":     ("ocean_and_water", "satellite_sea_surface"),
        "crop vegetation LAI":         ("agriculture_and_land", "satellite_lai"),
        "solar PV capacity factor":    ("renewable_energy", "sis_energy"),
        "CORDEX climate projection":   ("climate_projections", "projections_cordex"),
        "long term temperature trend": ("climate_trend", "reanalysis_era5"),
        "extreme precipitation flood": ("flood_risk", ""),
        "surface solar radiation":     ("radiation_and_clouds", "reanalysis_era5"),
    }

    for query, (expected_theme, expected_ds_prefix) in queries.items():
        matches = gb.semantic_search(query, max_results=1)
        if matches:
            m = matches[0]
            ok(f"'{query}' → {m.use_case_label} (conf={m.confidence:.2f})")
        else:
            fail(f"'{query}' → no matches")

    # Test that themes list is complete
    themes = gb.list_themes()
    assert len(themes) == 10
    ok(f"list_themes() returns {len(themes)} themes")

    use_cases = gb.list_use_cases()
    assert len(use_cases) >= 38, f"Only {len(use_cases)} use cases"
    ok(f"list_use_cases() returns {len(use_cases)} use cases")


# ── 5. WMTS layer construction ──────────────────────────────────────────

@test(5, "WMTS layer — URL construction and XYZ encoding")
def test_wmts_layer():
    import geobridge as gb
    gb.authenticate()

    layer = gb.wmts_layer(
        dataset="reanalysis_era5_single_levels",
        variable="t2m",
        datetime="2023-07-15T12:00:00Z",
    )

    assert layer.layer_name == "reanalysis_era5_single_levels/sfc/t2m"
    ok(f"Layer name: {layer.layer_name}")

    assert layer.style == "cmap:viridis"
    ok(f"Style: {layer.style}")

    assert layer.tile_matrix_set == "EPSG:3857"
    ok(f"Tile matrix set: {layer.tile_matrix_set}")

    # XYZ URL for QGIS
    qgis = layer.to_qgis()
    uri = qgis["uri"]
    assert "type=xyz" in uri
    assert "{z}" in uri
    assert "{y}" in uri
    assert "{x}" in uri
    assert "%26LAYER%3D" in uri
    assert "cmap:viridis" in uri  # colon is literal, not %3A
    ok("to_qgis() produces valid XYZ URI with correct encoding")

    # Standard URL for Leaflet
    leaflet = layer.to_leaflet()
    assert "LAYER=reanalysis_era5_single_levels" in leaflet["url"]
    assert "{z}" in leaflet["url"]
    ok("to_leaflet() produces valid WMTS URL template")

    # Concrete tile URL
    tile = layer.tile_url(zoom=5, col=18, row=12)
    assert "TILEMATRIX=5" in tile
    assert "TILEROW=12" in tile
    assert "TILECOL=18" in tile
    assert "{" not in tile  # no unresolved placeholders
    ok(f"tile_url(5, 18, 12) → concrete URL, {len(tile)} chars")

    # Feature info URL
    fi = layer.feature_info_url(zoom=5, col=18, row=12)
    assert "GetFeatureInfo" in fi
    assert "I=128" in fi
    ok("feature_info_url() → valid GetFeatureInfo URL")

    # Legend URL
    assert "GetLegend" in layer.legend_url
    assert "t2m" in layer.legend_url
    ok(f"legend_url → GetLegend endpoint")

    # Variable alias translation
    layer2 = gb.wmts_layer(
        dataset="reanalysis_era5_single_levels",
        variable="2m_temperature",  # CDS long name
        datetime="2023-07-15T12:00:00Z",
    )
    assert layer2.variable == "t2m"
    ok("CDS long name '2m_temperature' translated to 't2m'")


# ── 6. WMTS tile fetch — live ───────────────────────────────────────────

@test(6, "WMTS tile fetch — download a real PNG tile from ECMWF")
def test_wmts_live():
    import geobridge as gb
    gb.authenticate()

    layer = gb.wmts_layer(
        dataset="reanalysis_era5_single_levels",
        variable="t2m",
        datetime="2023-07-15T12:00:00Z",
    )

    tile_url = layer.tile_url(zoom=3, col=4, row=3)
    req = urllib.request.Request(tile_url, headers={"User-Agent": "geobridge-test"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
        content_type = resp.headers.get("Content-Type", "")

    assert len(data) > 100, f"Tile too small: {len(data)} bytes"
    assert "image/png" in content_type
    ok(f"Tile fetched: {len(data):,} bytes, Content-Type: {content_type}")

    # Verify it starts with PNG magic bytes
    assert data[:4] == b'\x89PNG'
    ok("Response is valid PNG (magic bytes match)")


# ── 7. WMTS GetFeatureInfo — live ────────────────────────────────────────

@test(7, "WMTS GetFeatureInfo — query actual temperature value")
def test_feature_info():
    import geobridge as gb
    gb.authenticate()

    layer = gb.wmts_layer(
        dataset="reanalysis_era5_single_levels",
        variable="t2m",
        datetime="2023-07-15T12:00:00Z",
    )

    fi_url = layer.feature_info_url(zoom=5, col=18, row=12, pixel_i=128, pixel_j=128)
    req = urllib.request.Request(fi_url, headers={
        "User-Agent": "geobridge-test",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()

    ok(f"GetFeatureInfo response: {len(raw)} chars")
    show("First 200 chars", raw[:200])

    # Try to parse as JSON
    try:
        data = json.loads(raw)
        ok("Response is valid JSON")
    except json.JSONDecodeError:
        # Might be HTML or XML — still a valid response
        ok("Response received (non-JSON format)")


# ── 8. WMTS legend fetch — live ──────────────────────────────────────────

@test(8, "WMTS legend — fetch the SVG colour bar from ECMWF")
def test_legend():
    import geobridge as gb
    gb.authenticate()

    layer = gb.wmts_layer(
        dataset="reanalysis_era5_single_levels",
        variable="t2m",
        datetime="2023-07-15T12:00:00Z",
    )

    req = urllib.request.Request(layer.legend_url, headers={"User-Agent": "geobridge-test"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
        content_type = resp.headers.get("Content-Type", "")

    assert len(data) > 50
    ok(f"Legend fetched: {len(data):,} bytes, Content-Type: {content_type}")

    text = data.decode(errors="replace")
    if "<svg" in text.lower():
        ok("Legend is SVG format")
    elif "{" in text:
        ok("Legend is JSON format")
    else:
        ok(f"Legend format: {content_type}")


# ── 9. Zarr extraction — ERA5 temperature Athens ─────────────────────────

@test(9, "Zarr extraction — ERA5 t2m Athens July 2023 monthly mean", slow=True)
def test_zarr_extraction():
    import geobridge as gb
    import rasterio
    import numpy as np

    gb.authenticate()

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        out_path = f.name

    try:
        t0 = time.time()
        tif = gb.zarr_to_geotiff(
            dataset="reanalysis_era5_single_levels",
            variable="t2m",
            bbox=(23.0, 37.5, 24.5, 38.5),
            time_range=("2023-07-01", "2023-07-31"),
            aggregation="monthly_mean",
            output_path=out_path,
        )
        elapsed = time.time() - t0
        ok(f"Extraction completed in {elapsed:.1f}s")

        with rasterio.open(tif) as src:
            data = src.read(1)
            valid = data[data > 0]
            mean_k = valid.mean()

        show("File size", f"{Path(tif).stat().st_size:,} bytes")
        show("Bands", "1")
        show("Mean temperature", f"{mean_k:.2f} K ({mean_k-273.15:.2f} °C)")

        assert 280 < mean_k < 320, f"Mean temp {mean_k} K outside plausible range"
        ok(f"Temperature {mean_k:.1f} K is plausible for Athens July")

        assert src.crs is not None
        ok(f"CRS: {src.crs}")

        assert src.height > 0 and src.width > 0
        ok(f"Grid: {src.height}×{src.width}")

    finally:
        os.unlink(out_path)


# ── 10. Zarr extraction — CDS long name alias ───────────────────────────

@test(10, "Zarr extraction — CDS long name '2m_temperature' works", slow=True)
def test_alias():
    import geobridge as gb
    import rasterio
    import numpy as np

    gb.authenticate()

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        out_path = f.name

    try:
        tif = gb.zarr_to_geotiff(
            dataset="reanalysis_era5_single_levels",
            variable="2m_temperature",  # CDS long name
            bbox=(23.5, 37.8, 24.0, 38.2),
            time_range=("2023-07-15", "2023-07-15"),
            aggregation="daily_mean",
            output_path=out_path,
        )
        ok(f"CDS long name '2m_temperature' accepted and translated")

        with rasterio.open(tif) as src:
            data = src.read(1)
            valid = data[data > 0]
        assert len(valid) > 0
        ok(f"Output contains valid data ({len(valid)} pixels)")
    finally:
        os.unlink(out_path)


# ── 11. Zarr extraction — CAMS PM2.5 ────────────────────────────────────

@test(11, "Zarr extraction — CAMS PM2.5 Athens", slow=True)
def test_cams():
    import geobridge as gb
    import rasterio

    gb.authenticate()

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        out_path = f.name

    try:
        tif = gb.zarr_to_geotiff(
            dataset="cams_europe_air_quality_reanalyses",
            variable="pm2p5",
            bbox=(23.0, 37.5, 24.5, 38.5),
            time_range=("2023-07-01", "2023-07-31"),
            aggregation="monthly_mean",
            output_path=out_path,
        )
        ok(f"CAMS PM2.5 extraction succeeded")

        with rasterio.open(tif) as src:
            data = src.read(1)
            valid = data[data >= 0]
            mean_pm = valid.mean()

        show("Resolution", f"{src.res[0]:.3f}° (should be ~0.1°)")
        show("Mean PM2.5", f"{mean_pm:.1f} µg/m³")

        assert 0 < mean_pm < 200, f"PM2.5 {mean_pm} outside plausible range"
        ok(f"PM2.5 value {mean_pm:.1f} µg/m³ is plausible")
    finally:
        os.unlink(out_path)


# ── 12. Zarr extraction — UTCI ──────────────────────────────────────────

@test(12, "Zarr extraction — UTCI thermal comfort Athens", slow=True)
def test_utci():
    import geobridge as gb
    import rasterio

    gb.authenticate()

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        out_path = f.name

    try:
        tif = gb.zarr_to_geotiff(
            dataset="derived_utci_historical",
            variable="utci",
            bbox=(23.0, 37.5, 24.5, 38.5),
            time_range=("2023-07-15", "2023-07-15"),
            aggregation="daily_max",
            output_path=out_path,
        )
        ok("UTCI extraction succeeded")

        with rasterio.open(tif) as src:
            data = src.read(1)
            valid = data[data > 0]
            peak = valid.max()

        show("Peak UTCI", f"{peak:.1f} K ({peak-273.15:.1f} °C)")
        assert 290 < peak < 340, f"UTCI {peak} K outside plausible range"
        ok(f"UTCI peak {peak:.1f} K is plausible for Athens July")
    finally:
        os.unlink(out_path)


# ── 13. Zarr extraction — multiple aggregations ─────────────────────────

@test(13, "Zarr extraction — aggregation options produce correct band counts", slow=True)
def test_aggregations():
    import geobridge as gb
    import rasterio

    gb.authenticate()

    cases = [
        ("monthly_mean", 1,  "one band"),
        ("daily_mean",   31, "31 bands (July has 31 days)"),
        ("daily_max",    31, "31 bands"),
    ]

    for agg, expected_bands, desc in cases:
        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
            out_path = f.name

        try:
            tif = gb.zarr_to_geotiff(
                dataset="reanalysis_era5_single_levels",
                variable="t2m",
                bbox=(23.5, 37.8, 24.0, 38.2),
                time_range=("2023-07-01", "2023-07-31"),
                aggregation=agg,
                output_path=out_path,
            )
            with rasterio.open(tif) as src:
                actual = src.count

            if actual == expected_bands:
                ok(f"{agg:14s} → {actual} band(s) — {desc}")
            else:
                fail(f"{agg:14s} → {actual} band(s), expected {expected_bands}")
        finally:
            os.unlink(out_path)


# ── 14. Zarr extraction — long time series ───────────────────────────────

@test(14, "Zarr extraction — 10-year annual mean (geo_chunked path)", slow=True)
def test_time_series():
    import geobridge as gb
    import rasterio

    gb.authenticate()

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        out_path = f.name

    try:
        t0 = time.time()
        tif = gb.zarr_to_geotiff(
            dataset="reanalysis_era5_single_levels",
            variable="t2m",
            bbox=(23.6, 37.9, 24.0, 38.1),
            time_range=("2013-01-01", "2023-12-31"),
            aggregation="annual_mean",
            output_path=out_path,
        )
        elapsed = time.time() - t0
        ok(f"10-year extraction in {elapsed:.1f}s")

        with rasterio.open(tif) as src:
            data = src.read()
            show("Bands (years)", str(src.count))
            assert src.count >= 10, f"Expected 10+ bands, got {src.count}"
            ok(f"{src.count} annual bands extracted")
    finally:
        os.unlink(out_path)


# ── 15. Style generation ─────────────────────────────────────────────────

@test(15, "Style generation — QML and LYRx files")
def test_styles():
    import geobridge as gb
    import xml.etree.ElementTree as ET

    with tempfile.NamedTemporaryFile(suffix=".qml", delete=False) as f:
        qml_path = f.name
    with tempfile.NamedTemporaryFile(suffix=".lyrx", delete=False) as f:
        lyrx_path = f.name

    try:
        gb.to_qgis_style("t2m", style_type="raw", output_path=qml_path)
        assert Path(qml_path).stat().st_size > 100
        root = ET.parse(qml_path).getroot()
        assert root.tag == "qgis"
        ok(f"QML style for t2m: {Path(qml_path).stat().st_size} bytes, valid XML")

        gb.to_qgis_style("pm2p5", style_type="raw", output_path=qml_path)
        ok("QML style for pm2p5 generated")

        gb.to_esri_lyrx("t2m", output_path=lyrx_path)
        lyrx = json.loads(Path(lyrx_path).read_text())
        assert "layers" in lyrx
        ok(f"LYRx style for t2m: {Path(lyrx_path).stat().st_size} bytes, valid JSON")
    finally:
        os.unlink(qml_path)
        os.unlink(lyrx_path)


# ── 16. Fusion ───────────────────────────────────────────────────────────

@test(16, "Fusion — co-register ERA5 + CAMS on common grid", slow=True)
def test_fusion():
    import geobridge as gb
    import rasterio

    gb.authenticate()

    bbox = (23.0, 37.5, 24.5, 38.5)
    time_range = ("2023-07-01", "2023-07-31")

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f1, \
         tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f2, \
         tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f3:
        t_path, p_path, f_path = f1.name, f2.name, f3.name

    try:
        gb.zarr_to_geotiff(
            dataset="reanalysis_era5_single_levels", variable="t2m",
            bbox=bbox, time_range=time_range, aggregation="monthly_mean",
            output_path=t_path,
        )
        ok("ERA5 temperature extracted for fusion")

        gb.zarr_to_geotiff(
            dataset="cams_europe_air_quality_reanalyses", variable="pm2p5",
            bbox=bbox, time_range=time_range, aggregation="monthly_mean",
            output_path=p_path,
        )
        ok("CAMS PM2.5 extracted for fusion")

        fused = gb.fuse(t_path, p_path)
        ok(f"Fused layer created")

        fused_path = fused.to_geotiff(f_path)
        ok(f"Exported to GeoTIFF: {fused_path}")

        with rasterio.open(fused_path) as src:
            assert src.count == 2
            ok(f"Fused GeoTIFF has {src.count} bands (temperature + PM2.5)")
            show("Resolution", f"{src.res[0]:.4f}°")
            show("Grid", f"{src.height}×{src.width}")
    finally:
        for p in [t_path, p_path, f_path]:
            try: os.unlink(p)
            except: pass


# ── 17. Form schema fetch — live ─────────────────────────────────────────

@test(17, "Form schema — fetch ERA5 form from ECMWF object store")
def test_form():
    import geobridge as gb

    schema = gb.fetch_form("reanalysis-era5-single-levels")
    assert schema is not None, "fetch_form returned None"
    ok(f"Form fetched: {len(schema.widgets)} widgets")

    vars = schema.variables()
    assert len(vars) > 10, f"Only {len(vars)} variables"
    ok(f"Variables: {len(vars)} (first: {vars[0].get('label','')})")

    pts = schema.product_types()
    ok(f"Product types: {len(pts)}")
    for pt in pts:
        show("", pt.get("label", pt.get("value", "")))

    years = schema.years()
    assert "2023" in years
    ok(f"Years: {years[0]}–{years[-1]}")

    params = schema.parameter_names()
    ok(f"Parameters: {', '.join(params[:6])}…")


# ── 18. Constraints fetch — live ─────────────────────────────────────────

@test(18, "Constraints — fetch and filter variables by product type")
def test_constraints():
    import geobridge as gb

    combos = gb.fetch_constraints("reanalysis-era5-single-levels")
    assert len(combos) > 0, "No constraints returned"
    ok(f"Constraints fetched: {len(combos)} valid combinations")

    # Find all unique product types
    all_pts = set()
    for c in combos:
        all_pts.update(c.get("product_type", []))
    ok(f"Product types in constraints: {sorted(all_pts)}")

    # Filter variables for reanalysis product type
    if "reanalysis" in all_pts:
        vars = gb.valid_variables_for_product_type(
            "reanalysis-era5-single-levels", "reanalysis"
        )
        assert len(vars) > 0
        ok(f"Variables for 'reanalysis': {len(vars)} (includes {vars[0]})")

    # Filter variables for monthly averaged
    if "monthly_averaged_reanalysis" in all_pts:
        monthly_vars = gb.valid_variables_for_product_type(
            "reanalysis-era5-single-levels", "monthly_averaged_reanalysis"
        )
        ok(f"Variables for 'monthly_averaged_reanalysis': {len(monthly_vars)}")


# ── 19. GetCapabilities fetch — live ─────────────────────────────────────

@test(19, "WMTS GetCapabilities — fetch per-dataset capabilities XML")
def test_getcap():
    url = (
        "https://wmts.datastores.ecmwf.int/teroWmts"
        "/reanalysis_era5_single_levels/sfc"
        "?SERVICE=WMTS&REQUEST=GetCapabilities"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "geobridge-test"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        xml = resp.read().decode()

    assert "<Capabilities" in xml
    ok(f"GetCapabilities XML: {len(xml):,} chars")

    # Count layers
    import re
    layers = re.findall(r'<ows:Identifier>(reanalysis_era5_single_levels/sfc/\w+)</ows:Identifier>', xml)
    ok(f"Layers found: {len(layers)}")
    for l in layers[:5]:
        show("", l)
    if len(layers) > 5:
        show("", f"... and {len(layers)-5} more")

    # Verify t2m is present
    assert any("t2m" in l for l in layers), "t2m layer not found"
    ok("t2m layer confirmed in GetCapabilities")

    # Verify wind layer exists
    assert any("wind" in l for l in layers), "wind layer not found"
    ok("Pre-composed wind layer confirmed")


# ── 20. End-to-end: semantic search → extraction → style ─────────────────

@test(20, "End-to-end: search → extract → style → verify", slow=True)
def test_e2e():
    import geobridge as gb
    import rasterio

    gb.authenticate()

    # Step 1: semantic search
    matches = gb.semantic_search("urban heat island in Athens")
    assert len(matches) > 0
    top = matches[0]
    ok(f"Semantic: '{top.use_case_label}' → {top.dataset_id} / {top.variable}")

    # Step 2: extract
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        out_path = f.name
    with tempfile.NamedTemporaryFile(suffix=".qml", delete=False) as f:
        style_path = f.name

    try:
        tif = gb.zarr_to_geotiff(
            dataset=top.dataset_id,
            variable=top.variable,
            bbox=(23.0, 37.5, 24.5, 38.5),
            time_range=("2023-07-01", "2023-07-31"),
            aggregation=top.recommended_aggregation or "monthly_mean",
            output_path=out_path,
        )
        ok("Extraction succeeded using semantic match parameters")

        # Step 3: generate style
        gb.to_qgis_style(top.variable, output_path=style_path)
        ok(f"Style generated for {top.variable}")

        # Step 4: verify
        with rasterio.open(tif) as src:
            data = src.read(1)
            valid = data[data > 0]
            assert len(valid) > 0
            ok(f"Output: {src.height}×{src.width}, mean={valid.mean():.1f} K")

        assert Path(style_path).stat().st_size > 100
        ok("Style file is non-empty")

        ok("End-to-end pipeline: search → extract → style → verified")

    finally:
        os.unlink(out_path)
        os.unlink(style_path)


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GeoBridge live integration tests")
    parser.add_argument("--only", type=int, metavar="N", help="Run only test N")
    parser.add_argument("--list", action="store_true", help="List all tests")
    parser.add_argument("--quick", action="store_true",
                        help="Skip slow tests (extraction, fusion, time series)")
    args = parser.parse_args()

    if args.list:
        print(f"\n{'─'*65}")
        print(f"GeoBridge live tests — {len(ALL_TESTS)} tests")
        print(f"{'─'*65}\n")
        for n, info in sorted(ALL_TESTS.items()):
            slow_mark = " [SLOW]" if info["slow"] else ""
            print(f"  {n:2d}  {info['label']}{slow_mark}")
        print(f"\nSlow tests require real ARCO extraction (~1-2 min each).")
        print(f"Use --quick to skip them.\n")
        sys.exit(0)

    tests_to_run = sorted(ALL_TESTS.keys())

    if args.only is not None:
        if args.only not in ALL_TESTS:
            print(f"No test {args.only}. Use --list.")
            sys.exit(1)
        tests_to_run = [args.only]

    print(f"\n{BOLD}{B}{'═'*65}{RST}")
    print(f"{BOLD}{B}  GeoBridge live integration tests{RST}")
    print(f"{BOLD}{B}{'═'*65}{RST}")
    print(f"  Tests to run: {len(tests_to_run)}")
    if args.quick:
        print(f"  Mode: --quick (skipping slow extraction tests)")
    print()

    t0 = time.time()

    for n in tests_to_run:
        info = ALL_TESTS[n]
        if args.quick and info["slow"]:
            title(n, info["label"])
            skip("Skipped (--quick mode)")
            continue

        title(n, info["label"])
        try:
            info["fn"]()
        except Exception as exc:
            fail(info["label"], exc)
            traceback.print_exc()

    elapsed = time.time() - t0

    print(f"\n{BOLD}{'═'*65}{RST}")
    print(f"  {G}PASS: {passed}{RST}   {R}FAIL: {failed}{RST}   {Y}SKIP: {skipped}{RST}   Time: {elapsed:.1f}s")
    print(f"{'═'*65}\n")

    if failed:
        print(f"{R}Failed tests:{RST}")
        for status, msg in results:
            if status == "FAIL":
                print(f"  {R}✗{RST}  {msg}")
        print()
        sys.exit(1)
    else:
        print(f"{G}All tests passed.{RST}\n")
