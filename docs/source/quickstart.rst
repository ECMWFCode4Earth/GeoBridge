Quickstart
==========

.. code-block:: python

   import geobridge as gb

   gb.authenticate()
   datasets = gb.discover(keyword="temperature")

   layer = gb.wmts_layer(
       dataset="reanalysis-era5-single-levels",
       variable="2m_temperature",
       datetime="2023-07-15T12:00:00Z",
   )

   tif = gb.zarr_to_geotiff(
       dataset="reanalysis-era5-single-levels",
       variable="2m_temperature",
       bbox=(23.5, 37.8, 24.1, 38.1),
       time_range=("2023-06-01", "2023-08-31"),
       cog=True,
   )

Semantic search
---------------

.. code-block:: python

   matches = gb.semantic_search("urban heat island")
   for m in matches:
       print(m.dataset_id, m.recommended_access, m.guidance)

What it does
------------

- **Discovery.** :func:`geobridge.discover` lists every dataset in the Copernicus
  catalogue with filters for keyword, service, variable, bounding box, and time range —
  all from a locally bundled snapshot, no network needed.
- **Authentication.** A single :func:`geobridge.authenticate` call handles credential
  resolution for the ARCO bearer-token model.
- **Extraction — ARCO path.** :func:`geobridge.zarr_to_geotiff` pulls a spatial/temporal
  subset from the ARCO Zarr Data Lake and writes it as a Cloud Optimized GeoTIFF, ready
  for QGIS or ArcGIS.
- **Extraction — CDS API path.** :func:`geobridge.cds_to_geotiff` submits a download job
  through the standard CDS API for datasets not yet in the ARCO lake, converts the result
  to GeoTIFF, and streams it to disk.
- **WMTS.** :func:`geobridge.wmts_layer` returns a ready-to-use WMTS layer object for live
  tile streaming inside QGIS or Leaflet.
- **Form schema.** :func:`geobridge.fetch_form` and :func:`geobridge.fetch_constraints`
  retrieve the server-side parameter form for any dataset so your UI can build validated
  request widgets. :func:`geobridge.valid_variables_for_product_type` filters the
  variable list to what the selected product type actually supports.
- **Styling.** :func:`geobridge.to_qgis_style` generates calibrated colour ramps for
  known Copernicus variables.
- **Fusion.** :func:`geobridge.fuse` co-registers multiple layers onto a common grid for
  joint analysis (e.g. heat + air quality).
- **Semantics.** :func:`geobridge.semantic_search` resolves user themes like "urban heat
  island" or "wildfire risk" into concrete dataset and workflow recommendations.
  :func:`geobridge.list_themes` and :func:`geobridge.list_use_cases` enumerate the
  built-in vocabulary.

More end-to-end examples — UTCI thermal comfort, CAMS PM2.5 air quality, dataset
discovery, and live-network tests — live under ``examples/`` in the repository.
