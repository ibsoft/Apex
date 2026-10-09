from db_connection import execute_query

def get_connected_kiosks():
    query = "SELECT COUNT(*) AS connected_kiosks FROM kiosks WHERE status = 'connected';"
    result = execute_query(query)
    return result[0][0] if result else 0

if __name__ == '__main__':
    print('Connected Kiosks:', get_connected_kiosks())