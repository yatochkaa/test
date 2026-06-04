import requests
import os

PARTSAPI_KEY = os.getenv("PARTSAPI_KEY")
BASE_URL = "https://api.partsapi.ru"

def get_parts_by_vin(vin: str, cat: str, type_: str = "oem") -> dict | None:
    """Запрос 1 — OEM артикулы по VIN и категории"""
    params = {
        "method": "getPartsbyVIN",
        "key": PARTSAPI_KEY,
        "vin": vin,
        "cat": cat,
        "type": type_
    }
    resp = requests.get(BASE_URL, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()

def get_crosses(brand: str, article: str) -> dict | None:
    """Запрос 2 — аналоги по бренду и артикулу"""
    params = {
        "method": "getCrosses",
        "key": PARTSAPI_KEY,
        "brand": brand,
        "article": article
    }
    resp = requests.get(BASE_URL, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()