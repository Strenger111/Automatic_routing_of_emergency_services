"""Запуск через waitress с правильными путями"""
import os
import sys

# Переходим в директорию проекта
project_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(project_dir)

# Добавляем в PYTHONPATH
if project_dir not in sys.path:
    sys.path.insert(0, project_dir)

print(f"📁 Рабочая директория: {os.getcwd()}")
print(f"📁 Проверка файлов:")
print(f"   - graph.graphml: {'✅' if os.path.exists('static/data/graph.graphml') else '❌'}")
print(f"   - houses.geojson: {'✅' if os.path.exists('static/data/houses.geojson') else '❌'}")

from waitress import serve
from app import app

if __name__ == "__main__":
    print("=" * 60)
    print("🚀 ЗАПУСК ЧЕРЕЗ WAITRESS")
    print("=" * 60)

    serve(
        app,
        host='0.0.0.0',
        port=5000,
        threads=8,
        connection_limit=1000,
        channel_timeout=5,
        ident='DispatcherSystem'
    )