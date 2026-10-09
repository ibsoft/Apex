#!/usr/bin/env python3
import os
import pyodbc
import json

# Load environment variables
DB_ADDRESS = os.getenv('DB_ADDRESS')
DB_USER = os.getenv('DB_USER')
DB_PASSWORD = os.getenv('DB_PASSWORD')
DB_TABLES = os.getenv('DB_TABLES').split(',')
DB_NAME = os.getenv('DB_NAME')

# Function to query the database

def query_table(table_name, query):
    if table_name not in DB_TABLES:
        return f'Table {table_name} is not allowed for querying.'

    conn_string = f'DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={DB_ADDRESS};UID={DB_USER};PWD={DB_PASSWORD};'
    if DB_NAME:
        conn_string += f'DATABASE={DB_NAME};'

    with pyodbc.connect(conn_string) as conn:
        cursor = conn.cursor()
        cursor.execute(query)
        columns = [column[0] for column in cursor.description]
        results = cursor.fetchall()
        data = [{columns[i]: row[i] for i in range(len(columns))} for row in results]
        return json.dumps(data, indent=4)

# Example usage: query_table('tblTCMessagesReceived', 'SELECT * FROM tblTCMessagesReceived')