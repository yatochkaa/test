# -*- coding: utf-8 -*-
"""
TecDoc-слой подбора OEM для test_vin_bot.

Идея: настоящий carId из TecDoc — основа подбора OEM. carId мы получаем
НАПРЯМУЮ из VINdecode (shop/21): этот метод по VIN отдаёт carId + марку,
модель, модификацию и год. getMakes/getModels/getCars нужны только как
РЕЗЕРВ — и срабатывают лишь когда бот не под нагрузкой.

Цепочка:
    VIN -> carId      (resolve_car_id)
        Основной путь:  VINdecode(vin) -> carId напрямую (+ manuId/modId/год)
        Резерв (по нагрузке): manuId+modId -> getCars -> carId по году
                              либо manuName+modelName -> getMakes/getModels/getCars
    carId + cat_id -> strId   (resolve_str_id)
        1) явный CAT_TO_STR_ID  2) динамический матч по дереву getSearchTree
    carId + strId -> [(brand, article), ...]  (get_oem_articles)

Каждый метод partsapi использует СВОЙ ключ (см. .env).
"""
from __future__ import annotations

import os
import re
import json
import asyncio
import logging
from typing import Any, Optional, Callable

import aiohttp

log = logging.getLogger("tecdoc")

BASE_URL = "https://api.partsapi.ru"
# --- ВАЖНО: разные методы partsapi требуют РАЗНЫЕ типы параметров! ---
# По официальной документации partsapi.ru:
#   VINdecode:     lang = String(2)  -> 2-буквенный код "ru"/"en"
#   getMakes:      carType = String(25)
#   getSearchTree: lang = ЧИСЛО (16=ru), carId = Integer, carType = "PC" (строка!)
#   getArticles:   lang = ЧИСЛО (16=ru), carId = Integer, carType = "PC" (строка!)
# Поэтому глобальный lang="ru" ЛОМАЛ getSearchTree/getArticles (они ждут ЧИСЛО
# языка) и сервер отдавал HTTP 500. А lang=16 ломал VINdecode (он ждёт буквенный
# код). ПРОВЕРЕНО в браузере: ...&lang=16&carId=...&carType=PC -> дерево/артикулы
# приходят. То есть carType остаётся СТРОКОЙ "PC", меняется только язык.
# Вывод: язык задаём ОТДЕЛЬНО ДЛЯ КАЖДОГО метода, carType = "PC".
# Здесь смешение строк/чисел НЕ страшно — они уходят в URL query как строки.

# Язык-код для VINdecode (String): "ru"/"en".
LANG_CODE = os.getenv("PARTSAPI_LANG_CODE", "ru")
# Числовой id языка для TecDoc-методов (getMakes/Models/Cars/SearchTree/Articles).
# 16 = русский в TecDoc.
LANG_ID = os.getenv("PARTSAPI_LANG_ID", "16")
# Язык по методу. Неизвестный метод -> буквенный код (как VINdecode).
_LANG_BY_METHOD = {
    "VINdecode": LANG_CODE,
    "getMakes": LANG_ID,
    "getModels": LANG_ID,
    "getCars": LANG_ID,
    "getSearchTree": LANG_ID,
    "getArticles": LANG_ID,
    "getArticle": LANG_ID,
}

# carType-строка для getMakes/getModels/getCars (резервный путь).
CAR_TYPE = os.getenv("PARTSAPI_CAR_TYPE", "PC")
# carType для getSearchTree/getArticles. Рабочее значение = "PC" (проверено
# в браузере). На всякий случай оставляем перебор кандидатов: сначала "PC",
# затем числовые 1/2/3 — вдруг для каких-то ТС нужен другой код. Первый
# успешный запомнится. Можно переопределить через .env.
_DEFAULT_CAR_TYPE_ID = os.getenv("PARTSAPI_CAR_TYPE_ID", "PC")
_CAR_TYPE_ID_CANDIDATES = os.getenv("PARTSAPI_CAR_TYPE_ID_CANDIDATES", "PC,1,2,3")
# Подтверждённый рабочий числовой carType (заполняется при первом успехе).
_WORKING_CAR_TYPE_ID: Optional[str] = None

API_DELAY = float(os.getenv("API_DELAY", "0.5"))
_MAX_RETRIES = 2
_TIMEOUT = 20.0

# --- ключи: у каждого метода свой ключ ---
# ВАЖНО: PARTSAPI_KEY_VINDECODE21 — это ключ метода VINdecode (shop/21),
# который отдаёт carId. Это ДРУГОЙ продукт, не VINdecodeOE (shop/64,
# который лежит у тебя в PARTSAPI_KEY_VINDECODE и carId НЕ отдаёт).
# Имя метода -> имя переменной окружения с его ключом.
_KEY_ENV = {
    "VINdecode": "PARTSAPI_KEY_VINDECODE21",
    "getMakes": "PARTSAPI_KEY_MAKES",
    "getModels": "PARTSAPI_KEY_MODELS",
    "getCars": "PARTSAPI_KEY_CARS",
    "getSearchTree": "PARTSAPI_KEY_TREE",
    "getArticles": "PARTSAPI_KEY_ARTICLES",
    # getArticle (ед. число) — детали по ОДНОМУ артикулу: OEM-номера, точное
    # название, характеристики (высота/диаметр/резьба), чем заменён. Свой ключ.
    "getArticle": "PARTSAPI_KEY_GETARTICLE",
}

# Пытаемся прочитать ключи сразу при импорте. НО если import tecdoc
# выполнился РАНЬШЕ load_dotenv() (частый случай), здесь будет пусто —
# поэтому ключ всё равно дочитывается лениво в get_key() при первом запросе.
KEYS = {m: os.getenv(env, "") for m, env in _KEY_ENV.items()}


def get_key(method: str) -> str:
    """Ключ метода. Сначала из KEYS (в т.ч. подменённый тестами),
    иначе лениво из .env — на случай, если load_dotenv() вызвали после
    import tecdoc."""
    k = KEYS.get(method) or ""
    if not k:
        env = _KEY_ENV.get(method)
        if env:
            k = os.getenv(env, "")
            if k:
                KEYS[method] = k  # запомним, чтобы не читать каждый раз
    return k

# Разрешить резервный путь make/model/cars (когда VINdecode не дал carId).
# По умолчанию включено, НО срабатывает только если бот не под нагрузкой
# (см. _under_load). Полностью выключить: USE_MAKE_MODEL_FALLBACK=0.
USE_MAKE_MODEL_FALLBACK = os.getenv("USE_MAKE_MODEL_FALLBACK", "1") == "1"

# Порог нагрузки: если одновременно в работе больше N резолвов — резерв
# make/model/cars пропускается (он дорогой: до 3 доп. запросов).
_FALLBACK_MAX_INFLIGHT = int(os.getenv("TECDOC_FALLBACK_MAX_INFLIGHT", "2"))

# Счётчик одновременных resolve_oem (грубая оценка нагрузки на бота).
_INFLIGHT = 0

# Необязательный внешний детектор нагрузки: bool-функция, True = бот занят.
# Бот может подключить свой (по очереди задач, RPS и т.п.) через set_load_hook.
_load_hook: Optional[Callable[[], bool]] = None


def set_load_hook(fn: Optional[Callable[[], bool]]) -> None:
    """Подключить внешний детектор нагрузки. fn() -> True, если бот под нагрузкой.
    Если задан — имеет приоритет над встроенным счётчиком _INFLIGHT.
    """
    global _load_hook
    _load_hook = fn


def _under_load() -> bool:
    """True -> резервный путь make/model/cars пропускаем."""
    if _load_hook is not None:
        try:
            return bool(_load_hook())
        except Exception:
            return False
    return _INFLIGHT > _FALLBACK_MAX_INFLIGHT


# ---------------------------------------------------------------------------
# CAT_TO_STR_ID — ручной маппинг "внутренний cat_id" -> "strId дерева TecDoc".
# ВНИМАНИЕ: cat_id (7, 8, 281...) из PARTS_MAP — это категории getPartsbyVIN,
# они НЕ равны strId дерева getSearchTree. Реальные strId надо один раз снять
# командой /debug tree <VIN> и вписать сюда. Пока словарь пуст — strId ищется
# динамически по названию узла дерева (resolve_str_id, матч по part_name).
# ---------------------------------------------------------------------------
CAT_TO_STR_ID: dict[str, int] = {
    # "7": 100002,    # масляный фильтр  (пример — заполнить реальными значениями)
    # "8": 100001,    # воздушный фильтр
    # "281": 100345,  # тормозные колодки перед
}

# ---------------------------------------------------------------------------
# Кэш: по умолчанию внутренний dict; можно подключить кэш бота через set_cache_hooks
# ---------------------------------------------------------------------------
_CACHE: dict[str, Any] = {}
_cache_get: Callable[[str], Any] = lambda k: _CACHE.get(k)
_cache_set: Callable[[str, Any], None] = lambda k, v: _CACHE.__setitem__(k, v)


def set_cache_hooks(get_fn: Callable[[str], Any], set_fn: Callable[[str, Any], None]) -> None:
    """Подключить кэш бота (cache_get/cache_set), чтобы переиспользовать cache.json."""
    global _cache_get, _cache_set
    _cache_get, _cache_set = get_fn, set_fn


# ---------------------------------------------------------------------------
# Низкоуровневый GET с ретраями, rate-limit и классификацией ошибок partsapi
# ---------------------------------------------------------------------------
async def _request(session, method: str, params: Optional[dict] = None,
                   *, use_cache: bool = True, max_retries: int = _MAX_RETRIES) -> dict:
    """Вернёт {"_ok": True, "data": ...} либо {"_error": CODE, ...}."""
    key = get_key(method)
    if not key:
        return {"_error": "NO_KEY", "_method": method}

    q = {"method": method, "key": key, "lang": _LANG_BY_METHOD.get(method, LANG_CODE)}
    if params:
        # aiohttp не принимает None в params -> кладём только заполненные значения
        q.update({k: str(v) for k, v in params.items() if v is not None})

    cache_key = "tecdoc:" + method + ":" + ",".join(
        f"{k}={v}" for k, v in sorted(q.items()) if k != "key")
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {"_ok": True, "data": cached, "_cached": True, "_method": method}

    last_err = None
    for attempt in range(max_retries + 1):
        try:
            if API_DELAY:
                await asyncio.sleep(API_DELAY)
            timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
            async with session.get(BASE_URL, params=q, timeout=timeout) as resp:
                status = resp.status
                text = await resp.text()

                if status == 404:
                    return {"_error": "NOT_FOUND", "_status": 404, "_method": method}
                if status in (401, 403):
                    # различим rate-limit (5000) и реальную авторизацию
                    code = _error_code_from_text(text)
                    if code == 5000:
                        return {"_error": "RATE_LIMIT", "_status": status, "_method": method}
                    return {"_error": "AUTH", "_status": status, "_method": method}
                if status >= 500:
                    last_err = f"HTTP {status}"
                    continue  # 5xx -> ретрай

                try:
                    data = json.loads(text)
                except Exception:
                    return {"_error": "JSON_ERROR", "_raw": text[:300], "_method": method}

                # partsapi иногда отдаёт ошибку телом при 200
                if isinstance(data, dict) and data.get("error_code"):
                    ec = data.get("error_code")
                    if ec == 5000:
                        return {"_error": "RATE_LIMIT", "_method": method}
                    if ec == 5007:
                        last_err = "SERVER_ERROR(5007)"
                        continue
                    return {"_error": "API_ERROR", "_code": ec,
                            "_msg": data.get("message"), "_method": method}

                if use_cache:
                    _cache_set(cache_key, data)
                return {"_ok": True, "data": data, "_status": status, "_method": method}

        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            last_err = f"{type(e).__name__}: {e}"
            continue

    return {"_error": "TIMEOUT_OR_5XX", "_detail": last_err, "_method": method}


def _error_code_from_text(text: str) -> Optional[int]:
    try:
        d = json.loads(text)
        return d.get("error_code") if isinstance(d, dict) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Хелперы парсинга разнородных полей ответа
# ---------------------------------------------------------------------------
def _as_list(data: Any) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("items", "data", "result", "results", "list"):
            v = data.get(k)
            if isinstance(v, list):
                return v
            # partsapi: {"result": {"0": {...}, "1": {...}}} -> список объектов
            if isinstance(v, dict):
                vals = [x for x in v.values() if isinstance(x, dict)]
                if vals:
                    return vals
        # одиночный объект -> список из одного
        return [data]
    return []


def _first_obj(data: Any) -> Optional[dict]:
    """Первый словарь из ответа (list или dict)."""
    if isinstance(data, dict):
        # ответ может быть обёрнут в items/data/result
        for k in ("items", "data", "result", "results", "list"):
            v = data.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v[0]
            # partsapi: {"result": {"0": {...}}} -> первый объект-значение
            if isinstance(v, dict):
                for vv in v.values():
                    if isinstance(vv, dict):
                        return vv
        return data
    if isinstance(data, list):
        for it in data:
            if isinstance(it, dict):
                return it
    return None


def _to_int(v: Any) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _pick(d: dict, *names: str) -> Any:
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    # без учёта регистра
    low = {k.lower(): v for k, v in d.items()}
    for n in names:
        if n.lower() in low and low[n.lower()] not in (None, ""):
            return low[n.lower()]
    return None


def _norm(s: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def _year(v: Any) -> Optional[int]:
    """Достаёт 4-значный год из стро��������и/числа (yearOfConstrFrom, yearStart и т.п.)."""
    if v is None:
        return None
    m = re.search(r"(19|20)\d{2}", str(v))
    return int(m.group(0)) if m else None


def _all_objs(data: Any) -> list:
    """Все словари-объекты из ответа (list / dict / обёртки items/result/...).
    В отличие от _first_obj возвращает ВСЕ варианты — нужно Слою 1, когда
    VINdecode отдаёт несколько модификаций авто (разные двигатели)."""
    if isinstance(data, list):
        return [it for it in data if isinstance(it, dict)]
    if isinstance(data, dict):
        for k in ("items", "data", "result", "results", "list"):
            v = data.get(k)
            if isinstance(v, list):
                objs = [it for it in v if isinstance(it, dict)]
                if objs:
                    return objs
            if isinstance(v, dict):
                objs = [vv for vv in v.values() if isinstance(vv, dict)]
                if objs:
                    return objs
        return [data]
    return []


def _engine_code(info: Any) -> Optional[str]:
    """Код двигателя из VINdecode (motorCode/engineCode/...), нормализованный.
    Если пришёл список 'AEB, ANB' — берём первый."""
    if not isinstance(info, dict):
        return None
    raw = _pick(info, "motorCode", "engineCode", "motor", "engine",
                "motorCodes", "engineCodes", "kod_dvigatelya", "engineNumber")
    if not raw:
        return None
    first = re.split(r"[,;/ ]+", str(raw).strip())[0]
    return _norm(first) or None


def _pick_car_by_engine(objs: list, engine_code: Optional[str]) -> Optional[int]:
    """Слой 1: из вариантов авто выбирает carId, чей код двигателя совпадает с
    engine_code. Нет совпадений / код неизвестен -> None (вызывающий код берёт
    первый carId, поведение как раньше — безопасно)."""
    if not engine_code:
        return None
    for o in objs:
        if not isinstance(o, dict):
            continue
        cid = _to_int(_pick(o, "carId", "carID", "car_id"))
        if cid and _engine_code(o) == engine_code:
            return cid
    return None


# ---------------------------------------------------------------------------
# Низкоуровневые обёртки методов
# ---------------------------------------------------------------------------
async def vindecode(session, vin: str) -> dict:
    """VINdecode (shop/21): VIN -> carId + марка/модель/модификация/год."""
    return await _request(session, "VINdecode", {"vin": vin})


async def get_makes(session) -> dict:
    return await _request(session, "getMakes", {"carType": CAR_TYPE})


async def get_models(session, make_id: int) -> dict:
    return await _request(session, "getModels", {"makeId": make_id, "carType": CAR_TYPE})


async def get_cars(session, make_id: int, model_id: int) -> dict:
    return await _request(session, "getCars",
                          {"makeId": make_id, "modelId": model_id, "carType": CAR_TYPE})


def _is_success_payload(data: Any) -> bool:
    """Похоже ли на УСПЕШНЫЙ непустой ответ partsapi. Нужно, чтобы отличить
    рабочий carType от неподходящего: на неверный тип partsapi отдаёт
    {"result":{},"statusMsg":"Failed"} либо 5xx."""
    if isinstance(data, list):
        return len(data) > 0
    if isinstance(data, dict):
        msg = str(data.get("statusMsg", "")).strip().lower()
        if msg in ("failed", "error", "fail"):
            return False
        res = data.get("result")
        if isinstance(res, (list, dict)):
            return len(res) > 0
        if res is not None:
            return True
        # нет поля result -> успех, если есть иные содержательные ключи
        return any(k not in ("statusMsg", "status") for k in data.keys())
    return False


def _car_type_id_candidates() -> list:
    """Кандидаты числового carType для getSearchTree/getArticles: сначала уже
    подтверждённый рабочий, затем дефолт и список из .env."""
    seq = []
    if _WORKING_CAR_TYPE_ID:
        seq.append(_WORKING_CAR_TYPE_ID)
    seq.append(_DEFAULT_CAR_TYPE_ID)
    seq += [x.strip() for x in _CAR_TYPE_ID_CANDIDATES.split(",") if x.strip()]
    out = []
    for x in seq:
        if x and x not in out:
            out.append(x)
    return out


async def get_search_tree(session, car_id: int) -> dict:
    """getSearchTree: carType = "PC" (строка, проверено в браузере),
    lang — число (16). На всякий случай перебираем кандидатов (PC, затем
    числовые) и запоминаем сработавший в _WORKING_CAR_TYPE_ID."""
    global _WORKING_CAR_TYPE_ID
    last = {"_error": "NO_CAR_TYPE", "_method": "getSearchTree"}
    for ct in _car_type_id_candidates():
        r = await _request(session, "getSearchTree", {"carId": car_id, "carType": ct})
        last = r
        if r.get("_ok") and _is_success_payload(r.get("data")):
            _WORKING_CAR_TYPE_ID = ct
            r["_carType"] = ct
            return r
    return last


async def get_articles(session, car_id: int, str_id: int) -> dict:
    """getArticles: carType тоже "PC". Берём подтверждённый getSearchTree-тип,
    иначе перебираем кандидатов."""
    last = {"_error": "NO_CAR_TYPE", "_method": "getArticles"}
    for ct in _car_type_id_candidates():
        r = await _request(session, "getArticles",
                           {"carId": car_id, "strId": str_id, "carType": ct})
        last = r
        if r.get("_ok") and _is_success_payload(r.get("data")):
            r["_carType"] = ct
            return r
    return last


# ---------------------------------------------------------------------------
# carId
# ---------------------------------------------------------------------------
async def _car_id_via_cars(session, make_id: int, model_id: int,
                           year: Optional[int]) -> Optional[int]:
    """Резерв (дёшево): makeId+modelId уже известны из VINdecode -> getCars -> carId по году."""
    if not make_id or not model_id:
        return None
    r = await get_cars(session, make_id, model_id)
    if not r.get("_ok"):
        return None
    return _pick_car_by_year(_as_list(r["data"]), year)


async def _car_id_via_make_model(session, brand: str, modely: str,
                                 year: Optional[int]) -> Optional[int]:
    """Резерв (дорого): по НАЗВАНИЯМ brand+model -> makeId -> modelId -> carId (по году)."""
    if not brand:
        return None
    r = await get_makes(session)
    if not r.get("_ok"):
        return None
    make_id = _match_id(_as_list(r["data"]), brand,
                        ("MFA_BRAND", "makeName", "name"), ("MFA_ID", "makeId", "id"))
    if make_id is None:
        return None

    r = await get_models(session, make_id)
    if not r.get("_ok"):
        return None
    model_id = _match_id(_as_list(r["data"]), modely,
                         ("modelName", "name"), ("modelId", "id"))
    if model_id is None:
        return None

    r = await get_cars(session, make_id, model_id)
    if not r.get("_ok"):
        return None
    return _pick_car_by_year(_as_list(r["data"]), year)


def _match_id(rows: list, query: str, name_keys: tuple, id_keys: tuple) -> Optional[int]:
    """Точное совпадение по нормализованному имени, иначе вхождение подстроки."""
    target = _norm(query)
    if not target:
        return None
    partial = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _norm(_pick(row, *name_keys))
        rid = _to_int(_pick(row, *id_keys))
        if rid is None or not name:
            continue
        if name == target:
            return rid
        if partial is None and (target in name or name in target):
            partial = rid
    return partial


def _pick_car_by_year(cars: list, year: Optional[int]) -> Optional[int]:
    if not cars:
        return None
    if year is None:
        # без года берём первую модификацию
        return _to_int(_pick(cars[0], "carId", "id"))
    for c in cars:
        if not isinstance(c, dict):
            continue
        ys = _year(_pick(c, "yearStart", "constructedFrom", "from"))
        ye = _year(_pick(c, "yearEnd", "constructedTo", "to"))
        cid = _to_int(_pick(c, "carId", "id"))
        if cid is None:
            continue
        if (ys is None or ys <= year) and (ye is None or year <= ye):
            return cid
    # год не попал в диапазоны -> первая модификация
    return _to_int(_pick(cars[0], "carId", "id"))


async def resolve_car_id(session, vin: str) -> dict:
    """Главная точка: VIN -> carId.

    Основной путь: VINdecode(vin) -> carId напрямую.
    Резерв (make/model/cars) — только если VINdecode н�� дал carId,
    включён флагом USE_MAKE_MODEL_FALLBACK И бот не под нагрузкой (_under_load).

    Возвращает {"car_id", "source", "candidates", "car_str", "brand"}.
    """
    out = {"car_id": None, "source": None, "candidates": [],
           "car_str": None, "brand": None, "engine_code": None}

    brand = modely = type_name = None
    manu_id = mod_id = None
    year = None

    vd = await vindecode(session, vin)
    if vd.get("_ok"):
        objs = _all_objs(vd["data"])
        info = objs[0] if objs else None
        if isinstance(info, dict):
            manu_id = _to_int(_pick(info, "manuId", "makeId"))
            mod_id = _to_int(_pick(info, "modId", "modelId"))
            brand = _pick(info, "manuName", "makeName", "brand", "brend")
            modely = _pick(info, "modelName", "modely")
            type_name = _pick(info, "typeName", "carName")
            year = _year(_pick(info, "yearOfConstrFrom", "yearOfConstr",
                               "year", "god", "data_vypuska"))
            engine_code = _engine_code(info)
            out["engine_code"] = engine_code
            car_str = " ".join(str(x) for x in (brand, modely, type_name) if x) or None

            # все варианты авто с carId (VINdecode иногда отдаёт несколько
            # модификаций — различаются кодом двигателя)
            cand = []
            for o in objs:
                cid = _to_int(_pick(o, "carId", "carID", "car_id"))
                if cid and cid not in cand:
                    cand.append(cid)

            # Слой 1: если вариантов несколько — берём по коду двигателя,
            # иначе первый carId (как раньше).
            car_id = _pick_car_by_engine(objs, engine_code) \
                or _to_int(_pick(info, "carId", "carID", "car_id"))

            if car_id:
                out.update({"car_id": car_id, "candidates": cand or [car_id],
                            "source": "VINdecode", "car_str": car_str,
                            "brand": brand or None})
                return out
            # carId не пришёл — запомним строку авто для резерва/вывода
            out["car_str"] = car_str
            out["brand"] = brand or None

    # --- Резерв make/model/cars: только без нагрузки ---
    if USE_MAKE_MODEL_FALLBACK and not _under_load():
        cid = None
        # дёшево: makeId+modelId уже есть из VINdecode
        if manu_id and mod_id:
            cid = await _car_id_via_cars(session, manu_id, mod_id, year)
            if cid:
                out.update({"car_id": cid, "candidates": [cid],
                            "source": "VINdecode+getCars"})
                return out
        # дорого: по названиям бренда/модели
        if brand and modely:
            cid = await _car_id_via_make_model(session, brand, modely, year)
            if cid:
                out.update({"car_id": cid, "candidates": [cid],
                            "source": "VINdecode+make/model"})
                return out
    return out


def _car_str_from_row(row: Any) -> Optional[str]:
    if not isinstance(row, dict):
        return None
    name = _pick(row, "carName", "name", "modelName")
    return str(name) if name else None


# ---------------------------------------------------------------------------
# strId (узел дерева getSearchTree)
# ---------------------------------------------------------------------------
def _flatten_tree(node: Any, acc: list) -> None:
    """Рекурсивно собирает (strId, name) из произвольной структуры дерева."""
    if isinstance(node, list):
        for n in node:
            _flatten_tree(n, acc)
        return
    if isinstance(node, dict):
        sid = _to_int(_pick(node, "STR_ID", "strId", "id", "nodeId", "assemblyGroupNodeId"))
        # Имя узла. У partsapi реальные ключи: STR_NODE_NAME (англ., "Oil
        # Filter") и STR_PATH (русский путь "Двигатель > Система смазки >
        # Масляный фильтр"). Для поиска по РУССКИМ названиям берём
        # ПОСЛЕДНИЙ сегмент STR_PATH, иначе STR_NODE_NAME / прочее.
        name = None
        path = _pick(node, "STR_PATH", "path")
        if path:
            name = str(path).split(">")[-1].strip()
        if not name:
            name = _pick(node, "STR_NODE_NAME", "name",
                         "assemblyGroupName", "text", "title")
        if sid is not None and name:
            acc.append((sid, str(name)))
        # рекурсивно заходим во ВСЕ вложенные списки/словари,
        # включая обёртки вроде {"result": {"0": {...}}}
        for v in node.values():
            if isinstance(v, (list, dict)):
                _flatten_tree(v, acc)


def _tokens(s: str) -> set:
    return {t for t in re.split(r"[^a-zа-я0-9]+", str(s).lower()) if len(t) > 2}


async def resolve_str_id(session, car_id: int, cat_id: str,
                         part_name: str = "", keywords: Optional[list] = None) -> Optional[int]:
    """cat_id (+ part_name) -> strId дерева для данного авто.

    1) Явный CAT_TO_STR_ID имеет приоритет.
    2) Иначе — ма��ч названия узла по part_name/keywords (пересечение токенов).
    """
    if cat_id in CAT_TO_STR_ID:
        return CAT_TO_STR_ID[cat_id]

    r = await get_search_tree(session, car_id)
    if not r.get("_ok"):
        return None
    nodes: list = []
    _flatten_tree(r["data"], nodes)
    if not nodes:
        return None

    want = _tokens(part_name)
    for kw in (keywords or []):
        want |= _tokens(kw)
    if not want:
        return None

    best_id, best_score = None, 0
    for sid, name in nodes:
        score = len(want & _tokens(name))
        if score > best_score:
            best_id, best_score = sid, score
    return best_id


async def debug_search_tree(session, car_id: int) -> str:
    """Человекочитаемый дамп дерева — чтобы вручную заполнить CAT_TO_STR_ID.
    При ошибке показывает максимум диагностики (какие carType пробовали и что
    вернул сервер) — это помогает понять причину прямо из /debugtree."""
    r = await get_search_tree(session, car_id)
    if not r.get("_ok"):
        return (f"getSearchTree error: {r.get('_error')}"
                f" | detail={r.get('_detail')}"
                f" | carType пробовали={_car_type_id_candidates()}"
                f" | lang={_LANG_BY_METHOD.get('getSearchTree')}")
    nodes: list = []
    _flatten_tree(r["data"], nodes)
    if not nodes:
        raw = json.dumps(r.get("data"), ensure_ascii=False)[:300]
        return (f"Дерево пустое/формат не распознан. carType={r.get('_carType')}, "
                f"lang={_LANG_BY_METHOD.get('getSearchTree')}\nОтвет: {raw}")
    lines = [f"{sid}\t{name}" for sid, name in nodes[:200]]
    head = f"OK carType={r.get('_carType')}, узлов={len(nodes)}\n"
    return head + "strId\tname\n" + "\n".join(lines)


async def debug_articles_by_vin(session, vin: str, str_id: int) -> str:
    """Диагностика шага getArticles: VIN + strId -> список OEM-артикулов.
    Сам резолвит carId из VIN, чтобы команда бота была максимально простой.
    При ошибке показывает диагностику (какой carType/lang, сырой ответ)."""
    info = await resolve_car_id(session, vin)
    car_id = info.get("car_id")
    if not car_id:
        return (f"carId не получен (source={info.get('source')}). "
                f"VINdecode не вернул carId — проверь VIN/ключ.")
    r = await get_articles(session, car_id, str_id)
    if not r.get("_ok"):
        return (f"getArticles error: {r.get('_error')} | detail={r.get('_detail')}"
                f" | carType пробовали={_car_type_id_candidates()}"
                f" | lang={_LANG_BY_METHOD.get('getArticles')}")
    parts = get_oem_articles_from_payload(r["data"])
    if not parts:
        raw = json.dumps(r.get("data"), ensure_ascii=False)[:300]
        return (f"Артикулы пустые/формат не распознан. carId={car_id}, "
                f"strId={str_id}, carType={r.get('_carType')}\nОтвет: {raw}")
    head = (f"OK carId={car_id} ({info.get('source')}), strId={str_id}, "
            f"carType={r.get('_carType')}, артикулов={len(parts)}\n")
    lines = [f"{b}\t{a}" for b, a in parts[:200]]
    return head + "brand\tarticle\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# OEM-артикулы
# ---------------------------------------------------------------------------
def get_oem_articles_from_payload(data: Any) -> list:
    """Разбор ответа getArticles -> [(brand, article)], дедуп по норм.
    Артикулу. Реальные ключи partsapi: ART_SUP_BRAND / ART_ARTICLE_NR."""
    out, seen = [], set()
    for it in _as_list(data):
        if not isinstance(it, dict):
            continue
        brand = _pick(it, "ART_SUP_BRAND", "brandName", "brand",
                      "manuName", "supplierName")
        art = _pick(it, "ART_ARTICLE_NR", "articleNumber", "article",
                    "number", "oem")
        if not brand or not art:
            continue
        key = (_norm(brand), _norm(art))
        if key in seen:
            continue
        seen.add(key)
        out.append((str(brand), str(art)))
    return out

async def get_oem_articles(session, car_id: int, str_id: int) -> list:
    """getArticles -> список (brand, article), дедуп по нормализованному артикулу."""
    r = await get_articles(session, car_id, str_id)
    if not r.get("_ok"):
        return []
    return get_oem_articles_from_payload(r["data"])


# ===========================================================================
# getArticle (ед. число): OEM-номер + точное название + характеристики + замена
# ---------------------------------------------------------------------------
# Цель: показывать НЕ "все 261 подряд", а оригинальный OEM-номер и короткий
# топ точно подходящих аналогов, с отсевом "не того" размера. Вход getArticle:
# ART_NUM (= ART_ARTICLE_NR из getArticles), SUP_ID (= SUP_ID из getArticles),
# LANG=16. Поэтому из getArticles нам нужен SUP_ID — его сохраняет
# get_article_rows_from_payload (ниже), в отличие от "плоского" парсера выше.
# ===========================================================================

# Премиальные/ОЕ-бренды: их показываем в первую очередь. Можно переопределить
# через .env PARTSAPI_PREMIUM_BRANDS="MANN,MAHLE,...".
_DEFAULT_PREMIUM_BRANDS = (
    "MANN,MANN-FILTER,MAHLE,KNECHT,BOSCH,HENGST,FILTRON,FEBI,FEBI BILSTEIN,"
    "MEYLE,SACHS,LEMFORDER,LEMFÖRDER,TRW,ATE,VALEO,NGK,NTK,SKF,INA,LUK,"
    "CONTITECH,GATES,ELRING,VICTOR REINZ,RUVILLE,VAICO,SWAG,UFI,PURFLUX,"
    "BLUE PRINT,JAPANPARTS,DENSO,HELLA,BREMBO,TEXTAR,ZIMMERMANN"
)
PREMIUM_BRANDS = [
    _norm(b) for b in os.getenv("PARTSAPI_PREMIUM_BRANDS", _DEFAULT_PREMIUM_BRANDS).split(",")
    if b.strip()
]


def get_article_rows_from_payload(data: Any) -> list:
    """Как get_oem_articles_from_payload, но СОХРАНЯЕТ sup_id/art_id (нужны для
    getArticle). -> [{"brand","article","sup_id","art_id"}], дедуп по артикулу."""
    out, seen = [], set()
    for it in _as_list(data):
        if not isinstance(it, dict):
            continue
        brand = _pick(it, "ART_SUP_BRAND", "brandName", "brand",
                      "manuName", "supplierName")
        art = _pick(it, "ART_ARTICLE_NR", "articleNumber", "article",
                    "number", "oem")
        if not brand or not art:
            continue
        key = (_norm(brand), _norm(art))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "brand": str(brand),
            "article": str(art),
            "sup_id": _pick(it, "SUP_ID", "supId", "supplierId", "ART_SUP_ID"),
            "art_id": _pick(it, "ART_ID", "artId", "articleId"),
        })
    return out


def _is_premium(brand: Any) -> bool:
    nb = _norm(brand)
    return any(nb == pb or (pb and pb in nb) for pb in PREMIUM_BRANDS)


# OE-/премиум-«первый эшелон»: обогащаем в первую очередь, чтобы при малом
# бюджете запросов (тест) попали именно авторитетные фильтр-бренды (MANN/MAHLE).
# Переопределяется через .env PARTSAPI_OE_TIER_BRANDS.
_DEFAULT_OE_TIER_BRANDS = ("MANN,MANN-FILTER,MAHLE,KNECHT,HENGST,BOSCH,FILTRON,"
                           "UFI,PURFLUX,MEYLE,SOFIMA,WIX")
OE_TIER_BRANDS = [
    _norm(b) for b in os.getenv("PARTSAPI_OE_TIER_BRANDS",
                                _DEFAULT_OE_TIER_BRANDS).split(",") if b.strip()
]


def _is_oe_tier(brand: Any) -> bool:
    nb = _norm(brand)
    return any(nb == pb or (pb and pb in nb) for pb in OE_TIER_BRANDS)


def rank_article_rows(rows: list) -> list:
    """Порядок обогащения: сначала OE-эшелон (MANN/MAHLE/...), затем прочие
    премиальные, затем остальное. sorted стабильна — внутри эшелона
    порядок исходный."""
    def _tier(r):
        b = r.get("brand")
        return 0 if _is_oe_tier(b) else (1 if _is_premium(b) else 2)
    return sorted(rows, key=_tier)


def _as_text_list(v: Any) -> list:
    """Любой вход (str / list[str] / list[dict] / dict) -> плоский список строк.
    Для объектов вытаскивает номер (+бренд, если есть). Дедуп с сохранением
    порядка."""
    raw = []
    if v is None:
        return raw
    if isinstance(v, str):
        raw = [p.strip() for p in re.split(r"[,;\n]\s*", v) if p.strip()]
    else:
        items = v if isinstance(v, list) else ([v] if isinstance(v, dict) else [])
        for it in items:
            if isinstance(it, str):
                if it.strip():
                    raw.append(it.strip())
            elif isinstance(it, dict):
                num = _pick(it, "OEM_NR", "ARO_OEM_NR", "oemNr", "oemNumber",
                            "number", "ARTICLE_OEM_NR", "value", "nr")
                brand = _pick(it, "OEM_BRAND", "ARO_OEM_BRAND", "brandName",
                              "mfrName", "brand", "manuName")
                if num:
                    raw.append(f"{brand} {num}".strip() if brand else str(num))
    seen, res = set(), []
    for x in raw:
        k = _norm(x)
        if k and k not in seen:
            seen.add(k)
            res.append(x)
    return res


def _extract_criteria(v: Any) -> dict:
    """ARTICLE_CRITERIA -> {название: значение(+ед.)}. Терпим к разным ключам."""
    crit: dict = {}
    # partsapi реально отдаёт criteria СТРОКОЙ:
    # "Высота [мм]: 79; Внешний диаметр [мм]: 76; Размер резьбы: M20 x 1.5; ..."
    if isinstance(v, str):
        for part in v.split(";"):
            part = part.strip()
            if not part or ":" not in part:
                continue
            name, val = part.split(":", 1)
            name, val = name.strip(), val.strip()
            if name and val:
                crit[name] = val
        return crit
    items = v if isinstance(v, list) else ([v] if isinstance(v, dict) else [])
    for it in items:
        if not isinstance(it, dict):
            continue
        name = _pick(it, "CRITERIA_NAME", "CRI_DESCRIPTION", "criteriaName",
                     "ACR_DESCRIPTION", "ART_CRITERIA_NAME", "name")
        val = _pick(it, "CRITERIA_VALUE", "CRI_VALUE", "criteriaValue",
                    "ACR_VALUE", "ART_CRITERIA_VALUE", "value")
        unit = _pick(it, "CRITERIA_UNIT", "CRI_UNIT", "ACR_UNIT", "unit")
        if name and val not in (None, ""):
            txt = str(val)
            if unit:
                txt = f"{txt} {unit}".strip()
            crit[str(name)] = txt
    return crit


def parse_article(data: Any) -> dict:
    """Разбор ответа getArticle -> структурированные детали по артикулу.
    Реальные ключи partsapi: OEM_NUMBERS, ART_PRODUCT_NAME, ARTICLE_CRITERIA,
    'SUPERSEDED BY' (с пробелом!), EAN_NUMBERS, ART_STATUS_TEXT."""
    obj = _first_obj(data) or {}
    if not isinstance(obj, dict):
        obj = {}
    return {
        "product_name": _pick(obj, "ART_PRODUCT_NAME", "productName",
                              "GA_PRODUCT_NAME", "genericArticleName", "name") or "",
        "oem_numbers": _as_text_list(_pick(obj, "OEM_NUMBERS", "oemNumbers",
                              "OEM", "ART_OEM_NR", "oem")),
        "criteria": _extract_criteria(_pick(obj, "ARTICLE_CRITERIA",
                              "articleCriteria", "CRITERIA", "ART_CRITERIA")),
        "superseded_by": _as_text_list(_pick(obj, "SUPERSEDED BY",
                              "SUPERSEDED_BY", "supersededBy", "SUCCESSOR")),
        "ean": _as_text_list(_pick(obj, "EAN_NUMBERS", "eanNumbers", "EAN")),
        "status": _pick(obj, "ART_STATUS_TEXT", "statusText", "ART_STATUS") or "",
    }


async def get_article(session, art_num: str, sup_id: Any,
                      *, use_cache: bool = True) -> dict:
    """getArticle: детали ОДНОГО артикула. Вход ART_NUM + SUP_ID (+LANG).
    Возвращает обычный _request-словарь {"_ok":True,"data":...} | {"_error":...}."""
    if not art_num or sup_id in (None, ""):
        return {"_error": "BAD_ARGS", "_method": "getArticle"}
    return await _request(
        session, "getArticle",
        {"ART_NUM": art_num, "SUP_ID": sup_id,
         "LANG": _LANG_BY_METHOD.get("getArticle", LANG_ID)},
        use_cache=use_cache,
    )


async def get_article_details(session, art_num: str, sup_id: Any) -> Optional[dict]:
    """getArticle -> распарсенные детали (parse_article) или None при ошибке."""
    r = await get_article(session, art_num, sup_id)
    if not r.get("_ok"):
        return None
    return parse_article(r["data"])


async def enrich_articles_with_oem(session, rows: list, *, max_calls: int = 8,
                                   premium_first: bool = True) -> list:
    """Для топ-N строк (из get_article_rows_from_payload) тянет getArticle и
    добавляет ключи oem_numbers/product_name/criteria/superseded_by. Остальные
    строки возвращаются как есть (без деталей). max_calls бережёт лимит запросов."""
    ordered = rank_article_rows(rows) if premium_first else list(rows)
    enriched, calls = [], 0
    for row in ordered:
        row = dict(row)
        if calls < max_calls and row.get("article") and row.get("sup_id") not in (None, ""):
            details = await get_article_details(session, row["article"], row["sup_id"])
            calls += 1
            if details:
                row.update({
                    "oem_numbers": details["oem_numbers"],
                    "product_name": details["product_name"],
                    "criteria": details["criteria"],
                    "superseded_by": details["superseded_by"],
                })
        enriched.append(row)
    return enriched


def build_oem_summary(enriched: list, *, top_n: int = 5,
                      min_oem_support: int = 2) -> dict:
    """Из обогащённых строк собирает компактную сводку для бота:
        {"oem_numbers": [...],          # оригинальные номера, по частоте
         "top": [{brand,article,product_name,criteria}],  # топ аналогов
         "groups": {ключ_размера: [article,...]}}        # отсев "не того" размера
    Группировка по ключевым характеристикам (высота/диаметр/резьба) помогает
    различить два размера одной детали (напр. два масляных фильтра под 1.8T).
    """
    # 1) OEM-номера: считаем частоту по премиальным артикулам, сортируем по ней
    freq: dict[str, int] = {}
    label: dict[str, str] = {}
    for r in enriched:
        for oem in r.get("oem_numbers", []) or []:
            k = _norm(oem)
            if not k:
                continue
            freq[k] = freq.get(k, 0) + 1
            label.setdefault(k, oem)
    oem_sorted = [label[k] for k, _ in sorted(freq.items(),
                  key=lambda kv: (-kv[1], kv[0]))]

    # 2) ЯКОРЬ (Слой 2): OE-номера, общие минимум для N аналогов — это
    #    вероятный настоящий OE машины. Никто не достиг порога -> топ-3 частых.
    anchor_keys = {k for k, c in freq.items() if c >= min_oem_support}
    if not anchor_keys and freq:
        anchor_keys = {k for k, _ in sorted(freq.items(),
                       key=lambda kv: (-kv[1], kv[0]))[:3]}
    anchor_oems = [label[k] for k in sorted(anchor_keys)]

    # 3) ТОЧНО подходящие: аналоги, чьи OEM пересекаются с якорем
    def _matches_anchor(r):
        return any(_norm(o) in anchor_keys for o in (r.get("oem_numbers") or []))
    exact_rows = rank_article_rows(
        [r for r in enriched if r.get("oem_numbers") and _matches_anchor(r)])

    def _clean(r, is_exact):
        return {"brand": r.get("brand"), "article": r.get("article"),
                "product_name": r.get("product_name", ""),
                "criteria": r.get("criteria", {}), "exact": is_exact}
    exact = [_clean(r, True) for r in exact_rows[:top_n]]

    # 4) top = точные сначала, добор премиальными (бот читает top —
    #    улучшается автоматически, без правок cmd_vin)
    seen = {(c["brand"], c["article"]) for c in exact}
    def _score(r):
        return (1 if _is_premium(r.get("brand")) else 0,
                1 if r.get("oem_numbers") else 0)
    top_clean = list(exact)
    for r in sorted(enriched, key=_score, reverse=True):
        if len(top_clean) >= top_n:
            break
        key = (r.get("brand"), r.get("article"))
        if key in seen:
            continue
        seen.add(key)
        top_clean.append(_clean(r, False))

    # 3) группировка по "размеру" (ключевые критерии) — для отсева не того варианта
    _size_keys = ("высот", "диаметр", "резьб", "height", "diameter", "thread")
    groups: dict[str, list] = {}
    for r in enriched:
        # т��лько обогащённые строки, у которых реально есть размерные критерии
        crit = r.get("criteria") or {}
        if not crit:
            continue
        sig_parts = [f"{name}={val}" for name, val in crit.items()
                     if any(sk in name.lower() for sk in _size_keys)]
        if not sig_parts:
            continue  # нет размеров — не можем отнести к конкретному варианту
        sig = "; ".join(sorted(sig_parts))
        groups.setdefault(sig, []).append(r.get("article"))

    return {"oem_numbers": oem_sorted, "anchor_oems": anchor_oems,
            "exact": exact, "top": top_clean, "groups": groups}


async def resolve_oem_detailed(session, vin: str, cat_id: str,
                               part_name: str = "", keywords: Optional[list] = None,
                               *, top_n: int = 5, max_calls: int = 8) -> dict:
    """Полный путь + OEM-детали. Возвращает meta (как resolve_oem) плюс:
        "rows": [{brand,article,sup_id,art_id,...}],   # все артикулы (с sup_id)
        "enriched": [...],                              # топ обогащён getArticle
        "summary": {oem_numbers, top, groups}.
    parts (старый формат (brand,article)) тоже заполняется — для совместимости.
    """
    meta = {"parts": [], "rows": [], "enriched": [], "summary": {},
            "car_id": None, "str_id": None, "source": "tecdoc",
            "reason": None, "car_str": None, "brand": None,
            "engine_code": None}

    car = await resolve_car_id(session, vin)
    meta["car_id"] = car["car_id"]
    meta["car_str"] = car.get("car_str")
    meta["brand"] = car.get("brand")
    meta["engine_code"] = car.get("engine_code")
    if not car["car_id"]:
        meta["reason"] = "no_car_id"
        return meta

    str_id = await resolve_str_id(session, car["car_id"], cat_id, part_name, keywords)
    meta["str_id"] = str_id
    if not str_id:
        meta["reason"] = "no_str_id"
        return meta

    ar = await get_articles(session, car["car_id"], str_id)
    if not ar.get("_ok"):
        meta["reason"] = "no_articles"
        return meta
    rows = get_article_rows_from_payload(ar["data"])
    meta["rows"] = rows
    meta["parts"] = [(r["brand"], r["article"]) for r in rows]
    if not rows:
        meta["reason"] = "no_articles"
        return meta

    enriched = await enrich_articles_with_oem(session, rows, max_calls=max_calls)
    meta["enriched"] = enriched
    meta["summary"] = build_oem_summary(enriched, top_n=top_n)
    return meta


async def debug_article_by_vin(session, vin: str, str_id: int,
                               *, top_n: int = 5) -> str:
    """Диагностика getArticle: VIN + strId -> OEM-номер(а) + топ аналогов.
    Сам резолвит carId, тянет артикулы и обогащает топ через getArticle."""
    info = await resolve_car_id(session, vin)
    car_id = info.get("car_id")
    if not car_id:
        return (f"carId не получен (source={info.get('source')}). "
                f"Проверь VIN/ключ VINdecode.")
    ar = await get_articles(session, car_id, str_id)
    if not ar.get("_ok"):
        return (f"getArticles error: {ar.get('_error')} | detail={ar.get('_detail')}")
    rows = get_article_rows_from_payload(ar["data"])
    if not rows:
        raw = json.dumps(ar.get("data"), ensure_ascii=False)[:300]
        return (f"Артикулы пустые/формат не распознан. carId={car_id}, "
                f"strId={str_id}\nОтвет: {raw}")
    enriched = await enrich_articles_with_oem(session, rows, max_calls=top_n + 3)
    summary = build_oem_summary(enriched, top_n=top_n)

    lines = [f"OK carId={car_id} ({info.get('source')}), strId={str_id}, "
             f"всего артикулов={len(rows)}"]
    if info.get("engine_code"):
        lines.append(f"Код двигателя: {info.get('engine_code')}")
    oem = summary.get("oem_numbers") or []
    anchor = summary.get("anchor_oems") or []
    lines.append("")
    lines.append("ОРИГИНАЛ (OEM): " + (", ".join(oem[:10]) if oem else "— не отдал getArticle (проверь ключ PARTSAPI_KEY_GETARTICLE)"))
    if anchor:
        lines.append("ЯКОРЬ (вероятный OE этой машины): " + ", ".join(anchor[:5]))
    lines.append("")
    _exact = summary.get("exact", [])
    lines.append("Точно подходящие (по якорю OE):" if _exact else "Топ подходящих (якорь не найден):")
    for t in (_exact or summary.get("top", [])):
        crit = t.get("criteria") or {}
        crit_s = "; ".join(f"{k}={v}" for k, v in list(crit.items())[:3])
        lines.append(f"  • {t.get('brand')} {t.get('article')}"
                     + (f" — {t.get('product_name')}" if t.get('product_name') else "")
                     + (f" [{crit_s}]" if crit_s else ""))
    if len(summary.get("groups", {})) > 1:
        lines.append("")
        lines.append("Разные размеры/исполнения (для отсева):")
        for sig, arts in summary["groups"].items():
            lines.append(f"  – {sig}: {len(arts)} шт (напр. {', '.join(str(a) for a in arts[:3])})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Оркестратор: VIN + cat_id -> (parts, meta)
# ---------------------------------------------------------------------------
async def resolve_oem(session, vin: str, cat_id: str,
                      part_name: str = "", keywords: Optional[list] = None) -> dict:
    """Полный путь TecDoc. Возвращает:
        {"parts": [(brand, article)...], "car_id": int|None,
         "str_id": int|None, "source": str, "reason": str|None,
         "car_str": str|None, "brand": str|None}
    parts пустой -> вызывающий код уходит в fallback getPartsbyVIN.
    """
    global _INFLIGHT
    meta = {"parts": [], "car_id": None, "str_id": None,
            "source": "tecdoc", "reason": None, "car_str": None, "brand": None}

    _INFLIGHT += 1
    try:
        car = await resolve_car_id(session, vin)
        meta["car_id"] = car["car_id"]
        meta["car_str"] = car.get("car_str")
        meta["brand"] = car.get("brand")
        if not car["car_id"]:
            meta["reason"] = "no_car_id"
            return meta

        str_id = await resolve_str_id(session, car["car_id"], cat_id, part_name, keywords)
        meta["str_id"] = str_id
        if not str_id:
            meta["reason"] = "no_str_id"
            return meta

        parts = await get_oem_articles(session, car["car_id"], str_id)
        meta["parts"] = parts
        if not parts:
            meta["reason"] = "no_articles"
        return meta
    finally:
        _INFLIGHT -= 1
