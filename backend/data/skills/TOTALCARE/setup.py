#!/usr/bin/env python3
import os

# Load environment variables for TotalCare skill
required_env_vars = ['DB_ADDRESS', 'DB_USER', 'DB_PASSWORD', 'DB_TABLES']

for var in required_env_vars:
    if var not in os.environ:
        raise EnvironmentError(f'Missing required environment variable: {var}')

print('All required environment variables are set.')