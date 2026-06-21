"""
Глобальные оптимизации для высокой нагрузки
"""
from functools import lru_cache, wraps
from threading import RLock, local
from datetime import datetime, timedelta
import hashlib
import json
from collections import OrderedDict
import time
import logging
from concurrent.futures import ThreadPoolExecutor

# ============= ПОТОКОВЫЙ ПУЛ =============
executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="async_worker")

# ============= LRU КЭШ С TTL =============
class TTLDict(OrderedDict):
    """Словарь с TTL для кэширования"""
    __slots__ = ('ttl', 'max_size')

    def __init__(self, ttl_seconds=60, max_size=1000):
        self.ttl = ttl_seconds
        self.max_size = max_size
        super().__init__()

    def __getitem__(self, key):
        value, expiry = super().__getitem__(key)
        if datetime.now() > expiry:
            del self[key]
            raise KeyError(key)
        self.move_to_end(key)
        return value

    def __setitem__(self, key, value):
        if len(self) >= self.max_size:
            self.popitem(last=False)
        super().__setitem__(key, (value, datetime.now() + timedelta(seconds=self.ttl)))

    def __contains__(self, key):
        try:
            self[key]
            return True
        except KeyError:
            return False

# Глобальные кэши - увеличенные размеры
query_cache = TTLDict(ttl_seconds=30, max_size=2000)
path_cache = TTLDict(ttl_seconds=300, max_size=50000)
isochrone_cache = TTLDict(ttl_seconds=600, max_size=500)

# ============= ПУЛ СЕССИЙ НА ПОТОК =============
_thread_local = local()

def get_db_session():
    """Получает сессию из пула (одна на поток)"""
    if not hasattr(_thread_local, 'session'):
        from database import Session
        _thread_local.session = Session()
    return _thread_local.session

def close_db_session():
    """Закрывает сессию в конце запроса"""
    if hasattr(_thread_local, 'session'):
        _thread_local.session.close()
        _thread_local.session.remove()
        del _thread_local.session

# ============= ДЕКОРАТОРЫ ДЛЯ КЭШИРОВАНИЯ =============
def cached(ttl_seconds=60, skip_args=None):
    """Декоратор для кэширования результатов функций"""
    def decorator(func):
        cache_key_prefix = func.__name__

        @wraps(func)
        def wrapper(*args, **kwargs):
            # Создаем ключ из аргументов
            if skip_args:
                filtered_args = [a for i, a in enumerate(args) if i not in skip_args]
            else:
                filtered_args = args
            key_data = f"{cache_key_prefix}:{filtered_args}{sorted(kwargs.items())}"
            key = hashlib.md5(key_data.encode()).hexdigest()

            try:
                return query_cache[key]
            except KeyError:
                result = func(*args, **kwargs)
                query_cache[key] = result
                return result
        return wrapper
    return decorator

def cached_path(func):
    """Специальный кэш для путей в графе"""
    @wraps(func)
    def wrapper(graph_id, from_node, to_node):
        key = f"path:{graph_id}:{from_node}:{to_node}"
        try:
            return path_cache[key]
        except KeyError:
            result = func(graph_id, from_node, to_node)
            path_cache[key] = result
            return result
    return wrapper

# ============= БАТЧНАЯ ОБРАБОТКА =============
def batch_process(items, batch_size=100, process_func=None):
    """Обрабатывает элементы партиями"""
    results = []
    for i in range(0, len(items), batch_size):
        batch = items[i:i+batch_size]
        if process_func:
            results.extend(process_func(batch))
        else:
            results.extend(batch)
    return results