# -*- coding: utf-8 -*-
"""
Оффлайн-тесты для tecdoc.py — без сети и без трат API.

Идея: подменяем aiohttp-сессию заглушкой (FakeSession), которая отдаёт заранее
заготовленные ответы по имени метода. Так мы проверяем всю логику разбора
(carId, strId, артикулы) детерминированно.

Запуск:
    cd /data && python tests/test_tecdoc_offline.py
    # или, если есть pytest:
    cd /data && python -m pytest tests/test_tecdoc_offline.py -q
"""
import os
import sys
import json
import asyncio

# aiohttp может быть не установлен — тесты его не используют (FakeSession).
# Если модуля нет — подкладываем мини-заглушку, чтобы import tecdoc не падал.
try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError:
    import types
    _stub = types.ModuleType("aiohttp")

    class _ClientError(Exception):
        pass

    class _ClientTimeout:
        def __init__(self, *a, **k):
            pass

    _stub.ClientError = _ClientError
    _stub.ClientTimeout = _ClientTimeout
    sys.modules["aiohttp"] = _stub

# --- импорт tecdoc из родительской папки ---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tecdoc  # noqa: E402

# Заглушаем ключи и задержку, чтобы _request не падал в NO_KEY и не спал.
tecdoc.KEYS = {m: "TESTKEY" for m in tecdoc.KEYS}
tecdoc.API_DELAY = 0


# ---------------------------------------------------------------------------
# Заглушка aiohttp-сессии
# ---------------------------------------------------------------------------
class FakeResp:
    def __init__(self, status, text):
        self._status = status
        self._text = text

    @property
    def status(self):
        return self._status

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """router(method, params) -> (status:int, text:str)"""
    def __init__(self, router):
        self._router = router
        self.calls = []

    def get(self, url, params=None, timeout=None):
        params = params or {}
        method = params.get("method")
        self.calls.append((method, dict(params)))
        status, text = self._router(method, params)
        return FakeResp(status, text)


def make_session(responses):
    """responses: {method: (status, payload)}; payload — объект (будет json) или строка."""
    def router(method, params):
        if method not in responses:
            return (404, json.dumps({"error": "not stubbed"}))
        status, payload = responses[method]
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return (status, text)
    return FakeSession(router)


def reset_state():
    """Чистим кэш и состояние перед каждым тестом."""
    tecdoc._CACHE.clear()
    tecdoc.CAT_TO_STR_ID = {}
    # По умолчанию резерв make/model/cars выкл. — тесты проверяют основной путь VINdecode.
    tecdoc.USE_MAKE_MODEL_FALLBACK = False
    tecdoc._INFLIGHT = 0
    tecdoc.set_load_hook(None)


def run(coro):
    return asyncio.run(coro)


# ===========================================================================
# 1. Чистые хелперы (без сети)
# ===========================================================================
def test_year():
    assert tecdoc._year("2015-06") == 2015
    assert tecdoc._year("2020") == 2020
    assert tecdoc._year(2008) == 2008
    assert tecdoc._year("кузов c 1998 по 2004") == 1998
    assert tecdoc._year(None) is None
    assert tecdoc._year("нет года") is None


def test_to_int():
    assert tecdoc._to_int("5002") == 5002
    assert tecdoc._to_int(" 7 ") == 7
    assert tecdoc._to_int(42) == 42
    assert tecdoc._to_int(None) is None
    assert tecdoc._to_int("x") is None


def test_norm():
    assert tecdoc._norm("BMW-X5!") == "BMWX5"
    assert tecdoc._norm(" w 712 ") == "W712"
    assert tecdoc._norm(None) == ""


def test_pick_case_insensitive():
    d = {"MakeName": "BMW", "empty": "", "id": 5}
    assert tecdoc._pick(d, "makeName") == "BMW"      # регистронезависимо
    assert tecdoc._pick(d, "empty", "id") == 5        # пустое поле пропускается
    assert tecdoc._pick(d, "nope") is None


def test_as_list():
    assert tecdoc._as_list([1, 2]) == [1, 2]
    assert tecdoc._as_list({"items": [3, 4]}) == [3, 4]
    assert tecdoc._as_list({"result": [5]}) == [5]
    assert tecdoc._as_list({"carId": 1}) == [{"carId": 1}]   # одиночный объект
    assert tecdoc._as_list("строка") == []


def test_tokens():
    assert tecdoc._tokens("Масляный фильтр") == {"масляный", "фильтр"}
    # короткие токены (<=2) отбрасываются
    assert "x5" not in tecdoc._tokens("BMW X5")


def test_match_id_exact_and_partial():
    rows = [
        {"makeName": "MERCEDES-BENZ", "makeId": 74},
        {"makeName": "BMW", "makeId": 16},
    ]
    assert tecdoc._match_id(rows, "BMW", ("makeName", "name"), ("makeId", "id")) == 16
    # частичное совпадение (подстрока)
    assert tecdoc._match_id(rows, "Mercedes", ("makeName", "name"), ("makeId", "id")) == 74
    assert tecdoc._match_id(rows, "AUDI", ("makeName",), ("makeId",)) is None


def test_flatten_tree_mixed_keys():
    tree = [
        {"strId": 100, "name": "Масляный фильтр"},
        {"assemblyGroupNodeId": 300, "assemblyGroupName": "Тормоза", "childNodes": [
            {"nodeId": 301, "text": "Колодки"},
        ]},
        {"id": 200, "title": "Воздушный фильтр", "children": []},
    ]
    acc = []
    tecdoc._flatten_tree(tree, acc)
    ids = {sid for sid, _ in acc}
    assert ids == {100, 300, 301, 200}


def test_pick_car_by_year():
    cars = [
        {"carId": 5001, "yearStart": "2005", "yearEnd": "2010"},
        {"carId": 5002, "yearStart": "2011", "yearEnd": "2017"},
    ]
    assert tecdoc._pick_car_by_year(cars, 2015) == 5002
    assert tecdoc._pick_car_by_year(cars, 2008) == 5001
    assert tecdoc._pick_car_by_year(cars, None) == 5001       # без года — первая
    assert tecdoc._pick_car_by_year(cars, 1990) == 5001       # вне диапазонов — первая
    assert tecdoc._pick_car_by_year([], 2015) is None


def test_car_str_from_row():
    assert tecdoc._car_str_from_row({"carName": "BMW X5"}) == "BMW X5"
    assert tecdoc._car_str_from_row({"modelName": "X5"}) == "X5"
    assert tecdoc._car_str_from_row("не словарь") is None


# ===========================================================================
# 2. resolve_str_id (async, с заглушкой)
# ===========================================================================
def test_resolve_str_id_explicit_mapping():
    reset_state()
    tecdoc.CAT_TO_STR_ID = {"7": 12345}
    sess = make_session({})  # сеть НЕ должна вызываться
    sid = run(tecdoc.resolve_str_id(sess, car_id=5002, cat_id="7", part_name="что угодно"))
    assert sid == 12345
    assert sess.calls == []   # подтверждаем: запросов не было


def test_resolve_str_id_dynamic_token_match():
    reset_state()
    tree = [
        {"strId": 100, "name": "Масляный фильтр"},
        {"strId": 200, "name": "Воздушный фильтр"},
        {"strId": 300, "name": "Тормозные колодки"},
    ]
    sess = make_session({"getSearchTree": (200, tree)})
    sid = run(tecdoc.resolve_str_id(sess, car_id=5002, cat_id="7",
                                    part_name="масляный фильтр"))
    assert sid == 100


def test_resolve_str_id_no_tree():
    reset_state()
    sess = make_session({"getSearchTree": (200, [])})
    sid = run(tecdoc.resolve_str_id(sess, car_id=5002, cat_id="7",
                                    part_name="масляный фильтр"))
    assert sid is None


# ===========================================================================
# 3. resolve_car_id / get_oem_articles / resolve_oem (async)
# ===========================================================================
def test_resolve_car_id_vindecode():
    reset_state()
    # VINdecode (shop/21) отдаёт carId НАПРЯМУЮ + марку/модель/модификацию
    decoded = {"carId": 5002, "manuName": "BMW", "modelName": "X5",
               "typeName": "3.0d", "yearOfConstrFrom": "2007"}
    sess = make_session({"VINdecode": (200, decoded)})
    out = run(tecdoc.resolve_car_id(sess, "WBAVIN0000000000"))
    assert out["car_id"] == 5002
    assert out["source"] == "VINdecode"
    assert out["brand"] == "BMW"
    assert out["car_str"] == "BMW X5 3.0d"
    # вызван только VINdecode — никаких getMakes/getCars
    assert [m for m, _ in sess.calls] == ["VINdecode"]


def test_resolve_car_id_none_when_empty():
    reset_state()
    sess = make_session({"VINdecode": (200, [])})
    out = run(tecdoc.resolve_car_id(sess, "WBAVIN0000000000"))
    assert out["car_id"] is None


def test_resolve_car_id_fallback_getcars_when_idle():
    """Нет carId, но VINdecode дал manuId/modId -> getCars (резерв, бот не занят)."""
    reset_state()
    tecdoc.USE_MAKE_MODEL_FALLBACK = True   # резерв разрешён
    decoded = {"manuId": 16, "modId": 99, "manuName": "BMW",
               "modelName": "X5", "yearOfConstrFrom": "2015"}
    cars = [{"carId": 5001, "yearStart": "2005", "yearEnd": "2010"},
            {"carId": 5002, "yearStart": "2011", "yearEnd": "2017"}]
    sess = make_session({"VINdecode": (200, decoded), "getCars": (200, cars)})
    out = run(tecdoc.resolve_car_id(sess, "WBAVIN0000000000"))
    assert out["car_id"] == 5002
    assert out["source"] == "VINdecode+getCars"


def test_resolve_car_id_fallback_skipped_under_load():
    """Тот же случай, но бот под нагрузкой -> резерв НЕ вызывается."""
    reset_state()
    tecdoc.USE_MAKE_MODEL_FALLBACK = True
    tecdoc.set_load_hook(lambda: True)     # имитируем нагрузку
    decoded = {"manuId": 16, "modId": 99, "manuName": "BMW", "modelName": "X5"}
    sess = make_session({"VINdecode": (200, decoded),
                         "getCars": (200, [{"carId": 5002}])})
    out = run(tecdoc.resolve_car_id(sess, "WBAVIN0000000000"))
    assert out["car_id"] is None
    assert [m for m, _ in sess.calls] == ["VINdecode"]   # getCars не вызывался


def test_get_oem_articles_dedup():
    reset_state()
    arts = [
        {"brand": "MANN", "article": "W712"},
        {"brandName": "Mann", "articleNumber": "w712"},   # дубль (нормализуется)
        {"brand": "BOSCH", "article": "0451103316"},
        {"brand": "", "article": "X"},                      # пропуск: нет бренда
    ]
    sess = make_session({"getArticles": (200, arts)})
    parts = run(tecdoc.get_oem_articles(sess, 5002, 100))
    assert parts == [("MANN", "W712"), ("BOSCH", "0451103316")]


def test_resolve_oem_happy_path():
    reset_state()
    sess = make_session({
        "VINdecode": (200, {"carId": 5002, "manuName": "BMW", "modelName": "X5"}),
        "getSearchTree": (200, [{"strId": 100, "name": "Масляный фильтр"}]),
        "getArticles": (200, [{"brand": "MANN", "article": "W712"}]),
    })
    meta = run(tecdoc.resolve_oem(sess, "WBAVIN", cat_id="7", part_name="масляный фильтр"))
    assert meta["car_id"] == 5002
    assert meta["str_id"] == 100
    assert meta["parts"] == [("MANN", "W712")]
    assert meta["reason"] is None
    assert meta["brand"] == "BMW"


def test_resolve_oem_reason_no_car_id():
    reset_state()
    sess = make_session({"VINdecode": (200, [])})
    meta = run(tecdoc.resolve_oem(sess, "WBAVIN", cat_id="7", part_name="масляный фильтр"))
    assert meta["car_id"] is None
    assert meta["reason"] == "no_car_id"
    assert meta["parts"] == []


def test_resolve_oem_reason_no_str_id():
    reset_state()
    sess = make_session({
        "VINdecode": (200, {"carId": 5002, "modelName": "X5"}),
        "getSearchTree": (200, []),   # дерево пустое -> strId не найден
    })
    meta = run(tecdoc.resolve_oem(sess, "WBAVIN", cat_id="999", part_name="масляный фильтр"))
    assert meta["car_id"] == 5002
    assert meta["str_id"] is None
    assert meta["reason"] == "no_str_id"


def test_resolve_oem_reason_no_articles():
    reset_state()
    sess = make_session({
        "VINdecode": (200, {"carId": 5002, "modelName": "X5"}),
        "getSearchTree": (200, [{"strId": 100, "name": "Масляный фильтр"}]),
        "getArticles": (200, []),     # артикулов нет
    })
    meta = run(tecdoc.resolve_oem(sess, "WBAVIN", cat_id="7", part_name="масляный фильтр"))
    assert meta["str_id"] == 100
    assert meta["parts"] == []
    assert meta["reason"] == "no_articles"


# ===========================================================================
# Самостоятельный запуск (без pytest)
# ===========================================================================
def _main():
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    passed, failed = 0, 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"PASS  {name}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
