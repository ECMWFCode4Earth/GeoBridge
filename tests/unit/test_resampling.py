"""Tests for geobridge.modules.resampling — semantics inference, kernel choice, QA."""

import logging

import pytest

from geobridge.modules import resampling as rs


# ---------------------------------------------------------------------------
# Semantics inference
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    # CDS long names
    ("2m_temperature", "continuous"),
    ("total_precipitation", "continuous"),
    ("high_vegetation_cover", "continuous"),      # a fraction, not a class
    ("land_sea_mask", "categorical"),
    ("Land-sea mask", "categorical"),
    ("type_of_high_vegetation", "categorical"),
    ("soil_type", "categorical"),
    ("lccs_class", "categorical"),
    ("maximum_2m_temperature_since_previous_post_processing", "extrema"),
    ("10m_wind_gust_since_previous_post_processing", "extrema"),
    ("minimum_2m_temperature_since_previous_post_processing", "minima"),
    # GRIB short codes
    ("t2m", "continuous"),
    ("pm2p5", "continuous"),
    ("lsm", "categorical"),
    ("tvh", "categorical"),
    ("slt", "categorical"),
    ("mx2t", "extrema"),
    ("mx2t24", "extrema"),
    ("i10fg", "extrema"),
    ("mn2t", "minima"),
    # Names as geobridge writes them: dataset prefix, aggregation / time suffix
    ("reanalysis-era5-single-levels_lsm_daily_mean", "categorical"),
    ("reanalysis-era5-single-levels_mx2t_2023-07-01T00:00:00", "extrema"),
    ("cams-europe-air-quality-reanalyses_pm2p5_monthly_mean", "continuous"),
    # A temporal max/min of a continuous field is still continuous in space
    ("reanalysis-era5-single-levels_2m_temperature_daily_max", "continuous"),
    ("reanalysis-era5-single-levels_2m_temperature_monthly_min", "continuous"),
    # Nothing recognisable defaults to continuous
    ("layer", "continuous"),
    ("", "continuous"),
])
def test_infer_semantics(name, expected):
    assert rs.infer_semantics(name) == expected


def test_infer_semantics_first_specific_hint_wins():
    # A generic label must not shadow an informative later hint.
    assert rs.infer_semantics("temperature", None, "era5_lsm_x") == "categorical"


def test_infer_semantics_no_hints_is_continuous():
    assert rs.infer_semantics() == "continuous"
    assert rs.infer_semantics(None, "") == "continuous"


# ---------------------------------------------------------------------------
# Kernel selection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("semantics, direction, expected", [
    ("continuous", "down", "average"),
    ("categorical", "down", "mode"),
    ("extrema", "down", "max"),
    ("minima", "down", "min"),
    ("continuous", "up", "bilinear"),
    ("categorical", "up", "nearest"),
    ("extrema", "up", "nearest"),
    ("continuous", "same", "bilinear"),
])
def test_select_method(semantics, direction, expected):
    assert rs.select_method(semantics, direction) == expected


def test_select_method_unknown_semantics_raises():
    with pytest.raises(ValueError, match="Unknown semantics"):
        rs.select_method("banana", "down")


def test_resolve_method_direction_from_resolution_ratio():
    down = rs.resolve_method(["t2m"], 0.01, 0.4)
    up = rs.resolve_method(["t2m"], 0.4, 0.01)
    same = rs.resolve_method(["t2m"], 0.25, 0.25)
    assert (down.direction, down.method) == ("down", "average")
    assert (up.direction, up.method) == ("up", "bilinear")
    assert (same.direction, same.method) == ("same", "bilinear")
    assert down.ratio == pytest.approx(40.0)


def test_resolve_method_user_override_wins_and_is_recorded():
    choice = rs.resolve_method(["lsm"], 0.01, 0.4, method="nearest")
    assert (choice.method, choice.source) == ("nearest", "user")
    assert choice.semantics == "categorical"      # inference still reported


def test_resolve_method_rejects_unknown_kernel():
    with pytest.raises(ValueError, match="method must be"):
        rs.resolve_method(["t2m"], 0.01, 0.4, method="lanczos3")


def test_resolve_method_warns_on_upsampling_only(caplog):
    with caplog.at_level(logging.WARNING, logger=rs.logger.name):
        rs.resolve_method(["t2m"], 0.01, 0.4, label="down")
        assert not caplog.records
        rs.resolve_method(["t2m"], 0.4, 0.01, label="up")
    assert len(caplog.records) == 1
    assert "artefact" in caplog.records[0].getMessage()


def test_resolve_method_warns_even_with_explicit_kernel(caplog):
    # The warning is about the grid change, not about which kernel was picked.
    with caplog.at_level(logging.WARNING, logger=rs.logger.name):
        rs.resolve_method(["t2m"], 0.4, 0.01, method="nearest")
    assert len(caplog.records) == 1


# ---------------------------------------------------------------------------
# QA
# ---------------------------------------------------------------------------

def test_check_resampling_flags_invented_classes():
    np = pytest.importorskip("numpy")
    source = np.array([0.0, 1.0, 2.0, 1.0])
    result = np.array([0.0, 0.5, 1.0, 1.7])
    qa = rs.check_resampling(source, result, "categorical")
    assert qa["n_invented_classes"] == 2
    assert "invented_classes" in qa["flags"]


def test_check_resampling_clean_categorical():
    np = pytest.importorskip("numpy")
    source = np.array([0, 1, 2, 1])
    qa = rs.check_resampling(source, np.array([2, 1]), "categorical")
    assert qa["n_invented_classes"] == 0
    assert qa["flags"] == []


def test_check_resampling_flags_continuous_mean_shift():
    np = pytest.importorskip("numpy")
    source = np.full(100, 300.0)
    assert rs.check_resampling(source, np.full(10, 300.5))["flags"] == []
    qa = rs.check_resampling(source, np.full(10, 306.0))
    assert qa["flags"] == ["mean_shift"]
    assert qa["mean_shift_pct"] == pytest.approx(2.0)


def test_check_resampling_mean_shift_not_flagged_for_extrema():
    np = pytest.importorskip("numpy")
    source = np.arange(100.0)
    qa = rs.check_resampling(source, np.full(10, 99.0), "extrema")
    assert qa["flags"] == []          # max-aggregation raises the mean by design


def test_check_resampling_ignores_nonfinite_and_handles_empty():
    np = pytest.importorskip("numpy")
    qa = rs.check_resampling(np.array([1.0, np.nan, 3.0]), np.array([2.0, np.nan]))
    assert (qa["n_source"], qa["n_result"]) == (2, 1)
    empty = rs.check_resampling(np.array([np.nan]), np.array([1.0]))
    assert empty["flags"] == ["no_valid_data"]
