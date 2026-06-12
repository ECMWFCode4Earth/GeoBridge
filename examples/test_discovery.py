import geobridge as gb
# All ARCO datasets (fast extraction)
print("ARCO only:")
for ds in gb.discover(arco_only=True):
    print(ds.id)
# All datasets that can be extracted (ARCO + CDS API)
print("\n Extracation only:")
for ds in gb.discover(extraction_only=True):
    print(ds.id, "ARCO" if ds.has_zarr else "CDS API")