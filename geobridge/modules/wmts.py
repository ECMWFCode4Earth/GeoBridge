"""
geobridge.modules.wmts
~~~~~~~~~~~~~~~~~~~~~~

WMTS tile layer construction for ECMWF Copernicus ARCO datasets.

Verified facts about the ECMWF WMTS (May 2026)
-----------------------------------------------
- Service URL:    https://wmts.datastores.ecmwf.int/teroWmts
- GetCapabilities per dataset/subset:
      {base}/{dataset}/{subset}?SERVICE=WMTS&REQUEST=GetCapabilities
- Layer identifier format:
      {dataset}/{subset}/{variable}
      e.g. reanalysis_era5_single_levels/sfc/t2m
- Variable is embedded in the layer name, NOT a DIM_variable parameter
- Only runtime dimension: TIME (ISO-8601, any hourly step 1940–present)
- Style format: cmap:{colormap}  e.g. cmap:viridis, cmap:balance
  Variants: cmap:viridis,logScale   cmap:speed,vectorStyle:solidAndVector
- Tile matrix sets: EPSG:3857, EPSG:4326, plus @2x and @3x HiDPI
- Zoom levels: 0–10
- Legend endpoint: GetLegend (not GetLegendGraphic), returns SVG or JSON
- No authentication required
- Wind: pre-composed vector layer at .../sfc/wind with directional arrows

QGIS integration (critical findings)
-------------------------------------
The QGIS WMS provider cannot reliably parse this server's GetCapabilities
XML — it fails with "Cannot calculate extent" regardless of URI format.

The working approach is to use XYZ tile URLs instead of the WMTS provider.
QGIS treats these as raster tile layers and fetches tiles directly without
needing GetCapabilities. The encoding is subtle:

  - The tile URL query string sits INSIDE the url= value of the QGIS
    data source string, so & must become %26 and = must become %3D
  - But {x}, {y}, {z} placeholders must remain as literal text so
    QGIS can substitute them at render time
  - Colons in LAYER, STYLE, and TILEMATRIXSET stay literal (not %3A)

This module provides three output formats:
  - .to_qgis()    → XYZ tile URI for QgsRasterLayer (verified working)
  - .url           → generic WMTS GetTile URL template with {x},{y},{z}
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Union

from geobridge.modules.discover import (
    LayerDescriptor,
    discover_one,
    _SERVICE_LABELS,
    _colormap_for_variable,
)

logger = logging.getLogger(__name__)

DateLike = Union[str, datetime]

# ECMWF WMTS base URL (verified May 2026)
WMTS_BASE = "https://wmts.datastores.ecmwf.int/teroWmts"

# Supported tile matrix sets (from GetCapabilities)
TILE_MATRIX_SETS = [
    "EPSG:3857",       # Web Mercator, 256px tiles
    "EPSG:3857@2x",    # Web Mercator, 512px HiDPI
    "EPSG:3857@3x",    # Web Mercator, 768px HiDPI
    "EPSG:4326",       # WGS-84 geographic, 256px
    "EPSG:4326@2x",    # WGS-84 geographic, 512px
    "EPSG:4326@3x",    # WGS-84 geographic, 768px
]

ZOOM_MIN = 0
ZOOM_MAX = 10

# Default tile matrix set per CRS
_TMS_BY_CRS: dict[str, str] = {
    "EPSG:4326": "EPSG:4326",
    "EPSG:3857": "EPSG:3857",
}


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class WmtsLayer:
    """A resolved WMTS layer ready for use in QGIS, Leaflet, or any viewer.

    The ECMWF WMTS layer identifier encodes dataset, subset, and variable
    directly in the layer name path::

        reanalysis_era5_single_levels/sfc/t2m

    There is no ``DIM_variable`` parameter — the variable is part of the
    layer name itself. The only runtime dimension is ``TIME``.

    QGIS integration uses XYZ tiles (not the WMTS provider) because QGIS
    cannot reliably parse this server's GetCapabilities response.
    """

    dataset: str
    variable: str
    datetime_str: str
    layer_name: str       # e.g. "reanalysis_era5_single_levels/sfc/t2m"
    base_url: str         # e.g. "https://wmts.datastores.ecmwf.int/teroWmts"
    tile_matrix_set: str  # e.g. "EPSG:3857"
    crs: str              # e.g. "EPSG:3857"
    style: str = "cmap:viridis"
    legend_url: str = ""
    colormap: dict = field(default_factory=dict)
    service: str = ""

    # ------------------------------------------------------------------ #
    # Generic URL — standard WMTS GetTile with {x},{y},{z} placeholders  #
    # ------------------------------------------------------------------ #

    @property
    def url(self) -> str:
        """Return the WMTS GetTile URL template with {x},{y},{z} tokens.

        Suitable for Leaflet, OpenLayers, or any viewer that supports
        WMTS-style tile URL templates. For QGIS, use ``to_qgis()`` instead.
        """
        params = {
            "SERVICE": "WMTS",
            "REQUEST": "GetTile",
            "VERSION": "1.0.0",
            "LAYER": self.layer_name,
            "STYLE": self.style,
            "FORMAT": "image/png",
            "TILEMATRIXSET": self.tile_matrix_set,
            "TILEMATRIX": "{z}",
            "TILEROW": "{y}",
            "TILECOL": "{x}",
            "TIME": self.datetime_str,
        }
        return self.base_url + "?" + urllib.parse.urlencode(params, safe="{}")

    @property
    def get_capabilities_url(self) -> str:
        """Return the per-dataset GetCapabilities URL.

        Note: the ECMWF WMTS requires the dataset/subset path in the URL::

            .../teroWmts/reanalysis_era5_single_levels/sfc?SERVICE=WMTS&...

        The generic .../teroWmts?SERVICE=WMTS&REQUEST=GetCapabilities
        returns HTTP 400.
        """
        # Extract dataset/subset from layer_name (strip the variable part)
        parts = self.layer_name.rsplit("/", 1)
        ds_subset = parts[0] if len(parts) == 2 else self.layer_name
        return (
            f"{self.base_url}/{ds_subset}"
            f"?SERVICE=WMTS&REQUEST=GetCapabilities&VERSION=1.0.0"
        )

    # ------------------------------------------------------------------ #
    # Export helpers                                                      #
    # ------------------------------------------------------------------ #

    def to_qgis(self) -> dict:
        """Return a dict ready for constructing a QgsRasterLayer.

        Uses the XYZ tile approach instead of the QGIS WMTS provider,
        because the QGIS WMS provider cannot reliably parse the ECMWF
        WMTS GetCapabilities XML (fails with "Cannot calculate extent").

        The XYZ approach bypasses GetCapabilities entirely and fetches
        tiles directly — simpler and confirmed working in QGIS 3.28+.

        Encoding rules for the QGIS data source URI
        ---------------------------------------------
        The tile URL sits inside the ``url=`` value of the URI string.
        Within the tile URL:
        - ``&`` between query params must become ``%26``
        - ``=`` between key and value must become ``%3D``
        - ``{x}``, ``{y}``, ``{z}`` must remain literal (NOT percent-encoded)
        - Colons in values (EPSG:3857, cmap:viridis) stay literal

        Examples
        --------
        >>> conf = layer.to_qgis()
        >>> rl = QgsRasterLayer(conf["uri"], conf["name"], conf["provider"])
        >>> QgsProject.instance().addMapLayer(rl)
        """
        # Build the inner tile URL with %26 for & and %3D for =
        # {x}, {y}, {z} remain as literal placeholders
        tile_url = (
            f"{self.base_url}"
            f"?SERVICE%3DWMTS"
            f"%26REQUEST%3DGetTile"
            f"%26VERSION%3D1.0.0"
            f"%26LAYER%3D{self.layer_name}"
            f"%26STYLE%3D{self.style}"
            f"%26FORMAT%3Dimage/png"
            f"%26TILEMATRIXSET%3D{self.tile_matrix_set}"
            f"%26TILEMATRIX%3D{{z}}"
            f"%26TILEROW%3D{{y}}"
            f"%26TILECOL%3D{{x}}"
            f"%26TIME%3D{self.datetime_str}"
        )

        uri = f"type=xyz&url={tile_url}&zmin={ZOOM_MIN}&zmax={ZOOM_MAX}"

        return {
            "uri": uri,
            "name": f"{self.dataset} — {self.variable} ({self.datetime_str})",
            "provider": "wms",
        }


    def to_dict(self) -> dict:
        """Return a plain dict for JSON serialisation."""
        return {
            "dataset": self.dataset,
            "variable": self.variable,
            "datetime": self.datetime_str,
            "layer_name": self.layer_name,
            "base_url": self.base_url,
            "style": self.style,
            "tile_matrix_set": self.tile_matrix_set,
            "crs": self.crs,
            "legend_url": self.legend_url,
            "tile_url_template": self.url,
            "qgis_uri": self.to_qgis()["uri"],
        }

    def tile_url(self, zoom: int, col: int, row: int) -> str:
        """Return a concrete tile URL with zoom, col, row filled in.

        Useful for downloading a single tile as a PNG file::

            url = layer.tile_url(zoom=5, col=18, row=12)
            # HTTP GET → 256×256 PNG of ERA5 temperature at that tile
        """
        return self.url.replace("{z}", str(zoom)).replace(
            "{y}", str(row)
        ).replace("{x}", str(col))

    def feature_info_url(
        self,
        zoom: int,
        col: int,
        row: int,
        pixel_i: int = 128,
        pixel_j: int = 128,
    ) -> str:
        """Return a GetFeatureInfo URL to query the data value at a pixel.

        The response is JSON containing the actual numeric value (e.g.
        temperature in K) at the clicked pixel location.

        Parameters
        ----------
        zoom, col, row : int
            Tile address (same as in tile_url).
        pixel_i, pixel_j : int
            Pixel position within the 256×256 tile (default: centre).
        """
        params = {
            "SERVICE": "WMTS",
            "REQUEST": "GetFeatureInfo",
            "VERSION": "1.0.0",
            "LAYER": self.layer_name,
            "STYLE": self.style,
            "FORMAT": "image/png",
            "TILEMATRIXSET": self.tile_matrix_set,
            "TILEMATRIX": str(zoom),
            "TILEROW": str(row),
            "TILECOL": str(col),
            "TIME": self.datetime_str,
            "INFOFORMAT": "application/json",
            "I": str(pixel_i),
            "J": str(pixel_j),
        }
        return self.base_url + "?" + urllib.parse.urlencode(params)

    def __repr__(self) -> str:
        return (
            f"WmtsLayer(dataset={self.dataset!r}, "
            f"variable={self.variable!r}, time={self.datetime_str!r})"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_datetime(dt: DateLike) -> str:
    """Convert any reasonable datetime input to ISO-8601 with Z suffix.

    Accepts:
      - datetime objects
      - 'YYYY-MM-DD'
      - 'YYYY-MM-DDTHH:MM:SS'
      - 'YYYY-MM-DDTHH:MM:SSZ'
    """
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    s = str(dt).strip()
    if "T" not in s and len(s) == 10:
        s = s + "T00:00:00"
    if not s.endswith("Z"):
        s = s.rstrip("+00:00") + "Z" if s.endswith("+00:00") else s + "Z"
    return s


def _resolve_tile_matrix_set(
    descriptor: LayerDescriptor,
    target_crs: Optional[str],
) -> tuple[str, str]:
    """Pick the best TileMatrixSet for the requested target CRS.

    For QGIS use, EPSG:3857 (Web Mercator) is preferred because it
    matches the default QGIS canvas CRS and avoids reprojection issues.
    """
    # If user explicitly requested a CRS, honour it
    if target_crs:
        for tms in descriptor.tile_matrix_sets:
            if tms.epsg_code == target_crs:
                return tms.identifier, target_crs
        # Check if it is a known ECMWF TMS
        if target_crs in _TMS_BY_CRS:
            return _TMS_BY_CRS[target_crs], target_crs

    # Default to EPSG:3857 — safest for QGIS and web viewers
    return "EPSG:3857", "EPSG:3857"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def wmts_layer(
    dataset: str,
    variable: str,
    datetime: DateLike,
    style: str = "default",
    target_crs: Optional[str] = None,
    descriptor: Optional[LayerDescriptor] = None,
) -> WmtsLayer:
    """Resolve a time-specific WMTS layer with all parameters filled in.

    Parameters
    ----------
    dataset : str
        Dataset identifier, e.g. ``'reanalysis_era5_single_levels'``.
    variable : str
        Variable name — accepts both CDS long names (``'2m_temperature'``)
        and ARCO short names (``'t2m'``). Translated silently.
    datetime : str or datetime
        Time slice. ISO strings (``'2023-07-15'``,
        ``'2023-07-15T12:00:00Z'``) or datetime objects.
    style : str
        WMTS style. Use ``'default'`` to auto-select from the ARCO
        snapshot colormap (usually ``cmap:viridis``). Can also pass
        ``'cmap:balance'``, ``'cmap:viridis,logScale'``, or
        ``'cmap:speed,vectorStyle:solidAndVector'`` for the wind layer.
    target_crs : str, optional
        CRS for tiles. Default ``EPSG:3857`` (Web Mercator).
        Also available: ``EPSG:4326``.
    descriptor : LayerDescriptor, optional
        Pre-fetched descriptor (from ``discover_one()``). If None,
        looked up automatically.

    Returns
    -------
    WmtsLayer
        Resolved layer with ``.to_qgis()``, ``.to_leaflet()``, ``.url``,
        ``.tile_url(z, x, y)``, ``.feature_info_url(...)``.

    Raises
    ------
    ValueError
        If the dataset cannot be found or has no WMTS endpoint.

    Examples
    --------
    Load in QGIS via Python console::

        >>> layer = gb.wmts_layer('reanalysis_era5_single_levels',
        ...     't2m', '2023-07-15T12:00:00Z')
        >>> conf = layer.to_qgis()
        >>> rl = QgsRasterLayer(conf["uri"], conf["name"], conf["provider"])
        >>> QgsProject.instance().addMapLayer(rl)

    Download a single tile as PNG::

        >>> url = layer.tile_url(zoom=5, col=18, row=12)
        >>> import urllib.request
        >>> data = urllib.request.urlopen(url).read()  # 256×256 PNG

    Query a point value::

        >>> info_url = layer.feature_info_url(zoom=5, col=18, row=12)
        >>> # Returns JSON with {"features":[{"properties":{"value":302.5}}]}
    """
    if descriptor is None:
        descriptor = discover_one(dataset)
        if descriptor is None:
            raise ValueError(
                f"Could not discover dataset {dataset!r}. "
                "Check the identifier or call gb.discover() to list options."
            )

    if not descriptor.has_wmts:
        raise ValueError(
            f"Dataset {dataset!r} has no WMTS preview endpoint.\n"
            "WMTS is available for ARCO datasets only (those with has_wmts=True).\n"
            "For spatial extraction use:  gb.zarr_to_geotiff(dataset, variable, ...)\n"
            "For CDS API download use:    gb.cds_to_geotiff(dataset, request, ...)"
        )

    # Resolve ARCO short variable name from alias map
    from geobridge.modules.extract import _load_overrides
    _overrides = _load_overrides()
    aliases = (_overrides or {}).get("variable_aliases", {})
    arco_variable = aliases.get(variable, variable)

    # ECMWF WMTS layer id = "{dataset}/{subset}/{variable}"
    layer_name = f"{descriptor.wmts_layer_name}/{arco_variable}"

    # Style: auto-select from ARCO snapshot colormap, or use user-provided
    if style and style != "default":
        wmts_style = style
    else:
        palette = (descriptor.colormap or {}).get("palette") or "viridis"
        wmts_style = f"cmap:{palette}"

    tms_id, resolved_crs = _resolve_tile_matrix_set(descriptor, target_crs)
    iso_time = _format_datetime(datetime)

    # Legend URL — GetLegend endpoint (NOT GetLegendGraphic)
    legend_url = (
        f"{descriptor.wmts_url}?SERVICE=WMTS&REQUEST=GetLegend"
        f"&LAYER={urllib.parse.quote(layer_name, safe='/')}"
        f"&STYLE={urllib.parse.quote(wmts_style)}"
        f"&FORMAT=image%2Fsvg%2Bxml"
    )

    colormap = descriptor.colormap or _colormap_for_variable(variable)

    return WmtsLayer(
        dataset=dataset,
        variable=arco_variable,
        datetime_str=iso_time,
        layer_name=layer_name,
        base_url=descriptor.wmts_url,
        tile_matrix_set=tms_id,
        crs=resolved_crs,
        style=wmts_style,
        legend_url=legend_url,
        colormap=colormap,
        service=descriptor.service,
    )
