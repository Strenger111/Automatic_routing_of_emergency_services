from sqlalchemy import func
from database import Session, Incident, Vehicle, Station
from datetime import datetime, timedelta
import logging
from models import DataModel
from routing import compute_zones, count_houses_in_zones
from shapely.geometry import shape, Point
from collections import defaultdict
from math import radians, cos, sin, asin, sqrt
import numpy as np
from functools import lru_cache
from sqlalchemy import func, text
from collections import defaultdict
import time

# Кэш для аналитики (TTL 60 секунд)
_analytics_cache = {}
_analytics_cache_time = {}


def cached_analytics(ttl=60):
    """Декоратор для кэширования результатов аналитики"""

    def decorator(func):
        def wrapper(*args, **kwargs):
            cache_key = f"{func.__name__}_{args}_{kwargs}"
            now = time.time()

            if cache_key in _analytics_cache:
                cached_time = _analytics_cache_time.get(cache_key, 0)
                if now - cached_time < ttl:
                    return _analytics_cache[cache_key]

            result = func(*args, **kwargs)
            _analytics_cache[cache_key] = result
            _analytics_cache_time[cache_key] = now
            return result

        return wrapper

    return decorator


@cached_analytics(ttl=30)  # Кэш на 30 секунд
def get_avg_response_time_by_type(start_date=None, end_date=None):
    """Оптимизированная версия"""
    session = Session()
    try:
        query = """
            SELECT type, AVG(response_time_sec) 
            FROM incidents 
            WHERE resolved = true
        """
        params = {}

        if start_date:
            query += " AND created_at >= :start"
            params['start'] = start_date
        if end_date:
            query += " AND created_at <= :end"
            params['end'] = end_date

        query += " GROUP BY type"

        result = session.execute(text(query), params).fetchall()
        return {typ: float(avg or 0) for typ, avg in result}
    finally:
        session.close()


@cached_analytics(ttl=30)
@cached_analytics(ttl=30)
def get_incidents_per_hour(hours=24):
    """Оптимизированная версия"""
    session = Session()
    try:
        # PostgreSQL требует явного указания интервала
        query = """
            SELECT 
                date_trunc('hour', created_at) as hour,
                COUNT(*) 
            FROM incidents 
            WHERE created_at >= NOW() - (INTERVAL '1 HOUR' * :hours)
            GROUP BY hour 
            ORDER BY hour
        """
        result = session.execute(text(query), {'hours': hours}).fetchall()
        return [{"hour": r[0].isoformat(), "hour_local": r[0], "count": r[1]} for r in result]
    finally:
        session.close()


def get_vehicle_utilization(vehicle_id, hours=24):
    session = Session()
    vehicle = session.query(Vehicle).get(vehicle_id)
    if not vehicle:
        return None
    total_calls = vehicle.total_calls
    avg_response = vehicle.avg_response_time or 0
    total_service_sec = total_calls * (avg_response + (vehicle.service_time or 2))
    total_sec = hours * 3600
    utilization = total_service_sec / total_sec if total_sec > 0 else 0
    session.close()
    return {"vehicle_id": vehicle_id, "utilization": min(utilization, 1.0), "total_calls": total_calls}


def get_station_coverage():
    data = DataModel()
    session = Session()
    stations = session.query(Station).all()
    coverage = []
    for st in stations:
        zones = compute_zones(st.node_id, data.graph,
                              data.config['speed_kmh'],
                              data.config['time_limits_sec'])
        counts = count_houses_in_zones(zones, data.houses_gdf)
        coverage.append({
            "station_id": st.id,
            "name": st.name,
            "type": st.type,
            "zones": counts
        })
    session.close()
    return coverage


def get_zone_overlap():
    data = DataModel()
    session = Session()
    stations = session.query(Station).all()
    zones_geom = {}
    for st in stations:
        zones = compute_zones(st.node_id, data.graph,
                              data.config['speed_kmh'],
                              data.config['time_limits_sec'])
        if "0-5 мин" in zones:
            zones_geom[st.id] = shape(zones["0-5 мин"])
    overlaps = []
    sids = list(zones_geom.keys())
    for i in range(len(sids)):
        for j in range(i + 1, len(sids)):
            id1, id2 = sids[i], sids[j]
            geom1, geom2 = zones_geom[id1], zones_geom[id2]
            if geom1.is_valid and geom2.is_valid and geom1.intersects(geom2):
                area_int = geom1.intersection(geom2).area
                area1 = geom1.area
                area2 = geom2.area
                overlap_perc = (area_int / min(area1, area2)) * 100
                overlaps.append({
                    "station_a": id1, "station_b": id2,
                    "overlap_percent": round(overlap_perc, 2)
                })
    session.close()
    return overlaps


def get_incident_density_grid(grid_size=20):
    """Разбивает территорию на сетку и считает плотность инцидентов"""
    session = Session()
    incidents = session.query(Incident).filter(Incident.resolved == True).all()
    session.close()

    if not incidents:
        return []

    lats = [inc.lat for inc in incidents]
    lons = [inc.lon for inc in incidents]

    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)

    lat_step = (lat_max - lat_min) / grid_size
    lon_step = (lon_max - lon_min) / grid_size

    grid = []
    for i in range(grid_size):
        for j in range(grid_size):
            lat_center = lat_min + i * lat_step + lat_step / 2
            lon_center = lon_min + j * lon_step + lon_step / 2

            count = sum(1 for inc in incidents
                        if lat_min + i * lat_step <= inc.lat < lat_min + (i + 1) * lat_step
                        and lon_min + j * lon_step <= inc.lon < lon_min + (j + 1) * lon_step)

            if count > 0:
                grid.append({
                    "lat": lat_center,
                    "lon": lon_center,
                    "count": count,
                    "intensity": count / max(1, max(lats))
                })

    return grid


def get_hot_zones_for_station(incident_type=None, min_incidents=3):
    """Определяет горячие зоны для размещения новой станции с учетом существующих станций"""
    session = Session()

    # Получаем все инциденты
    query = session.query(Incident).filter(Incident.resolved == True)
    if incident_type:
        query = query.filter(Incident.type == incident_type)
    incidents = query.all()

    # Получаем существующие станции и их зоны покрытия
    data = DataModel()
    stations = session.query(Station).all()

    # Собираем зоны покрытия существующих станций (5-минутные зоны)
    covered_areas = []
    for station in stations:
        if station.type == incident_type or incident_type is None:
            try:
                zones = compute_zones(station.node_id, data.graph,
                                      data.config['speed_kmh'],
                                      data.config['time_limits_sec'])
                if "0-5 мин" in zones:
                    covered_areas.append(shape(zones["0-5 мин"]))
            except:
                pass

    session.close()

    if len(incidents) < min_incidents:
        return []

    def haversine(lat1, lon1, lat2, lon2):
        R = 6371
        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)
        a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
        c = 2 * asin(sqrt(a))
        return R * c

    # Функция для проверки, покрыт ли инцидент существующей станцией
    def is_incident_covered(incident):
        point = Point(incident.lon, incident.lat)
        for area in covered_areas:
            if area.contains(point):
                return True
        return False

    # Фильтруем только НЕпокрытые инциденты
    uncovered_incidents = [inc for inc in incidents if not is_incident_covered(inc)]

    if len(uncovered_incidents) < min_incidents:
        return [{
            "lat": None,
            "lon": None,
            "incident_count": 0,
            "type": incident_type or "mixed",
            "radius_km": 0,
            "message": "Все инциденты уже покрыты существующими станциями",
            "uncovered_count": len(uncovered_incidents)
        }]

    # Кластеризация только непокрытых инцидентов
    clusters = []
    used = [False] * len(uncovered_incidents)

    for i, inc in enumerate(uncovered_incidents):
        if used[i]:
            continue

        cluster = [inc]
        used[i] = True

        for j, other in enumerate(uncovered_incidents[i + 1:], i + 1):
            if not used[j]:
                dist = haversine(inc.lat, inc.lon, other.lat, other.lon)
                if dist < 0.5:
                    cluster.append(other)
                    used[j] = True

        if len(cluster) >= min_incidents:
            center_lat = sum(inc.lat for inc in cluster) / len(cluster)
            center_lon = sum(inc.lon for inc in cluster) / len(cluster)

            # Вычисляем приоритет на основе количества инцидентов и расстояния до ближайшей станции
            min_dist_to_station = float('inf')
            for station in stations:
                if station.type == incident_type or incident_type is None:
                    dist = haversine(center_lat, center_lon, station.lat, station.lon)
                    min_dist_to_station = min(min_dist_to_station, dist)

            priority_score = len(cluster) * (1 + (min_dist_to_station if min_dist_to_station != float('inf') else 5))

            clusters.append({
                "lat": center_lat,
                "lon": center_lon,
                "incident_count": len(cluster),
                "type": incident_type or "mixed",
                "radius_km": max(haversine(center_lat, center_lon, inc.lat, inc.lon) for inc in cluster),
                "distance_to_nearest_station_km": round(min_dist_to_station, 2) if min_dist_to_station != float(
                    'inf') else None,
                "priority_score": round(priority_score, 2)
            })

    # Сортируем по приоритету (учитываем и количество, и удаленность от станций)
    return sorted(clusters, key=lambda x: x["priority_score"], reverse=True)[:5]


def get_patrol_recommendations():
    """Рекомендует места для патрулирования на основе времени прибытия"""
    data = DataModel()
    session = Session()
    stations = session.query(Station).all()

    incidents = session.query(Incident).filter(Incident.resolved == True).all()
    session.close()

    recommendations = []

    for station in stations:
        zones = compute_zones(station.node_id, data.graph,
                              data.config['speed_kmh'],
                              data.config['time_limits_sec'])

        if "0-5 мин" in zones:
            zone_geom = shape(zones["0-5 мин"])

            far_incidents = []
            for inc in incidents:
                point = Point(inc.lon, inc.lat)
                if not zone_geom.contains(point):
                    far_incidents.append(inc)

            if far_incidents:
                center_lat = sum(inc.lat for inc in far_incidents) / len(far_incidents)
                center_lon = sum(inc.lon for inc in far_incidents) / len(far_incidents)

                recommendations.append({
                    "station_id": station.id,
                    "station_name": station.name,
                    "station_type": station.type,
                    "recommended_patrol_lat": center_lat,
                    "recommended_patrol_lon": center_lon,
                    "far_incidents_count": len(far_incidents),
                    "reason": f"Обнаружено {len(far_incidents)} инцидентов вне 5-минутной зоны"
                })

    return sorted(recommendations, key=lambda x: x["far_incidents_count"], reverse=True)[:5]


def get_economic_analysis():
    """Экономический анализ эффективности станций"""
    session = Session()
    stations = session.query(Station).all()
    incidents = session.query(Incident).filter(Incident.resolved == True).all()

    analysis = []

    for station in stations:
        station_incidents = []
        for inc in incidents:
            if inc.assigned_vehicle_id:
                vehicle = session.query(Vehicle).get(inc.assigned_vehicle_id)
                if vehicle and vehicle.station_id == station.id:
                    station_incidents.append(inc)

        avg_response = sum((inc.response_time_sec or 0) for inc in station_incidents) / max(1, len(station_incidents))

        # Экономическая оценка (условная)
        cost_per_incident = 500
        time_penalty = avg_response / 60 * 100
        total_cost = len(station_incidents) * (cost_per_incident + time_penalty)

        analysis.append({
            "station_id": station.id,
            "station_name": station.name,
            "station_type": station.type,
            "incidents_handled": len(station_incidents),
            "avg_response_sec": round(avg_response, 1),
            "estimated_cost_rub": round(total_cost, 2),
            "efficiency_score": round(len(station_incidents) / max(1, avg_response) * 100, 2)
        })

    session.close()
    return sorted(analysis, key=lambda x: x["efficiency_score"], reverse=True)