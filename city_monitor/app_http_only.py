"""HTTP-only версия для нагрузочного тестирования"""
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
from models import DataModel
from database import Session, Station as DBStation, Vehicle as DBVehicle, Incident as DBIncident
from routing import get_nearest_road_point
from simulation import get_best_vehicle_for_incident, add_to_pending_incident, dispatch_pending_incidents, vehicles_lock
import logging
from sqlalchemy import func
from functools import wraps
import time
import hashlib

app = Flask(__name__)
CORS(app)

# Отключаем логгирование для скорости
logging.basicConfig(level=logging.WARNING)

data = DataModel()

# Простой кэш
_api_cache = {}
_api_cache_time = {}


def cache_api(ttl_seconds=30):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            cache_key = f"{func.__name__}_{args}_{sorted(kwargs.items())}"
            cache_key = hashlib.md5(cache_key.encode()).hexdigest()
            now = time.time()
            if cache_key in _api_cache:
                if now - _api_cache_time.get(cache_key, 0) < ttl_seconds:
                    return jsonify(_api_cache[cache_key])
            result = func(*args, **kwargs)
            if hasattr(result, 'status_code') and result.status_code < 400:
                if hasattr(result, 'get_json'):
                    _api_cache[cache_key] = result.get_json()
                _api_cache_time[cache_key] = now
            return result

        return wrapper

    return decorator


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


@app.route('/')
def index():
    return "OK", 200


@app.route('/api/stations')
@cache_api(ttl_seconds=30)
def stations():
    with db_session() as session:
        stations = session.query(DBStation).all()
        return jsonify([{
            "id": s.id, "name": s.name, "type": s.type,
            "lat": s.lat, "lon": s.lon
        } for s in stations])


@app.route('/api/admin/vehicles')
def admin_vehicles():
    with db_session() as session:
        vehicles = session.query(DBVehicle).all()
        return jsonify([{
            "id": v.id, "name": v.name, "type": v.type, "status": v.status
        } for v in vehicles])


@app.route('/api/admin/incidents')
def admin_incidents():
    with db_session() as session:
        incidents = session.query(DBIncident).filter_by(resolved=False).all()
        return jsonify([{
            "id": i.id, "type": i.type, "lat": i.lat, "lon": i.lon
        } for i in incidents])


@app.route('/api/admin/stats/incidents')
def incident_stats():
    with db_session() as session:
        total_by_type = session.query(DBIncident.type, func.count(DBIncident.id)).filter_by(resolved=True).group_by(
            DBIncident.type).all()
        stats = {
            "total_resolved": session.query(DBIncident).filter_by(resolved=True).count(),
            "by_type": {t: cnt for t, cnt in total_by_type}
        }
    return jsonify(stats)


@app.route('/api/analytics/avg_response')
def analytics_avg_response():
    return jsonify({"fire": 0, "ambulance": 0, "police": 0})


@app.route('/api/analytics/hot_zones')
def analytics_hot_zones():
    return jsonify([])


@app.route('/api/incidents', methods=['POST'])
def create_incident():
    lat = request.json.get('lat')
    lon = request.json.get('lon')
    typ = request.json.get('type', 'fire')

    target_node = get_nearest_road_point(data.graph, lat, lon)
    if target_node is None:
        return jsonify({"error": "no road near point"}), 400

    with db_session() as session:
        incident = DBIncident(type=typ, lat=lat, lon=lon, assigned_vehicle_id=None)
        session.add(incident)
        session.flush()
        incident_id = incident.id

    return jsonify({"incident_id": incident_id, "vehicle_id": None}), 202


@app.route('/api/admin/incidents/<int:iid>/resolve', methods=['POST'])
def resolve_incident(iid):
    with db_session() as session:
        incident = session.get(DBIncident, iid)
        if incident:
            incident.resolved = True
    return jsonify({"success": True})


if __name__ == "__main__":
    # Критически важно: debug=False и threaded=True
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)