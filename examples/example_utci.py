import os
import geobridge as gb

def main():
    # Authenticate with the geobridge service
    gb.authenticate()

    print('Testing UTCI...')
    
    # Get the directory where this script is currently located
    current_dir = os.path.dirname(os.path.abspath(__file__))
    output_file = os.path.join(current_dir, 'athens_utci.tif')
    
    # Download and convert the Zarr dataset to GeoTIFF format
    tif2 = gb.zarr_to_geotiff(
        dataset='derived_utci_historical',
        variable='utci',
        bbox=(23.0, 37.5, 24.5, 38.5),
        time_range=('2023-07-01', '2023-07-31'),
        aggregation='monthly_mean',
        output_path='/tmp/athens_utci.tif',
    )
    
    print(f'UTCI done: {tif2}')

if __name__ == '__main__':
    main()

