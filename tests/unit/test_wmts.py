"""Tests for geobridge.modules.wmts — URL construction and exports."""

from datetime import datetime, timezone

import pytest

from geobridge.modules import discover, wmts


def make_descriptor(**overrides):
    base = dict(
        id="reanalysis-era5-single-levels/2m_temperature",
        title="ERA5 2m Temperature",
        service="C3S",
        variables=["2m_temperature"],
        time_range=(datetime(1940, 1, 1, tzinfo=timezone.utc),
                    datetime(2024, 12, 31, tzinfo=timezone.utc)),
        time_step="1h",
        crs="EPSG:4326",
        bbox=(-180.0, -90.0, 180.0, 90.0),
        wmts_url="https://wmts.climate.copernicus.eu/tileserver/wmts",
        wmts_layer_name="reanalysis-era5-single-levels/2m_temperature",
        tile_matrix_sets=[
            discover.TileMatrixSet(identifier="EPSG:4326", crs="urn:ogc:def:crs:EPSG::4326"),
            discover.TileMatrixSet(identifier="EPSG:3857", crs="urn:ogc:def:crs:EPSG::3857"),
        ],
    )
    base.update(overrides)
    return discover.LayerDescriptor(**base)


# ---------------------------------------------------------------------------
# Datetime formatting
# ---------------------------------------------------------------------------

def test_format_datetime_from_object():
    dt = datetime(2023, 7, 15, 12, 0, 0)
    assert wmts._format_datetime(dt) == "2023-07-15T12:00:00Z"


def test_format_datetime_from_iso_string_with_z():
    assert wmts._format_datetime("2023-07-15T12:00:00Z") == "2023-07-15T12:00:00Z"


def test_format_datetime_from_iso_string_without_z():
    assert wmts._format_datetime("2023-07-15T12:00:00") == "2023-07-15T12:00:00Z"


def test_format_datetime_from_date_string():
    assert wmts._format_datetime("2023-07-15") == "2023-07-15T00:00:00Z"


# ---------------------------------------------------------------------------
# Tile matrix set resolution
# ---------------------------------------------------------------------------

def test_resolve_tms_with_target_crs_match():
    desc = make_descriptor()
    tms_id, crs = wmts._resolve_tile_matrix_set(desc, "EPSG:3857")
    assert tms_id == "EPSG:3857"
    assert crs == "EPSG:3857"


def test_resolve_tms_with_target_crs_no_match_falls_back():
    desc = make_descriptor()
    tms_id, crs = wmts._resolve_tile_matrix_set(desc, "EPSG:9999")
    # Falls back to first TMS
    assert tms_id == "EPSG:4326"


def test_resolve_tms_no_tms_returns_default():
    desc = make_descriptor(tile_matrix_sets=[])
    tms_id, crs = wmts._resolve_tile_matrix_set(desc, None)
    assert tms_id is not None


# ---------------------------------------------------------------------------
# wmts_layer() with explicit descriptor
# ---------------------------------------------------------------------------

def test_wmts_layer_returns_resolved_layer():
    desc = make_descriptor()
    layer = wmts.wmts_layer(
        dataset="reanalysis-era5-single-levels",
        variable="2m_temperature",
        datetime="2023-07-15T12:00:00Z",
        descriptor=desc,
    )
    assert isinstance(layer, wmts.WmtsLayer)
    assert layer.datetime_str == "2023-07-15T12:00:00Z"


def test_wmts_layer_url_contains_required_params():
    desc = make_descriptor()
    layer = wmts.wmts_layer(
        dataset="reanalysis-era5-single-levels",
        variable="2m_temperature",
        datetime="2023-07-15",
        descriptor=desc,
    )
    url = layer.url
    assert "SERVICE=WMTS" in url
    assert "REQUEST=GetTile" in url
    assert "TIME=2023-07-15T00%3A00%3A00Z" in url
    assert "{x}" in url and "{y}" in url and "{z}" in url


def test_wmts_layer_legend_url_present():
    desc = make_descriptor()
    layer = wmts.wmts_layer("ds", "var", "2023-01-01", descriptor=desc)
    assert "GetLegendGraphic" in layer.legend_url


def test_wmts_layer_to_leaflet_dict_shape():
    desc = make_descriptor()
    layer = wmts.wmts_layer("ds", "2m_temperature", "2023-01-01", descriptor=desc)
    leaflet = layer.to_leaflet()
    assert "url" in leaflet
    assert leaflet["options"]["time"] == "2023-01-01T00:00:00Z"
    assert leaflet["options"]["transparent"] is True


def test_wmts_layer_to_qgis_includes_time():
    desc = make_descriptor()
    layer = wmts.wmts_layer("ds", "2m_temperature", "2023-07-15", descriptor=desc)
    qgis = layer.to_qgis()
    assert "time=2023-07-15T00:00:00Z" in qgis["uri"]
    assert qgis["provider"] == "wms"


def test_wmts_layer_target_crs_changes_tms():
    desc = make_descriptor()
    layer = wmts.wmts_layer(
        "ds", "2m_temperature", "2023-01-01",
        descriptor=desc, target_crs="EPSG:3857",
    )
    assert layer.tile_matrix_set == "EPSG:3857"
    assert layer.crs == "EPSG:3857"


def test_wmts_layer_unknown_dataset_raises():
    """When no descriptor is given and discover_one returns None."""
    import unittest.mock
    with unittest.mock.patch("geobridge.modules.wmts.discover_one", return_value=None):
        with pytest.raises(ValueError) as exc_info:
            wmts.wmts_layer("nonexistent", "var", "2023-01-01")
        assert "Could not discover" in str(exc_info.value)
