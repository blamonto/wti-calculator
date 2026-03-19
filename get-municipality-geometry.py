#!/usr/bin/env python3
"""
Get municipality polygon geometry via OSMnx (OpenStreetMap).
Returns GeoJSON for use with GEE zonal analysis.

Usage:
  python get-municipality-geometry.py "Somiedo"
  python get-municipality-geometry.py "Cangas de Onís"
  python get-municipality-geometry.py "Somiedo, Asturias, España"
"""
import sys
import json
import osmnx as ox

def get_municipality_geojson(name):
    """Get GeoJSON polygon for a Spanish municipality."""
    queries = [
        f"{name}, España",
        f"{name}, Asturias, España",
        f"{name}, Spain",
        name,
    ]
    for query in queries:
        try:
            gdf = ox.geocode_to_gdf(query)
            if gdf is not None and len(gdf) > 0:
                row = gdf.iloc[0]
                geom = row.geometry
                area_km2 = gdf.to_crs(epsg=32629).area.iloc[0] / 1e6
                centroid = geom.centroid
                return {
                    "name": row.get("display_name", name).split(",")[0],
                    "lat": round(centroid.y, 6),
                    "lng": round(centroid.x, 6),
                    "area_km2": round(area_km2, 2),
                    "geojson": json.loads(gdf.to_json()),
                    "geometry": geom.__geo_interface__,
                }
        except Exception:
            continue
    raise ValueError(f"Municipality not found: {name}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python get-municipality-geometry.py 'Municipality Name'", file=sys.stderr)
        sys.exit(1)
    result = get_municipality_geojson(sys.argv[1])
    print(json.dumps(result))
