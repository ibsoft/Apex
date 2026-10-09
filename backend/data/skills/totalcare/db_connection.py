import pymssql
import os
from pathlib import Path

def get_db_connection():
    env_file = Path(__file__).with_name('.env')
    from dotenv import load_dotenv
    load_dotenv(env_file)

    host = os.getenv('DB_HOST')
    user = os.getenv('DB_USER')
    password = os.getenv('DB_PASSWORD')
    database = os.getenv('DB_NAME')

    conn = pymssql.connect(server=host, user=user, password=password, database=database)
    return conn

def execute_query(query):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(query)
    results = cursor.fetchall()
    conn.close()
    return results
