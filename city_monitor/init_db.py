from sqlalchemy import create_engine, text

engine = create_engine('postgresql://postgres:1109@localhost/', isolation_level='AUTOCOMMIT')
with engine.connect() as conn:
    result = conn.execute(text("SELECT 1 FROM pg_database WHERE datname='emergency_db'"))
    if not result.fetchone():
        conn.execute(text("CREATE DATABASE emergency_db"))
        print("Created database emergency_db")
    else:
        print("Database already exists")
from database import Base, engine as db_engine
Base.metadata.create_all(db_engine)
print("Tables created.")
