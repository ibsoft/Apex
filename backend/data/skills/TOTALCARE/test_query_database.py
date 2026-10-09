import os
import json
import pytest
import pyodbc
from query_database import query_table

# Mock environment variables for testing
os.environ['DB_ADDRESS'] = 'your_db_address'
os.environ['DB_USER'] = 'your_db_user'
os.environ['DB_PASSWORD'] = 'your_db_password'
os.environ['DB_TABLES'] = 'tblTCMessagesReceived,tblTcSpTmaConnection'
os.environ['DB_NAME'] = 'your_db_name'

# Test querying a valid table

def test_query_valid_table():
    result = query_table('tblTCMessagesReceived', 'SELECT * FROM tblTCMessagesReceived')
    data = json.loads(result)
    assert isinstance(data, list)  # Ensure the result is a list

# Test querying an invalid table

def test_query_invalid_table():
    result = query_table('invalid_table_name', 'SELECT * FROM invalid_table_name')
    assert "Table invalid_table_name is not allowed for querying." in result