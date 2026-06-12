"""Minimal proof-of-concept: read ERA5 from the new ARCO data lake."""
import os
import pathlib

import xarray as xr


def get_cds_key() -> str:
    """Read CDS API key from ~/.cdsapirc."""
    config = pathlib.Path.home() / ".cdsapirc"
    for line in config.read_text().splitlines():
        line = line.strip()
        if line.startswith("key"):
            sep = ":" if ":" in line else "="
            _, _, value = line.partition(sep)
            return value.strip().strip('"').strip("'")
    raise RuntimeError("No 'key' line found in ~/.cdsapirc")


def main() -> None:
    cds_key = get_cds_key()
    print(f"Key length: {len(cds_key)}")

    # Time-chunked: optimised for spatial maps over short periods
    url = ("https://arco.datastores.ecmwf.int/cadl-arco-time-002/arco/"
           "reanalysis_era5_single_levels/sfc/timeChunked.zarr")

    print(f"\nOpening Zarr store...")
    ds = xr.open_zarr(
        url,
        consolidated=True,
        storage_options={
            "headers": {"Authorization": f"Bearer {cds_key}"},
        },
    )

    print(f"\nVariables in dataset: {list(ds.data_vars)[:10]} ...")
    print(f"Dimensions: {dict(ds.sizes)}")
    print(f"Time coverage: {ds.time.min().values} to {ds.time.max().values}")

    print(f"\nSelecting Athens, July 2023, 2m temperature...")
    t2m_athens = ds["t2m"].sel(
        time=slice("2023-07-01", "2023-07-31"),
        longitude=slice(23.5, 24.1),
        latitude=slice(37.8, 38.1),    # ← swapped: ascending order
    )
    print(f"Subset shape: {dict(t2m_athens.sizes)}")

    print(f"\nComputing monthly mean (this triggers the actual download)...")
    monthly_mean_kelvin = t2m_athens.mean(dim="time")
    monthly_mean_celsius = monthly_mean_kelvin - 273.15

    # .compute() forces the lazy graph to execute
    result = monthly_mean_celsius.compute()
    print(f"\nResult:\n{result}")
    print(f"\nMean temperature across Athens grid: {float(result.mean()):.2f} °C")


if __name__ == "__main__":
    main()