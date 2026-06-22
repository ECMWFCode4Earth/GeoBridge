"""Proof-of-concept: read any ARCO Zarr dataset from ECMWF's data lake.

Usage examples
--------------
# ERA5 2m temperature over Athens (default)
python examples/arco_test.py

# CAMS PM2.5 over Europe, different time window
python examples/arco_test.py \\
    --url "https://arco.datastores.ecmwf.int/cadl-arco-time-002/arco/cams_europe_air_quality_reanalyses/sfc/timeChunked.zarr" \\
    --variable pm2p5 \\
    --time-start 2022-01-01 --time-end 2022-01-31 \\
    --lon-min -10 --lon-max 40 \\
    --lat-min 35 --lat-max 60

# Custom dataset, geo-chunked store
python examples/arco_test.py \\
    --url "https://arco.datastores.ecmwf.int/cadl-arco-geo-002/arco/reanalysis_era5_single_levels/sfc/geoChunked.zarr" \\
    --variable u10
"""
import argparse
import pathlib


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read any ARCO Zarr dataset and compute a spatial mean.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--url",
        default=(
            "https://arco.datastores.ecmwf.int/cadl-arco-time-002/arco/"
            "reanalysis_era5_single_levels/sfc/timeChunked.zarr"
        ),
        help="ARCO Zarr store URL (default: ERA5 single-levels, time-chunked)",
    )
    parser.add_argument(
        "--variable", "-v", default="t2m",
        help="Variable short name to extract (default: t2m)",
    )
    parser.add_argument(
        "--time-start", default="2023-07-01",
        help="Start of time slice, ISO-8601 date (default: 2023-07-01)",
    )
    parser.add_argument(
        "--time-end", default="2023-07-31",
        help="End of time slice, ISO-8601 date (default: 2023-07-31)",
    )
    parser.add_argument(
        "--lon-min", type=float, default=23.5,
        help="Western longitude bound (default: 23.5 — Athens)",
    )
    parser.add_argument(
        "--lon-max", type=float, default=24.1,
        help="Eastern longitude bound (default: 24.1 — Athens)",
    )
    parser.add_argument(
        "--lat-min", type=float, default=37.8,
        help="Southern latitude bound (default: 37.8 — Athens)",
    )
    parser.add_argument(
        "--lat-max", type=float, default=38.1,
        help="Northern latitude bound (default: 38.1 — Athens)",
    )
    parser.add_argument(
        "--kelvin-offset", type=float, default=None,
        help=(
            "Subtract this value from the result before displaying "
            "(e.g. 273.15 to convert Kelvin→Celsius). "
            "Defaults to 273.15 when --variable is 't2m', else 0."
        ),
    )
    parser.add_argument(
        "--no-consolidated", action="store_true",
        help="Open the Zarr store without consolidated metadata.",
    )
    return parser.parse_args()


def main() -> None:
    import xarray as xr

    args = parse_args()

    cds_key = get_cds_key()
    print(f"Key length: {len(cds_key)}")

    # Resolve unit offset: default Kelvin→Celsius only for t2m
    if args.kelvin_offset is not None:
        offset = args.kelvin_offset
    elif args.variable == "t2m":
        offset = 273.15
    else:
        offset = 0.0

    print(f"\nOpening Zarr store: {args.url}")
    ds = xr.open_zarr(
        args.url,
        consolidated=not args.no_consolidated,
        storage_options={
            "headers": {"Authorization": f"Bearer {cds_key}"},
        },
    )

    print(f"\nVariables: {list(ds.data_vars)[:10]} ...")
    print(f"Dimensions: {dict(ds.sizes)}")
    if "time" in ds:
        print(f"Time coverage: {ds.time.min().values} to {ds.time.max().values}")

    if args.variable not in ds:
        raise KeyError(
            f"Variable {args.variable!r} not found. "
            f"Available: {list(ds.data_vars)}"
        )

    print(
        f"\nSelecting '{args.variable}' "
        f"time={args.time_start}..{args.time_end} "
        f"lon={args.lon_min}..{args.lon_max} "
        f"lat={args.lat_min}..{args.lat_max} ..."
    )

    # Detect whether latitude is stored ascending or descending
    lat_dim = next((d for d in ds[args.variable].dims if "lat" in d.lower()), None)
    lon_dim = next((d for d in ds[args.variable].dims if "lon" in d.lower()), None)
    time_dim = next((d for d in ds[args.variable].dims if "time" in d.lower()), None)

    sel_kwargs: dict = {}
    if time_dim:
        sel_kwargs[time_dim] = slice(args.time_start, args.time_end)
    if lon_dim:
        sel_kwargs[lon_dim] = slice(args.lon_min, args.lon_max)
    if lat_dim:
        lat_vals = ds[lat_dim].values
        if lat_vals[0] > lat_vals[-1]:
            # Descending: flip the slice so .sel works correctly
            sel_kwargs[lat_dim] = slice(args.lat_max, args.lat_min)
        else:
            sel_kwargs[lat_dim] = slice(args.lat_min, args.lat_max)

    subset = ds[args.variable].sel(**sel_kwargs)
    print(f"Subset shape: {dict(subset.sizes)}")

    print("\nComputing mean (this triggers the actual download)...")
    mean_vals = subset.mean(dim=time_dim) if time_dim else subset
    if offset:
        mean_vals = mean_vals - offset

    result = mean_vals.compute()
    print(f"\nResult:\n{result}")

    unit_note = f" (−{offset})" if offset else ""
    print(f"\nMean across selection{unit_note}: {float(result.mean()):.4f}")


if __name__ == "__main__":
    main()
