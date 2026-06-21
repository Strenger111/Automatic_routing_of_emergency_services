from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey, Boolean, Text, BigInteger
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()

Base = declarative_base()

class Station(Base):
    __tablename__ = 'stations'
    id = Column(Integer, primary_key=True)
    name = Column(String)
    type = Column(String)
    lat = Column(Float)
    lon = Column(Float)
    node_id = Column(BigInteger)  # <-- изменено на BigInteger
    max_vehicles = Column(Integer, default=3)
    vehicles = relationship("Vehicle", back_populates="station", cascade="all, delete")

class Vehicle(Base):
    __tablename__ = 'vehicles'
    id = Column(Integer, primary_key=True)
    name = Column(String)
    type = Column(String)
    status = Column(String)
    station_id = Column(Integer, ForeignKey('stations.id'))
    station = relationship("Station", back_populates="vehicles")
    total_calls = Column(Integer, default=0)
    total_response_time = Column(Float, default=0.0)
    avg_response_time = Column(Float, default=0.0)
    is_temp = Column(Boolean, default=False)
    service_time = Column(Float, default=30.0)
    last_node_id = Column(BigInteger, nullable=True)  # <-- изменено на BigInteger
    last_lat = Column(Float, nullable=True)
    last_lon = Column(Float, nullable=True)
    patrol_lat = Column(Float, nullable=True)
    patrol_lon = Column(Float, nullable=True)
    patrol_radius = Column(Float, nullable=True, default=500)
    patrol_zone_polygon = Column(Text, nullable=True)

class Incident(Base):
    __tablename__ = 'incidents'
    id = Column(Integer, primary_key=True)
    type = Column(String)
    lat = Column(Float)
    lon = Column(Float)
    created_at = Column(DateTime, default=datetime.now)
    assigned_vehicle_id = Column(Integer, ForeignKey('vehicles.id'))
    response_time_sec = Column(Float, nullable=True)
    resolved = Column(Boolean, default=False)

DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('DB_NAME', 'emergency_db')
DB_USER = os.getenv('DB_USER', 'postgres')
DB_PASSWORD = os.getenv('DB_PASSWORD', '1109')

engine = create_engine(
    f'postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}',
    pool_size=50,          # было 30
    max_overflow=30,       # было 20
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_timeout=60        # было 30 - увеличил таймаут
)

Base.metadata.create_all(engine)


def create_indexes():
    """Создаёт индексы для ускорения запросов"""
    from sqlalchemy import Index, text

    with engine.connect() as conn:
        # Индексы для incidents
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_incidents_resolved ON incidents(resolved)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_incidents_type ON incidents(type)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_incidents_created_at ON incidents(created_at)"))
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_incidents_assigned_vehicle ON incidents(assigned_vehicle_id)"))

        # Составные индексы для частых запросов
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_incidents_type_resolved ON incidents(type, resolved)"))
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_incidents_created_resolved ON incidents(created_at, resolved)"))

        # Индексы для vehicles
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_vehicles_station ON vehicles(station_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_vehicles_status ON vehicles(status)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_vehicles_type ON vehicles(type)"))

        # Индексы для stations
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_stations_type ON stations(type)"))

        conn.commit()
        print("✅ Database indexes created")


# Вызвать после создания таблиц
create_indexes()
Session = sessionmaker(bind=engine)