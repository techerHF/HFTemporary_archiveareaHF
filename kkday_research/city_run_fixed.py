from __future__ import annotations

import sys
import time

import city_run


def load_cities_with_retry():
    last_error = None
    for attempt in range(6):
        try:
            return city_run.discover_cities()
        except Exception as exc:
            last_error = exc
            if attempt == 5:
                break
            time.sleep(3 * (attempt + 1))
    raise last_error


cached_cities = load_cities_with_retry()
city_run.discover_cities = lambda: cached_cities

if __name__ == "__main__":
    sys.exit(city_run.main())
