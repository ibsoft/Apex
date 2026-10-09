import pymssql
import os
from dotenv import load_dotenv
from pathlib import Path

def test_db_connection():
    env_file = Path(__file__).with_name('.env')
    load_dotenv(env_file)

    # Get database connection parameters
    host = os.getenv('DB_HOST')
    user = os.getenv('DB_USER')
    password = os.getenv('DB_PASSWORD')
    database = os.getenv('DB_NAME')

    try:
        # Establish the connection
        conn = pymssql.connect(server=host, user=user, password=password, database=database)
        print("Connection successful!")
        conn.close()
    except Exception as e:
        print("Connection failed:", e)

if __name__ == "__main__":
    test_db_connection()