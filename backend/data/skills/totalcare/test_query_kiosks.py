import unittest
from db_connection import execute_query

def get_connected_kiosks():
    query = "SELECT COUNT(*) AS connected_kiosks FROM kiosks WHERE status = 'connected';"
    result = execute_query(query)
    return result[0][0] if result else 0

class TestKioskQueries(unittest.TestCase):
    def test_connected_kiosks(self):
        result = get_connected_kiosks()
        self.assertIsInstance(result, int)
        self.assertGreaterEqual(result, 0)

if __name__ == '__main__':
    unittest.main()