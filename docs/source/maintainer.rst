Maintainer workflow
====================

Refreshing catalogue snapshots
-------------------------------

The ``cds_snapshot.yaml`` and ``arco_snapshot.yaml`` files are regenerated periodically
from the live ECMWF catalogues. End users never run these scripts; they get the
snapshots bundled with whatever GeoBridge version they install.

To refresh the CDS STAC snapshot (maintainers only):

.. code-block:: bash

   python scripts/refresh_catalogue.py --limit 5 --output /tmp/test.yaml   # quick test
   python scripts/refresh_catalogue.py                                      # full run
   git diff geobridge/semantic/cds_snapshot.yaml
   git add geobridge/semantic/cds_snapshot.yaml
   git commit -m "Refresh CDS catalogue snapshot"

To refresh the ARCO snapshot:

.. code-block:: bash

   python scripts/refresh_arco_catalogue.py
   git add geobridge/semantic/arco_snapshot.yaml
   git commit -m "Refresh ARCO catalogue snapshot"

Adding a new ARCO dataset
--------------------------

1. Visit the dataset page on `cds.climate.copernicus.eu
   <https://cds.climate.copernicus.eu>`_ and open the "Analysis ready data" tab.
2. Copy the Zarr URLs (typically there are two: ``time_chunked`` and ``geo_chunked``).
3. Verify each URL responds with a 200 status:

   .. code-block:: bash

      curl -I -H "Authorization: Bearer $CDS_API_KEY" "https://.../.zmetadata"

4. Add an entry to ``geobridge/semantic/arco_overrides.yaml`` following the schema of
   existing entries.
5. Add the variable aliases (CDS long-form name → ARCO short-form name) by inspecting
   the Zarr store with ``xarray.open_zarr()`` and listing ``ds.data_vars``.
