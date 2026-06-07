# -*- coding: utf-8 -*-
"""
test_one_oem.py — VIN/carId -> strId -> getArticles -> getArticle -> ГЛАВНЫЙ OEM.
СТАБИЛЬНО и ОБЩО (не под один VIN/деталь):
  • узел (strId) резолвится ОФЛАЙН из tecdoc.db (search_trees), а не из живого дерева;
  • финальный OEM выбирается через collapse_oem: доминирующее семейство + последняя ревизия.
Запуск:
  py test_one_oem.py <VIN> [<часть|strId>] [car=<carId>] [make=<AUDI|VW|...>]
Примеры:
  py test_one_oem.py WAUBH54B11N111054 "масляный фильтр" car=8320 make=AUDI
  py test_one_oem.py WAUBH54B11N111054 "воздушный фильтр" car=8320 make=AUDI
  py test_one_oem.py WAUBH54B11N111054 100470 car=8320 make=AUDI
"""
import os, re, sys, json, time, sqlite3
import urllib.request, urllib.parse, urllib.error
from collections import Counter

BASE      = "https://api.partsapi.ru"
HERE      = os.path.dirname(os.path.abspath(__file__))
ENV_PATH  = os.path.join(HERE, ".env")
DB_PATH   = os.path.join(HERE, "tecdoc_db", "tecdoc.db")
MAX_CALLS = 25
DELAY     = 0.6
TIMEOUT   = 25

# ---------- свой .env-загрузчик (срезает инлайн # и кавычки -> чинит 401) ----------
def _load_env(path=ENV_PATH):
    data = {}
    if not os.path.exists(path):
        return data
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = re.split(r"\s+#", v.strip(), 1)[0].strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1].strip()
            data[k.strip()] = v
    return data

ENV = _load_env()

def env(*names, default=""):
    for n in names:
        v = ENV.get(n) or os.getenv(n)
        if v:
            return v.strip()
    return default

KEY_VIN     = env("PARTSAPI_KEY_VINDECODE21", "PARTSAPI_KEY_VINDECODE")
KEY_TREE    = env("PARTSAPI_KEY_TREE")
KEY_ARTS    = env("PARTSAPI_KEY_ARTICLES")
KEY_ARTICLE = env("PARTSAPI_KEY_GETARTICLE")

def _mask(v):
    return f"{v[:4]}…{v[-4:]} (len{len(v)})" if v else "—(нет)"

# ---------- HTTP (urllib + UA, как в рабочем ядре) ----------
def call(params, retries=2):
    url, last = BASE + "/?" + urllib.parse.urlencode(params), ""
    for _ in range(retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = f"HTTPError: HTTP Error {e.code}: {e.reason}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(DELAY)
    print(f"[сеть] {last}")
    return ""

def call_json(params, retries=2):
    raw = call(params, retries)
    try:
        return json.loads(raw) if raw else None
    except Exception:
        return None

def as_objs(data):
    if data is None:
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        if isinstance(data.get("result"), (list, dict)):
            return as_objs(data["result"])
        objs = [v for v in data.values() if isinstance(v, dict)]
        return objs or [data]
    return []

def pick(d, *keys):
    for k in keys:
        for kk in d:
            if kk.lower() == k.lower() and d[kk] not in (None, ""):
                return d[kk]
    return ""

# ---------- марки/семейства ----------
MAKE_HINTS = {"AUDI": "VAG", "VW": "VAG", "VOLKSWAGEN": "VAG", "SEAT": "VAG",
              "SKODA": "VAG", "PORSCHE": "VAG", "VAG": "VAG"}
OWN_BRANDS = {"VAG": ("AUDI", "VW", "VOLKSWAGEN", "SEAT", "SKODA", "PORSCHE", "VAG")}

# ---------- OEM: разбор + СХЛОПЫВАНИЕ (общее правило) ----------
_OEM_SPLIT = re.compile(r"[;,]")
_OEM_SUF   = re.compile(r"^(\d.*?)([A-Z]{0,2})$")

def _oem_norm(s):
    return re.sub(r"\s+", "", (s or "").upper())

def parse_oems(s):
    out = []
    for chunk in _OEM_SPLIT.split(s or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        brand = ""
        if ":" in chunk:
            brand, chunk = chunk.split(":", 1)
            brand, chunk = brand.strip().upper(), chunk.strip()
        norm = _oem_norm(chunk)
        if not norm or set(norm) <= {"0"}:
            continue
        out.append((brand, norm, chunk))
    return out

def _split_base(norm):
    m = _OEM_SUF.match(norm)
    return (m.group(1), m.group(2)) if m else (norm, "")

def _suf_rank(suf):
    return (len(suf), suf)   # "" < D < H < J

def collapse_oem(freq):
    """Берём ДОМИНИРУЮЩЕЕ семейство и его ПОСЛЕДНЮЮ ревизию. Общее, без хардкода."""
    fams = {}
    for raw, cnt in freq.items():
        norm = _oem_norm(raw)
        if not norm or set(norm) <= {"0"}:
            continue
        base, suf = _split_base(norm)
        f = fams.setdefault(base, {"total": 0, "suf": {}})
        f["total"] += cnt
        s = f["suf"].setdefault(suf, {"disp": Counter()})
        s["disp"][raw.strip()] += cnt
    if not fams:
        return None
    base   = max(fams, key=lambda b: fams[b]["total"])
    f      = fams[base]
    latest = max(f["suf"], key=_suf_rank)
    main   = f["suf"][latest]["disp"].most_common(1)[0][0]
    older  = [s for s in sorted(f["suf"], key=_suf_rank) if s != latest]
    return {"main": main, "latest": latest, "older": older, "family_total": f["total"]}

# ---------- strId ОФЛАЙН (общее, для любой детали) ----------
CAT_TO_STR_ID = {"масляный фильтр": 100470}   # кэш подтверждённых узлов

def _stem(w):
    return w[:-2] if len(w) > 5 else w

def strid_from_db(part_name):
    if not os.path.exists(DB_PATH):
        return []
    words = [_stem(w) for w in re.split(r"\s+", part_name.lower().strip()) if len(w) > 2] \
            or [part_name.lower().strip()]
    where  = " AND ".join("LOWER(col3) LIKE ?" for _ in words)
    params = [f"%{w}%" for w in words]
    con = sqlite3.connect(DB_PATH)
    try:
        rows = con.execute(
            f"SELECT col1, col3 FROM search_trees WHERE {where} ORDER BY LENGTH(col3) LIMIT 12",
            params).fetchall()
    finally:
        con.close()
    seen, out = set(), []
    for nid, nm in rows:
        if nid not in seen:
            seen.add(nid); out.append((nid, nm))
    return out

def resolve_str_id(part_arg):
    if part_arg and part_arg.isdigit():
        return int(part_arg), f"задан вручную = {part_arg}"
    name = (part_arg or "").lower().strip()
    if name in CAT_TO_STR_ID:
        return CAT_TO_STR_ID[name], f"из CAT_TO_STR_ID = {CAT_TO_STR_ID[name]}"
    cands = strid_from_db(name)
    if len(cands) == 1:
        return int(cands[0][0]), f"из офлайн-базы = {cands[0][0]} ({cands[0][1]})"
    if cands:
        print("Кандидаты strId из офлайн-базы:")
        for nid, nm in cands:
            print(f"  strId={nid}   {nm}")
        print("-> перезапусти, добавив нужный strId последним аргументом.")
        return None, "неоднозначно"
    print(f"Узел для «{name}» не найден в офлайн-базе.")
    return None, "не найдено"

# ---------- шаги ----------
def step_vin(vin):
    print(f"\n[1] VINdecode {vin}")
    objs = as_objs(call_json({"method": "VINdecode", "key": KEY_VIN, "vin": vin, "lang": "ru"}))
    if not objs:
        print("carId не найден: null"); return None, ""
    car_id = pick(objs[0], "carId", "car_id", "id")
    make   = pick(objs[0], "manuName", "make", "brand")
    print(f"carId = {car_id}   марка = {make}")
    return (int(car_id) if str(car_id).isdigit() else None), str(make).upper()

def step_articles(str_id, car_id):
    print(f"\n[3] getArticles (strId={str_id})")
    rows = as_objs(call_json({"method": "getArticles", "key": KEY_ARTS, "lang": "16",
                              "strId": str(str_id), "carId": str(car_id), "carType": "PC"}))
    print(f"артикулов: {len(rows)}")
    return [{"brand": pick(r, "ART_SUP_BRAND", "brand"),
             "article": pick(r, "ART_ARTICLE_NR", "article"),
             "sup_id": pick(r, "SUP_ID", "sup_id")} for r in rows]

def step_oem(rows, family):
    own = OWN_BRANDS.get(family, ())
    print(f"\n[4] getArticle по первым {MAX_CALLS} артикулам"
          + (f" (фильтр OE по брендам {family})" if own else ""))
    freq, calls = Counter(), 0
    for r in rows:
        if calls >= MAX_CALLS:
            break
        if not r["article"] or not r["sup_id"]:
            continue
        calls += 1
        time.sleep(DELAY)
        objs = as_objs(call_json({"method": "getArticle", "key": KEY_ARTICLE, "LANG": "16",
                                  "ART_NUM": r["article"], "SUP_ID": r["sup_id"]}))
        if not objs:
            continue
        oems = parse_oems(pick(objs[0], "OEM_NUMBERS", "oem_numbers"))
        if own:   # если знаем семейство — оставляем «свои» OE (и без бренда)
            oems = [t for t in oems if (not t[0] or t[0] in own)]
        if not oems:
            continue
        print(f"{r['brand']:<15} {r['article']:<16} OE: {', '.join(t[2] for t in oems)}")
        for d in {t[2] for t in oems}:     # +1 за деталь, не за повтор внутри детали
            freq[d] += 1
    return freq

def main():
    args = sys.argv[1:]
    if not args:
        print("Запуск: py test_one_oem.py <VIN> [<часть|strId>] [car=<carId>] [make=<AUDI>]")
        return
    vin, rest = args[0], args[1:]
    car_override, make_override, part_tokens = None, "", []
    for a in rest:
        m = re.match(r"(?:car|carid|car_id)=(\d+)$", a, re.I)
        if m: car_override = int(m.group(1)); continue
        m = re.match(r"(?:fam|family|make)=(.+)$", a, re.I)
        if m: make_override = m.group(1).strip().upper(); continue
        part_tokens.append(a)
    part_arg = " ".join(part_tokens).strip() or "масляный фильтр"

    print(f"VIN={vin}  деталь='{part_arg}'")
    print(f"ключи: VIN= {_mask(KEY_VIN)} | TREE= {_mask(KEY_TREE)} | "
          f"ARTICLES= {_mask(KEY_ARTS)} | GETARTICLE= {_mask(KEY_ARTICLE)}")

    if car_override:
        car_id = car_override
        family = MAKE_HINTS.get(make_override, make_override) or ""
        print(f"carId(ручной) = {car_id}   семейство = {family or '—'}")
    else:
        car_id, make = step_vin(vin)
        family = MAKE_HINTS.get(make, make) if make else ""
        if not car_id:
            print("Без carId дальше нельзя. Укажи car=<carId>."); return

    str_id, how = resolve_str_id(part_arg)
    print(f"\n[2] strId {how}")
    if not str_id:
        return

    rows = step_articles(str_id, car_id)
    if not rows:
        print("Нет артикулов — проверь strId/carId."); return
    freq = step_oem(rows, family)

    print("\n========== РЕЗУЛЬТАТ ==========")
    col = collapse_oem(freq)
    if not col:
        print("OEM не извлечён."); return
    print(f"⭐ ОРИГИНАЛ для авто: {col['main']}   (актуальная ревизия)")
    if col["older"]:
        print("   взаимозаменяемые/устаревшие ревизии: "
              + ", ".join(s or "(без буквы)" for s in col["older"]))
    print("\nчастоты:")
    for disp, n in freq.most_common(8):
        print(f"  {disp}  (x{n})")

if __name__ == "__main__":
    main()
