"""
Athens summer 2023 — minimal end-to-end demo
============================================

Extracts ERA5 2m temperature for Athens from the ARCO Data Lake,
aggregates to a monthly mean, writes a Cloud Optimized GeoTIFF,
and generates a calibrated QGIS style file.

Tested against the live ECMWF ARCO service on 2026-05-05.

Requires:
    pip install "geobridge[zarr]"

Run:
    python examples/athens_urban_heat.py
"""

import logging
from pathlib import Path

import geobridge as gb

logging.basicConfig(level=logging.INFO, format="%(message)s")

# Athens metropolitan area bbox (WGS-84) — slightly widened so the
# 0.25-degree ERA5 grid contains at least 2x2 cells.
ATHENS_BBOX = (23.0, 37.5, 24.5, 38.5)
SUMMER_2023 = ("2023-06-01", "2023-08-31")
OUTPUT_DIR = Path("./athens_outputs")


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)

    # 1. Authenticate (reads ~/.cdsapirc by default)
    print("\n[1/4] Authenticating...")
    gb.authenticate()

    # 2. Resolve the user's intent via semantic search
    print("\n[2/4] Resolving 'urban heat island' via semantic search...")
    matches = gb.semantic_search("urban heat island")
    top = matches[0]
    print(f"  Top match: {top.use_case_label}")
    print(f"  Dataset:   {top.dataset_id}")
    print(f"  Variable:  {top.variable}")
    print(f"  Aggregation: {top.recommended_aggregation}")

    # 3. Extract a Cloud Optimized GeoTIFF for Athens, summer 2023
    print("\n[3/4] Extracting ERA5 monthly mean temperature for Athens...")
    print("       (this fetches Zarr chunks over HTTP — first run may take ~1 min)")
    tif = gb.zarr_to_geotiff(
        dataset=top.dataset_id,
        variable=top.variable,
        bbox=ATHENS_BBOX,
        time_range=SUMMER_2023,
        aggregation=top.recommended_aggregation,
        output_path=OUTPUT_DIR / "athens_temperature_summer2023.tif",
        cog=True,
    )
    print(f"  Wrote: {tif}")

    # 4. Generate a calibrated QGIS style for the result
    print("\n[4/4] Generating QGIS QML style...")
    qml = gb.to_qgis_style(
        "2m_temperature",
        style_type="raw",
        output_path=OUTPUT_DIR / "temperature_anomaly.qml",
    )
    print(f"  Wrote: {qml}")

    print(f"\nDone. Outputs in: {OUTPUT_DIR.resolve()}")
    print("Load the .tif into QGIS, then Properties → Style → Load Style → "
          "select the .qml.")


if __name__ == "__main__":
    main()
