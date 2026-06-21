from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import osmnx as ox
from models import DataModel
from routing import compute_zones, count_houses_in_zones, compute_travel_time, get_nearest_road_point
from simulation import start_simulation, vehicles_dict, assign_incident_to_vehicle, get_best_vehicle_for_incident, \
    add_to_pending_incident, add_vehicle, delete_vehicle_from_dict, stop_vehicle_patrol, dispatch_pending_incidents, \
    vehicles_lock, update_simulation_speed
from database import Session, Station as DBStation, Vehicle as DBVehicle, Incident as DBIncident
import os
from celery import Celery
import logging
from sqlalchemy import func
import random
import time
import threading
from contextlib import contextmanager
from shapely.geometry import Point, Polygon
from shapely import wkt

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
app.config['CELERY_BROKER_URL'] = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
app.config['CELERY_RESULT_BACKEND'] = app.config['CELERY_BROKER_URL']
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")


@contextmanager
def db_session():
    session = Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def make_celery(app):
    celery = Celery(app.import_name,
                    broker=app.config['CELERY_BROKER_URL'],
                    backend=app.config['CELERY_RESULT_BACKEND'])
    celery.conf.update(app.config)
    return celery


celery = make_celery(app)
CORS(app)
data = DataModel()

# --- Глобальные переменные для автоспавна ---
auto_incident_enabled = False
auto_incident_mode = 'light'
auto_incident_thread = None


def get_random_graph_node():
    import random
    nodes = list(data.graph.nodes)
    node = random.choice(nodes)
    lat = data.graph.nodes[node]['y']
    lon = data.graph.nodes[node]['x']
    return lat, lon


def create_incident_internal(lat, lon, typ):
    from routing import get_nearest_road_point
    target_node = get_nearest_road_point(data.graph, lat, lon)
    if target_node is None:
        return None
    with db_session() as session:
        with vehicles_lock:
            vehicle_id, _ = get_best_vehicle_for_incident(lat, lon, typ, data.graph, data.config)
            if vehicle_id is not None:
                incident = DBIncident(type=typ, lat=lat, lon=lon, assigned_vehicle_id=vehicle_id)
                session.add(incident)
                session.flush()
                incident_id = incident.id
                success = assign_incident_to_vehicle(vehicle_id, lat, lon, incident_id, typ, data.graph, socketio,
                                                     target_node)
                if success:
                    vehicle_db = session.get(DBVehicle, vehicle_id)
                    if vehicle_db:
                        vehicle_db.status = 'responding'
                    socketio.emit('incident_created', {
                        'id': incident_id, 'lat': lat, 'lon': lon,
                        'type': typ, 'vehicle_id': vehicle_id
                    })
                    return incident_id
                else:
                    return None
            else:
                incident = DBIncident(type=typ, lat=lat, lon=lon, assigned_vehicle_id=None)
                session.add(incident)
                session.flush()
                incident_id = incident.id
                add_to_pending_incident(lat, lon, typ, incident_id, target_node)
                socketio.emit('incident_created', {
                    'id': incident_id, 'lat': lat, 'lon': lon,
                    'type': typ, 'vehicle_id': None
                })
                dispatch_pending_incidents()
                return incident_id


def auto_create_incident():
    incident_type = random.choice(['fire', 'ambulance', 'police'])
    lat, lon = get_random_graph_node()
    create_incident_internal(lat, lon, incident_type)
    logging.info(f"Auto incident created: {incident_type} at ({lat:.5f}, {lon:.5f})")


def auto_incident_worker():
    global auto_incident_enabled, auto_incident_mode
    while auto_incident_enabled:
        if auto_incident_mode == 'light':
            delay = random.uniform(60, 120)
        elif auto_incident_mode == 'medium':
            delay = random.uniform(30, 60)
        else:
            delay = random.uniform(5, 15)
        time.sleep(delay)
        if not auto_incident_enabled:
            break
        auto_create_incident()


# --- Flask routes ---
@app.route('/')
def index():
    return render_template('index.html')


@socketio.on('connect')
def on_connect(auth=None):
    print("Client connected")
    start_simulation(socketio, data)
    with vehicles_lock:
        active = []
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
    socketio.emit('vehicles_list', active)


@app.route('/api/houses')
def houses():
    return data.houses_gdf.to_json()


@app.route('/api/stations')
def stations():
    with db_session() as session:
        db_stations = session.query(DBStation).all()
        features = [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [st.lon, st.lat]},
            "properties": {"id": st.id, "name": st.name, "type": st.type}
        } for st in db_stations]
    return jsonify({"type": "FeatureCollection", "features": features})


@app.route('/api/roads')
def roads():
    edges = ox.graph_to_gdfs(data.graph, nodes=False)[['geometry']]
    return edges.to_json()


@app.route('/api/station/<int:sid>/zones')
def zones(sid):
    with db_session() as session:
        station = session.get(DBStation, sid)
        if not station:
            return {"error": "station not found"}, 404
        node_id = station.node_id  # Сохраняем значение до закрытия сессии

    # Используем node_id после закрытия сессии
    zones_dict = compute_zones(
        node_id, data.graph,
        data.config['speed_kmh'], data.config['time_limits_sec']
    )
    return jsonify(zones_dict)


@app.route('/api/station/<int:sid>/house_counts')
def station_house_counts(sid):
    with db_session() as session:
        station = session.get(DBStation, sid)
        if not station:
            return {"error": "station not found"}, 404
        node_id = station.node_id

    zones_dict = compute_zones(
        node_id, data.graph,
        data.config['speed_kmh'], data.config['time_limits_sec']
    )
    counts = count_houses_in_zones(zones_dict, data.houses_gdf)
    return jsonify(counts)


@app.route('/api/vehicle_zones', methods=['POST'])
def vehicle_zones():
    lat = request.json.get('lat');
    lon = request.json.get('lon')
    if lat is None or lon is None:
        return {"error": "missing lat/lon"}, 400
    node = data.get_nearest_node(lat, lon)
    zones_dict = compute_zones(node, data.graph,
                               data.config['speed_kmh'], data.config['time_limits_sec'])
    return jsonify(zones_dict)


@app.route('/api/house/<int:hid>/nearest')
def nearest(hid):
    if hid < 0 or hid >= len(data.houses_gdf):
        return {"error": "invalid house id"}, 404
    house_node = data.houses_gdf.iloc[hid]['node_id']
    with db_session() as session:
        stations = session.query(DBStation).all()
        res = []
        for st in stations:
            t = compute_travel_time(data.graph, st.node_id, house_node, data.config['speed_kmh'])
            res.append({"station": st.name, "eta": t})
    return jsonify(sorted(res, key=lambda x: x['eta'])[:3])


@app.route('/api/incidents', methods=['POST'])
def create_incident():
    lat = request.json.get('lat')
    lon = request.json.get('lon')
    typ = request.json.get('type', 'fire')  # тип теперь приходит из формы
    logging.info(f"Creating incident at {lat},{lon} of type {typ}")

    for attempt in range(3):
        try:
            target_node = get_nearest_road_point(data.graph, lat, lon)
            if target_node is None:
                return jsonify({"error": "no road near point"}), 400

            with db_session() as session:
                with vehicles_lock:
                    vehicle_id, _ = get_best_vehicle_for_incident(lat, lon, typ, data.graph, data.config)
                    if vehicle_id is not None:
                        incident = DBIncident(type=typ, lat=lat, lon=lon, assigned_vehicle_id=vehicle_id)
                        session.add(incident)
                        session.flush()
                        incident_id = incident.id
                        success = assign_incident_to_vehicle(vehicle_id, lat, lon, incident_id, typ, data.graph,
                                                             socketio, target_node)
                        if success:
                            vehicle_db = session.get(DBVehicle, vehicle_id)
                            if vehicle_db:
                                vehicle_db.status = 'responding'
                            socketio.emit('incident_created', {
                                'id': incident_id, 'lat': lat, 'lon': lon,
                                'type': typ, 'vehicle_id': vehicle_id
                            })
                            return jsonify({"incident_id": incident_id, "vehicle_id": vehicle_id})
                        else:
                            return jsonify({"error": "route not found"}), 500
                    else:
                        incident = DBIncident(type=typ, lat=lat, lon=lon, assigned_vehicle_id=None)
                        session.add(incident)
                        session.flush()
                        incident_id = incident.id
                        add_to_pending_incident(lat, lon, typ, incident_id, target_node)
                        socketio.emit('incident_created', {
                            'id': incident_id, 'lat': lat, 'lon': lon,
                            'type': typ, 'vehicle_id': None
                        })
                        dispatch_pending_incidents()
                        return jsonify({"incident_id": incident_id, "vehicle_id": None, "message": "queued"}), 202
        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed: {e}")
            if attempt == 2:
                return jsonify({"error": "Database error, please retry"}), 500



@app.route('/api/stations', methods=['POST'])
def create_station():
    data_json = request.get_json()
    name = data_json.get('name');
    typ = data_json.get('type')
    lat = data_json.get('lat');
    lon = data_json.get('lon')
    max_vehicles = data_json.get('max_vehicles', 3)
    if not (name and typ and lat and lon):
        return {"error": "missing fields"}, 400
    node_id = data.get_nearest_node(lat, lon)
    with db_session() as session:
        max_id = session.query(DBStation.id).order_by(DBStation.id.desc()).first()
        new_id = (max_id[0] + 1) if max_id else 1
        station = DBStation(id=new_id, name=name, type=typ, lat=lat, lon=lon, node_id=node_id,
                            max_vehicles=max_vehicles)
        session.add(station)
        for i in range(min(2, max_vehicles)):
            vehicle = DBVehicle(
                name=f"{typ}_{new_id}_{i + 1}", type=typ, status="idle",
                station_id=new_id, is_temp=False
            )
            session.add(vehicle)
    data.stations.append({"id": new_id, "name": name, "type": typ,
                          "lat": lat, "lon": lon, "node_id": node_id, "max_vehicles": max_vehicles})
    start_simulation(socketio, data)
    return jsonify({"id": new_id})


# ===================== НОВЫЙ МАРШРУТ ДЛЯ ЗОНЫ ПАТРУЛЯ =====================
@app.route('/api/vehicle/<int:vid>/patrol_zone', methods=['POST'])
def set_vehicle_patrol_zone(vid):
    """Устанавливает зону патрулирования для машины (полигон)"""
    data_req = request.json
    points = data_req.get('points')  # список {lat, lon}
    radius = data_req.get('radius', 500)

    if not points or len(points) < 3:
        return jsonify({"error": "Need at least 3 points for polygon"}), 400

    # Создаём полигон из точек (в порядке обхода)
    from shapely.geometry import Polygon as ShapelyPolygon
    coords = [(p['lon'], p['lat']) for p in points]
    polygon = ShapelyPolygon(coords)

    if not polygon.is_valid:
        return jsonify({"error": "Invalid polygon"}), 400

    with vehicles_lock:
        v = vehicles_dict.get(vid)
        if not v:
            return jsonify({"error": "Vehicle not found"}), 404

        # Сохраняем полигон
        v.patrol_zone_polygon = polygon
        # Также сохраняем центр для совместимости со старым кодом
        center = polygon.centroid
        v.patrol_center = (center.y, center.x)
        v.patrol_radius = radius

        # Сохраняем в БД
        with db_session() as session:
            vehicle_db = session.get(DBVehicle, v.db_id)
            if vehicle_db:
                vehicle_db.patrol_lat = center.y
                vehicle_db.patrol_lon = center.x
                vehicle_db.patrol_radius = radius
                vehicle_db.patrol_zone_polygon = polygon.wkt

    return jsonify({"success": True, "center": {"lat": center.y, "lon": center.x}, "area": polygon.area})


@app.route('/api/vehicle/<int:vid>/patrol_zone/clear', methods=['POST'])
def clear_vehicle_patrol_zone(vid):
    """Очищает зону патрулирования"""
    with vehicles_lock:
        v = vehicles_dict.get(vid)
        if not v:
            return jsonify({"error": "Vehicle not found"}), 404

        v.patrol_zone_polygon = None
        v.patrol_center = None
        v.patrol_radius = None

        with db_session() as session:
            vehicle_db = session.get(DBVehicle, v.db_id)
            if vehicle_db:
                vehicle_db.patrol_lat = None
                vehicle_db.patrol_lon = None
                vehicle_db.patrol_radius = None
                vehicle_db.patrol_zone_polygon = None

    return jsonify({"success": True})


@app.route('/api/vehicle/<int:vid>/patrol_zone', methods=['GET'])
def get_vehicle_patrol_zone(vid):
    """Получает зону патрулирования машины"""
    with vehicles_lock:
        v = vehicles_dict.get(vid)
        if not v:
            return jsonify({"error": "Vehicle not found"}), 404

        if v.patrol_zone_polygon:
            coords = [[lon, lat] for lat, lon in v.patrol_zone_polygon.exterior.coords]
            return jsonify({
                "type": "Polygon",
                "coordinates": coords,
                "center": {"lat": v.patrol_center[0], "lon": v.patrol_center[1]} if v.patrol_center else None,
                "radius": v.patrol_radius
            })

    return jsonify({"error": "No patrol zone set"}), 404


# ===================== АДМИНКА =====================
@app.route('/admin')
def admin():
    return render_template('admin.html')


@app.route('/api/admin/stations')
def admin_stations():
    with db_session() as session:
        stations = session.query(DBStation).all()
        res = [{"id": s.id, "name": s.name, "type": s.type, "lat": s.lat, "lon": s.lon, "max_vehicles": s.max_vehicles}
               for s in stations]
    return jsonify(res)


@app.route('/api/admin/vehicles')
def admin_vehicles():
    with db_session() as session:
        vehicles = session.query(DBVehicle).all()
        res = []
        for v in vehicles:
            res.append({
                "id": v.id, "name": v.name, "type": v.type, "status": v.status,
                "station_name": v.station.name if v.station else None,
                "total_calls": v.total_calls, "avg_response_time": v.avg_response_time,
                "is_temp": v.is_temp, "service_time": v.service_time,
                "has_patrol_zone": bool(v.patrol_zone_polygon)
            })
    return jsonify(res)


@app.route('/api/admin/vehicles', methods=['POST'])
def admin_add_vehicle():
    data_req = request.json
    station_id = data_req.get('station_id')
    veh_type = data_req.get('type')
    name = data_req.get('name', f"{veh_type}_auto")
    service_time = data_req.get('service_time', 2)
    if not station_id or not veh_type:
        return {"error": "station_id and type required"}, 400

    with db_session() as session:
        station = session.get(DBStation, station_id)
        if not station:
            return {"error": "station not found"}, 404
        current_count = session.query(DBVehicle).filter_by(station_id=station_id, is_temp=False).count()
        if current_count >= station.max_vehicles:
            return {"error": f"Maximum {station.max_vehicles} vehicles for this station"}, 400
        station_node_id = station.node_id
        new_vehicle = DBVehicle(
            name=name, type=veh_type, status='idle',
            station_id=station_id, is_temp=False, service_time=service_time
        )
        session.add(new_vehicle)
        session.flush()
        vehicle_id = new_vehicle.id

    add_vehicle(vehicle_id, station_node_id, data.graph,
                data.config['speed_kmh'], vehicle_id,
                station_node=station_node_id, vehicle_type=veh_type,
                service_time=service_time, is_temp=False, inside_station=True)
    logging.info(f"Vehicle {vehicle_id} added to vehicles_dict")
    start_simulation(socketio, data)
    return jsonify({"id": vehicle_id})


@app.route('/api/admin/vehicles/<int:vid>', methods=['DELETE'])
def delete_vehicle(vid):
    stop_vehicle_patrol(vid)
    with vehicles_lock:
        if vid in vehicles_dict:
            del vehicles_dict[vid]
    with db_session() as session:
        session.query(DBIncident).filter_by(assigned_vehicle_id=vid).update({DBIncident.assigned_vehicle_id: None})
        session.query(DBVehicle).filter_by(id=vid).delete()
    start_simulation(socketio, data)
    return jsonify({"success": True})


@app.route('/api/admin/vehicles/<int:vid>', methods=['PUT'])
def edit_vehicle(vid):
    data_req = request.json
    name = data_req.get('name')
    service_time = data_req.get('service_time')
    with db_session() as session:
        vehicle = session.get(DBVehicle, vid)
        if not vehicle:
            return {"error": "vehicle not found"}, 404
        if name:
            vehicle.name = name
        if service_time is not None:
            vehicle.service_time = service_time
            with vehicles_lock:
                if vid in vehicles_dict:
                    vehicles_dict[vid].service_time_sec = service_time
    return jsonify({"success": True})


@app.route('/api/admin/stations/<int:sid>', methods=['DELETE'])
def delete_station(sid):
    with db_session() as session:
        station = session.get(DBStation, sid)
        if station:
            with vehicles_lock:
                for vid in list(vehicles_dict.keys()):
                    v = vehicles_dict.get(vid)
                    if v and hasattr(v, 'station_node') and v.station_node == station.node_id:
                        stop_vehicle_patrol(vid)
                        del vehicles_dict[vid]
            for vehicle in session.query(DBVehicle).filter_by(station_id=sid):
                session.query(DBIncident).filter_by(assigned_vehicle_id=vehicle.id).update(
                    {DBIncident.assigned_vehicle_id: None})
            session.query(DBVehicle).filter_by(station_id=sid).delete()
            session.delete(station)
    start_simulation(socketio, data)
    return jsonify({"success": True})


@app.route('/api/admin/stations/<int:sid>', methods=['PUT'])
def edit_station(sid):
    data_req = request.json
    with db_session() as session:
        station = session.get(DBStation, sid)
        if not station:
            return {"error": "station not found"}, 404
        if 'name' in data_req:
            station.name = data_req['name']
        if 'type' in data_req:
            station.type = data_req['type']
        if 'max_vehicles' in data_req:
            station.max_vehicles = data_req['max_vehicles']
        if 'lat' in data_req and 'lon' in data_req:
            station.lat = data_req['lat']
            station.lon = data_req['lon']
            station.node_id = data.get_nearest_node(station.lat, station.lon)
        data.stations = []
        db_stations = session.query(DBStation).all()
        for st in db_stations:
            data.stations.append({
                "id": st.id, "name": st.name, "type": st.type,
                "lat": st.lat, "lon": st.lon, "node_id": st.node_id,
                "max_vehicles": st.max_vehicles
            })
    start_simulation(socketio, data)
    return jsonify({"success": True})


@app.route('/api/admin/incidents/<int:iid>/resolve', methods=['POST'])
def resolve_incident_admin(iid):
    with db_session() as session:
        incident = session.get(DBIncident, iid)
        if incident and not incident.resolved:
            incident.resolved = True
            incident.response_time_sec = 0
            if incident.assigned_vehicle_id:
                vehicle = session.get(DBVehicle, incident.assigned_vehicle_id)
                if vehicle and not vehicle.is_temp:
                    vehicle.status = 'idle'
            socketio.emit('incident_resolved', {'incident_id': iid})
            dispatch_pending_incidents()
    return jsonify({"success": True})


@app.route('/api/admin/incidents/<int:iid>', methods=['DELETE'])
def delete_incident_admin(iid):
    with db_session() as session:
        incident = session.get(DBIncident, iid)
        if incident:
            if incident.assigned_vehicle_id and not incident.resolved:
                vehicle = session.get(DBVehicle, incident.assigned_vehicle_id)
                if vehicle and vehicle.status == 'responding':
                    vehicle.status = 'idle'
            session.delete(incident)
            socketio.emit('incident_resolved', {'incident_id': iid})
            dispatch_pending_incidents()
    return jsonify({"success": True})


@app.route('/api/admin/incidents/clear_resolved', methods=['DELETE'])
def clear_resolved_incidents():
    with db_session() as session:
        deleted = session.query(DBIncident).filter_by(resolved=True).delete()
    return jsonify({"deleted": deleted})


@app.route('/api/admin/incidents')
def admin_incidents():
    with db_session() as session:
        incidents = session.query(DBIncident).filter_by(resolved=False).all()
        res = [{"id": i.id, "type": i.type, "lat": i.lat, "lon": i.lon,
                "created_at": i.created_at.isoformat(), "assigned_vehicle_id": i.assigned_vehicle_id}
               for i in incidents]
    return jsonify(res)


# ===================== СТАТИСТИКА =====================
@app.route('/api/admin/stats/incidents')
def incident_stats():
    with db_session() as session:
        total_by_type = session.query(DBIncident.type, func.count(DBIncident.id)).filter_by(resolved=True).group_by(
            DBIncident.type).all()
        avg_response_by_type = session.query(DBIncident.type, func.avg(DBIncident.response_time_sec)).filter_by(
            resolved=True).group_by(DBIncident.type).all()
        stats = {
            "total_resolved": session.query(DBIncident).filter_by(resolved=True).count(),
            "by_type": {t: cnt for t, cnt in total_by_type},
            "avg_response_sec_by_type": {t: float(avg or 0) for t, avg in avg_response_by_type}
        }
    return jsonify(stats)


@app.route('/api/admin/stats/vehicles')
def vehicle_stats():
    with db_session() as session:
        vehicles = session.query(DBVehicle).filter(DBVehicle.total_calls > 0).all()
        res = []
        for v in vehicles:
            res.append({
                "id": v.id,
                "name": v.name,
                "type": v.type,
                "total_calls": v.total_calls,
                "avg_response_sec": v.avg_response_time or 0,
                "station_name": v.station.name if v.station else None
            })
    return jsonify(res)


# ===================== ТЕПЛОВАЯ КАРТА =====================
@app.route('/api/heatmap/<string:incident_type>')
def heatmap_data(incident_type):
    with db_session() as session:
        incidents = session.query(DBIncident).filter_by(resolved=True, type=incident_type).all()
        points = [[inc.lat, inc.lon, 1] for inc in incidents]
    return jsonify(points)


# Патрулирование
@app.route('/api/admin/vehicles/<int:vid>/patrol', methods=['POST'])
def vehicle_patrol(vid):
    data_req = request.get_json() or {}
    duration = data_req.get('duration_sec')
    logging.info(f"Patrol request for vehicle {vid}")
    with vehicles_lock:
        v = vehicles_dict.get(vid)
        if not v:
            return {"error": "vehicle not found"}, 404
        if v.status not in ('idle', 'moving'):
            return {"error": f"cannot patrol in status {v.status}"}, 400
        v.start_patrol(duration)
    return jsonify({"success": True})


@app.route('/api/admin/vehicles/<int:vid>/return', methods=['POST'])
def vehicle_return(vid):
    with vehicles_lock:
        v = vehicles_dict.get(vid)
        if not v:
            return {"error": "vehicle not found"}, 404
        v.return_to_station()
    return jsonify({"success": True})


# Автоспавн ЧП
@app.route('/api/admin/auto_incidents/start', methods=['POST'])
def start_auto_incidents():
    global auto_incident_enabled, auto_incident_thread
    if auto_incident_enabled:
        return jsonify({"error": "Already running"}), 400
    auto_incident_enabled = True
    auto_incident_thread = threading.Thread(target=auto_incident_worker, daemon=True)
    auto_incident_thread.start()
    return jsonify({"success": True})


@app.route('/api/admin/auto_incidents/stop', methods=['POST'])
def stop_auto_incidents():
    global auto_incident_enabled
    auto_incident_enabled = False
    return jsonify({"success": True})


@app.route('/api/admin/auto_incidents/set_mode/<mode>', methods=['POST'])
def set_auto_incident_mode(mode):
    global auto_incident_mode
    if mode not in ('light', 'medium', 'overload'):
        return jsonify({"error": "Invalid mode"}), 400
    auto_incident_mode = mode
    return jsonify({"success": True, "mode": mode})


@app.route('/api/admin/auto_incidents/status', methods=['GET'])
def auto_incident_status():
    return jsonify({
        "enabled": auto_incident_enabled,
        "mode": auto_incident_mode
    })


@app.route('/api/admin/auto_incidents/create_one', methods=['POST'])
def create_one_random_incident():
    auto_create_incident()
    return jsonify({"success": True})


@socketio.on('disconnect')
def on_disconnect():
    print("Client disconnected")


@app.route('/api/houses/upload', methods=['POST'])
def upload_houses():
    if not request.is_json:
        return {"error": "Expected JSON"}, 400
    geojson_data = request.get_json()
    import json
    geojson_str = json.dumps(geojson_data)
    try:
        count = data.add_houses(geojson_str)
        return jsonify({"success": True, "added": count})
    except Exception as e:
        logging.error(f"Error adding houses: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/houses/added', methods=['DELETE'])
def clear_added_houses():
    data.clear_added_houses()
    return jsonify({"success": True})


@app.route('/api/admin/simulation_speed', methods=['POST'])
def set_simulation_speed():
    speed = request.json.get('speed')
    if speed is None:
        return jsonify({"error": "missing speed"}), 400
    speed_val = max(1, min(500, float(speed)))
    data.update_config('simulation_speed', speed_val)
    update_simulation_speed(speed_val)
    return jsonify({"speed": data.config['simulation_speed']})


@app.route('/api/admin/simulation_speed', methods=['GET'])
def get_simulation_speed():
    return jsonify({"speed": data.config['simulation_speed']})


# ===================== АНАЛИТИКА =====================
from analytics import (
    get_avg_response_time_by_type,
    get_incidents_per_hour,
    get_vehicle_utilization,
    get_station_coverage,
    get_zone_overlap
)


@app.route('/api/analytics/avg_response')
def analytics_avg_response():
    start = request.args.get('start')
    end = request.args.get('end')
    return jsonify(get_avg_response_time_by_type(start, end))


@app.route('/api/analytics/incidents_per_hour')
def analytics_incidents_per_hour():
    hours = int(request.args.get('hours', 24))
    return jsonify(get_incidents_per_hour(hours))


@app.route('/api/analytics/vehicle_utilization/<int:vid>')
def analytics_vehicle_utilization(vid):
    hours = int(request.args.get('hours', 24))
    return jsonify(get_vehicle_utilization(vid, hours))


@app.route('/api/analytics/station_coverage')
def analytics_station_coverage():
    return jsonify(get_station_coverage())


@app.route('/api/analytics/zone_overlap')
def analytics_zone_overlap():
    return jsonify(get_zone_overlap())


@app.route('/api/analytics/hot_zones')
def analytics_hot_zones():
    from analytics import get_hot_zones_for_station
    incident_type = request.args.get('type')
    return jsonify(get_hot_zones_for_station(incident_type))


@app.route('/api/analytics/patrol_recommendations')
def analytics_patrol_recommendations():
    from analytics import get_patrol_recommendations
    return jsonify(get_patrol_recommendations())


@app.route('/api/analytics/economic_analysis')
def analytics_economic_analysis():
    from analytics import get_economic_analysis
    return jsonify(get_economic_analysis())


@app.route('/analytics')
def analytics_page():
    return render_template('analytics.html')


@app.route('/api/admin/vehicles/start_all_patrol', methods=['POST'])
def start_all_patrol():
    started = 0
    with vehicles_lock:
        for vid, v in vehicles_dict.items():
            if v.status == 'idle':
                v.start_patrol()
                started += 1
    return jsonify({"started": started})


@app.route('/api/admin/vehicles/stop_all_patrol', methods=['POST'])
def stop_all_patrol():
    returned = 0
    with vehicles_lock:
        for vid, v in vehicles_dict.items():
            if v.status == 'patrolling' or (v.status == 'moving' and not v.inside_station):
                v.return_to_station()
                returned += 1
    return jsonify({"returned": returned})


# ===================== ЗАГРУЗКА ДОРОГ В ГРАФ =====================

@app.route('/api/roads/upload_graph', methods=['POST'])
def upload_roads_to_graph():
    """
    Загружает дороги из GeoJSON в граф.
    Ожидает файл в формате JSON с GeoJSON данными.
    """
    if not request.is_json:
        return jsonify({"error": "Expected JSON"}), 400

    geojson_data = request.get_json()

    try:
        import json
        geojson_str = json.dumps(geojson_data)

        # Добавляем дороги в граф
        added_count = data.add_roads_from_geojson(geojson_str)

        # Перезапускаем симуляцию с новым графом
        start_simulation(socketio, data)

        return jsonify({
            "success": True,
            "added_edges": added_count,
            "total_nodes": len(data.graph.nodes),
            "total_edges": len(data.graph.edges),
            "added_nodes": len(data.added_graph.nodes) if hasattr(data, 'added_graph') else 0
        })
    except Exception as e:
        logging.error(f"Error uploading roads: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/roads/clear_added', methods=['DELETE'])
def clear_added_roads():
    """Удаляет все добавленные дороги из графа"""
    try:
        data.clear_added_roads()
        start_simulation(socketio, data)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/roads/status', methods=['GET'])
def roads_status():
    """Возвращает информацию о состоянии графа"""
    return jsonify({
        "total_nodes": len(data.graph.nodes),
        "total_edges": len(data.graph.edges),
        "base_nodes": len(data.base_graph.nodes) if hasattr(data, 'base_graph') else 0,
        "added_nodes": len(data.added_graph.nodes) if hasattr(data, 'added_graph') else 0,
        "connection_threshold_meters": CONNECTION_THRESHOLD
    })


if __name__ == "__main__":
    socketio.run(app, port=5000, debug=False, use_reloader=False)