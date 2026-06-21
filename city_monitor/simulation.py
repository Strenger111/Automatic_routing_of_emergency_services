import threading
import time
import random
import networkx as nx
import osmnx as ox
import logging
from sqlalchemy.exc import OperationalError
from shapely.geometry import Point, Polygon
from shapely import wkt
from functools import lru_cache

from database import Session, Vehicle as DBVehicle, Station as DBStation, Incident

logging.basicConfig(level=logging.INFO)

vehicles_dict = {}
vehicles_lock = threading.RLock()
socketio_instance = None
pending_incidents = []
pending_lock = threading.Lock()
simulation_speed_global = 1
simulation_running = True
simulation_thread = None

TYPE_SPEED_MS = {
    'fire': 60 / 3.6,
    'ambulance': 50 / 3.6,
    'police': 70 / 3.6
}


def safe_db_operation(func):
    def wrapper(*args, **kwargs):
        max_retries = 3
        for i in range(max_retries):
            try:
                return func(*args, **kwargs)
            except OperationalError as e:
                if i < max_retries - 1:
                    logging.warning(f"DB error, retrying: {e}")

                else:
                    raise
        return None

    return wrapper


class Vehicle:
    def __init__(self, vid, start_node, graph, speed_kmh=100,
                 is_temp=False, db_id=None, station_node=None, vehicle_type=None, service_time=30,
                 inside_station=True, saved_status=None, saved_route=None, saved_target=None):
        self.id = vid
        self.db_id = db_id or vid
        self.graph = graph
        self.current_node = start_node
        self.route = saved_route or []
        self.segment = None
        self.status = saved_status or "idle"
        self.type = vehicle_type
        if vehicle_type and vehicle_type in TYPE_SPEED_MS:
            self.base_speed_mps = TYPE_SPEED_MS[vehicle_type]
        else:
            self.base_speed_mps = speed_kmh / 3.6
        self.speed_mps = self.base_speed_mps
        self.current_speed_mps = 0.0
        self.is_temp = is_temp
        self.station_node = station_node
        self.target_node = saved_target
        self.assigned_incident_id = None
        self.patrol_timer = None
        self.service_timer = None
        self.service_time_sec = service_time
        self.inside_station = inside_station
        self.total_calls = 0
        self.avg_response_time = 0.0
        self.service_start_time = None
        self._last_save_time = 0
        self.pending_incidents = []
        self.simulation_speed = 30.0
        self.patrol_center = None
        self.patrol_radius = None
        self.patrol_zone_polygon = None

        if not is_temp and db_id:
            session = Session()
            try:
                veh_db = session.get(DBVehicle, db_id)
                if veh_db:
                    self.total_calls = veh_db.total_calls or 0
                    self.avg_response_time = veh_db.avg_response_time or 0.0
                    if veh_db.patrol_lat and veh_db.patrol_lon:
                        self.patrol_center = (veh_db.patrol_lat, veh_db.patrol_lon)
                        self.patrol_radius = veh_db.patrol_radius or 500
                    if veh_db.patrol_zone_polygon:
                        try:
                            self.patrol_zone_polygon = wkt.loads(veh_db.patrol_zone_polygon)
                        except:
                            pass
                    # Восстанавливаем последнюю позицию
                    if veh_db.last_lat and veh_db.last_lon and not inside_station:
                        self.current_node = ox.nearest_nodes(graph, veh_db.last_lon, veh_db.last_lat)
            except Exception as e:
                logging.error(f"Error loading vehicle stats: {e}")
            finally:
                session.close()

    def save_state(self):
        """Сохраняет текущее состояние машины в БД"""
        if self.is_temp:
            return
        session = Session()
        try:
            vehicle = session.get(DBVehicle, self.db_id)
            if vehicle:
                lat, lon = self.get_current_coords()
                vehicle.last_node_id = self.current_node
                vehicle.last_lat = lat
                vehicle.last_lon = lon
                vehicle.status = self.status
                if self.patrol_zone_polygon:
                    vehicle.patrol_zone_polygon = self.patrol_zone_polygon.wkt
                session.commit()
        except Exception as e:
            logging.error(f"Error saving vehicle state: {e}")
            session.rollback()
        finally:
            session.close()

    def get_speed_for_segment(self, from_node, to_node):
        base_speed = self.base_speed_mps
        variation = random.uniform(0.85, 1.15)
        speed = base_speed * variation
        if self.route:
            next_node = self.route[0]

            def get_bearing(u, v):
                from math import atan2, cos, sin, radians, degrees
                lon1, lat1 = self.graph.nodes[u]['x'], self.graph.nodes[u]['y']
                lon2, lat2 = self.graph.nodes[v]['x'], self.graph.nodes[v]['y']
                dLon = radians(lon2 - lon1)
                y = sin(dLon) * cos(radians(lat2))
                x = cos(radians(lat1)) * sin(radians(lat2)) - sin(radians(lat1)) * cos(radians(lat2)) * cos(dLon)
                return degrees(atan2(y, x))

            bearing1 = get_bearing(from_node, to_node)
            bearing2 = get_bearing(to_node, next_node)
            angle = abs(bearing1 - bearing2) % 360
            if angle > 180:
                angle = 360 - angle
            if angle > 30:
                reduction = min(0.5, angle / 180.0)
                speed *= (1 - reduction)
        return max(speed, 2.0)

    def _init_first_segment(self):
        if self.route and self.segment is None:
            from_node = self.current_node
            to_node = self.route.pop(0)
            try:
                edge_data = self.graph[from_node][to_node]
                if 0 in edge_data:
                    dist = edge_data[0]['length']
                else:
                    dist = list(edge_data.values())[0]['length']
                speed = self.get_speed_for_segment(from_node, to_node)
                self.segment = (from_node, to_node, dist, 0.0, speed)
                return True
            except KeyError:
                self.segment = None
                return False
        return False

    def update_position(self, dt=1.0):
        global simulation_speed_global

        if self.inside_station:
            self.current_speed_mps = 0
            return

        self.simulation_speed = simulation_speed_global
        dt_real = dt * self.simulation_speed

        # Обработка прибытия на место ЧП
        if self.segment is None and not self.route:
            if self.status == "responding" and self.current_node == self.target_node:
                self.status = "servicing"
                self.service_start_time = time.time()
                self.update_db_status("servicing")
                self.save_state()
                if self.service_timer:
                    self.service_timer.cancel()
                self.service_timer = threading.Timer(self.service_time_sec, self.finish_servicing)
                self.service_timer.daemon = True
                self.service_timer.start()
                return

            # Обработка возврата на станцию
            if self.status == "returning" and self.current_node == self.station_node:
                self.inside_station = True
                self.status = "idle"
                self.update_db_status("idle")
                self.route = []
                self.segment = None
                self.target_node = None
                self.current_speed_mps = 0
                self.save_state()
                logging.info(f"Vehicle {self.id} arrived at station")
                self.process_pending_incidents()
                dispatch_pending_incidents()
                if socketio_instance:
                    socketio_instance.emit('vehicle_hide', {'id': self.id})
                return

            # Патрулирование или движение
            if self.status in ("patrolling", "moving"):
                self.assign_random_route()
                return
            elif self.status == "returning":
                self.return_to_station()
                return
            else:
                if self.status == "idle" and not self.inside_station:
                    self.return_to_station()
                else:
                    self.assign_random_route()
                return

        if self.segment is None and self.route:
            self._init_first_segment()
            if self.segment is None:
                return

        if self.segment:
            from_node, to_node, dist, prog, speed = self.segment
            step = speed * dt_real / dist
            prog += step
            self.current_speed_mps = speed
            if prog >= 1.0:
                self.current_node = to_node
                self.segment = None
                self.save_state()
            else:
                self.segment = (from_node, to_node, dist, prog, speed)

        # Периодическое сохранение состояния
        if not self.is_temp and time.time() - self._last_save_time > 5.0:
            self._last_save_time = time.time()
            self.save_state()

    def get_current_coords(self):
        if self.inside_station:
            return self.graph.nodes[self.station_node]['y'], self.graph.nodes[self.station_node]['x']
        node = self.current_node
        return self.graph.nodes[node]['y'], self.graph.nodes[node]['x']

    def set_patrol_zone(self, center_lat, center_lon, radius_meters=500):
        """Устанавливает зону патрулирования как полигон"""
        from math import radians, cos, sin, asin, sqrt, atan2, degrees

        self.patrol_center = (center_lat, center_lon)
        self.patrol_radius = radius_meters

        # Создаём полигон зоны патруля (круг)
        R = 6371000  # радиус Земли в метрах
        d = radius_meters / R
        lat1 = radians(center_lat)
        lon1 = radians(center_lon)

        points = []
        for bearing in range(0, 360, 30):
            brng = radians(bearing)
            lat2 = asin(sin(lat1) * cos(d) + cos(lat1) * sin(d) * cos(brng))
            lon2 = lon1 + atan2(sin(brng) * sin(d) * cos(lat1), cos(d) - sin(lat1) * sin(lat2))
            points.append((degrees(lon2), degrees(lat2)))

        from shapely.geometry import Polygon
        self.patrol_zone_polygon = Polygon(points)

        # Сохраняем в БД
        from database import Session
        session = Session()
        try:
            vehicle = session.get(DBVehicle, self.db_id)
            if vehicle:
                vehicle.patrol_lat = center_lat
                vehicle.patrol_lon = center_lon
                vehicle.patrol_radius = radius_meters
                vehicle.patrol_zone_polygon = self.patrol_zone_polygon.wkt
                session.commit()
        finally:
            session.close()

    def is_in_patrol_zone(self, lat, lon):
        """Проверяет, находится ли точка в зоне патруля"""
        if not self.patrol_zone_polygon:
            return True
        point = Point(lon, lat)
        return self.patrol_zone_polygon.contains(point)

    def _get_patrol_target(self):
        with vehicles_lock:
            session = Session()
            try:
                # Сначала ищем нерешенные инциденты в зоне патруля
                incidents = session.query(Incident).filter_by(resolved=False, type=self.type).all()
                nearby = [inc for inc in incidents if self.is_in_patrol_zone(inc.lat, inc.lon)]
                if nearby:
                    inc = random.choice(nearby)
                    node = ox.nearest_nodes(self.graph, inc.lon, inc.lat)
                    return node
            finally:
                session.close()

        # Если нет инцидентов в зоне, патрулируем в пределах зоны
        if self.patrol_zone_polygon:
            bounds = self.patrol_zone_polygon.bounds
            for _ in range(10):
                target_lon = random.uniform(bounds[0], bounds[2])
                target_lat = random.uniform(bounds[1], bounds[3])
                point = Point(target_lon, target_lat)
                if self.patrol_zone_polygon.contains(point):
                    return ox.nearest_nodes(self.graph, target_lon, target_lat)

        return random.choice(list(self.graph.nodes))

    def assign_random_route(self):
        if self.status == "patrolling":
            target = self._get_patrol_target()
            try:
                path = nx.shortest_path(self.graph, self.current_node, target, weight='length')
                if len(path) > 1:
                    self.route = path[1:]
                    self._init_first_segment()
                else:
                    neighbors = list(self.graph.neighbors(self.current_node))
                    if neighbors:
                        self.route = [random.choice(neighbors)]
                        self._init_first_segment()
                    else:
                        self.status = "idle"
                        self.update_db_status("idle")
                return
            except:
                pass
        nodes = list(self.graph.nodes)
        for _ in range(10):
            target = random.choice(nodes)
            try:
                path = nx.shortest_path(self.graph, self.current_node, target, weight='length')
                if len(path) > 1:
                    self.route = path[1:]
                    self._init_first_segment()
                    if self.status != "patrolling":
                        self.status = "moving"
                        self.update_db_status("moving")
                    return
            except:
                continue
        neighbors = list(self.graph.neighbors(self.current_node))
        if neighbors:
            self.route = [random.choice(neighbors)]
            self._init_first_segment()
            if self.status != "patrolling":
                self.status = "moving"
                self.update_db_status("moving")
        else:
            self.status = "idle"
            self.update_db_status("idle")

    def assign_incident(self, incident_node, incident_id, incident_type, force=False):
        if not force and self.status not in ('idle', 'moving', 'patrolling', 'returning'):
            self.pending_incidents.append({
                'node': incident_node,
                'incident_id': incident_id,
                'type': incident_type
            })
            logging.info(f"=== ASSIGN INCIDENT ===")
            logging.info(f"Vehicle {self.id} assigned to incident {incident_id}")
            logging.info(f"Current node: {self.current_node}, Target node: {incident_node}")
            logging.info(f"Vehicle {self.id} busy, incident {incident_id} queued")
            return True

        try:
            if self.patrol_timer:
                self.patrol_timer.cancel()
                self.patrol_timer = None
            if self.service_timer:
                self.service_timer.cancel()
                self.service_timer = None

            path = nx.shortest_path(self.graph, self.current_node, incident_node, weight='length')
            if len(path) < 2:
                if self.current_node == incident_node:
                    self.target_node = incident_node
                    self.status = "servicing"
                    self.assigned_incident_id = incident_id
                    self.service_start_time = time.time()
                    self.update_db_status("servicing")
                    self.save_state()
                    self.service_timer = threading.Timer(self.service_time_sec, self.finish_servicing)
                    self.service_timer.daemon = True
                    self.service_timer.start()
                    return True
                return False
            self.route = path[1:]
            self.target_node = incident_node
            self.status = "responding"
            self.assigned_incident_id = incident_id
            self.update_db_status("responding")
            try:
                dist = nx.shortest_path_length(self.graph, self.current_node, incident_node, weight='length')
                travel_time = dist / self.base_speed_mps
                logging.info(f"Distance: {dist:.0f}m, Travel time: {travel_time:.2f}s")
            except:
                pass

            logging.info(f"=========================")
            if self.inside_station:
                self.inside_station = False
                if socketio_instance:
                    lat, lon = self.get_current_coords()
                    socketio_instance.emit('vehicle_update', {
                        'id': self.id, 'lat': lat, 'lon': lon,
                        'type': self.type, 'status': self.status, 'speed': 0
                    })
            self.segment = None
            self._init_first_segment()
            self.save_state()
            if socketio_instance:
                coords = [(self.graph.nodes[n]['y'], self.graph.nodes[n]['x']) for n in path]
                socketio_instance.emit('route_update', {
                    'vehicle_id': self.id,
                    'path_coords': coords
                })
            logging.info(f"Vehicle {self.id} assigned to incident {incident_id}")
            return True
        except Exception as e:
            logging.error(f"assign_incident error: {e}")
            return False

    def process_pending_incidents(self):
        if not self.pending_incidents:
            return
        best = None
        best_time = float('inf')
        for inc in self.pending_incidents:
            try:
                dist = nx.shortest_path_length(self.graph, self.current_node, inc['node'], weight='length')
                travel = dist / self.base_speed_mps
                if travel < best_time:
                    best_time = travel
                    best = inc
            except:
                continue
        if best:
            self.pending_incidents.remove(best)
            self.assign_incident(best['node'], best['incident_id'], best['type'], force=True)

    def finish_servicing(self):
        self.resolve_incident()

        # Если есть зона патруля - возвращаемся в неё
        if self.patrol_zone_polygon:
            # Возвращаемся в центр зоны патруля
            center_point = self.patrol_zone_polygon.centroid
            center_node = ox.nearest_nodes(self.graph, center_point.x, center_point.y)
            try:
                path = nx.shortest_path(self.graph, self.current_node, center_node, weight='length')
                if len(path) > 1:
                    self.route = path[1:]
                    self.target_node = center_node
                    self.status = "returning"
                    self.update_db_status("returning")
                    self.segment = None
                    self._init_first_segment()
                    self.save_state()
                    logging.info(f"Vehicle {self.id} returning to patrol zone")
                    return
            except Exception as e:
                logging.error(f"Error returning to patrol zone: {e}")

        # Если нет зоны патруля - возвращаемся на станцию
        self.return_to_station()

    def resolve_incident(self):
        session = Session()
        try:
            incident = session.get(Incident, self.assigned_incident_id)
            if incident and not incident.resolved:
                incident.resolved = True

                from datetime import datetime
                real_time = (datetime.now() - incident.created_at).total_seconds()

                # Умножаем на скорость симуляции
                simulation_time = real_time * simulation_speed_global

                logging.info(f"=== INCIDENT RESOLVED ===")
                logging.info(f"Real time: {real_time:.2f}s")
                logging.info(f"Simulation speed: {simulation_speed_global}x")
                logging.info(f"Simulation time: {simulation_time:.2f}s")
                logging.info(f"=========================")

                incident.response_time_sec = simulation_time

                vehicle_db = session.get(DBVehicle, self.db_id)
                if vehicle_db:
                    vehicle_db.total_calls += 1
                    vehicle_db.total_response_time += simulation_time
                    vehicle_db.avg_response_time = vehicle_db.total_response_time / vehicle_db.total_calls
                    self.total_calls = vehicle_db.total_calls
                    self.avg_response_time = vehicle_db.avg_response_time

                session.commit()
                if socketio_instance:
                    socketio_instance.emit('incident_resolved', {'incident_id': incident.id})
        except Exception as e:
            logging.error(e)
            session.rollback()
        finally:
            session.close()
        self.assigned_incident_id = None

    def update_db_status(self, status):
        if self.is_temp:
            return
        session = Session()
        try:
            vehicle = session.get(DBVehicle, self.db_id)
            if vehicle:
                vehicle.status = status
                session.commit()
            if status == 'idle':
                self.process_pending_incidents()
                dispatch_pending_incidents()
        except Exception as e:
            logging.error(f"update_db_status error: {e}")
            session.rollback()
        finally:
            session.close()

    def start_patrol(self, duration_sec=None):
        if self.status not in ('idle', 'moving'):
            return False
        if self.patrol_timer:
            self.patrol_timer.cancel()
        self.status = "patrolling"
        self.update_db_status("patrolling")
        if self.inside_station:
            self.inside_station = False
            if socketio_instance:
                lat, lon = self.get_current_coords()
                socketio_instance.emit('vehicle_update', {
                    'id': self.id, 'lat': lat, 'lon': lon,
                    'type': self.type, 'status': self.status, 'speed': 0
                })
        if duration_sec and duration_sec > 0:
            self.patrol_timer = threading.Timer(duration_sec, self.return_to_station)
            self.patrol_timer.daemon = True
            self.patrol_timer.start()
        self.assign_random_route()
        self.save_state()
        return True

    def return_to_station(self):
        if self.patrol_timer:
            self.patrol_timer.cancel()
            self.patrol_timer = None
        if self.service_timer:
            self.service_timer.cancel()
            self.service_timer = None
        if self.status in ('returning', 'responding', 'servicing'):
            return
        if self.inside_station:
            return
        try:
            path = nx.shortest_path(self.graph, self.current_node, self.station_node, weight='length')
            if len(path) > 1:
                self.route = path[1:]
                self.target_node = self.station_node
                self.status = "returning"
                self.update_db_status("returning")
                self.segment = None
                self._init_first_segment()
                self.save_state()
                logging.info(f"Vehicle {self.id} returning to station")
            else:
                self.inside_station = True
                self.status = "idle"
                self.update_db_status("idle")
                self.save_state()
                if socketio_instance:
                    socketio_instance.emit('vehicle_hide', {'id': self.id})
                self.process_pending_incidents()
                dispatch_pending_incidents()
        except Exception as e:
            logging.error(f"return_to_station error: {e}")
            self.inside_station = True
            self.status = "idle"
            self.update_db_status("idle")
            self.save_state()
            if socketio_instance:
                socketio_instance.emit('vehicle_hide', {'id': self.id})


def get_best_vehicle_for_incident(lat, lon, typ, graph, config):
    incident_node = ox.nearest_nodes(graph, lon, lat)
    best_vehicle = None
    best_total_time = float('inf')
    now = time.time()

    # 🔥 ДОБАВЬТЕ ЛОГИРОВАНИЕ:
    logging.info(f"=== GET BEST VEHICLE ===")
    logging.info(f"Incident at: ({lat:.5f}, {lon:.5f}), type: {typ}")
    logging.info(f"Incident node: {incident_node}")

    with vehicles_lock:
        for vid, v in vehicles_dict.items():
            if v.type != typ:
                continue

            start_node = None
            wait_time = 0.0

            if v.status == 'idle':
                start_node = v.station_node if v.inside_station else v.current_node
                wait_time = 0.0
            elif v.status in ('moving', 'patrolling'):
                start_node = v.current_node
                wait_time = 0.0
            elif v.status == 'responding':
                try:
                    remaining_dist = nx.shortest_path_length(v.graph, v.current_node, v.target_node, weight='length')
                    wait_time = remaining_dist / (config['speed_kmh'] / 3.6)
                    start_node = v.target_node
                except:
                    continue
            elif v.status == 'servicing':
                remaining_service = max(0, v.service_time_sec - (
                            now - v.service_start_time)) if v.service_start_time else 0
                wait_time = remaining_service
                start_node = v.current_node
            else:
                continue

            try:
                dist = nx.shortest_path_length(graph, start_node, incident_node, weight='length')
                travel = dist / (config['speed_kmh'] / 3.6)
                total = wait_time + travel
            except:
                continue

            if total < best_total_time:
                best_total_time = total
                best_vehicle = vid

                # 🔥 ЛОГ ДЛЯ КАЖДОЙ МАШИНЫ:
                logging.info(
                    f"Vehicle {vid}: status={v.status}, wait={wait_time:.1f}s, travel={travel:.1f}s, total={total:.1f}s")

                if total < best_total_time:
                    best_total_time = total
                    best_vehicle = vid

        logging.info(f"Best vehicle: {best_vehicle} with total time: {best_total_time:.1f}s")
        logging.info(f"=========================")

        return best_vehicle, incident_node


def add_to_pending_incident(lat, lon, typ, incident_id, target_node=None):
    with pending_lock:
        pending_incidents.append({'id': incident_id, 'lat': lat, 'lon': lon, 'type': typ, 'node': target_node})
    logging.info(f"Incident {incident_id} queued globally")


def dispatch_pending_incidents():
    if not vehicles_dict:
        return
    graph = None
    with vehicles_lock:
        if vehicles_dict:
            graph = next(iter(vehicles_dict.values())).graph
    if not graph:
        return
    with pending_lock:
        if not pending_incidents:
            return
        incidents_to_process = list(pending_incidents)
        pending_incidents.clear()
    speed_kmh = 50
    for inc in incidents_to_process:
        vehicle_id, incident_node = get_best_vehicle_for_incident(
            inc['lat'], inc['lon'], inc['type'],
            graph, {'speed_kmh': speed_kmh}
        )
        if vehicle_id:
            with vehicles_lock:
                v = vehicles_dict.get(vehicle_id)
            if v:
                success = assign_incident_to_vehicle(vehicle_id, inc['lat'], inc['lon'], inc['id'], inc['type'], graph,
                                                     socketio_instance, inc.get('node'))
                if success:
                    session = Session()
                    try:
                        incident_db = session.get(Incident, inc['id'])
                        if incident_db:
                            incident_db.assigned_vehicle_id = vehicle_id
                            session.commit()
                    except:
                        session.rollback()
                    finally:
                        session.close()
                    continue
        with pending_lock:
            pending_incidents.append(inc)


def assign_incident_to_vehicle(vehicle_id, lat, lon, incident_id, typ, graph, socketio, target_node=None):
    with vehicles_lock:
        v = vehicles_dict.get(vehicle_id)
    if not v:
        return False
    node = target_node or ox.nearest_nodes(graph, lon, lat)
    return v.assign_incident(node, incident_id, typ, force=False)


def add_vehicle(vid, start_node, graph, speed_kmh, db_id, station_node=None, vehicle_type=None, service_time=2,
                is_temp=False, inside_station=True, saved_status=None):
    with vehicles_lock:
        vehicles_dict[vid] = Vehicle(vid, start_node, graph, speed_kmh, is_temp, db_id,
                                     station_node, vehicle_type, service_time, inside_station, saved_status)


def stop_vehicle_patrol(vid):
    with vehicles_lock:
        v = vehicles_dict.get(vid)
    if v and v.patrol_timer:
        v.patrol_timer.cancel()
        v.patrol_timer = None


def delete_vehicle_from_dict(vid):
    with vehicles_lock:
        if vid in vehicles_dict:
            v = vehicles_dict[vid]
            if v.patrol_timer:
                v.patrol_timer.cancel()
            del vehicles_dict[vid]


def update_simulation_speed(speed):
    global simulation_speed_global
    simulation_speed_global = max(1, min(500, speed))
    logging.info(f"Simulation speed updated to {simulation_speed_global}")


def simulation_loop():
    global simulation_running
    last_send = 0
    while simulation_running:
        now = time.time()
        dt = 0.05

        with vehicles_lock:
            vehicles_copy = list(vehicles_dict.values())

        for v in vehicles_copy:
            v.update_position(dt)
            if not v.inside_station and socketio_instance:
                lat, lon = v.get_current_coords()
                socketio_instance.emit('vehicle_update', {
                    'id': v.id, 'lat': lat, 'lon': lon,
                    'type': v.type, 'status': v.status,
                    'speed': round(v.current_speed_mps * 3.6, 1)
                })

        if now - last_send > 2 and socketio_instance:
            active = []
            with vehicles_lock:
                for v in vehicles_dict.values():
                    if not v.inside_station:
                        lat, lon = v.get_current_coords()
                        active.append({
                            'id': v.id, 'type': v.type, 'status': v.status,
                            'lat': lat, 'lon': lon,
                            'total_calls': v.total_calls,
                            'avg_response_time': v.avg_response_time,
                            'speed': round(v.current_speed_mps * 3.6, 1)
                        })
            socketio_instance.emit('vehicles_list', active)
            last_send = now

        time.sleep(dt)


def start_simulation(socketio, data):
    global socketio_instance, simulation_speed_global, simulation_thread, simulation_running
    socketio_instance = socketio
    simulation_speed_global = data.config.get('simulation_speed', 30)
    graph = data.graph

    with vehicles_lock:
        vehicles_dict.clear()

    session = Session()

    # Сброс зависших статусов и восстановление состояния машин
    db_vehicles = session.query(DBVehicle).filter_by(is_temp=False).all()
    for v in db_vehicles:
        # Проверяем, есть ли реальное ЧП для responding
        if v.status == 'responding':
            incident = session.query(Incident).filter_by(assigned_vehicle_id=v.id, resolved=False).first()
            if not incident:
                v.status = 'idle'
                logging.info(f"Reset stuck vehicle {v.id} from responding to idle")

    # Восстанавливаем статус returning → idle если нет активного возврата
    for v in db_vehicles:
        if v.status == 'returning':
            v.status = 'idle'
            logging.info(f"Reset vehicle {v.id} from returning to idle")

    session.commit()

    # Загрузка машин из БД с сохранённым состоянием
    db_vehicles = session.query(DBVehicle).filter_by(is_temp=False).all()
    for v in db_vehicles:
        station = v.station
        # Используем сохранённую позицию, если есть
        if v.last_lat and v.last_lon and v.status != 'idle':
            start_node = ox.nearest_nodes(graph, v.last_lon, v.last_lat)
            inside = False
        else:
            start_node = v.last_node_id if v.last_node_id is not None else station.node_id
            inside = (v.last_node_id is None)

        vehicle_obj = Vehicle(
            v.id, start_node, graph, data.config['speed_kmh'],
            is_temp=False, db_id=v.id, station_node=station.node_id,
            vehicle_type=v.type, service_time=v.service_time or data.config.get('service_time_sec', 2),
            inside_station=inside, saved_status=v.status
        )
        vehicle_obj.simulation_speed = simulation_speed_global

        # Восстанавливаем зону патруля из БД
        if v.patrol_lat and v.patrol_lon:
            vehicle_obj.set_patrol_zone(v.patrol_lat, v.patrol_lon, v.patrol_radius or 500)

        # Восстанавливаем активные задания
        if v.status == 'responding':
            incident = session.query(Incident).filter_by(assigned_vehicle_id=v.id, resolved=False).first()
            if incident:
                target_node = ox.nearest_nodes(graph, incident.lon, incident.lat)
                vehicle_obj.assign_incident(target_node, incident.id, incident.type, force=True)
                vehicle_obj.status = 'responding'
        elif v.status == 'servicing':
            incident = session.query(Incident).filter_by(assigned_vehicle_id=v.id, resolved=False).first()
            if incident:
                vehicle_obj.current_node = ox.nearest_nodes(graph, incident.lon, incident.lat)
                vehicle_obj.status = 'servicing'
                vehicle_obj.assigned_incident_id = incident.id
                elapsed = time.time() - incident.created_at.timestamp()
                remaining = max(1, vehicle_obj.service_time_sec - elapsed)
                vehicle_obj.service_start_time = time.time() - (vehicle_obj.service_time_sec - remaining)
                vehicle_obj.service_timer = threading.Timer(remaining, vehicle_obj.finish_servicing)
                vehicle_obj.service_timer.daemon = True
                vehicle_obj.service_timer.start()
        elif v.status == 'patrolling':
            vehicle_obj.start_patrol()
        # idle машины НЕ запускаем автоматически - они ждут на станции

        with vehicles_lock:
            vehicles_dict[v.id] = vehicle_obj

    session.close()
    logging.info(f"Loaded {len(vehicles_dict)} vehicles into simulation")

    # Запускаем цикл симуляции если ещё не запущен
    if simulation_thread is None or not simulation_thread.is_alive():
        simulation_running = True
        simulation_thread = threading.Thread(target=simulation_loop, daemon=True)
        simulation_thread.start()


def stop_simulation():
    global simulation_running
    simulation_running = False
    # Сохраняем состояние всех машин перед остановкой
    with vehicles_lock:
        for v in vehicles_dict.values():
            v.save_state()
    logging.info("Simulation stopped")


import atexit

atexit.register(stop_simulation)