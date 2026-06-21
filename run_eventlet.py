"""Запуск с eventlet для максимальной производительности"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'city_monitor'))
# Устанавливаем рабочую директорию
project_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(project_dir)

# Патчим все для асинхронности
from eventlet import monkey_patch

monkey_patch(all=True)

# Теперь импортируем приложение
from city_monitor.app import app, socketio

if __name__ == "__main__":
    print("=" * 60)
    print("🚀 ЗАПУСК С EVENTLET (АСИНХРОННЫЙ РЕЖИМ)")
    print("=" * 60)
    print("🌐 Откройте в браузере: http://127.0.0.1:5000/")
    socketio.run(
        app,
        host='0.0.0.0',
        port=5000,
        debug=False,  # Критично: False
        use_reloader=False,  # Критично: False
          # Используем eventlet, не werkzeug
    )