GeoBridge
=========

**Python library that bridges Copernicus Data Stores into mainstream GIS workflows.**

GeoBridge removes the technical friction GIS users face when working with Copernicus
climate, atmosphere, and emergency data. It resolves dataset discovery, authentication,
and access-method differences into a single ergonomic API that produces analysis-ready
Cloud Optimized GeoTIFFs.

The library is the foundation for the project's QGIS plugin.

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

.. toctree::
   :maxdepth: 2
   :caption: Contents

   installation
   quickstart
   maintainer
   api/index
