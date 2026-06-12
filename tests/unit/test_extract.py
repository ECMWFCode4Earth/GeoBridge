"""Tests for geobridge.modules.extract — overrides, snapshot, variable mapping, chunking."""

import pytest

from geobridge.modules import extract


# ---------------------------------------------------------------------------
# Overrides + snapshot loading
# ---------------------------------------------------------------------------

def test_load_overrides_returns_known_dataset():
    overrides = extract._load_overrides()
    assert "reanalysis-era5-single-levels" in overrides


def test_load_overrides_entry_has_zarr_urls():
    overrides = extract._load_overrides()
    era5 = overrides["reanalysis-era5-single-levels"]
    assert "zarr" in era5
    assert "time_chunked" in era5["zarr"]
    assert "geo_chunked" in era5["zarr"]
    # Verified URLs point at the live ARCO host
    assert era5["zarr"]["time_chunked"].startswith("https://arco.datastores.ecmwf.int/")


def test_load_snapshot_returns_dict():
    """Snapshot loads cleanly even when the file is absent or empty."""
    snapshot = extract._load_snapshot()
    assert isinstance(snapshot, dict)


# ---------------------------------------------------------------------------
# Dataset entry resolution
# ---------------------------------------------------------------------------

def test_get_dataset_entry_known():
    entry = extract._get_dataset_entry("reanalysis-era5-single-levels")
    assert "zarr" in entry
    assert "variable_aliases" in entry
    assert entry["coordinate_order"] == "lat_ascending"


def test_get_dataset_entry_unknown_raises_with_helpful_message():
    with pytest.raises(extract.ExtractionError) as exc_info:
        extract._get_dataset_entry("nonexistent-dataset")
    msg = str(exc_info.value)
    assert "no access details" in msg
    assert "arco_overrides.yaml" in msg


def test_get_dataset_entry_merges_stac_metadata_when_available():
    """If the snapshot has a matching entry, it is exposed under '_stac'."""
    entry = extract._get_dataset_entry("reanalysis-era5-single-levels")
    # _stac may or may not be present depending on whether the snapshot
    # has been generated yet.  Either way the access details (zarr) are.
    assert "zarr" in entry
    if "_stac" in entry:
        # When present, the STAC block should expose the dataset id and title.
        assert entry["_stac"].get("id") == "reanalysis-era5-single-levels"
        assert "title" in entry["_stac"]


# ---------------------------------------------------------------------------
# Public listing helpers
# ---------------------------------------------------------------------------

def test_list_datasets_returns_known_id():
    datasets = extract.list_datasets()
    assert "reanalysis-era5-single-levels" in datasets


def test_list_variables_returns_long_names():
    variables = extract.list_variables("reanalysis-era5-single-levels")
    assert "2m_temperature" in variables
    assert "10m_u_component_of_wind" in variables


def test_list_variables_unknown_dataset_raises():
    with pytest.raises(extract.ExtractionError):
        extract.list_variables("nonexistent")


# ---------------------------------------------------------------------------
# Variable name aliasing
# ---------------------------------------------------------------------------

def test_resolve_variable_translates_long_to_short():
    entry = extract._get_dataset_entry("reanalysis-era5-single-levels")
    assert extract._resolve_variable_name(entry, "2m_temperature") == "t2m"
    assert extract._resolve_variable_name(entry, "total_precipitation") == "tp"


def test_resolve_variable_passes_through_short_name():
    """Already-short names should be returned unchanged."""
    entry = extract._get_dataset_entry("reanalysis-era5-single-levels")
    assert extract._resolve_variable_name(entry, "t2m") == "t2m"


def test_resolve_variable_passes_through_unknown():
    """Unknown names get passed through; xarray raises later."""
    entry = extract._get_dataset_entry("reanalysis-era5-single-levels")
    assert extract._resolve_variable_name(entry, "totally_made_up") == "totally_made_up"


# ---------------------------------------------------------------------------
# Chunking heuristic
# ---------------------------------------------------------------------------

def _era5_entry():
    return extract._get_dataset_entry("reanalysis-era5-single-levels")


def test_chunking_picks_time_chunked_for_spatial_map():
    """Wide bbox + short time → time_chunked."""
    flavour = extract._pick_chunking(
        _era5_entry(),
        time_range=("2023-07-01", "2023-07-31"),
        bbox=(20.0, 35.0, 30.0, 45.0),  # 10x10 deg, ~1 month
    )
    assert flavour == "time_chunked"


def test_chunking_picks_geo_chunked_for_long_timeseries_at_point():
    """Tiny bbox + long time → geo_chunked."""
    flavour = extract._pick_chunking(
        _era5_entry(),
        time_range=("2010-01-01", "2023-12-31"),
        bbox=(23.7, 37.95, 23.8, 38.05),  # tiny bbox
    )
    assert flavour == "geo_chunked"


def test_chunking_picks_time_chunked_for_short_timeseries_at_point():
    """Tiny bbox + short time → time_chunked is fine."""
    flavour = extract._pick_chunking(
        _era5_entry(),
        time_range=("2023-07-01", "2023-07-31"),
        bbox=(23.7, 37.95, 23.8, 38.05),
    )
    assert flavour == "time_chunked"


# ---------------------------------------------------------------------------
# Public API guards
# ---------------------------------------------------------------------------

def test_zarr_to_geotiff_unknown_dataset_raises():
    """Without authentication or even network access, unknown dataset
    should fail with a clear error.  Two possible paths:
    - If [zarr] extras are installed, fails at overrides lookup with
      "no access details" message.
    - If [zarr] extras are not installed, fails earlier with a clear
      install hint.
    """
    with pytest.raises(extract.ExtractionError) as exc_info:
        extract.zarr_to_geotiff(
            dataset="nonexistent",
            variable="t2m",
            bbox=(0, 0, 1, 1),
            time_range=("2023-01-01", "2023-01-02"),
        )
    msg = str(exc_info.value)
    # Either failure mode is acceptable — both produce actionable errors.
    assert ("no access details" in msg) or ("[zarr] extras" in msg)


def test_zarr_to_geotiff_aggregations_constant_well_formed():
    """The _AGGREGATIONS constant should be present and contain the
    canonical entries.  This is here to catch typos at import time."""
    assert "monthly_mean" in extract._AGGREGATIONS
    assert "raw" in extract._AGGREGATIONS
    assert extract._AGGREGATIONS["raw"] is None
    assert extract._AGGREGATIONS["monthly_mean"] == ("MS", "mean")
