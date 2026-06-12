"""Tests for geobridge.modules.discover — snapshot+overrides, filtering, exports."""

from datetime import datetime, timezone

import pytest

from geobridge.modules import discover


# ---------------------------------------------------------------------------
# TileMatrixSet
# ---------------------------------------------------------------------------

def test_tilematrixset_extracts_epsg():
    tms = discover.TileMatrixSet(identifier="3857", crs="urn:ogc:def:crs:EPSG::3857")
    assert tms.epsg_code == "EPSG:3857"


def test_tilematrixset_no_epsg_returns_none():
    tms = discover.TileMatrixSet(identifier="custom", crs="custom-crs-string")
    assert tms.epsg_code is None


# ---------------------------------------------------------------------------
# LayerDescriptor helpers
# ---------------------------------------------------------------------------

def _make_descriptor(**overrides) -> discover.LayerDescriptor:
    base = dict(
        id="reanalysis-era5-single-levels",
        title="ERA5 hourly data on single levels",
        service="C3S",
        abstract="ERA5 reanalysis.",
        variables=["2m_temperature", "total_precipitation"],
        keywords=["Provider: Copernicus C3S"],
        license="CC-BY-4.0",
        providers=["ECMWF"],
        bbox=(0.0, -89.0, 360.0, 89.0),
    )
    base.update(overrides)
    return discover.LayerDescriptor(**base)


def test_has_zarr_false_when_no_urls():
    d = _make_descriptor()
    assert d.has_zarr is False
    assert d.extraction_supported is False


def test_has_zarr_true_when_url_present():
    d = _make_descriptor(zarr_time_chunked="https://arco.example.com/data.zarr")
    assert d.has_zarr is True
    assert d.extraction_supported is True


def test_has_wmts_requires_both_url_and_layer():
    assert not _make_descriptor(wmts_url="https://wmts.example.com").has_wmts
    assert _make_descriptor(
        wmts_url="https://wmts.example.com",
        wmts_layer_name="layer-1",
    ).has_wmts


def test_to_leaflet_none_when_no_wmts():
    assert _make_descriptor().to_leaflet() is None


def test_to_qgis_none_when_no_wmts():
    assert _make_descriptor().to_qgis() is None


def test_to_leaflet_returns_config_when_wmts_available():
    d = _make_descriptor(
        wmts_url="https://wmts.example.com",
        wmts_layer_name="layer-1",
    )
    leaflet = d.to_leaflet()
    assert leaflet is not None
    assert leaflet["url"] == "https://wmts.example.com"
    assert leaflet["options"]["layer"] == "layer-1"


def test_to_qgis_returns_wms_provider_when_wmts_available():
    d = _make_descriptor(
        wmts_url="https://wmts.example.com",
        wmts_layer_name="layer-1",
    )
    qgis = d.to_qgis()
    assert qgis is not None
    assert qgis["provider"] == "wms"
    assert "layers=" in qgis["uri"]


def test_to_dict_is_json_serialisable():
    import json
    d = _make_descriptor(zarr_time_chunked="https://example.com/data.zarr")
    payload = d.to_dict()
    json.dumps(payload)  # raises if not JSON-safe
    assert payload["id"] == "reanalysis-era5-single-levels"
    assert payload["access_methods"]["zarr_time_chunked"] == "https://example.com/data.zarr"
    assert payload["access_methods"]["wmts"] is None


def test_repr_shows_metadata_only_when_no_access():
    assert "metadata-only" in repr(_make_descriptor())


def test_repr_lists_zarr_when_present():
    d = _make_descriptor(zarr_time_chunked="https://example.com/x.zarr")
    assert "zarr" in repr(d)


# ---------------------------------------------------------------------------
# YAML loaders
# ---------------------------------------------------------------------------

def test_load_overrides_has_era5():
    overrides = discover._load_overrides()
    assert "reanalysis-era5-single-levels" in overrides


def test_load_overrides_era5_has_zarr_urls():
    era5 = discover._load_overrides()["reanalysis-era5-single-levels"]
    assert "zarr" in era5
    assert era5["zarr"]["time_chunked"].startswith("https://arco.datastores.ecmwf.int/")
    assert era5["zarr"]["geo_chunked"].startswith("https://arco.datastores.ecmwf.int/")


def test_load_snapshot_returns_dict():
    snapshot = discover._load_snapshot()
    assert isinstance(snapshot, dict)


# ---------------------------------------------------------------------------
# discover() filtering
# ---------------------------------------------------------------------------

def test_discover_returns_era5():
    results = discover.discover()
    assert any(d.id == "reanalysis-era5-single-levels" for d in results)


def test_discover_keyword_match():
    results = discover.discover(keyword="ERA5")
    assert results
    for d in results:
        text = (d.id + d.title + d.abstract + " ".join(d.keywords)).lower()
        assert "era5" in text


def test_discover_keyword_no_match():
    assert discover.discover(keyword="xyzxyzxyz_no_such_thing") == []


def test_discover_filter_by_variable():
    results = discover.discover(variable="2m_temperature")
    assert all("2m_temperature" in d.variables for d in results)


def test_discover_filter_by_service():
    results = discover.discover(services=["C3S"])
    assert all(d.service == "C3S" for d in results)


def test_discover_invalid_service_raises():
    with pytest.raises(ValueError) as exc_info:
        discover.discover(services=["UNKNOWN"])
    assert "Unknown service" in str(exc_info.value)


def test_discover_bbox_over_athens_matches_global_era5():
    results = discover.discover(bbox=(23.0, 37.5, 24.5, 38.5))
    assert any(d.id == "reanalysis-era5-single-levels" for d in results)


def test_discover_bbox_no_intersection():
    # Impossible bbox (longitude > 360) — should not match ERA5
    results = discover.discover(bbox=(400.0, -10.0, 500.0, 10.0))
    assert all(d.id != "reanalysis-era5-single-levels" for d in results)


def test_discover_time_after_in_range():
    after = datetime(2020, 1, 1, tzinfo=timezone.utc)
    results = discover.discover(time_after=after)
    assert any(d.id == "reanalysis-era5-single-levels" for d in results)


def test_discover_extraction_only_only_returns_zarr_capable():
    results = discover.discover(extraction_only=True)
    assert results  # at least ERA5 should be here
    for d in results:
        assert d.has_zarr is True


def test_discover_results_sorted_alphabetically():
    results = discover.discover()
    ids = [d.id for d in results]
    assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# discover_one()
# ---------------------------------------------------------------------------

def test_discover_one_known_id():
    desc = discover.discover_one("reanalysis-era5-single-levels")
    assert desc is not None
    assert desc.id == "reanalysis-era5-single-levels"
    assert desc.has_zarr is True
    assert desc.has_wmts is False


def test_discover_one_unknown_returns_none():
    assert discover.discover_one("not-a-real-dataset") is None


def test_discover_one_with_service_filter():
    desc = discover.discover_one("era5", services=["C3S"])
    assert desc is not None
    assert "era5" in desc.id.lower()


# ---------------------------------------------------------------------------
# _descriptor_from_entries merging
# ---------------------------------------------------------------------------

def test_descriptor_uses_stac_title_when_available():
    stac = {"title": "STAC Title", "service": "C3S", "license": "CC-BY-4.0"}
    desc = discover._descriptor_from_entries("some-dataset", stac, None)
    assert desc.title == "STAC Title"


def test_descriptor_falls_back_to_id_when_no_title():
    desc = discover._descriptor_from_entries("my-dataset", None, None)
    assert desc.title == "my-dataset"


def test_descriptor_variables_from_overrides():
    over = {"variable_aliases": {"2m_temperature": "t2m", "total_precipitation": "tp"}}
    desc = discover._descriptor_from_entries("ds", None, over)
    assert "2m_temperature" in desc.variables
    assert "total_precipitation" in desc.variables


def test_descriptor_zarr_urls_from_overrides():
    over = {"zarr": {
        "time_chunked": "https://example.com/time.zarr",
        "geo_chunked":  "https://example.com/geo.zarr",
    }}
    desc = discover._descriptor_from_entries("ds", None, over)
    assert desc.zarr_time_chunked == "https://example.com/time.zarr"
    assert desc.zarr_geo_chunked  == "https://example.com/geo.zarr"
    assert desc.has_zarr is True
