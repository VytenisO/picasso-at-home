#!/bin/sh
# Perseverance traverse + waypoints from NASA/JPL MMGIS into data/.
# The DEM (M20_JezeroCrater_CTXDEM_20m.tif) comes from USGS Astrogeology separately.
set -e
cd "$(dirname "$0")"
mkdir -p data
for f in M20_waypoints.json M20_traverse.json; do
    curl -fsSL -o "data/$f" "https://mars.nasa.gov/mmgis-maps/M20/Layers/json/$f"
done
