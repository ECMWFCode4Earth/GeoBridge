"""
geobridge.modules.style
~~~~~~~~~~~~~~~~~~~~~~~

Style file generation for QGIS (.qml)

Produces calibrated colormap and legend files that match the physical
range and units of common Copernicus variables, so users do not need to
manually configure styling for each layer they load.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional, Union
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

PathLike = Union[str, os.PathLike]

# ---------------------------------------------------------------------------
# Built-in colour ramps as (position, R, G, B) tuples
# ---------------------------------------------------------------------------

_RAMPS: dict[str, list[tuple[float, int, int, int]]] = {
    "RdBu_r": [
        (0.0,   5,  48,  97),
        (0.25, 67, 147, 195),
        (0.5, 247, 247, 247),
        (0.75, 214,  96,  77),
        (1.0,  103,   0,  31),
    ],
    "YlGnBu": [
        (0.0, 255, 255, 217),
        (0.25, 199, 233, 180),
        (0.5,   65, 182, 196),
        (0.75,  29, 145, 192),
        (1.0,    8,  29,  88),
    ],
    "YlOrRd": [
        (0.0, 255, 255, 178),
        (0.25, 254, 217, 118),
        (0.5,  254, 178,  76),
        (0.75, 240,  59,  32),
        (1.0,  189,   0,  38),
    ],
    "RdYlBu_r": [
        (0.0,   49,  54, 149),
        (0.25, 116, 173, 209),
        (0.5,  255, 255, 191),
        (0.75, 244, 109,  67),
        (1.0,  165,   0,  38),
    ],
    "Purples": [
        (0.0,  252, 251, 253),
        (0.5,  158, 154, 200),
        (1.0,   63,   0, 125),
    ],
    "BuPu": [
        (0.0,  247, 252, 253),
        (0.5,  140, 150, 198),
        (1.0,   77,   0,  75),
    ],
    "PuOr": [
        (0.0,  127,  59,   8),
        (0.5,  247, 247, 247),
        (1.0,   45,   0,  75),
    ],
    "hot_r": [
        (0.0,  255, 255, 255),
        (0.25, 255, 255,   0),
        (0.5,  255, 102,   0),
        (0.75, 178,   0,   0),
        (1.0,    0,   0,   0),
    ],
    "viridis": [
        (0.0,   68,   1,  84),
        (0.25,  59,  82, 139),
        (0.5,   33, 145, 140),
        (0.75,  94, 201,  98),
        (1.0,  253, 231,  37),
    ],
}


# ---------------------------------------------------------------------------
# Built-in variable presets
# ---------------------------------------------------------------------------

_VARIABLE_PRESETS: dict[str, dict] = {
    "2m_temperature": {
    "palette": "RdBu_r", "unit": "K", "min": 240, "max": 320,
    "label": "Temperature (Kelvin)", "offset": 0,
    },
    "total_precipitation": {
        "palette": "YlGnBu", "unit": "mm", "min": 0, "max": 50,
        "label": "Precipitation", "offset": 0, "scale": 1000,
    },
    "pm2p5": {
        "palette": "YlOrRd", "unit": "µg/m³", "min": 0, "max": 80,
        "label": "PM2.5 concentration", "offset": 0,
    },
    "no2": {
        "palette": "Purples", "unit": "µg/m³", "min": 0, "max": 100,
        "label": "NO2 concentration", "offset": 0,
    },
    "ozone": {
        "palette": "BuPu", "unit": "DU", "min": 200, "max": 500,
        "label": "Ozone column", "offset": 0,
    },
    "utci": {
    "palette": "RdYlBu_r", "unit": "K", "min": 233, "max": 320,
    "label": "UTCI heat stress (Kelvin)", "offset": 0,
    },
    "fire_radiative_power": {
        "palette": "hot_r", "unit": "MW", "min": 0, "max": 1000,
        "label": "Fire radiative power", "offset": 0,
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_preset(variable: str) -> dict:
    """Return the preset for a variable, or a neutral default."""
    key = variable.lower().replace("-", "_").replace(" ", "_")
    return _VARIABLE_PRESETS.get(
        key,
        {"palette": "viridis", "unit": "", "min": 0, "max": 1,
         "label": variable, "offset": 0},
    )


def _adjust_for_style_type(preset: dict, style_type: str) -> dict:
    """Adjust min/max/palette for anomaly or percentile styles."""
    p = dict(preset)
    if style_type == "anomaly":
        # Symmetric range around zero
        magnitude = max(abs(p["min"]), abs(p["max"])) / 4
        p["min"] = -magnitude
        p["max"] = magnitude
        p["label"] = f"{p['label']} anomaly"
        # Force a diverging palette
        if p["palette"] not in ("RdBu_r", "PuOr", "RdYlBu_r"):
            p["palette"] = "RdBu_r"
    elif style_type == "percentile":
        p["min"] = 0
        p["max"] = 100
        p["unit"] = "percentile"
        p["label"] = f"{p['label']} percentile"
    return p


def _interpolate_colour_stops(
    palette: str, vmin: float, vmax: float, n: int = 11
) -> list[tuple[float, int, int, int]]:
    """Return *n* (value, r, g, b) tuples spanning [vmin, vmax]."""
    if palette not in _RAMPS:
        logger.warning("Unknown palette %r; falling back to viridis", palette)
        palette = "viridis"
    ramp = _RAMPS[palette]

    stops: list[tuple[float, int, int, int]] = []
    for i in range(n):
        t = i / (n - 1)
        # Find ramp segment containing t
        for j in range(len(ramp) - 1):
            t0, r0, g0, b0 = ramp[j]
            t1, r1, g1, b1 = ramp[j + 1]
            if t0 <= t <= t1:
                f = (t - t0) / (t1 - t0) if t1 > t0 else 0
                r = round(r0 + f * (r1 - r0))
                g = round(g0 + f * (g1 - g0))
                b = round(b0 + f * (b1 - b0))
                value = vmin + t * (vmax - vmin)
                stops.append((value, r, g, b))
                break
    return stops


# ---------------------------------------------------------------------------
# QGIS QML generator
# ---------------------------------------------------------------------------

def _build_qml(preset: dict, n_stops: int = 11) -> str:
    """Construct a complete QGIS .qml document for a singleband raster."""
    vmin = float(preset["min"])
    vmax = float(preset["max"])
    label = preset["label"]
    unit = preset["unit"]
    palette = preset["palette"]

    stops = _interpolate_colour_stops(palette, vmin, vmax, n_stops)

    qgis = ET.Element("qgis", attrib={
        "version": "3.34.0",
        "styleCategories": "Symbology|Labeling|Legend",
    })

    pipe = ET.SubElement(qgis, "pipe")
    rasterrenderer = ET.SubElement(
        pipe, "rasterrenderer",
        attrib={
            "type": "singlebandpseudocolor",
            "band": "1",
            "classificationMin": str(vmin),
            "classificationMax": str(vmax),
            "opacity": "1",
        },
    )
    rastershader = ET.SubElement(rasterrenderer, "rastershader")
    colorrampshader = ET.SubElement(
        rastershader, "colorrampshader",
        attrib={"colorRampType": "INTERPOLATED", "clip": "0"},
    )

    for value, r, g, b in stops:
        colour_hex = f"#{r:02x}{g:02x}{b:02x}"
        ET.SubElement(
            colorrampshader, "item",
            attrib={
                "value": f"{value:g}",
                "color": colour_hex,
                "alpha": "255",
                "label": f"{value:g} {unit}".strip(),
            },
        )

    # Embed metadata for traceability
    meta = ET.SubElement(qgis, "customproperties")
    ET.SubElement(meta, "Option", attrib={
        "name": "geobridge_label", "type": "QString", "value": label,
    })
    ET.SubElement(meta, "Option", attrib={
        "name": "geobridge_unit", "type": "QString", "value": unit,
    })
    ET.SubElement(meta, "Option", attrib={
        "name": "geobridge_palette", "type": "QString", "value": palette,
    })

    # Pretty-print XML
    rough = ET.tostring(qgis, encoding="unicode")
    try:
        from xml.dom import minidom
        return minidom.parseString(rough).toprettyxml(indent="  ")
    except Exception:
        return rough


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def to_qgis_style(
    variable: str,
    style_type: str = "raw",
    output_path: Optional[PathLike] = None,
    overrides: Optional[dict] = None,
) -> Path:
    """
    Generate a QGIS .qml style file calibrated to a Copernicus variable.

    Parameters
    ----------
    variable : str
        Variable name, e.g. '2m_temperature', 'pm2p5'.
    style_type : str
        'raw'        — physical values with conventional palette
        'anomaly'    — symmetric diverging palette around zero
        'percentile' — percentile range 0-100
    output_path : path-like, optional
        Where to save the .qml file. Defaults to ``./{variable}.qml``.
    overrides : dict, optional
        Override preset values, e.g. {"min": -10, "max": 40}.

    Returns
    -------
    Path
        Path to the written .qml file.

    Examples
    --------
    >>> import geobridge as gb
    >>> qml = gb.to_qgis_style("2m_temperature", style_type="anomaly")
    >>> qml = gb.to_qgis_style(
    ...     "pm2p5",
    ...     output_path="./pm25.qml",
    ...     overrides={"min": 0, "max": 150},
    ... )
    """
    if style_type not in {"raw", "anomaly", "percentile"}:
        raise ValueError(
            f"style_type must be 'raw', 'anomaly', or 'percentile'; "
            f"got {style_type!r}"
        )

    preset = _adjust_for_style_type(_get_preset(variable), style_type)
    if overrides:
        preset.update(overrides)

    qml_xml = _build_qml(preset)

    if output_path is None:
        suffix = "" if style_type == "raw" else f"_{style_type}"
        output_path = Path.cwd() / f"{variable}{suffix}.qml"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(qml_xml, encoding="utf-8")

    logger.info("Wrote QGIS style to %s", output_path)
    return output_path

