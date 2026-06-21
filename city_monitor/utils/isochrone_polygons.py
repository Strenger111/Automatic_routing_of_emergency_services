import networkx as nx
import geopandas as gpd
from shapely.geometry import Point, LineString, Polygon, MultiPolygon
from shapely.ops import unary_union, polygonize
import numpy as np

ISOCHRONE_CACHE = {}


def get_isochrone_rings(station_node, graph, time_limits_sec, speed_kmh):
    import geopandas as gpd
    from shapely.geometry import Point, LineString, Polygon
    from shapely.ops import unary_union, polygonize
    import networkx as nx

    speed_ms = speed_kmh / 3.6
    zones = {}

    dist_dict = nx.single_source_dijkstra_path_length(
        graph, station_node, weight='length'
    )

    prev_poly = None

    for t_sec in sorted(time_limits_sec):
        max_dist = t_sec * speed_ms

        nodes = {n for n, d in dist_dict.items() if d <= max_dist}
        if len(nodes) < 2:
            continue

        lines = []
        for u, v in graph.edges():
            if u in nodes and v in nodes:
                lines.append(LineString([
                    (graph.nodes[u]['x'], graph.nodes[u]['y']),
                    (graph.nodes[v]['x'], graph.nodes[v]['y'])
                ]))

        if not lines:
            continue

        gdf = gpd.GeoSeries(lines, crs="EPSG:4326").to_crs("EPSG:3857")

        # Создаем изолинии
        poly = unary_union(gdf.buffer(250))  # Увеличен буфер для лучшего покрытия

        # Заполняем "дырки" - полигоны, которые полностью окружены зоной
        if isinstance(poly, Polygon):
            # Одиночный полигон - заполняем внутренние дырки
            if poly.interiors:
                # Создаем заполненные дырки как отдельные полигоны и объединяем
                filled_poly = Polygon(poly.exterior.coords)
                for interior in poly.interiors:
                    # Если внутренняя дырка полностью окружена, добавляем её
                    if interior.area < poly.area * 0.3:  # Не слишком большая дыра
                        filled_poly = filled_poly.union(Polygon(interior.coords))
                poly = filled_poly
        elif isinstance(poly, MultiPolygon):
            # Для мультиполигонов - соединяем близкие полигоны
            polygons = list(poly.geoms)
            merged = []
            used = set()

            for i, p1 in enumerate(polygons):
                if i in used:
                    continue
                cluster = [p1]
                used.add(i)
                for j, p2 in enumerate(polygons[i + 1:], i + 1):
                    if j not in used and p1.distance(p2) < 100:  # 100 метров между полигонами
                        cluster.append(p2)
                        used.add(j)
                if len(cluster) > 1:
                    merged.append(unary_union(cluster))
                else:
                    merged.append(p1)
            poly = unary_union(merged)

        # Небольшое сглаживание
        poly = poly.buffer(30).buffer(-20)

        poly = gpd.GeoSeries([poly], crs="EPSG:3857").to_crs("EPSG:4326").iloc[0]

        minutes = t_sec // 60

        if prev_poly is None:
            ring = poly
            name = f"0-{minutes} мин"
        else:
            # Вычитаем предыдущую зону, но заполняем оставшиеся дырки
            diff_poly = poly.difference(prev_poly).buffer(0)

            # Если остались дырки в разности, заполняем их
            if isinstance(diff_poly, (Polygon, MultiPolygon)):
                if diff_poly.area > 0:
                    ring = diff_poly
                else:
                    # Если разность пуста, значит новая зона полностью внутри старой
                    # создаем кольцо
                    ring = poly.buffer(0)
            else:
                ring = diff_poly
            name = f"{prev_minutes}-{minutes} мин"

        if hasattr(ring, 'is_empty') and not ring.is_empty and ring.area > 0:
            zones[name] = ring.__geo_interface__
        elif not ring.is_empty and ring.area > 0:
            zones[name] = ring.__geo_interface__

        prev_poly = poly
        prev_minutes = minutes

    return zones