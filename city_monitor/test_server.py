"""Максимально простой сервер для проверки"""
from flask import Flask, jsonify, request
from flask_cors import CORS
import time
import sqlite3
from contextlib import contextmanager

app = Flask(__name__)
CORS(app)


# Простое соединение с БД
@contextmanager
def get_db():
    conn = sqlite3.connect('dispatcher.db')
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@app.route('/')
def index():
    return "OK", 200


@app.route('/api/stations')
def stations():
    with get_db() as conn:
        cursor = conn.execute('SELECT id, name, type, lat, lon FROM stations')
        stations = [dict(row) for row in cursor.fetchall()]
    return jsonify(stations)


@app.route('/api/admin/incidents')
def incidents():
    with get_db() as conn:
        cursor = conn.execute('SELECT id, type, lat, lon FROM incidents WHERE resolved = 0')
        incidents = [dict(row) for row in cursor.fetchall()]
    return jsonify(incidents)


@app.route('/api/incidents', methods=['POST'])
def create_incident():
    data = request.json
    lat = data.get('lat')
    lon = data.get('lon')
    typ = data.get('type', 'fire')

    with get_db() as conn:
        cursor = conn.execute(
            'INSERT INTO incidents (type, lat, lon, resolved, created_at) VALUES (?, ?, ?, 0, datetime("now"))',
            (typ, lat, lon)
        )
        incident_id = cursor.lastrowid

    return jsonify({"incident_id": incident_id, "vehicle_id": None}), 202


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)