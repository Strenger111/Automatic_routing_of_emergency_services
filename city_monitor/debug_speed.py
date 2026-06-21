import time
import requests
import logging

logging.basicConfig(level=logging.INFO)


def test_endpoint(url, name):
    times = []
    for i in range(5):
        start = time.time()
        try:
            resp = requests.get(url, timeout=10)
            elapsed = (time.time() - start) * 1000
            times.append(elapsed)
            print(f"  {name} #{i + 1}: {elapsed:.0f}ms - {resp.status_code}")
        except Exception as e:
            print(f"  {name} #{i + 1}: ERROR - {e}")
            times.append(None)
    valid = [t for t in times if t]
    if valid:
        print(f"  Среднее: {sum(valid) / len(valid):.0f}ms")
    print()


if __name__ == "__main__":
    base = "http://localhost:5000"

    print("=" * 60)
    print("ТЕСТ СКОРОСТИ API")
    print("=" * 60)

    # 1. Статическая страница (должна быть быстрой)
    test_endpoint(f"{base}/", "GET / (HTML)")

    # 2. Кэшированный запрос (должен быть очень быстрым)
    test_endpoint(f"{base}/api/stations", "GET /api/stations (кэшируется)")

    # 3. Запрос к БД (должен быть быстрым)
    test_endpoint(f"{base}/api/admin/incidents", "GET /api/admin/incidents")

    # 4. Создание инцидента (самый тяжёлый)
    start = time.time()
    resp = requests.post(f"{base}/api/incidents", json={
        'lat': 56.73, 'lon': 37.17, 'type': 'ambulance'
    })
    elapsed = (time.time() - start) * 1000
    print(f"  POST /api/incidents: {elapsed:.0f}ms - {resp.status_code}")