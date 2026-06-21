import networkx as nx
from utils.isochrone_polygons import get_isochrone_rings
from functools import lru_cache
import hashlib
import json

# LRU кэш для изохрон
_compute_zones_cache = {}
_compute_zones_cache_maxsize = 128


def compute_zones(station_node, graph, speed_kmh, time_limits_sec):
    """Вычисление зон с кэшированием"""
    # Создаём ключ кэша
    time_limits_tuple = tuple(time_limits_sec)
    cache_key = f"zone_{station_node}_{speed_kmh}_{time_limits_tuple}"

    # Проверяем кэш
    if cache_key in _compute_zones_cache:
        return _compute_zones_cache[cache_key]

    # Вычисляем изохроны
    result = get_isochrone_rings(station_node, graph, time_limits_sec, speed_kmh)

    # Сохраняем в кэш (с ограничением размера)
    if len(_compute_zones_cache) >= _compute_zones_cache_maxsize:
        # Удаляем первый (самый старый) элемент
        _compute_zones_cache.pop(next(iter(_compute_zones_cache)))

    _compute_zones_cache[cache_key] = result
    return result


def count_houses_in_zones(zones_geojson, houses_gdf):
    """
    Подсчёт количества домов в каждой изохронной зоне
    zones_geojson: словарь {название_зоны: geojson_geometry}
    houses_gdf: GeoDataFrame с домами (должен содержать колонку 'geometry')
    """
    from shapely.geometry import shape

    if houses_gdf is None or houses_gdf.empty:
        return {zone_name: 0 for zone_name in zones_geojson.keys()}

    counts = {}

    # Оптимизация: используем spatial index если доступен
    try:
        # Создаём пространственный индекс для быстрых запросов
        spatial_index = houses_gdf.sindex

        for zone_name, geom in zones_geojson.items():
            poly = shape(geom)

            # Используем spatial index для быстрого поиска
            possible_matches_idx = list(spatial_index.intersection(poly.bounds))
            if possible_matches_idx:
                # Проверяем только потенциальные кандидаты
                mask = houses_gdf.iloc[possible_matches_idx].geometry.within(poly)
                counts[zone_name] = int(mask.sum())
            else:
                counts[zone_name] = 0
    except:
        # Fallback: медленный полный перебор
        for zone_name, geom in zones_geojson.items():
            poly = shape(geom)
            mask = houses_gdf.geometry.within(poly)
            counts[zone_name] = int(mask.sum())

    return counts


# Кэш для travel time
_travel_time_cache = {}
_travel_time_cache_maxsize = 5000


def compute_travel_time(graph, from_node, to_node, speed_kmh=50):
    """Кэшированный расчёт времени пути"""
    cache_key = f"tt_{from_node}_{to_node}_{speed_kmh}"

    if cache_key in _travel_time_cache:
        return _travel_time_cache[cache_key]

    try:
        length = nx.shortest_path_length(graph, from_node, to_node, weight='length')
        result = length / (speed_kmh / 3.6)
    except:
        result = float('inf')

    # Ограничиваем размер кэша
    if len(_travel_time_cache) >= _travel_time_cache_maxsize:
        _travel_time_cache.clear()

    _travel_time_cache[cache_key] = result
    return result


# Кэш для nearest road point
_nearest_road_cache = {}
_nearest_road_cache_maxsize = 2000


def get_nearest_road_point(graph, lat, lon):
    """Находит ближайший узел графа к точке"""
    import osmnx as ox
    from geopy.distance import distance

    cache_key = f"nrp_{lat:.6f}_{lon:.6f}"

    if cache_key in _nearest_road_cache:
        return _nearest_road_cache[cache_key]

    try:
        u, v, _ = ox.distance.nearest_edges(graph, lon, lat, return_dist=False)
        u_lat, u_lon = graph.nodes[u]['y'], graph.nodes[u]['x']
        v_lat, v_lon = graph.nodes[v]['y'], graph.nodes[v]['x']
        dist_u = distance((lat, lon), (u_lat, u_lon)).meters
        dist_v = distance((lat, lon), (v_lat, v_lon)).meters
        result = u if dist_u < dist_v else v
    except Exception:
        result = ox.nearest_nodes(graph, lon, lat)

    # Ограничиваем размер кэша
    if len(_nearest_road_cache) >= _nearest_road_cache_maxsize:
        _nearest_road_cache.clear()

    _nearest_road_cache[cache_key] = result
    return result


# Функция для очистки кэша (полезна при перезагрузке данных)
def clear_routing_cache():
    """Очищает все кэши маршрутизации"""
    global _compute_zones_cache, _travel_time_cache, _nearest_road_cache
    _compute_zones_cache.clear()
    _travel_time_cache.clear()
    _nearest_road_cache.clear()
    print("✅ Routing caches cleared")