import osmnx as ox
import geopandas as gpd
import pandas as pd
import json
import os
import networkx as nx
import logging
from database import Session, Station as DBStation
from geopy.distance import distance

CONFIG_FILE = "static/data/config.json"
ADDED_GRAPH_FILE = "static/data/added_graph.graphml"


class DataModel:
    def __init__(self):
        print("Loading data...")

        # 1. Сначала загружаем конфиг (чтобы получить порог)
        self.config = self._load_config()
        self.connection_threshold = self.config.get('connection_threshold_meters', 5)
        print(f"🔗 Порог соединения графов: {self.connection_threshold} метров")

        # 2. Загружаем основной граф
        self.base_graph = ox.load_graphml("static/data/graph.graphml")

        # 3. Загружаем добавленный граф (если есть)
        self.added_graph = self._load_added_graph()

        # 4. Объединяем графы (используем connection_threshold)
        self.graph = self._merge_graphs(self.base_graph, self.added_graph)

        # 5. Загружаем дома
        self.base_houses_gdf = gpd.read_file("static/data/houses.geojson")
        if self.base_houses_gdf.crs is None:
            self.base_houses_gdf.set_crs("EPSG:4326", inplace=True)
        self.base_houses_gdf = self.base_houses_gdf[['geometry']].copy()
        self.base_houses_gdf = self._attach_nodes_to_gdf(self.base_houses_gdf)

        # 6. Загружаем добавленные дома
        self.added_houses_gdf = self._load_added_houses()
        if self.added_houses_gdf is not None and not self.added_houses_gdf.empty:
            self.added_houses_gdf = self._attach_nodes_to_gdf(self.added_houses_gdf)
        else:
            self.added_houses_gdf = gpd.GeoDataFrame(columns=['geometry', 'node_id'], crs="EPSG:4326")

        self._rebuild_houses_gdf()

        # 7. Загружаем станции
        self._load_stations()

        print(f"✅ Загрузка завершена: {len(self.houses_gdf)} домов, {len(self.stations)} станций")
        print(f"📊 Граф: {len(self.graph.nodes)} узлов, {len(self.graph.edges)} рёбер")

    def _load_config(self):
        """Загружает конфигурацию из файла"""
        default = {
            "speed_kmh": 50,
            "time_limits_sec": [300, 600, 900],
            "service_time_sec": 2,
            "simulation_speed": 30,
            "connection_threshold_meters": 5  # <-- ДОБАВЛЯЕМ СЮДА
        }
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    saved = json.load(f)
                    default.update(saved)
            except Exception as e:
                logging.error(f"Ошибка загрузки конфига: {e}")
        else:
            self._save_config(default)
        return default

    def _save_config(self, config=None):
        """Сохраняет конфигурацию в файл"""
        if config is None:
            config = self.config
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)

    def _load_added_graph(self):
        """Загружает сохранённый дополнительный граф"""
        if os.path.exists(ADDED_GRAPH_FILE):
            try:
                graph = ox.load_graphml(ADDED_GRAPH_FILE)
                print(f"Загружен дополнительный граф: {len(graph.nodes)} узлов, {len(graph.edges)} рёбер")
                return graph
            except Exception as e:
                logging.error(f"Ошибка загрузки дополнительного графа: {e}")
                return nx.MultiDiGraph()
        return nx.MultiDiGraph()

    def _merge_graphs(self, base_graph, added_graph):
        """
        Объединяет основной и дополнительный графы.
        Соединяет графы ТОЛЬКО если узлы находятся на расстоянии < connection_threshold метров.
        """
        if not added_graph or len(added_graph.nodes) == 0:
            return base_graph

        # Создаём копию базового графа
        merged = nx.MultiDiGraph(base_graph)

        # Сначала добавляем все узлы из дополнительного графа
        for node, data in added_graph.nodes(data=True):
            if node not in merged:
                merged.add_node(node, **data)

        # Собираем информацию о базовых узлах для быстрого поиска
        base_nodes_coords = {}
        for node_id in base_graph.nodes():
            base_nodes_coords[node_id] = {
                'lat': base_graph.nodes[node_id]['y'],
                'lon': base_graph.nodes[node_id]['x']
            }

        # Добавляем рёбра из дополнительного графа
        for u, v, key, data in added_graph.edges(data=True, keys=True):
            if not merged.has_edge(u, v, key):
                merged.add_edge(u, v, key, **data)

        # СОЕДИНЯЕМ ГРАФЫ — ТОЛЬКО ДЛЯ БЛИЗКИХ УЗЛОВ
        connection_count = 0
        added_nodes = list(added_graph.nodes)

        for added_node in added_nodes:
            if added_node not in added_graph:
                continue

            added_lat = added_graph.nodes[added_node]['y']
            added_lon = added_graph.nodes[added_node]['x']

            # Ищем ближайший узел в БАЗОВОМ графе
            nearest_base_node = None
            nearest_distance = float('inf')

            for base_node, coords in base_nodes_coords.items():
                dist = distance(
                    (added_lat, added_lon),
                    (coords['lat'], coords['lon'])
                ).meters

                if dist < nearest_distance:
                    nearest_distance = dist
                    nearest_base_node = base_node

            # СОЕДИНЯЕМ ТОЛЬКО ЕСЛИ РАССТОЯНИЕ МЕНЬШЕ ПОРОГА
            if nearest_base_node and nearest_distance <= self.connection_threshold:
                # Добавляем соединительное ребро
                merged.add_edge(
                    added_node,
                    nearest_base_node,
                    length=nearest_distance,
                    highway='connection'
                )
                merged.add_edge(
                    nearest_base_node,
                    added_node,
                    length=nearest_distance,
                    highway='connection'
                )
                connection_count += 1
                logging.debug(
                    f"Соединены узлы {added_node} и {nearest_base_node} (расстояние: {nearest_distance:.2f}м)")

        print(f"🔗 Создано соединений между базовым и добавленным графом: {connection_count}")
        return merged

    def _merge_added_graphs(self, existing_graph, new_graph):
        """
        Объединяет два дополнительных графа между собой.
        Соединяет их ТОЛЬКО если узлы находятся на расстоянии < connection_threshold метров.
        """
        if len(existing_graph.nodes) == 0:
            return new_graph

        if len(new_graph.nodes) == 0:
            return existing_graph

        # Копируем существующий граф
        merged = nx.MultiDiGraph(existing_graph)

        # Добавляем все узлы из нового графа
        for node, data in new_graph.nodes(data=True):
            if node not in merged:
                merged.add_node(node, **data)

        # Добавляем все рёбра из нового графа
        for u, v, key, data in new_graph.edges(data=True, keys=True):
            if not merged.has_edge(u, v, key):
                merged.add_edge(u, v, key, **data)

        # СОЕДИНЯЕМ ГРАФЫ МЕЖДУ СОБОЙ (только близкие узлы)
        connection_count = 0

        # Получаем узлы существующего графа
        existing_nodes = list(existing_graph.nodes)
        new_nodes = list(new_graph.nodes)

        # Создаём индекс существующих узлов для быстрого поиска
        existing_coords = {}
        for node in existing_nodes:
            if node in existing_graph:
                existing_coords[node] = {
                    'lat': existing_graph.nodes[node]['y'],
                    'lon': existing_graph.nodes[node]['x']
                }

        # Для каждого нового узла ищем ближайший существующий
        for new_node in new_nodes:
            if new_node not in new_graph:
                continue

            new_lat = new_graph.nodes[new_node]['y']
            new_lon = new_graph.nodes[new_node]['x']

            nearest_existing = None
            nearest_distance = float('inf')

            for existing_node, coords in existing_coords.items():
                dist = distance(
                    (new_lat, new_lon),
                    (coords['lat'], coords['lon'])
                ).meters

                if dist < nearest_distance:
                    nearest_distance = dist
                    nearest_existing = existing_node

            # СОЕДИНЯЕМ ТОЛЬКО ЕСЛИ РАССТОЯНИЕ МЕНЬШЕ ПОРОГА
            if nearest_existing and nearest_distance <= self.connection_threshold:
                merged.add_edge(
                    new_node,
                    nearest_existing,
                    length=nearest_distance,
                    highway='connection'
                )
                merged.add_edge(
                    nearest_existing,
                    new_node,
                    length=nearest_distance,
                    highway='connection'
                )
                connection_count += 1
                logging.debug(f"Соединены графы: {new_node} и {nearest_existing} (расстояние: {nearest_distance:.2f}м)")

        print(f"🔗 Создано соединений между добавленными графами: {connection_count}")
        return merged

    def _geojson_to_graph(self, gdf):
        """
        Преобразует GeoDataFrame с линиями в граф NetworkX.
        Поддерживает LineString и MultiLineString.
        """
        import networkx as nx
        from shapely.geometry import LineString, MultiLineString

        graph = nx.MultiDiGraph()

        for idx, row in gdf.iterrows():
            geom = row.geometry

            # Обрабатываем разные типы геометрии
            lines = []
            if geom.geom_type == 'LineString':
                lines = [geom]
            elif geom.geom_type == 'MultiLineString':
                lines = list(geom.geoms)
            else:
                logging.warning(f"Пропущен объект типа {geom.geom_type}")
                continue

            for line in lines:
                coords = list(line.coords)
                if len(coords) < 2:
                    continue

                # Добавляем узлы
                node_ids = []
                for i, (lon, lat) in enumerate(coords):
                    node_id = f"added_{idx}_{i}"
                    if not graph.has_node(node_id):
                        graph.add_node(node_id, x=lon, y=lat)
                    node_ids.append(node_id)

                # Добавляем рёбра
                for i in range(len(node_ids) - 1):
                    u = node_ids[i]
                    v = node_ids[i + 1]

                    # Вычисляем длину в метрах
                    lat1, lon1 = coords[i][1], coords[i][0]
                    lat2, lon2 = coords[i + 1][1], coords[i + 1][0]
                    length = distance((lat1, lon1), (lat2, lon2)).meters

                    # Добавляем ребро в обоих направлениях (двустороннее движение)
                    graph.add_edge(u, v, length=length, highway='unclassified')
                    graph.add_edge(v, u, length=length, highway='unclassified')

        print(f"Создан граф из GeoJSON: {len(graph.nodes)} узлов, {len(graph.edges)} рёбер")
        return graph

    def add_roads_from_geojson(self, geojson_str):
        """
        Добавляет дороги из GeoJSON в граф.
        Возвращает количество добавленных рёбер.
        """
        try:
            # Парсим GeoJSON
            gdf = gpd.read_file(geojson_str)
            if gdf.crs is None:
                gdf.set_crs("EPSG:4326", inplace=True)

            # Преобразуем в граф
            new_graph = self._geojson_to_graph(gdf)

            if len(new_graph.nodes) == 0:
                logging.warning("Не удалось извлечь дороги из GeoJSON")
                return 0

            # Объединяем с существующим дополнительным графом
            self.added_graph = self._merge_added_graphs(self.added_graph, new_graph)

            # Пересобираем основной граф
            self.graph = self._merge_graphs(self.base_graph, self.added_graph)

            # Сохраняем дополнительный граф
            self._save_added_graph()

            # Очищаем кэши маршрутизации
            from routing import clear_routing_cache
            clear_routing_cache()

            logging.info(f"✅ Добавлено дорог: {len(new_graph.edges)}")
            return len(new_graph.edges)

        except Exception as e:
            logging.error(f"Ошибка добавления дорог: {e}")
            raise

    def clear_added_roads(self):
        """Удаляет все добавленные дороги"""
        self.added_graph = nx.MultiDiGraph()
        self.graph = self.base_graph
        self._save_added_graph()
        from routing import clear_routing_cache
        clear_routing_cache()
        logging.info("🗑️ Добавленные дороги удалены")

    def _load_stations(self):
        """Загружает станции из базы данных или из файла"""
        session = Session()
        db_stations = session.query(DBStation).all()
        self.stations = []
        for st in db_stations:
            self.stations.append({
                "id": st.id, "name": st.name, "type": st.type,
                "lat": st.lat, "lon": st.lon,
                "node_id": st.node_id if st.node_id else self.get_nearest_node(st.lat, st.lon),
                "max_vehicles": st.max_vehicles
            })
        session.close()

        if not self.stations:
            with open("static/data/stations.json", encoding="utf-8") as f:
                json_stations = json.load(f)
            session = Session()
            for st in json_stations:
                node_id = self.get_nearest_node(st['lat'], st['lon'])
                db_st = DBStation(id=st['id'], name=st['name'], type=st['type'],
                                  lat=st['lat'], lon=st['lon'], node_id=node_id,
                                  max_vehicles=3)
                session.add(db_st)
                self.stations.append({
                    "id": st['id'], "name": st['name'], "type": st['type'],
                    "lat": st['lat'], "lon": st['lon'], "node_id": node_id,
                    "max_vehicles": 3
                })
            session.commit()
            session.close()

    def _attach_nodes_to_gdf(self, gdf):
        """Привязывает дома к ближайшим узлам графа"""
        gdf = gdf.copy()
        if gdf.crs != "EPSG:4326":
            gdf = gdf.to_crs("EPSG:4326")
        if not all(gdf.geometry.type.isin(['Point', 'MultiPoint'])):
            gdf_proj = gdf.to_crs("EPSG:3857")
            centroids_proj = gdf_proj.geometry.centroid
            gdf['geometry'] = centroids_proj.to_crs("EPSG:4326")
        gdf['node_id'] = ox.nearest_nodes(
            self.graph,
            X=gdf.geometry.x,
            Y=gdf.geometry.y
        )
        return gdf[['geometry', 'node_id']]

    def _load_added_houses(self):
        """Загружает добавленные дома"""
        added_path = "static/data/added_houses.geojson"
        if os.path.exists(added_path):
            gdf = gpd.read_file(added_path)
            if gdf.crs is None:
                gdf.set_crs("EPSG:4326", inplace=True)
            gdf = gpd.GeoDataFrame(geometry=gdf.geometry, crs=gdf.crs)
            gdf['node_id'] = None
            return gdf
        return gpd.GeoDataFrame(columns=['geometry', 'node_id'], crs="EPSG:4326")

    def _save_added_houses(self):
        """Сохраняет добавленные дома"""
        added_path = "static/data/added_houses.geojson"
        if self.added_houses_gdf is not None and not self.added_houses_gdf.empty:
            to_save = gpd.GeoDataFrame(geometry=self.added_houses_gdf.geometry, crs=self.added_houses_gdf.crs)
            to_save.to_file(added_path, driver="GeoJSON")
        elif os.path.exists(added_path):
            os.remove(added_path)

    def _rebuild_houses_gdf(self):
        """Объединяет базовые и добавленные дома"""
        base = self.base_houses_gdf[['geometry', 'node_id']].copy()
        if self.added_houses_gdf is not None and not self.added_houses_gdf.empty:
            added = self.added_houses_gdf[['geometry', 'node_id']].copy()
            combined = pd.concat([base, added], ignore_index=True)
        else:
            combined = base
        combined = combined[['geometry', 'node_id']]
        self.houses_gdf = combined

    def add_houses(self, geojson_str):
        """Добавляет дома из GeoJSON"""
        temp_gdf = gpd.read_file(geojson_str)
        if temp_gdf.crs is None:
            temp_gdf.set_crs("EPSG:4326", inplace=True)
        else:
            temp_gdf = temp_gdf.to_crs("EPSG:4326")
        temp_gdf = gpd.GeoDataFrame(geometry=temp_gdf.geometry, crs=temp_gdf.crs)
        temp_gdf = self._attach_nodes_to_gdf(temp_gdf)
        self.added_houses_gdf = pd.concat([self.added_houses_gdf, temp_gdf], ignore_index=True)
        self._rebuild_houses_gdf()
        self._save_added_houses()
        return len(temp_gdf)

    def clear_added_houses(self):
        """Удаляет добавленные дома"""
        self.added_houses_gdf = gpd.GeoDataFrame(columns=['geometry', 'node_id'], crs="EPSG:4326")
        self._rebuild_houses_gdf()
        self._save_added_houses()

    def get_nearest_node(self, lat, lon):
        """Возвращает ближайший узел графа к точке"""
        return ox.nearest_nodes(self.graph, lon, lat)

    def update_config(self, key, value):
        """Обновляет параметр конфигурации"""
        self.config[key] = value
        self._save_config()
        if key == 'connection_threshold_meters':
            self.connection_threshold = value
            # Пересобираем граф с новым порогом
            self.graph = self._merge_graphs(self.base_graph, self.added_graph)