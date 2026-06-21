import os
import osmnx as ox
import geopandas as gpd
from shapely.geometry import Point
import json

ox.settings.use_cache = True
ox.settings.cache_folder = "./cache/osmnx"
ox.settings.overpass_endpoint = "https://overpass-api.de/api/interpreter"

os.makedirs("static/data", exist_ok=True)

# Задаём ограничивающий прямоугольник (bbox)
north, south, east, west = 56.75, 56.70, 37.20, 37.10

print("Loading graph...")
G = ox.graph_from_bbox(north, south, east, west, network_type="drive")
ox.save_graphml(G, "static/data/graph.graphml")
print("Graph saved.")

print("Loading buildings...")
tags = {"building": True}
buildings = ox.features_from_bbox(north, south, east, west, tags=tags)[["geometry"]]
buildings_proj = buildings.to_crs("EPSG:3857")
buildings_proj["centroid"] = buildings_proj.geometry.centroid
houses = gpd.GeoDataFrame(geometry=buildings_proj["centroid"].to_crs("EPSG:4326"))
houses = houses.sample(min(len(houses), 1500))
houses.to_file("static/data/houses.geojson", driver="GeoJSON")
print(f"Houses: {len(houses)} saved.")

stations = [
    {"id": 1, "name": "Пожарная", "type": "fire", "lat": 56.734, "lon": 37.162},
    {"id": 2, "name": "Скорая", "type": "ambulance", "lat": 56.741, "lon": 37.178},
    {"id": 3, "name": "Полиция", "type": "police", "lat": 56.728, "lon": 37.155}
]
with open("static/data/stations.json", "w", encoding="utf-8") as f:
    json.dump(stations, f, ensure_ascii=False, indent=2)
print("Initial stations saved.")
