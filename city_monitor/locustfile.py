"""
Нагрузочное тестирование системы диспетчеризации
Запуск: locust -f locustfile.py --host=http://localhost:5000
"""

import time
import random
from locust import HttpUser, task, between, events
import json


class EmergencyDispatcherUser(HttpUser):
    """
    Симулирует диспетчера, работающего с системой
    """
    wait_time = between(0.5, 3)  # Реалистичные паузы между действиями

    @task(3)
    def create_incident(self):
        """Создание нового инцидента"""
        lat = random.uniform(56.71, 56.76)
        lon = random.uniform(37.12, 37.22)

        incident_type = random.choices(
            ['fire', 'ambulance', 'police'],
            weights=[0.3, 0.5, 0.2]
        )[0]

        # Без with, просто запрос
        response = self.client.post(
            '/api/incidents',
            json={'lat': lat, 'lon': lon, 'type': incident_type},
            name='/api/incidents - создание ЧП'
        )

        # Логируем только ошибки
        if response.status_code not in [200, 202]:
            response.failure(f"Ошибка {response.status_code}")

    @task(2)
    def view_stations(self):
        """Просмотр списка станций"""
        self.client.get('/api/stations', name='/api/stations - список станций')

    @task(2)
    def view_vehicles(self):
        """Просмотр списка машин"""
        self.client.get('/api/admin/vehicles', name='/api/admin/vehicles - список машин')

    @task(2)
    def view_incidents(self):
        """Просмотр активных инцидентов"""
        self.client.get('/api/admin/incidents', name='/api/admin/incidents - активные ЧП')

    @task(1)
    def view_analytics(self):
        """Просмотр аналитики"""
        self.client.get('/api/analytics/avg_response', name='/api/analytics/avg_response')

    @task(1)
    def view_hot_zones(self):
        """Просмотр горячих зон"""
        self.client.get('/api/analytics/hot_zones?type=fire', name='/api/analytics/hot_zones')


class AdminUser(HttpUser):
    """Симулирует администратора"""
    wait_time = between(1, 5)

    @task(1)
    def admin_stats(self):
        """Получение статистики"""
        self.client.get('/api/admin/stats/incidents', name='/api/admin/stats/incidents')
        self.client.get('/api/admin/stats/vehicles', name='/api/admin/stats/vehicles')

    @task(1)
    def resolve_incident(self):
        """Решение инцидента"""
        response = self.client.get('/api/admin/incidents')
        if response.status_code == 200 and response.json():
            incidents = response.json()
            if incidents:
                inc_id = random.choice(incidents)['id']
                self.client.post(
                    f'/api/admin/incidents/{inc_id}/resolve',
                    name='/api/admin/incidents/resolve'
                )


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """Проверка перед началом теста"""
    print("\n" + "=" * 60)
    print("🔥 НАЧАЛО НАГРУЗОЧНОГО ТЕСТИРОВАНИЯ 🔥")
    print("=" * 60)

    # Быстрая проверка сервера
    import requests
    try:
        start = time.time()
        resp = requests.get('http://localhost:5000/api/stations', timeout=5)
        elapsed = (time.time() - start) * 1000
        print(f"📊 Базовое время ответа: {elapsed:.0f}ms")

        if elapsed > 2000:
            print("\n⚠️  КРИТИЧЕСКАЯ ПРОБЛЕМА!")
            print("   Сервер отвечает за {:.0f}ms".format(elapsed))
            print("   Это указывает на искусственную задержку в коде:")
            print("   - time.sleep(2) где-то в обработчиках")
            print("   - Блокирующая операция без асинхронности")
            print("   - Проблемы с сетевым стеком\n")
    except Exception as e:
        print(f"❌ Сервер не отвечает: {e}")

    print("=" * 60)


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    """Анализ результатов"""
    print("\n" + "=" * 60)
    print("📊 РЕЗУЛЬТАТЫ ТЕСТИРОВАНИЯ")
    print("=" * 60)

    if environment.runner and environment.runner.stats:
        stats = environment.runner.stats
        total_avg = 0
        count = 0

        for stat in stats.values():
            if stat.num_requests > 0:
                total_avg += stat.avg_response_time
                count += 1

        if count > 0:
            avg_time = total_avg / count
            print(f"📈 Среднее время ответа: {avg_time:.0f}ms")

            if avg_time > 2000:
                print("\n🔴 ДИАГНОЗ: Сервер работает очень медленно (>2 секунд)")
                print("\n   Что нужно проверить в коде сервера:")
                print("   1. Поищите 'time.sleep(2)' во всех обработчиках")
                print("   2. Проверьте синхронные вызовы БД")
                print("   3. Посмотрите нет ли блокировок на глобальных объектах")
                print("   4. Убедитесь что Flask запущен с отключенным debug режимом")
                print("\n   Быстрое решение для теста:")
                print("   - Удалите все time.sleep из кода")
                print("   - Используйте async/await где возможно")
                print("   - Добавьте индексы в БД")

    print("\n" + "=" * 60)
    print("✅ ТЕСТ ЗАВЕРШЕН")
    print("=" * 60)