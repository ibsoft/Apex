import unittest
from query_kiosks import execute_query

class TestKioskQueries(unittest.TestCase):
    def test_connected_kiosks(self):
        query = "SELECT COUNT(*) AS connected_kiosks FROM kiosks WHERE status = 'connected';"
        result = execute_query(query)
        self.assertIsInstance(result, list)
        self.assertGreaterEqual(result[0][0], 0)

if __name__ == '__main__':
    unittest.main()