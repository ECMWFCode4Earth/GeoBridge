"""
Smoke test for GeoBridge — exercises the library's offline functionality
without needing pytest or network access.
"""

import sys
from pathlib import Path

# Add the package to path
sys.path.insert(0, str(Path(__file__).parent))

print("=" * 70)
print("GeoBridge smoke test")
print("=" * 70)

# ---------- Test 1: imports ----------
print("\n[1/12] Importing geobridge...")
import geobridge as gb
print(f"      version = {gb.__version__}")
print(f"      exported names = {len(gb.__all__)}")

# ---------- Test 2: auth credential resolution ----------
print("\n[2/12] Testing auth (without network)...")
gb.authenticate(key="test-key-fake-12345678")
assert gb.is_authenticated()
assert gb.get_token() == "test-key-fake-12345678"
header = gb.auth_header()
assert header == {"Authorization": "Bearer test-key-fake-12345678"}
print("      authenticate(key=...) → ok")
print("      get_token() returns the API key directly → ok")
print("      auth_header() returns Bearer token → ok")

# ---------- Test 3: discover() — snapshot + overrides ----------
print("\n[3/12] Testing discover() — snapshot + overrides...")
import json
from geobridge.modules.discover import discover as _discover, discover_one as _discover_one, LayerDescriptor

# Without filters, ERA5 must be present (it's in overrides).
results = _discover()
assert any(d.id == "reanalysis_era5_single_levels" for d in results), \
    "ERA5 should always be discoverable via overrides"
print(f"      discover()          → {len(results)} dataset(s)")

# discover_one fast path
ds = _discover_one("reanalysis_era5_single_levels")
assert ds is not None
assert ds.has_zarr is True
# ERA5 now has a WMTS endpoint discovered from the ARCO STAC catalogue
print(f"      discover_one ERA5   → ok")
print(f"        has_zarr={ds.has_zarr}, has_wmts={ds.has_wmts}")
print(f"        zarr_url = {(ds.zarr_time_chunked or '')[:65]}...")

# Keyword filter — use "era5" which matches the dataset id even without a snapshot
temp_results = _discover(keyword="era5")
assert any(d.id == "reanalysis_era5_single_levels" for d in temp_results)
print(f"      discover(keyword='era5') → {len(temp_results)} dataset(s)")

# Variable filter — uses ARCO short names
var_results = _discover(variable="t2m")
assert any(d.id == "reanalysis_era5_single_levels" for d in var_results)
print(f"      discover(variable='t2m') → {len(var_results)} dataset(s)")

# extraction_only filter
extractable = _discover(extraction_only=True)
assert all(d.extraction_supported for d in extractable)
print(f"      discover(extraction_only=True)   → {len(extractable)} dataset(s)")

# Results are sorted alphabetically
ids = [d.id for d in results]
assert ids == sorted(ids), "discover() results must be sorted"
print(f"      alphabetical sort → ok")

# ---------- Test 4: LayerDescriptor exports ----------
print("\n[4/12] Testing LayerDescriptor exports...")

# ERA5 now has WMTS from the ARCO STAC catalogue
if ds.has_wmts:
    leaflet = ds.to_leaflet()
    assert leaflet is not None and "url" in leaflet
    qgis = ds.to_qgis()
    assert qgis is not None and qgis["provider"] == "wms"
    print(f"      to_leaflet() → ok (WMTS discovered from ARCO STAC) ✓")
    print(f"      to_qgis()    → ok ✓")
else:
    assert ds.to_leaflet() is None
    assert ds.to_qgis() is None
    print(f"      to_leaflet() / to_qgis() → None (no WMTS) ✓")

# to_dict is always JSON-safe
payload = ds.to_dict()
json.dumps(payload)
assert payload["access_methods"]["zarr_time_chunked"] is not None
# wmts may or may not be populated depending on whether the ARCO STAC
# exposed a WMTS endpoint for this dataset
print(f"      to_dict() → {len(payload)} keys, JSON-serialisable ✓")

# A WMTS-capable descriptor exercises the populated path
wmts_desc = LayerDescriptor(
    id="synthetic-marine",
    title="Synthetic Marine Layer",
    service="Marine",
    wmts_url="https://wmts.marine.copernicus.eu/teroWmts",
    wmts_layer_name="GLOBAL_ANALYSISFORECAST_PHY_001_024/sst",
)
assert wmts_desc.has_wmts is True
leaflet = wmts_desc.to_leaflet()
assert leaflet is not None and "url" in leaflet
qgis = wmts_desc.to_qgis()
assert qgis is not None and qgis["provider"] == "wms"
print(f"      to_leaflet() / to_qgis() for WMTS-enabled descriptor → ok ✓")

# ---------- Test 5: wmts_layer ----------
print("\n[5/12] Testing wmts_layer...")
from datetime import datetime as _dt
from geobridge.modules import wmts as wmts_module

# ERA5 now has WMTS — the call should succeed
if ds.has_wmts:
    layer = gb.wmts_layer(
        dataset="reanalysis_era5_single_levels",
        variable="t2m",
        datetime="2023-07-15T12:00:00Z",
        descriptor=ds,
    )
    assert layer.url
    print(f"      wmts_layer() on ERA5 → returned layer ✓")
    print(f"        url length = {len(layer.url)}")
else:
    try:
        gb.wmts_layer(
            dataset="reanalysis_era5_single_levels",
            variable="t2m",
            datetime="2023-07-15T12:00:00Z",
        )
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "no WMTS preview endpoint" in str(exc)
        print(f"      wmts_layer() → clear ValueError (no WMTS) ✓")

# Datetime formatter still works
assert wmts_module._format_datetime("2023-07-15") == "2023-07-15T00:00:00Z"
assert wmts_module._format_datetime(_dt(2023, 7, 15, 12)) == "2023-07-15T12:00:00Z"
print(f"      datetime formatting → ok ✓")

# ---------- Test 6: style generation ----------
print("\n[6/12] Testing style generation...")
import tempfile
from xml.etree import ElementTree as ET
with tempfile.TemporaryDirectory() as tmp:
    qml_path = gb.to_qgis_style(
        "2m_temperature",
        style_type="anomaly",
        output_path=Path(tmp) / "temp.qml",
    )
    assert qml_path.exists()
    qml_text = qml_path.read_text()
    qml_root = ET.fromstring(qml_text)
    assert qml_root.tag == "qgis"
    print(f"      QML file size = {len(qml_text)} bytes")
    print(f"      QML root element = {qml_root.tag}")

    lyrx_path = gb.to_esri_lyrx(
        "pm2p5",
        output_path=Path(tmp) / "pm25.lyrx",
    )
    lyrx_data = json.loads(lyrx_path.read_text())
    assert lyrx_data["type"] == "CIMLayerDocument"
    print(f"      LYRx layers = {len(lyrx_data['layers'])}")

# ---------- Test 7: semantic engine ----------
print("\n[7/12] Testing semantic search...")

themes = gb.list_themes()
print(f"      themes available = {len(themes)}")
for t in themes:
    print(f"        - {t['id']}: {t['label']}")

print()
queries = [
    "urban heat island",
    "air quality exposure",
    "wildfire risk",
    "combined heat and air pollution",
    "long term temperature trend",
]
for q in queries:
    matches = gb.semantic_search(q, max_results=2)
    if matches:
        top = matches[0]
        print(f"      {q!r}")
        print(f"        → {top.use_case_label}")
        print(f"        → dataset: {top.dataset_id}")
        print(f"        → access: {top.recommended_access}, "
              f"agg: {top.recommended_aggregation}, conf: {top.confidence:.2f}")
    else:
        print(f"      {q!r} → no match")

# Verify guidance text is built
matches = gb.semantic_search("urban heat island")
assert "Recommended dataset" in matches[0].guidance
print(f"\n      guidance text generated successfully")

# ---------- Test 8: combined pollution + heat triggers fusion advice ----------
print("\n[8/12] Testing fusion compatibility detection...")
matches = gb.semantic_search("combined heat and air pollution")
fusion_matches = [m for m in matches if m.requires_fusion]
print(f"      matches requiring fusion: {len(fusion_matches)}")
if fusion_matches:
    print(f"      compatibility note example:")
    for line in fusion_matches[0].guidance.split("\n"):
        if "INFO" in line or "WARNING" in line:
            print(f"        {line.strip()}")

# ---------- Test 9: ARCO catalogue and variable aliasing ----------
print("\n[9/12] Testing ARCO catalogue and variable aliasing...")
datasets = gb.list_datasets()
assert "reanalysis_era5_single_levels" in datasets
print(f"      catalogued datasets ({len(datasets)}): {datasets}")

# list_variables now returns short names from the ARCO snapshot
variables = gb.list_variables("reanalysis_era5_single_levels")
assert "t2m" in variables
print(f"      ERA5 variables exposed: {len(variables)} — {variables[:5]}...")

# Variable aliasing — verify CDS long form maps to ARCO short form
from geobridge.modules.extract import (
    _get_dataset_entry, _resolve_variable_name, _pick_chunking
)
era5 = _get_dataset_entry("reanalysis_era5_single_levels")
assert _resolve_variable_name(era5, "2m_temperature") == "t2m"
assert _resolve_variable_name(era5, "total_precipitation") == "tp"
assert _resolve_variable_name(era5, "t2m") == "t2m"  # passthrough
print(f"      variable aliasing: 2m_temperature → t2m ✓")

# Chunking heuristic
flav_map = _pick_chunking(era5,
                          ("2023-07-01", "2023-07-31"),
                          (20.0, 35.0, 30.0, 45.0))
flav_pt = _pick_chunking(era5,
                         ("2010-01-01", "2023-12-31"),
                         (23.7, 37.95, 23.8, 38.05))
assert flav_map == "time_chunked"
assert flav_pt == "geo_chunked"
print(f"      chunking heuristic: spatial map → time_chunked, "
      f"long timeseries → geo_chunked ✓")

# Verify Zarr URL is correct format
zarr_url = era5["zarr"]["time_chunked"]
assert zarr_url.startswith("https://arco.datastores.ecmwf.int/")
print(f"      Zarr URL hosts on arco.datastores.ecmwf.int ✓")
stage = 9

# ── Stage 10 ── CDS download module imports and error classes
stage += 1
print(f"\n[{stage}/12] Testing CDS download module...")
from geobridge.modules.cds_download import (
    cds_to_geotiff, CdsApiError, CdsJobTimeout,
    _submit_job, _poll_job, _netcdf_to_geotiff,
)
assert issubclass(CdsApiError, Exception)
assert issubclass(CdsJobTimeout, Exception)
print(f"      cds_to_geotiff importable ✓")
print(f"      CdsApiError, CdsJobTimeout exception classes ✓")

# Verify cds_to_geotiff is in the public API
assert hasattr(gb, "cds_to_geotiff")
assert hasattr(gb, "CdsApiError")
assert hasattr(gb, "CdsJobTimeout")
print(f"      Exported in gb.__all__ ✓")

# ── Stage 11 ── Form module imports and FormSchema
stage += 1
print(f"\n[{stage}/12] Testing form module...")
from geobridge.modules.form import (
    fetch_form, fetch_constraints, valid_variables_for_product_type,
    FormSchema, FormWidget, clear_form_cache,
)

# Construct a FormSchema manually to verify the class works
test_widget = FormWidget(
    name="variable", label="Variable", widget_type="StringListWidget",
    values=[{"value": "t2m", "label": "2m temperature"},
            {"value": "tp",  "label": "Total precipitation"}],
)
test_schema = FormSchema(
    dataset_id="test-dataset",
    widgets=[test_widget],
)
assert test_schema.variable_widget is not None
assert test_schema.variable_widget.value_list == ["t2m", "tp"]
assert test_schema.variable_widget.label_map == {
    "t2m": "2m temperature", "tp": "Total precipitation"
}
assert test_schema.variables() == [
    {"value": "t2m", "label": "2m temperature"},
    {"value": "tp",  "label": "Total precipitation"},
]
assert test_schema.parameter_names() == ["variable"]
assert test_schema.product_type_widget is None  # not in this test schema
assert test_schema.years() == []
assert test_schema.pressure_levels() == []
print(f"      FormSchema construction and accessors ✓")
print(f"      FormWidget.value_list and label_map ✓")

# Verify public API exports
assert hasattr(gb, "fetch_form")
assert hasattr(gb, "fetch_constraints")
assert hasattr(gb, "valid_variables_for_product_type")
assert hasattr(gb, "FormSchema")
print(f"      Exported in gb.__all__ ✓")

# Clear cache to avoid leaking test data
clear_form_cache()

# ── Stage 12 ── Updated discover flags
stage += 1
print(f"\n[{stage}/12] Testing updated discover flags...")
from geobridge.modules.discover import discover, discover_one

# arco_only should return ~26 datasets
arco_ds = discover(arco_only=True)
print(f"      discover(arco_only=True) → {len(arco_ds)} dataset(s)")
assert len(arco_ds) > 0
for d in arco_ds:
    assert d.has_zarr, f"{d.id} is in arco_only result but has_zarr=False"

# extraction_only should return >= arco count (ARCO + CDS API)
all_extractable = discover(extraction_only=True)
print(f"      discover(extraction_only=True) → {len(all_extractable)} dataset(s)")
assert len(all_extractable) >= len(arco_ds)
for d in all_extractable:
    assert d.extraction_supported, f"{d.id} not extraction_supported"

# Verify descriptor has form/constraints URLs
era5 = discover_one("reanalysis_era5_single_levels")
d = era5.to_dict()
assert "flags" in d, "to_dict() missing 'flags' key"
assert d["flags"]["has_zarr"] is True
assert d["flags"]["extraction_supported"] is True
assert "cds_form" in d["access_methods"]
assert "cds_constraints" in d["access_methods"]
print(f"      LayerDescriptor.to_dict() has flags and form/constraints URLs ✓")
print(f"      extraction_supported covers both ARCO and CDS paths ✓")

print("\n" + "=" * 70)
print("ALL SMOKE TESTS PASSED")
print("=" * 70)
