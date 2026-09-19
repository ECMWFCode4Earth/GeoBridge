# GeoBridge WMTS Module — Reference Notes

Source: `geobridge/modules/wmts.py`. Last reviewed against commit `c0640a6`.
See also [DISCOVERY.md](DISCOVERY.md) (the catalogue this module resolves
against) and [EXTRACT.md](EXTRACT.md) (the sibling data-download path).

## 1. What it does

`wmts_layer()` builds a ready-to-use **map preview tile layer** against
ECMWF's WMTS (Web Map Tile Service) for a given dataset/variable/time —
for visualization in QGIS, OpenLayers, or any XYZ/WMTS-capable viewer.

This is the **preview path**, not a data-extraction path: it returns tile
URL templates, not pixel values or files. For actual data extraction use
`gb.zarr_to_geotiff()` ([EXTRACT.md](EXTRACT.md)) or `gb.cds_to_geotiff()`.
For a single numeric value at a point, see `feature_info_url()` below or
`modules/timeseries.py`.

**No authentication required** — this is the one access path in GeoBridge
that's fully public (contrast with ARCO Zarr and the CDS retrieve API,
both of which need `gb.authenticate()` — see [AUTHENTICATION.md](AUTHENTICATION.md)).

## 2. Verified facts about the ECMWF WMTS (as documented in-module, May 2026)

- Service base URL: `https://wmts.datastores.ecmwf.int/teroWmts`
- GetCapabilities is **per dataset/subset**, not global:
  `{base}/{dataset}/{subset}?SERVICE=WMTS&REQUEST=GetCapabilities` — the
  bare `.../teroWmts?SERVICE=WMTS&REQUEST=GetCapabilities` returns HTTP 400.
- Layer identifier format: `{dataset}/{subset}/{variable}`, e.g.
  `reanalysis_era5_single_levels/sfc/t2m`. The variable is baked into the
  layer name path — there is **no `DIM_variable` parameter**.
- Only runtime dimension is `TIME` (ISO-8601, any hourly step from 1940 to
  present).
- Style format: `cmap:{colormap}`, e.g. `cmap:viridis`, `cmap:balance`.
  Variants exist: `cmap:viridis,logScale`,
  `cmap:speed,vectorStyle:solidAndVector` (used for the wind layer).
- Tile matrix sets: `EPSG:3857` and `EPSG:4326`, each with `@2x`/`@3x`
  HiDPI variants (`TILE_MATRIX_SETS` in-module).
- Zoom levels: 0–10 (`ZOOM_MIN`/`ZOOM_MAX`).
- Legend endpoint is `GetLegend` — **not** the more common
  `GetLegendGraphic` — and returns SVG or JSON.
- Wind is a pre-composed vector layer at `.../sfc/wind` with directional
  arrows, resolved from underlying u/v components (see §4).

## 3. QGIS integration quirk — why XYZ instead of the WMTS provider

QGIS's native WMS/WMTS provider cannot reliably parse this server's
GetCapabilities XML — it fails with "Cannot calculate extent" regardless of
URI formatting. The documented working approach, and what `.to_qgis()`
implements, is to treat the layer as a generic **XYZ tile source** instead:
QGIS fetches tiles directly without ever calling GetCapabilities.

The URI encoding is subtle and QGIS-specific (`WmtsLayer.to_qgis()`):

- The tile query string sits *inside* the `url=` value of the QGIS data
  source string, so `&` between params must become `%26` and `=` must
  become `%3D`.
- `{x}`, `{y}`, `{z}` placeholders must stay **literal** (not
  percent-encoded) so QGIS can substitute them at render time.
- Colons in `LAYER`, `STYLE`, `TILEMATRIXSET` values (e.g. `EPSG:3857`,
  `cmap:viridis`) stay literal, not `%3A`.

Verified working in QGIS 3.28+.

## 4. `wmts_layer(dataset, variable, datetime, style="default", target_crs=None, descriptor=None) -> WmtsLayer`

The main entry point. Steps:

1. **Resolve the dataset** via `discover_one(dataset)` (from
   [`discover.py`](DISCOVERY.md)) unless a pre-fetched `descriptor` is
   passed in. Raises `ValueError` if the dataset can't be found.
2. **Check `has_wmts`** — raises `ValueError` with a pointer to the
   extraction functions if the dataset has no WMTS preview endpoint (WMTS
   is ARCO-only).
3. **Resolve the variable name** to ARCO short form via the same
   `arco_overrides.yaml` alias map `extract.py` uses (imported directly
   from `geobridge.modules.extract._load_overrides` — a light coupling
   between the two modules so variable-name handling stays consistent).
4. **Resolve the correct subset for the variable** using
   `discover._arco_subset_for_variable(dataset, arco_variable)` — ARCO
   datasets split variables across multiple subsets, and the subset is
   part of the WMTS layer identifier, so the dataset's "primary" subset
   (`descriptor.wmts_layer_name`) can't just be reused blindly. Falls back
   to the primary subset (with a debug log) only if no subset lists the
   variable at all.
5. **Pick a style**: `style="default"` (the default) auto-selects
   `cmap:{palette}` from, in priority order, the resolved subset's
   per-variable colormap metadata → the dataset-level colormap → hardcoded
   `viridis`. Any other `style` value is passed through verbatim, letting
   callers use `cmap:balance`, `cmap:viridis,logScale`, or the wind vector
   style directly.
6. **Resolve tile matrix set / CRS** via `_resolve_tile_matrix_set` —
   honours an explicit `target_crs` if the descriptor lists a matching
   `TileMatrixSet`, or if it's a known ECMWF TMS (`EPSG:4326`/`EPSG:3857`);
   otherwise defaults to `EPSG:3857` (Web Mercator — matches the default
   QGIS canvas CRS and avoids reprojection issues).
7. **Format the time** via `_format_datetime` — accepts `datetime` objects
   or ISO-ish strings (`'2023-07-15'`, `'2023-07-15T12:00:00Z'`) and
   normalises to `YYYY-MM-DDTHH:MM:SSZ`.
8. Builds the `GetLegend` URL and returns a fully populated `WmtsLayer`.

```python
import geobridge as gb

layer = gb.wmts_layer(
    "reanalysis_era5_single_levels", "t2m", "2023-07-15T12:00:00Z",
)
```

## 5. `WmtsLayer` — the returned object

A dataclass carrying `dataset`, `variable` (resolved short form),
`datetime_str`, `layer_name`, `base_url`, `tile_matrix_set`, `crs`,
`style`, `legend_url`, `colormap`, `service`. Methods/properties:

| Member | Returns |
|---|---|
| `.url` | Generic WMTS `GetTile` URL template with literal `{x}`,`{y}`,`{z}` — for OpenLayers or any viewer supporting WMTS tile templates. |
| `.get_capabilities_url` | Per-dataset/subset `GetCapabilities` URL (strips the variable off `layer_name`). |
| `.to_qgis()` | `{"uri", "name", "provider": "wms"}` dict for `QgsRasterLayer(uri, name, provider)`, using the XYZ-encoded URI described in §3. |
| `.to_dict()` | Plain JSON-serialisable dict, including all URL variants. |
| `.tile_url(zoom, col, row)` | One concrete tile URL with `{z}/{y}/{x}` filled in — e.g. to fetch a single 256×256 PNG directly. |
| `.feature_info_url(zoom, col, row, pixel_i=128, pixel_j=128)` | `GetFeatureInfo` URL — returns JSON with the actual numeric data value at a specific pixel within a tile (default: tile centre). This is how you get a real value without downloading/opening the full dataset. |

```python
# Load in QGIS
conf = layer.to_qgis()
rl = QgsRasterLayer(conf["uri"], conf["name"], conf["provider"])
QgsProject.instance().addMapLayer(rl)

# Fetch one tile as PNG
import urllib.request
data = urllib.request.urlopen(layer.tile_url(zoom=5, col=18, row=12)).read()

# Query a point value without downloading data
info_url = layer.feature_info_url(zoom=5, col=18, row=12)
# → JSON: {"features": [{"properties": {"value": 302.5}}]}
```

## 6. Relationship to `discover.py` and `extract.py`

- Depends directly on `discover.py` for dataset resolution
  (`discover_one`, `LayerDescriptor`, `_colormap_for_variable`,
  `_arco_subset_for_variable`) — see [DISCOVERY.md](DISCOVERY.md) §4 for
  how subset resolution and colormap hints work internally, since
  `wmts.py` reuses that exact logic rather than re-implementing it.
- Depends on `extract.py` only for its variable-alias overrides loader
  (`_load_overrides`), imported lazily inside `wmts_layer()` to avoid a
  module-level circular import (`extract.py` doesn't import `wmts.py`, so
  this one-directional lazy import is safe).
- Does **not** touch `geobridge.auth` at all — this is the module to reach
  for when you want to show data on a map without requiring the user to
  have a CDS API key.
