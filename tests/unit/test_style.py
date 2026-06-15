"""Tests for geobridge.modules.style — QGIS QML generation."""

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


