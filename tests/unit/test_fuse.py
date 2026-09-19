"""Tests for geobridge.modules.fuse — kernel wiring, north-up grid, GeoTIFF output."""

import pytest

np = pytest.importorskip("numpy")
xr = pytest.importorskip("xarray")
pytest.importorskip("rioxarray")
rasterio = pytest.importorskip("rasterio")

from geobridge.modules import fuse as F  # noqa: E402
from geobridge.modules.resampling import check_resampling  # noqa: E402


def _layer(values, res, west, north, name):
    ny, nx = values.shape
    da = xr.DataArray(
        values,
        coords={"y": north - res * (np.arange(ny) + 0.5),
                "x": west + res * (np.arange(nx) + 0.5)},
        dims=("y", "x"), name=name,
    )
    return da.rio.write_crs("EPSG:4326")


@pytest.fixture
def class_layer():
    """Blocky 4-class raster at 0.01° over 6°x6° (a land-sea-mask stand-in)."""
    rng = np.random.default_rng(0)
    blocks = rng.integers(0, 4, size=(50, 50)).astype(float)
    return _layer(np.kron(blocks, np.ones((12, 12))), 0.01, 20.0, 40.0, "lsm")


@pytest.fixture
def coarse_layer():
    return _layer(np.zeros((60, 60)), 0.1, 20.0, 40.0, "pm2p5")


def test_categorical_layer_gets_no_invented_classes(class_layer, coarse_layer):
    fused = F.fuse(class_layer, coarse_layer, resolution=0.1)
    choice = fused.metadata["resampling"]["lsm"]
    assert (choice["method"], choice["semantics"], choice["direction"]) == (
        "mode", "categorical", "down")
    qa = check_resampling(class_layer, fused.data["lsm"], "categorical")
    assert qa["n_invented_classes"] == 0


def test_old_bilinear_behaviour_did_invent_classes(class_layer, coarse_layer):
    """Guards the premise: the previous hardcoded resampling=1 was the bug."""
    target = F._common_target_grid(class_layer, coarse_layer, 0.1)
    old = class_layer.rio.reproject_match(target, resampling=1)
    assert check_resampling(class_layer, old, "categorical")["n_invented_classes"] > 0


def test_continuous_layer_uses_average_and_keeps_mean(coarse_layer):
    rng = np.random.default_rng(1)
    fine = _layer(rng.normal(280.0, 3.0, (600, 600)), 0.01, 20.0, 40.0, "2m_temperature")
    fused = F.fuse(fine, coarse_layer, resolution=0.1)
    assert fused.metadata["resampling"]["2m_temperature"]["method"] == "average"
    qa = check_resampling(fine, fused.data["2m_temperature"])
    assert qa["flags"] == []
    assert abs(qa["mean_shift_pct"]) < 0.1


def test_per_layer_override_and_default_method(class_layer, coarse_layer):
    fused = F.fuse(class_layer, coarse_layer, resolution=0.1,
                   method="max", method_b="min")
    meta = fused.metadata["resampling"]
    assert (meta["lsm"]["method"], meta["lsm"]["source"]) == ("max", "user")
    assert (meta["pm2p5"]["method"], meta["pm2p5"]["source"]) == ("min", "user")

    mixed = F.fuse(class_layer, coarse_layer, resolution=0.1, method_a="nearest")
    assert mixed.metadata["resampling"]["lsm"]["method"] == "nearest"
    assert mixed.metadata["resampling"]["pm2p5"]["source"] == "auto"


def test_invalid_method_raises(class_layer, coarse_layer):
    with pytest.raises(ValueError, match="method must be"):
        F.fuse(class_layer, coarse_layer, resolution=0.1, method="lanczos3")


def test_semantics_inferred_from_geotiff_stem_and_band_description(tmp_path, class_layer, coarse_layer):
    """A path input carries the variable only in its name / band description."""
    p = tmp_path / "reanalysis-era5-single-levels_lsm_daily_mean.tif"
    class_layer.rio.to_raster(p)
    fused = F.fuse(str(p), coarse_layer, resolution=0.1)
    (choice,) = [v for k, v in fused.metadata["resampling"].items() if "lsm" in k]
    assert choice["semantics"] == "categorical"


def test_upsampling_is_flagged(class_layer, coarse_layer, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="geobridge.modules.resampling"):
        F.fuse(class_layer, coarse_layer, resolution=0.01)   # coarse layer upsampled
    assert any("artefact" in r.getMessage() for r in caplog.records)


def test_target_grid_is_north_up(class_layer, coarse_layer):
    grid = F._common_target_grid(class_layer, coarse_layer, 0.1)
    assert grid.y.values[0] > grid.y.values[-1]
    assert np.all(np.diff(grid.y.values) < 0)


def test_geotiff_output_has_standard_north_up_transform(tmp_path, class_layer, coarse_layer):
    out = F.fuse(class_layer, coarse_layer, resolution=0.1).to_geotiff(
        tmp_path / "fused.tif", cog=False)
    with rasterio.open(out) as src:
        assert src.transform.e < 0            # negative pixel height = north-up
        assert src.count == 2
        assert src.descriptions == ("lsm", "pm2p5")
