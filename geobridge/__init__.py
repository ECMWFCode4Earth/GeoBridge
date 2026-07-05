"""
GeoBridge — Copernicus to GIS interoperability toolkit.

A Python library that bridges Copernicus Data Stores (C3S, CAMS, CEMS)
and mainstream GIS platforms (QGIS, Leaflet, ArcGIS, Jupyter).

Quickstart
----------
    >>> import geobridge as gb
    >>> gb.authenticate()
    >>> datasets = gb.discover(keyword="temperature")
    >>> layer = gb.wmts_layer(
    ...     dataset="reanalysis-era5-single-levels",
    ...     variable="2m_temperature",
    ...     datetime="2023-07-15T12:00:00Z",
    ... )
    >>> tif = gb.zarr_to_geotiff(
    ...     dataset="reanalysis-era5-single-levels",
    ...     variable="2m_temperature",
    ...     bbox=(23.5, 37.8, 24.1, 38.1),
    ...     time_range=("2023-06-01", "2023-08-31"),
    ...     cog=True,
    ... )

Semantic search
---------------
    >>> matches = gb.semantic_search("urban heat island")
    >>> for m in matches:
    ...     print(m.dataset_id, m.recommended_access, m.guidance)
"""

from __future__ import annotations

__version__ = "0.1.4"
__author__ = "GeoBridge contributors"
__license__ = "MIT"

from geobridge.auth import (
    authenticate,
    auth_header,
    get_token,
    is_authenticated,
    AuthenticationError,
)
from geobridge.modules.discover import (
    discover,
    discover_one,
    LayerDescriptor,
    TileMatrixSet,
)
from geobridge.modules.wmts import (
    wmts_layer,
    WmtsLayer,
)
from geobridge.modules.extract import (
    zarr_to_geotiff,
    list_datasets,
    list_variables,
    ExtractionError,
)
from geobridge.modules.style import (
    to_qgis_style,
)
from geobridge.modules.fuse import (
    fuse,
    FusedLayer,
)
from geobridge.semantic.engine import (
    semantic_search,
    semantic_resources,
    SemanticMatch,
    ResourceMatch,
    list_themes,
    list_use_cases,
)

from geobridge.modules.cds_download import (
    cds_to_geotiff,
    CdsApiError,
    CdsJobTimeout,
)
from geobridge.modules.form import (
    fetch_form,
    fetch_constraints,
    valid_variables_for_product_type,
    validate_request,
    FormSchema,
)

__all__ = [
    # Auth
    "authenticate",
    "auth_header",
    "get_token",
    "is_authenticated",
    "AuthenticationError",
    # Discovery
    "discover",
    "discover_one",
    "LayerDescriptor",
    "TileMatrixSet",
    # WMTS
    "wmts_layer",
    "WmtsLayer",
    # Extraction — ARCO Zarr path
    "zarr_to_geotiff",
    "list_datasets",
    "list_variables",
    "ExtractionError",
    # Extraction — CDS API path (non-ARCO datasets)
    "cds_to_geotiff",
    "CdsApiError",
    "CdsJobTimeout",
    # Form schema
    "fetch_form",
    "fetch_constraints",
    "valid_variables_for_product_type",
    "validate_request",
    "FormSchema",
    # Styling
    "to_qgis_style",
    # Fusion
    "fuse",
    "FusedLayer",
    # Semantic
    "semantic_search",
    "semantic_resources",
    "SemanticMatch",
    "ResourceMatch",
    "list_themes",
    "list_use_cases",
]
