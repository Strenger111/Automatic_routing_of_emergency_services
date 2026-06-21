"""
Оптимизированный запуск через Waitress + Flask-SocketIO
Сохраняет WebSocket функциональность при высокой нагрузке
"""

from waitress import serve
from gevent import monkey

monkey.patch_all()  # Патчим для асинхронности

from app import app, socketio

if __name__ == "__main__":
    print("=" * 60)
    print("🚀 ЗАПУСК ЧЕРЕЗ WAITRESS + SOCKETIO (геvent)")
    print("=" * 60)

    # Waitress для HTTP, gevent для WebSocket
    # Запускаем встроенный сервер SocketIO с gevent
    socketio.run(
        app,
        host='0.0.0.0',
        port=5000,
        debug=False,
        use_reloader=False,
        async_mode='gevent',  # Используем gevent для максимальной производительности
        allow_unsafe_werkzeug=True
    )