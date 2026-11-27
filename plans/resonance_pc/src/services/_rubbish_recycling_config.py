"""City configuration shared by the freight planner and rubbish recycling."""

import json
from pathlib import Path


RUBBISH_RECYCLING_CONFIG_PATH = "data/meta/rubbish_recycling_pc.json"


def load_rubbish_recycling_cities(path: Path) -> dict[str, dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cities = payload.get("eligible_cities") if isinstance(payload, dict) else None
    if not isinstance(cities, dict):
        raise ValueError("rubbish city configuration must contain eligible_cities")
    return {
        str(city_id): {
            "city_name": str(city.get("city_name") or "").strip(),
            "city_key": str(city.get("city_key") or "").strip(),
            "shop_name": str(city.get("shop_name") or "垃圾处理中心").strip(),
        }
        for city_id, city in cities.items() if isinstance(city, dict)
    }
