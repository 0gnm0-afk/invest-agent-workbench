"""Existing CLI for importing user-entered API credentials without printing values."""
import json
from news.auth import import_credentials

if __name__ == '__main__':
    print(json.dumps(import_credentials()))
