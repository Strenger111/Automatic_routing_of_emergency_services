import psycopg2

conn = psycopg2.connect(
    dbname="emergency_db",
    user="postgres",
    password="1109",
    host="localhost",
    port="5432"
)

print("✅ OK")
conn.close()