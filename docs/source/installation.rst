Installation
============

Prerequisites
-------------

- **Python 3.10 or newer.** GeoBridge does not support older Python.
- **A free Copernicus account.** Register at
  `cds.climate.copernicus.eu <https://cds.climate.copernicus.eu>`_ and copy your personal
  access token from your profile page.
- **macOS, Linux, or Windows with WSL2.** Native Windows may work but is not tested.

Step 1 — Create a clean environment
------------------------------------

A separate environment avoids dependency conflicts with anything else you have installed.
With Conda (recommended because some geospatial libraries need compiled C bindings that
pip alone struggles with):

.. code-block:: bash

   conda create -n geobridge python=3.11 -y
   conda activate geobridge

Or with ``venv``:

.. code-block:: bash

   python3.11 -m venv ~/.venvs/geobridge
   source ~/.venvs/geobridge/bin/activate

Step 2 — Install the geospatial stack
---------------------------------------

If you used Conda, install the heavy native dependencies through conda-forge first:

.. code-block:: bash

   conda install -c conda-forge rasterio rioxarray zarr fsspec httpio dask -y

This avoids the most common build failures (GDAL, PROJ, libtiff).

Step 3 — Install GeoBridge
----------------------------

GeoBridge is published on PyPI: https://pypi.org/project/geobridge/

.. code-block:: bash

   pip install "geobridge[full]"

The ``[full]`` extra adds everything: xarray/rasterio/zarr for
:func:`~geobridge.zarr_to_geotiff`, plus OWSLib (WMTS) and cfgrib (GRIB support). Use
``geobridge[zarr]`` instead if you only need the ARCO extraction path, or plain
``pip install geobridge`` for the lightweight core (``discover()``, ``wmts_layer()``,
``to_qgis_style()``, and the semantic search functions all work with only ``pyyaml``,
the sole hard runtime dependency — no extra needed).

If you're contributing to GeoBridge itself, install from a clone in editable mode instead
so your local edits take effect immediately:

.. code-block:: bash

   git clone https://github.com/ECMWFCode4Earth/GeoBridge
   cd geobridge
   pip install -e ".[dev]"

Step 4 — Configure your credentials
--------------------------------------

Create ``~/.cdsapirc`` with your personal access token:

.. code-block:: text

   key: YOUR-CDS-API-KEY-HERE

Then protect the file so other users on the machine cannot read it:

.. code-block:: bash

   chmod 600 ~/.cdsapirc

Alternatively, export your key as an environment variable instead of writing it to a file:

.. code-block:: bash

   export CDS_API_KEY=YOUR-CDS-API-KEY-HERE
