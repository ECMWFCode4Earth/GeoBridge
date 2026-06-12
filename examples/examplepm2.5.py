import os
import geobridge as gb

def main():
    # Authenticate with the geobridge service
    gb.authenticate()

    print('Testing CAMS PM2.5...')
    
    # Get the directory where this script is currently located
    current_dir = os.path.dirname(os.path.abspath(__file__))
    output_file = os.path.join(current_dir, 'athens_pm25.tif')
    
    # Download and convert the Zarr dataset to GeoTIFF format
    tif = gb.zarr_to_geotiff(
        dataset='cams_europe_air_quality_reanalyses',
        variable='pm2p5',
        bbox=(23.0, 37.5, 24.5, 38.5),
        time_range=('2023-07-01', '2023-07-31'),
        aggregation='monthly_mean',
        output_path=output_file,
    )
    
    print(f'CAMS done: {tif}')

if __name__ == '__main__':
    main()
