"""Запуск через waitress (без 2-секундных задержек)"""
from waitress import serve
from app import app  # или ваш основной app

if __name__ == "__main__":
    print("=" * 60)
    print("🚀 ЗАПУСК ЧЕРЕЗ WAITRESS (оптимизировано для Windows)")
    print("=" * 60)

    serve(
        app,
        host='0.0.0.0',
        port=5000,
        threads=8,  # Количество потоков
        connection_limit=1000,
        channel_timeout=5,  # Таймаут 5 секунд вместо 2
        cleanup_interval=1,
        ident='DispatcherSystem'
    )