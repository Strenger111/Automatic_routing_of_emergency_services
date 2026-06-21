"""Сервер с хранением в памяти (без БД) для нагрузочного теста"""
from flask import Flask, jsonify, request
from flask_cors import CORS
import time
from threading import Lock

app = Flask(__name__)
CORS(app)

# Хранилище в памяти
stations = [
    {"id": 1, "name": "Пожарная часть", "type": "fire", "lat": 56.73, "lon": 37.17},
    {"id": 2, "name": "Скорая помощь", "type": "ambulance", "lat": 56.74, "lon": 37.18},
    {"id": 3, "name": "Полиция", "type": "police", "lat": 56.72, "lon": 37.16},
]

incidents = {}
incident_counter = 0
incidents_lock = Lock()

vehicles = [
    {"id": 1, "name": "Пожарная машина 1", "type": "fire", "status": "idle", "station_name": "Пожарная часть"},
    {"id": 2, "name": "Скорая 1", "type": "ambulance", "status": "idle", "station_name": "Скорая помощь"},
    {"id": 3, "name": "Полицейская машина 1", "type": "police", "status": "idle", "station_name": "Полиция"},
]


@app.route('/')
def index():
    return "OK", 200


@app.route('/api/stations')
def get_stations():
    return jsonify(stations)


@app.route('/api/admin/vehicles')
def get_vehicles():
    return jsonify(vehicles)


@app.route('/api/admin/incidents')
def get_incidents():
    with incidents_lock:
        active = [inc for inc in incidents.values() if not inc['resolved']]
    return jsonify(active)


@app.route('/api/admin/stats/incidents')
def stats_incidents():
    with incidents_lock:
        total_resolved = sum(1 for inc in incidents.values() if inc['resolved'])
        by_type = {}
        for inc in incidents.values():
            if inc['resolved']:
                by_type[inc['type']] = by_type.get(inc['type'], 0) + 1
    return jsonify({"total_resolved": total_resolved, "by_type": by_type})


@app.route('/api/admin/stats/vehicles')
def stats_vehicles():
    # Простая статистика
    return jsonify([{"id": v['id'], "name": v['name'], "total_calls": 0, "avg_response_sec": 0} for v in vehicles])


@app.route('/api/analytics/avg_response')
def avg_response():
    return jsonify({"fire": 120.5, "ambulance": 90.3, "police": 150.2})


@app.route('/api/analytics/hot_zones')
def hot_zones():
    return jsonify([])


@app.route('/api/incidents', methods=['POST'])
def create_incident():
    global incident_counter
    data = request.json
    lat = data.get('lat')
    lon = data.get('lon')
    typ = data.get('type', 'fire')

    with incidents_lock:
        incident_counter += 1
        incident = {
            "id": incident_counter,
            "type": typ,
            "lat": lat,
            "lon": lon,
            "resolved": False
        }
        incidents[incident_counter] = incident

    return jsonify({"incident_id": incident_counter, "vehicle_id": None}), 202


@app.route('/api/admin/incidents/<int:iid>/resolve', methods=['POST'])
def resolve_incident(iid):
    with incidents_lock:
        if iid in incidents:
            incidents[iid]['resolved'] = True
    return jsonify({"success": True})


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)