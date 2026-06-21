import osmnx as ox
import geopandas as gpd

# Загружаем граф
G = ox.load_graphml("static/data/graph.graphml")
nodes_gdf = ox.graph_to_gdfs(G, edges=False)

# Берём 500 случайных узлов графа (это будут "дома" на дорогах)
sample_nodes = nodes_gdf.sample(min(500, len(nodes_gdf)))
houses = gpd.GeoDataFrame(geometry=sample_nodes.geometry, crs="EPSG:4326")
houses.to_file("static/data/houses.geojson", driver="GeoJSON")
print(f"Создано {len(houses)} тестовых домов")