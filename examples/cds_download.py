import geobridge as gb
gb.authenticate()

tif = gb.cds_to_geotiff(
    dataset="reanalysis-era5-pressure-levels",
    request={
        "product_type": ["reanalysis"],
        "variable": ["temperature", "geopotential"],
        "pressure_level": ["500", "850"],
        "year": ["2023"],
        "month": ["07"],
        "day": ["01", "15"],
        "time": ["12:00"],
        "data_format": "netcdf",
    },
    bbox=(23.0, 37.5, 24.5, 38.5),
    output_path="era5_pl_500hpa.tif",
)