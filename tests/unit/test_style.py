"""Tests for geobridge.modules.style — QGIS QML generation."""

import logging
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from geobridge.modules import style


def test_get_preset_known_variable():
    p = style._get_preset("2m_temperature")
    assert p["palette"] == "RdBu_r"
    assert p["unit"] == "°C"


def test_get_preset_unknown_variable_returns_default():
    p = style._get_preset("totally_made_up_var")
    assert p["palette"] == "viridis"
    assert p["min"] == 0
    assert p["max"] == 1


def test_adjust_for_anomaly_makes_symmetric_range():
    base = style._get_preset("2m_temperature")
    p = style._adjust_for_style_type(base, "anomaly")
    assert p["min"] == -p["max"]
    assert "anomaly" in p["label"]


def test_adjust_for_percentile_sets_0_100_range():
    base = style._get_preset("2m_temperature")
    p = style._adjust_for_style_type(base, "percentile")
    assert p["min"] == 0
    assert p["max"] == 100
    assert p["unit"] == "percentile"


def test_interpolate_colour_stops_returns_n_stops():
    stops = style._interpolate_colour_stops("RdBu_r", 0, 100, n=5)
    assert len(stops) == 5
    # First stop at vmin, last at vmax
    assert stops[0][0] == 0
    assert stops[-1][0] == 100


def test_interpolate_colour_stops_unknown_palette_falls_back():
    stops = style._interpolate_colour_stops("does_not_exist", 0, 1, n=3)
    assert len(stops) == 3


def test_to_qgis_style_writes_valid_xml(tmp_path):
    out = tmp_path / "temp.qml"
    result = style.to_qgis_style("2m_temperature", output_path=out)
    assert result.exists()

    # Should parse as XML
    content = out.read_text(encoding="utf-8")
    root = ET.fromstring(content)
    assert root.tag == "qgis"


def test_to_qgis_style_anomaly_has_diverging_palette(tmp_path):
    out = tmp_path / "temp_anomaly.qml"
    style.to_qgis_style("pm2p5", style_type="anomaly", output_path=out)
    content = out.read_text(encoding="utf-8")
    # Anomaly forces RdBu_r (or similar diverging) when current palette isn't diverging
    assert "RdBu_r" in content or "PuOr" in content or "RdYlBu_r" in content


def test_to_qgis_style_invalid_style_type_raises():
    with pytest.raises(ValueError):
        style.to_qgis_style("2m_temperature", style_type="bogus")


def test_to_qgis_style_overrides_apply(tmp_path):
    out = tmp_path / "custom.qml"
    style.to_qgis_style(
        "2m_temperature",
        output_path=out,
        overrides={"min": 10, "max": 40},
    )
    content = out.read_text(encoding="utf-8")
    root = ET.fromstring(content)
    rr = root.find(".//rasterrenderer")
    assert rr.get("classificationMin") == "10.0"
    assert rr.get("classificationMax") == "40.0"


def test_to_qgis_style_default_filename(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = style.to_qgis_style("pm2p5")
    assert result.name == "pm2p5.qml"
    assert result.exists()


# ---------------------------------------------------------------------------
# scale / offset: stops sit at raw raster values, labels stay in display units
# ---------------------------------------------------------------------------

def _parse(path):
    return ET.fromstring(path.read_text(encoding="utf-8"))


def test_scale_places_stops_at_raw_raster_values(tmp_path):
    # ERA5 total precipitation is stored in metres; the preset is in mm.
    root = _parse(style.to_qgis_style("total_precipitation", output_path=tmp_path / "tp.qml"))
    rr = root.find(".//rasterrenderer")
    assert float(rr.get("classificationMin")) == 0
    assert float(rr.get("classificationMax")) == pytest.approx(0.05)

    items = root.findall(".//item")
    assert float(items[-1].get("value")) == pytest.approx(0.05)
    assert items[-1].get("label") == "50 mm"


def test_offset_shifts_stops_onto_raw_values(tmp_path):
    out = style.to_qgis_style(
        "2m_temperature",
        output_path=tmp_path / "t.qml",
        overrides={"unit": "°C", "min": -30, "max": 50, "offset": -273.15},
    )
    root = _parse(out)
    rr = root.find(".//rasterrenderer")
    assert float(rr.get("classificationMin")) == pytest.approx(243.15)
    assert float(rr.get("classificationMax")) == pytest.approx(323.15)
    assert root.findall(".//item")[0].get("label") == "-30 °C"


def test_identity_scale_leaves_values_unchanged(tmp_path):
    root = _parse(style.to_qgis_style("pm2p5", output_path=tmp_path / "pm.qml"))
    rr = root.find(".//rasterrenderer")
    assert rr.get("classificationMin") == "0.0"
    assert rr.get("classificationMax") == "80.0"


@pytest.mark.parametrize("scale", [0, -1])
def test_non_positive_scale_raises(tmp_path, scale):
    with pytest.raises(ValueError, match="scale"):
        style.to_qgis_style(
            "pm2p5", output_path=tmp_path / "x.qml", overrides={"scale": scale},
        )


def test_percentile_ramp_ignores_variable_scale(tmp_path):
    # A percentile raster stores 0-100 whatever the variable's units are.
    out = style.to_qgis_style(
        "total_precipitation", style_type="percentile", output_path=tmp_path / "p.qml",
    )
    rr = _parse(out).find(".//rasterrenderer")
    assert float(rr.get("classificationMin")) == 0
    assert float(rr.get("classificationMax")) == 100


def test_percentile_resets_scale_and_offset():
    p = style._adjust_for_style_type(style._get_preset("total_precipitation"), "percentile")
    assert p["scale"] == 1
    assert p["offset"] == 0


# ---------------------------------------------------------------------------
# anomaly range
# ---------------------------------------------------------------------------

def test_anomaly_uses_curated_range_for_kelvin_variables():
    for variable in ("2m_temperature", "utci"):
        p = style._adjust_for_style_type(style._get_preset(variable), "anomaly")
        assert (p["min"], p["max"]) == (-10, 10)


def test_anomaly_default_uses_range_width_not_max_value():
    # 240-320 K must give +/-20 K (a quarter of the width), not +/-80 K.
    base = {"palette": "RdBu_r", "unit": "K", "min": 240, "max": 320,
            "label": "x", "offset": 0}
    p = style._adjust_for_style_type(base, "anomaly")
    assert (p["min"], p["max"]) == (-20, 20)


def test_anomaly_drops_additive_offset():
    base = {**style._get_preset("2m_temperature"), "offset": -273.15}
    assert style._adjust_for_style_type(base, "anomaly")["offset"] == 0


def test_anomaly_qml_is_centred_on_zero(tmp_path, monkeypatch):
    # A Celsius-display preset must not shift a difference by 273.15.
    monkeypatch.setitem(style._VARIABLE_PRESETS, "temp_c", {
        "palette": "RdBu_r", "unit": "°C", "min": -30, "max": 50,
        "label": "Temperature", "offset": -273.15, "anomaly_range": 10,
    })
    out = style.to_qgis_style(
        "temp_c", style_type="anomaly", output_path=tmp_path / "a.qml",
    )
    rr = _parse(out).find(".//rasterrenderer")
    assert float(rr.get("classificationMin")) == -10
    assert float(rr.get("classificationMax")) == 10


# ---------------------------------------------------------------------------
# fallback warning
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("overrides", [None, {"min": 230}])
def test_unknown_variable_warns(tmp_path, caplog, overrides):
    with caplog.at_level(logging.WARNING, logger="geobridge.modules.style"):
        style.to_qgis_style("t2m", output_path=tmp_path / "t.qml", overrides=overrides)
    assert "No built-in style preset" in caplog.text
    assert "'t2m'" in caplog.text


@pytest.mark.parametrize("variable, style_type, overrides", [
    ("2m_temperature", "raw", None),           # has a preset
    ("t2m", "percentile", None),               # percentile is always 0-100
    ("t2m", "raw", {"min": 230, "max": 310}),  # caller calibrated the range
])
def test_no_warning_when_range_is_not_a_guess(
    tmp_path, caplog, variable, style_type, overrides,
):
    with caplog.at_level(logging.WARNING, logger="geobridge.modules.style"):
        style.to_qgis_style(
            variable, style_type=style_type,
            output_path=tmp_path / "t.qml", overrides=overrides,
        )
    assert "No built-in style preset" not in caplog.text


