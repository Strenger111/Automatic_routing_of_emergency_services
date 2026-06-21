document.addEventListener('DOMContentLoaded', () => {
    const map = L.map('map').setView([56.73, 37.17], 13);
    const vehicles = {};
    let currentZonesLayer = null, allZonesLayer = null, selectedStationMarker = null, routeLayer = null;
    let heatLayer = null;
    let incidentData = {};
    const socket = io();

    // Переменные для рисования зоны патруля
    let drawingMode = false;
    let drawingPoints = [];
    let drawingLayer = null;
    let selectedVehicleId = null;
    let patrolZoneLayer = null;
let loadedRoadLayers = [];
    let loadedBuildingLayers = [];
    let loadedRoadNames = [];
    let loadedBuildingNames = [];
    function getVehicleIcon(type) {
        const symbols = {fire: '🚒', ambulance: '🚑', police: '🚓'};
        const symbol = symbols[type] || '🚗';
        return L.divIcon({ html: `<div style="font-size:24px; filter:drop-shadow(2px 2px 2px rgba(0,0,0,0.5));">${symbol}</div>`, iconSize: [28, 28], className: 'leaflet-div-icon' });
    }

    let vehiclePositions = {};

    socket.on('vehicle_update', v => {
        if (!vehicles[v.id]) {
            const marker = L.marker([v.lat, v.lon], {icon: getVehicleIcon(v.type)}).addTo(map);
            marker.on('click', async () => {
                const res = await fetch('/api/vehicle_zones', {
                    method: 'POST', headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({lat: v.lat, lon: v.lon})
                });
                const zones = await res.json();
                showZones(zones, true);
            });
            vehicles[v.id] = marker;
        }
        vehiclePositions[v.id] = {
            lat: v.lat, lon: v.lon,
            speed: v.speed || 0,
            timestamp: Date.now()
        };
    });

    function animateVehicles() {
        for (let id in vehiclePositions) {
            const pos = vehiclePositions[id];
            const marker = vehicles[id];
            if (marker && pos) {
                marker.setLatLng([pos.lat, pos.lon]);
                const popupContent = `Скорость: ${pos.speed} км/ч`;
                if (marker.getPopup()) marker.getPopup().setContent(popupContent);
                else marker.bindPopup(popupContent);
            }
        }
        requestAnimationFrame(animateVehicles);
    }
    animateVehicles();

    const incidentMarkers = {};

    socket.on('incident_created', incident => {
        console.log('Received incident', incident);
        const incIcon = L.divIcon({html: '🚨', iconSize:[32,32], className:'incident-marker'});
        const marker = L.marker([incident.lat, incident.lon], {icon: incIcon}).addTo(map);
        let popupContent = `<b>ЧП #${incident.id}</b><br>Тип: ${incident.type}`;
        if (incident.vehicle_id) {
            popupContent += `<br>Машина: ${incident.vehicle_id}`;
        } else {
            popupContent += `<br><i>В очереди</i>`;
        }
        marker.bindPopup(popupContent).openPopup();
        incidentMarkers[incident.id] = marker;
    });

    socket.on('incident_resolved', data => {
        const marker = incidentMarkers[data.incident_id];
        if (marker) {
            map.removeLayer(marker);
            delete incidentMarkers[data.incident_id];
        }
    });

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '&copy; OpenStreetMap'
    }).addTo(map);

    function showZones(zones, replace=true) {
        const order = ["0-5 мин", "5-10 мин", "10-15 мин"];
        const styles = {
            "0-5 мин": { color:"#2ecc71", fillColor:"#2ecc71", fillOpacity:0.5, weight:2, opacity:0.8 },
            "5-10 мин": { color:"#f39c12", fillColor:"#f39c12", fillOpacity:0.4, weight:2, opacity:0.8 },
            "10-15 мин": { color:"#e74c3c", fillColor:"#e74c3c", fillOpacity:0.3, weight:2, opacity:0.8 }
        };
        if (replace) {
            if (currentZonesLayer) map.removeLayer(currentZonesLayer);
            currentZonesLayer = L.layerGroup().addTo(map);
        } else {
            if (!allZonesLayer) allZonesLayer = L.layerGroup().addTo(map);
        }
        const target = replace ? currentZonesLayer : allZonesLayer;
        order.forEach(zone => {
            const poly = zones[zone];
            if (poly) {
                const layer = L.geoJSON(poly, {style: styles[zone]});
                layer.addTo(target);
                if (replace && zone==="0-5 мин") try { map.fitBounds(layer.getBounds()); } catch(e){}
            }
        });
    }

    async function showStationStats(stationId, marker, stationType) {
        if (selectedStationMarker) {
            const prevType = selectedStationMarker.feature.properties.type;
            selectedStationMarker.setIcon(getStationIcon(prevType, false));
        }
        selectedStationMarker = marker;
        marker.setIcon(getStationIcon(stationType, true));
        const res = await fetch(`/api/station/${stationId}/house_counts`);
        const counts = await res.json();
        let html = `<b>${marker.feature.properties.name}</b><br>Дома в зонах:<br>`;
        for (let [zone, count] of Object.entries(counts)) {
            html += `${zone}: ${count} шт.<br>`;
        }
        marker.bindPopup(html).openPopup();
    }

    function getStationIcon(type, isHighlighted=false) {
        const symbols = {fire: '🔥', ambulance: '🚑', police: '👮'};
        const symbol = symbols[type] || '📍';
        const fontSize = isHighlighted ? '36px' : '28px';
        const shadow = isHighlighted ? '0 0 0 3px yellow, 1px 1px 2px black' : '1px 1px 1px white';
        return L.divIcon({
            html: `<div style="font-size:${fontSize}; text-shadow:${shadow};">${symbol}</div>`,
            iconSize: [isHighlighted?44:36, isHighlighted?44:36],
            className: `station-icon-${type}`,
            popupAnchor: [0, -18]
        });
    }

    socket.on('route_update', data => {
        if (routeLayer) map.removeLayer(routeLayer);
        routeLayer = L.polyline(data.path_coords, { color:'red', weight:5, opacity:0.8 }).addTo(map);
        setTimeout(() => { if (routeLayer) map.removeLayer(routeLayer); }, 10000);
    });

// === ФУНКЦИИ ДЛЯ УПРАВЛЕНИЯ ЗАГРУЖЕННЫМИ СЛОЯМИ ===
    function updateRoadsList() {
        const container = document.getElementById('roads-list');
        if (!container) return;
        if (loadedRoadLayers.length === 0) {
            container.innerHTML = '<i>Нет загруженных дорог</i>';
            return;
        }
        container.innerHTML = loadedRoadLayers.map((layer, idx) => `
            <div class="layer-item">
                <input type="checkbox" class="layer-checkbox" data-layer-idx="${idx}" data-type="road" ${layer.isVisible ? 'checked' : ''} onchange="toggleLayerVisibility(${idx}, 'road')">
                🛣️ Дороги #${idx + 1} (${loadedRoadNames[idx] || 'безымянные'})
                <span class="delete-layer" onclick="deleteLayer(${idx}, 'road')">✖</span>
            </div>
        `).join('');
    }

    function updateBuildingsList() {
        const container = document.getElementById('buildings-list');
        if (!container) return;
        if (loadedBuildingLayers.length === 0) {
            container.innerHTML = '<i>Нет загруженных зданий</i>';
            return;
        }
        container.innerHTML = loadedBuildingLayers.map((layer, idx) => `
            <div class="layer-item">
                <input type="checkbox" class="layer-checkbox" data-layer-idx="${idx}" data-type="building" ${layer.isVisible ? 'checked' : ''} onchange="toggleLayerVisibility(${idx}, 'building')">
                🏢 Здания #${idx + 1} (${loadedBuildingNames[idx] || 'безымянные'})
                <span class="delete-layer" onclick="deleteLayer(${idx}, 'building')">✖</span>
            </div>
        `).join('');
    }

    window.toggleLayerVisibility = (idx, type) => {
        if (type === 'road' && loadedRoadLayers[idx]) {
            if (loadedRoadLayers[idx].isVisible) {
                map.removeLayer(loadedRoadLayers[idx]);
                loadedRoadLayers[idx].isVisible = false;
            } else {
                map.addLayer(loadedRoadLayers[idx]);
                loadedRoadLayers[idx].isVisible = true;
            }
        } else if (type === 'building' && loadedBuildingLayers[idx]) {
            if (loadedBuildingLayers[idx].isVisible) {
                map.removeLayer(loadedBuildingLayers[idx]);
                loadedBuildingLayers[idx].isVisible = false;
            } else {
                map.addLayer(loadedBuildingLayers[idx]);
                loadedBuildingLayers[idx].isVisible = true;
            }
        }
    };

    window.deleteLayer = (idx, type) => {
        if (type === 'road' && loadedRoadLayers[idx]) {
            map.removeLayer(loadedRoadLayers[idx]);
            loadedRoadLayers.splice(idx, 1);
            loadedRoadNames.splice(idx, 1);
            updateRoadsList();
        } else if (type === 'building' && loadedBuildingLayers[idx]) {
            map.removeLayer(loadedBuildingLayers[idx]);
            loadedBuildingLayers.splice(idx, 1);
            loadedBuildingNames.splice(idx, 1);
            updateBuildingsList();
        }
    };

    function clearAllLayers() {
        if (confirm('Удалить все загруженные слои?')) {
            loadedRoadLayers.forEach(layer => map.removeLayer(layer));
            loadedBuildingLayers.forEach(layer => map.removeLayer(layer));
            loadedRoadLayers = [];
            loadedBuildingLayers = [];
            loadedRoadNames = [];
            loadedBuildingNames = [];
            updateRoadsList();
            updateBuildingsList();
        }
    }
    // ============= ЗОНА ПАТРУЛИРОВАНИЯ (РИСОВАНИЕ) =============
    function startDrawing() {
        if (drawingMode) {
            stopDrawing();
            return;
        }

        const vehicleSelect = document.getElementById('patrol-vehicle-select');
        selectedVehicleId = vehicleSelect.value;

        if (!selectedVehicleId) {
            alert('Сначала выберите машину');
            return;
        }

        drawingMode = true;
        drawingPoints = [];

        if (drawingLayer) map.removeLayer(drawingLayer);
        drawingLayer = L.layerGroup().addTo(map);

        const btn = document.getElementById('draw-patrol-zone-btn');
        btn.textContent = '⏹️ Остановить рисование';
        btn.classList.add('active');

        map.getContainer().style.cursor = 'crosshair';

        map.on('click', onMapClickForDrawing);
    }

    function onMapClickForDrawing(e) {
        if (!drawingMode) return;

        const { lat, lng } = e.latlng;
        drawingPoints.push({lat, lon: lng});

        // Рисуем маркер
        const marker = L.circleMarker([lat, lng], {
            radius: 5,
            color: '#ff0000',
            fillColor: '#ff0000',
            fillOpacity: 0.8,
            weight: 2
        }).addTo(drawingLayer);

        // Рисуем линию между точками
        if (drawingPoints.length >= 2) {
            const lastPoint = drawingPoints[drawingPoints.length - 2];
            const line = L.polyline([[lastPoint.lat, lastPoint.lon], [lat, lng]], {
                color: '#ff0000',
                weight: 3,
                dashArray: '5, 10'
            }).addTo(drawingLayer);
        }

        // Если достаточно точек для полигона (>=3), показываем предпросмотр
        if (drawingPoints.length >= 3) {
            if (window.tempPolygon) map.removeLayer(window.tempPolygon);
            const coords = drawingPoints.map(p => [p.lat, p.lon]);
            window.tempPolygon = L.polygon(coords, {
                color: '#00ff00',
                weight: 2,
                fillColor: '#00ff00',
                fillOpacity: 0.2
            }).addTo(drawingLayer);
        }
    }

    async function stopDrawing() {
        if (!drawingMode) return;

        map.off('click', onMapClickForDrawing);
        drawingMode = false;
        map.getContainer().style.cursor = '';

        const btn = document.getElementById('draw-patrol-zone-btn');
        btn.textContent = '✏️ Рисовать зону';
        btn.classList.remove('active');

        if (drawingPoints.length >= 3) {
            // Завершаем полигон
            const coords = drawingPoints.map(p => [p.lat, p.lon]);
            coords.push(coords[0]); // замыкаем

            // Отправляем на сервер
            try {
                const response = await fetch(`/api/vehicle/${selectedVehicleId}/patrol_zone`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ points: drawingPoints, radius: 500 })
                });

                const result = await response.json();
                if (result.success) {
                    alert(`Зона патруля сохранена! Площадь: ${(result.area / 1000000).toFixed(2)} км²`);
                    // Показываем зону на карте
                    if (patrolZoneLayer) map.removeLayer(patrolZoneLayer);
                    patrolZoneLayer = L.polygon(coords, {
                        color: '#27ae60',
                        weight: 3,
                        fillColor: '#27ae60',
                        fillOpacity: 0.2,
                        dashArray: '10, 5'
                    }).addTo(map);
                } else {
                    alert('Ошибка: ' + (result.error || 'неизвестная'));
                }
            } catch (err) {
                alert('Ошибка сохранения: ' + err.message);
            }
        } else {
            alert('Нужно минимум 3 точки для создания зоны');
        }

        // Очищаем временные слои
        if (drawingLayer) map.removeLayer(drawingLayer);
        drawingLayer = null;
        drawingPoints = [];
        if (window.tempPolygon) window.tempPolygon = null;
    }

    async function clearPatrolZone() {
        const vehicleSelect = document.getElementById('patrol-vehicle-select');
        const vehicleId = vehicleSelect.value;

        if (!vehicleId) {
            alert('Выберите машину');
            return;
        }

        if (confirm('Очистить зону патрулирования для этой машины?')) {
            try {
                const response = await fetch(`/api/vehicle/${vehicleId}/patrol_zone/clear`, {
                    method: 'POST'
                });
                const result = await response.json();
                if (result.success) {
                    alert('Зона очищена');
                    if (patrolZoneLayer) map.removeLayer(patrolZoneLayer);
                    patrolZoneLayer = null;
                } else {
                    alert('Ошибка: ' + (result.error || 'неизвестная'));
                }
            } catch (err) {
                alert('Ошибка: ' + err.message);
            }
        }
    }

    async function loadPatrolZone(vehicleId) {
        try {
            const response = await fetch(`/api/vehicle/${vehicleId}/patrol_zone`);
            if (response.status === 404) {
                if (patrolZoneLayer) map.removeLayer(patrolZoneLayer);
                patrolZoneLayer = null;
                return;
            }
            const data = await response.json();
            if (data.type === 'Polygon' && data.coordinates) {
                const coords = data.coordinates[0].map(c => [c[1], c[0]]);
                if (patrolZoneLayer) map.removeLayer(patrolZoneLayer);
                patrolZoneLayer = L.polygon(coords, {
                    color: '#27ae60',
                    weight: 3,
                    fillColor: '#27ae60',
                    fillOpacity: 0.2,
                    dashArray: '10, 5'
                }).addTo(map);
            }
        } catch (err) {
            console.error('Error loading patrol zone:', err);
        }
    }

 // === ФУНКЦИЯ ДЛЯ ЗАГРУЗКИ СПИСКА СОХРАНЁННЫХ ЗОН ===
    async function loadSavedZonesList() {
        const zonesList = document.getElementById('zones-list');
        if (!zonesList) return;

        try {
            const vehicles = await fetch('/api/admin/vehicles').then(r => r.json());
            const vehiclesWithZones = vehicles.filter(v => v.has_patrol_zone);

            if (vehiclesWithZones.length === 0) {
                zonesList.innerHTML = '<i>Нет сохранённых зон</i>';
                return;
            }

            zonesList.innerHTML = vehiclesWithZones.map(v => `
                <div class="zone-item" data-vehicle-id="${v.id}">
                    ${v.type === 'fire' ? '🔥' : (v.type === 'ambulance' ? '🚑' : '👮')} Машина ${v.id} (${v.name})
                    <span class="delete-zone" onclick="event.stopPropagation(); deleteZoneFromList(${v.id})">✖</span>
                </div>
            `).join('');

            document.querySelectorAll('.zone-item').forEach(el => {
                el.addEventListener('click', () => {
                    const vid = el.dataset.vehicleId;
                    loadPatrolZone(vid);
                });
            });
        } catch (err) {
            console.error('Error loading zones list:', err);
        }
    }

    window.deleteZoneFromList = async (vehicleId) => {
        if (confirm('Удалить зону патрулирования?')) {
            try {
                await fetch(`/api/vehicle/${vehicleId}/patrol_zone/clear`, { method: 'POST' });
                if (patrolZoneLayer) map.removeLayer(patrolZoneLayer);
                patrolZoneLayer = null;
                loadSavedZonesList();
            } catch (err) {
                alert('Ошибка удаления: ' + err.message);
            }
        }
    };
    // Обработчик смены выбранной машины
    document.getElementById('patrol-vehicle-select')?.addEventListener('change', (e) => {
        if (e.target.value) {
            loadPatrolZone(e.target.value);
        } else {
            if (patrolZoneLayer) map.removeLayer(patrolZoneLayer);
            patrolZoneLayer = null;
        }
    });

    // Кнопки управления зонами
    document.getElementById('draw-patrol-zone-btn')?.addEventListener('click', startDrawing);
    document.getElementById('clear-patrol-zone-btn')?.addEventListener('click', clearPatrolZone);

    // Завершение рисования при нажатии Escape
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && drawingMode) {
            stopDrawing();
        }
    });

    // ============= ЗАГРУЗКА ДАННЫХ =============
    fetch('/api/roads').then(r => r.json()).then(data => {
        L.geoJSON(data, { style: {color:"#3498db", weight:2, opacity:0.6} }).addTo(map);
    });
    fetch('/api/houses').then(r => r.json()).then(data => {
        L.geoJSON(data, {
            pointToLayer: (f, ll) => L.circleMarker(ll, {
                radius: 3, color:"#2c3e50", fillColor:"#95a5a6",
                fillOpacity: 0.7, weight: 1
            })
        }).addTo(map);
    });
    fetch('/api/stations').then(r => r.json()).then(geojson => {
        L.geoJSON(geojson, {
            pointToLayer: (feature, latlng) => {
                return L.marker(latlng, { icon: getStationIcon(feature.properties.type, false) });
            },
            onEachFeature: (feature, layer) => {
                layer.feature = feature;
                layer.bindPopup(`<b>${feature.properties.name}</b><br>Тип: ${feature.properties.type}`);
                layer.on('click', async () => {
                    const sid = feature.properties.id;
                    const res = await fetch(`/api/station/${sid}/zones`);
                    const zones = await res.json();
                    showZones(zones, true);
                    showStationStats(sid, layer, feature.properties.type);
                });
            }
        }).addTo(map);
    });

    // ============= КНОПКИ УПРАВЛЕНИЯ =============
    document.getElementById('show-all-zones')?.addEventListener('click', async () => {
        if (allZonesLayer) map.removeLayer(allZonesLayer);
        allZonesLayer = L.layerGroup().addTo(map);
        const stationsRes = await fetch('/api/stations');
        const stationsJson = await stationsRes.json();
        for (const feat of stationsJson.features) {
            const zonesRes = await fetch(`/api/station/${feat.properties.id}/zones`);
            const zones = await zonesRes.json();
            showZones(zones, false);
        }
    });

    document.getElementById('clear-zones')?.addEventListener('click', () => {
        if (currentZonesLayer) map.removeLayer(currentZonesLayer);
        if (allZonesLayer) map.removeLayer(allZonesLayer);
        currentZonesLayer = allZonesLayer = null;
    });

    document.getElementById('new-incident-btn')?.addEventListener('click', () => {
    // Получаем выбранный тип из селектора
    const typeSelect = document.getElementById('incident-type-select');
    const selectedType = typeSelect?.value || 'fire';

    // Показываем подсказку
    const typeNames = {fire: '🔥 Пожар', ambulance: '🚑 Скорая', police: '👮 Полиция'};
    alert(`Выбран тип: ${typeNames[selectedType]}\nКликните на карте для создания ЧП`);

    map.once('click', async e => {
        try {
            const res = await fetch('/api/incidents', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    lat: e.latlng.lat,
                    lon: e.latlng.lng,
                    type: selectedType  // используем выбранный тип
                })
            });
            const data = await res.json();
            if (!res.ok) {
                alert(`Ошибка: ${data.error || 'неизвестная'}`);
                return;
            }
            if (data.vehicle_id) {
                alert(`✅ ЧП создано! Отправлена машина ${data.vehicle_id} (${typeNames[selectedType]})`);
            } else {
                alert(`⏳ ЧП добавлено в очередь (ID ${data.incident_id})`);
            }
        } catch(err) {
            alert(`❌ Ошибка: ${err.message}`);
        }
    });
});

    document.getElementById('new-station-btn')?.addEventListener('click', () => {
        map.once('click', async e => {
            let name = prompt("Название станции:");
            let type = prompt("Тип станции (fire/ambulance/police):");
            if (!name || !type) return;
            const res = await fetch('/api/stations', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name, type, lat: e.latlng.lat, lon: e.latlng.lng})
            });
            if (res.ok) { alert('Станция добавлена'); location.reload(); }
            else alert("Ошибка создания станции");
        });
        alert('Кликните на карте для размещения станции');
    });

    // ============= ТЕПЛОВАЯ КАРТА =============
    async function loadIncidentsForHeatmap(type) {
        const res = await fetch(`/api/heatmap/${type}`);
        const points = await res.json();
        return points;
    }

    function createEnhancedHeatmap(points, mapInstance) {
        if (heatLayer) {
            mapInstance.removeLayer(heatLayer);
        }
        if (!points || points.length === 0) {
            console.log('No data for heatmap');
            return;
        }
        const heatOptions = {
            radius: 45,
            blur: 25,
            maxZoom: 17,
            minOpacity: 0.3,
            max: 1.0,
            gradient: {
                0.0: 'blue',
                0.2: 'cyan',
                0.4: 'lime',
                0.6: 'yellow',
                0.8: 'orange',
                1.0: 'red'
            }
        };
        let enhancedPoints = points;
        if (points.length < 5) {
            enhancedPoints = [];
            for (const p of points) {
                enhancedPoints.push(p);
                for (let i = -1; i <= 1; i++) {
                    for (let j = -1; j <= 1; j++) {
                        if (i !== 0 || j !== 0) {
                            const offset = 0.0005;
                            enhancedPoints.push([
                                p[0] + i * offset,
                                p[1] + j * offset,
                                p[2] * 0.5
                            ]);
                        }
                    }
                }
            }
        }
        heatLayer = L.heatLayer(enhancedPoints, heatOptions).addTo(mapInstance);
    }

    document.getElementById('show-heatmap')?.addEventListener('click', async () => {
        const typeSelect = document.getElementById('heatmap-type');
        if (!typeSelect) return;
        const type = typeSelect.value;
        if (!type) {
            if (heatLayer) map.removeLayer(heatLayer);
            return;
        }
        const points = await loadIncidentsForHeatmap(type);
        createEnhancedHeatmap(points, map);
    });

    map.on('zoomend', () => {
        if (heatLayer) {
            if (map.getZoom() > 15) {
                heatLayer.setOptions({ radius: 60, blur: 30 });
            } else {
                heatLayer.setOptions({ radius: 45, blur: 25 });
            }
        }
    });

    // ============= СПИСОК МАШИН =============
    const vehiclesListDiv = document.getElementById('vehicles-list');
    if (vehiclesListDiv) {
        socket.on('vehicles_list', (activeVehicles) => {
            vehiclesListDiv.innerHTML = '';
            if (activeVehicles.length === 0) {
                vehiclesListDiv.innerHTML = '<i>Нет активных машин</i>';
                return;
            }
            activeVehicles.forEach(v => {
                const div = document.createElement('div');
                div.style.marginBottom = '8px';
                div.style.padding = '4px';
                div.style.borderBottom = '1px solid #ddd';
                div.style.cursor = 'pointer';
                div.style.borderRadius = '4px';
                div.onmouseover = () => div.style.backgroundColor = '#f0f0f0';
                div.onmouseout = () => div.style.backgroundColor = 'transparent';
                const symbols = {fire:'🚒', ambulance:'🚑', police:'🚓'};
                const symbol = symbols[v.type] || '🚗';
                div.innerHTML = `<strong>${symbol} ${v.id}</strong><br>Статус: ${v.status}<br>📞 ${v.total_calls} вызовов, ⏱ ${(v.avg_response_time || 0).toFixed(1)}с<br>🚀 ${v.speed || 0} км/ч`;
                div.onclick = () => {
                    map.setView([v.lat, v.lon], 16);
                    if (vehicles[v.id]) vehicles[v.id].openPopup();
                };
                vehiclesListDiv.appendChild(div);
            });
        });
    }

    socket.on('vehicle_hide', (data) => {
        if (vehicles[data.id]) {
            map.removeLayer(vehicles[data.id]);
            delete vehicles[data.id];
            delete vehiclePositions[data.id];
        }
    });
    // Добавляем в main.js отображение количества соединений
async function updateRoadsGraphStatus() {
    if (!roadsGraphStatus) return;

    try {
        const response = await fetch('/api/roads/status');
        const data = await response.json();

        roadsGraphStatus.innerHTML = `
            <div style="display:flex; gap:15px; flex-wrap:wrap; margin-top:5px; font-size:11px;">
                <span>🟢 Всего узлов: ${data.total_nodes}</span>
                <span>🔵 Рёбер: ${data.total_edges}</span>
                <span>🟡 Добавлено узлов: ${data.added_nodes}</span>
                <span>⚪ Базовых: ${data.base_nodes}</span>
                <span style="color:#888;">🔗 Порог: ${data.connection_threshold_meters}м</span>
            </div>
        `;
    } catch (err) {
        roadsGraphStatus.innerHTML = '❌ Ошибка загрузки статуса';
    }
}

    // ============= ЗАГРУЗКА ФАЙЛОВ С ПОДДЕРЖКОЙ МНОЖЕСТВЕННЫХ СЛОЁВ =============
    const roadsInput = document.getElementById('roads-file');
    const buildingsInput = document.getElementById('buildings-file');
    const clearAllBtn = document.getElementById('clear-all-layers');

    if (roadsInput) {
        roadsInput.addEventListener('change', function(e) {
            const files = Array.from(e.target.files);
            files.forEach(file => {
                const reader = new FileReader();
                reader.onload = function(evt) {
                    try {
                        const geojson = JSON.parse(evt.target.result);
                        const layer = L.geoJSON(geojson, {
                            style: { color: "#e67e22", weight: 2, opacity: 0.7 },
                            className: 'custom-road-layer'
                        });
                        layer.addTo(map);
                        layer.isVisible = true;
                        loadedRoadLayers.push(layer);
                        loadedRoadNames.push(file.name);
                        updateRoadsList();
                        alert(`Дороги загружены: ${geojson.features?.length || 0} объектов (${file.name})`);
                    } catch (err) {
                        alert('Ошибка парсинга GeoJSON: ' + err.message);
                    }
                };
                reader.readAsText(file);
            });
            e.target.value = '';
        });
    }

    if (buildingsInput) {
        buildingsInput.addEventListener('change', function(e) {
            const files = Array.from(e.target.files);
            files.forEach(file => {
                const reader = new FileReader();
                reader.onload = function(evt) {
                    try {
                        const geojson = JSON.parse(evt.target.result);
                        const layer = L.geoJSON(geojson, {
                            pointToLayer: (f, ll) => L.circleMarker(ll, {
                                radius: 3,
                                color: "#9b59b6",
                                fillColor: "#8e44ad",
                                fillOpacity: 0.6,
                                weight: 1
                            }),
                            className: 'custom-building-layer'
                        });
                        layer.addTo(map);
                        layer.isVisible = true;
                        loadedBuildingLayers.push(layer);
                        loadedBuildingNames.push(file.name);
                        updateBuildingsList();
                        alert(`Здания загружены: ${geojson.features?.length || 0} объектов (${file.name})`);
                    } catch (err) {
                        alert('Ошибка парсинга GeoJSON: ' + err.message);
                    }
                };
                reader.readAsText(file);
            });
            e.target.value = '';
        });
    }

    if (clearAllBtn) {
        clearAllBtn.addEventListener('click', clearAllLayers);
    }

    // Инициализация обновления списка машин
    updatePatrolVehicleSelect();
    setInterval(updatePatrolVehicleSelect, 5000);

    // Загрузка зон для выбранной машины при старте
    setTimeout(() => {
        const select = document.getElementById('patrol-vehicle-select');
        if (select && select.value) {
            loadPatrolZone(select.value);
        }
    }, 1000);

    // Загрузка списка сохранённых зон
    loadSavedZonesList();
    setInterval(loadSavedZonesList, 10000);
});