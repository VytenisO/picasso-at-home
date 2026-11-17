#!/bin/sh
# Perseverance traverse + waypoints from NASA/JPL MMGIS, and from the PDS Geosciences Node the
# PLACES rover localizations and the Three Forks depot tube drop table, into data/.
# The DEM (M20_JezeroCrater_CTXDEM_20m.tif) comes from USGS Astrogeology separately.
set -e
cd "$(dirname "$0")"
mkdir -p data/pds
for f in M20_waypoints.json M20_traverse.json; do
    curl -fsSL -o "data/$f" "https://mars.nasa.gov/mmgis-maps/M20/Layers/json/$f"
done
PDS=https://pds-geosciences.wustl.edu/m2020
curl -fsSL -o data/pds/best_interp.csv "$PDS/urn-nasa-pds-mars2020_rover_places/data_localizations/best_interp.csv"
for f in sample_tube_locations sample_tube_event_sols sample_tube_identifiers; do
    curl -fsSL -o "data/pds/$f.csv" "$PDS/urn-nasa-pds-mars2020_sample_depot_three_forks/miscellaneous/sample_metadata/$f.csv"
done
