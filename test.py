# -*- coding: utf-8 -*-
"""
test.py — проверка авто-резолва carId по VIN (VINdecode + VINdecodeOE + getCars)
и якоря OEM. Запуск: py test.py
"""
import os, re, json, time, urllib.parse, urllib.request
import urllib.error
from collections import Counter
from dotenv import load_dotenv

load_dotenv()

BASE      = "https://api.partsapi.ru/"
VIN       = "WAUBH54B11N111054"
CAT_STR_ID = 100470     # масляный фильтр
LANG_ID   = "16"        # для TecDoc-методов (getCars/getArticles/getArticle)
LANG_TXT  = "ru"        # для VINdecode / VINdecodeOE
CAR_TYPE  = "PC"
MAX_ENRICH = 8
DELAY     = 0.5

# допуски расхождения двигателя
CC_TOL = 150            # см³: больше -> считаем «другая машина»
KW_TOL = 8              # кВт

KEYS = {
    "VINdecode":   os.getenv("PARTSAPI_KEY_VINDECODE21"),
    "VINdecodeOE": os.getenv("PARTSAPI_KEY_VINDECODE"),
    "getCars":     os.getenv("PARTSAPI_KEY_CARS"),
    "getArticles": os.getenv("PARTSAPI_KEY_ARTICLES"),
    "getArticle":  os.getenv("PARTSAPI_KEY_GETARTICLE"),
}

# --------------------------------------------------------------------------- #
def _get(method, **params):
    key = KEYS.get(method)
    if not key:
        print(f"! Нет ключа в .env для {method}")
        return None
    q = {"method": method, "key": key}
    q.update({k: str(v) for k, v in params.items() if v is not None})
    url = BASE + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                print(f"! {method}: HTTP 401 — ключ не авторизован / исчерпан дневной лимит метода")
                return None          # не ретраим — само не починится
            print(f"! {method}: HTTP {e.code}")
            time.sleep(1.0); continue
        except Exception as e:
            print(f"! {method}: сеть/JSON: {e}")
            time.sleep(1.0); continue
        if isinstance(data, dict) and data.get("error_code"):
            ec = data.get("error_code")
            print(f"! {method}: error_code={ec} ({data.get('message','')})")
            if ec in (5000, 5007):
                time.sleep(1.5); continue
            return None
        return data
    return None

def _num(v):
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return None

def _first(a):
    if isinstance(a, list):
        return a[0] if a and isinstance(a[0], dict) else {}
    if isinstance(a, dict):
        if any(k in a for k in ("OEM_NUMBERS", "ARTICLE_CRITERIA", "ART_ARTICLE_NR")):
            return a
        vals = [v for v in a.values() if isinstance(v, dict)]
        return vals[0] if vals else a
    return {}

# --------------------------------------------------------------------------- #
def vindecode(vin):
    d = _get("VINdecode", vin=vin, lang=LANG_TXT)
    res = d.get("result") if isinstance(d, dict) else None
    if isinstance(res, dict):
        return [v for v in res.values() if isinstance(v, dict)]
    return []

def vindecode_oe(vin):
    d = _get("VINdecodeOE", vin=vin, lang=LANG_TXT)
    arr = (d.get("data") or {}).get("array") if isinstance(d, dict) else None
    return arr if isinstance(arr, dict) else None

def get_cars(make_id, model_id):
    d = _get("getCars", makeId=make_id, modelId=model_id, carType=CAR_TYPE, lang=LANG_ID)
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        v = d.get("data")
        return v if isinstance(v, list) else []
    return []

def get_articles(car_id, str_id):
    d = _get("getArticles", carId=car_id, strId=str_id, carType=CAR_TYPE, lang=LANG_ID)
    return d if isinstance(d, list) else []

def get_article(art_num, sup_id):
    return _get("getArticle", ART_NUM=art_num, SUP_ID=sup_id, LANG=LANG_ID)

# --------------------------------------------------------------------------- #
def parse_oe_engine(oe):
    """VINdecodeOE 'dvigately': '2800CC / 193hp / 142kW V6' -> dict."""
    t = str(oe.get("dvigately") or "")
    cc  = re.search(r"(\d{3,5})\s*CC", t, re.I)
    kw  = re.search(r"(\d{2,4})\s*kW", t, re.I)
    hp  = re.search(r"(\d{2,4})\s*hp", t, re.I)
    lay = re.search(r"\b([VWRLB]\d{1,2})\b", t)
    return {
        "cc": int(cc.group(1)) if cc else None,
        "kw": int(kw.group(1)) if kw else None,
        "hp": int(hp.group(1)) if hp else None,
        "layout": lay.group(1) if lay else None,
        "raw": t,
    }

def pick_car(cars, spec, fwd=True):
    tkw, tcc, thp = spec.get("kw"), spec.get("cc"), spec.get("hp")
    scored = []
    for c in cars:
        kw  = _num(c.get("POWER_KW"))
        ps  = _num(c.get("POWER_PS"))
        cap = _num(c.get("CAPACITY"))
        s = 0.0
        if tkw and kw:                       # мощность кВт — главный признак
            d = abs(kw - tkw)
            if d == 0:   s += 5
            elif d <= 2: s += 2
        if thp and ps:                       # л.с. — тай-брейк
            d = abs(ps - thp)
            if d == 0:   s += 3
            elif d <= 3: s += 1
        if tcc and cap and abs(cap - tcc) <= 60:
            s += 2
        if s == 0:
            continue
        name = str(c.get("carName", "")).lower()
        is_q = ("quattro" in name) or ("4x4" in name)
        if fwd and is_q:       s -= 1
        if (not fwd) and is_q: s += 1
        scored.append((s, c))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]

def resolve_car_id():
    vds = vindecode(VIN)
    if not vds:
        print("VINdecode не дал данных"); return None
    vd = vds[0]
    vd_car = int(vd.get("carId") or 0)
    vd_cc  = int(vd.get("ccmTech") or vd.get("cylinderCapacityCcm") or 0)
    vd_kw  = int(vd.get("powerKwFrom") or vd.get("powerKwTo") or 0)
    manu   = int(vd.get("manuId") or vd.get("makeId") or 0)
    mod    = int(vd.get("modId") or vd.get("modelId") or 0)
    fwd    = "передн" in str(vd.get("impulsionType", "")).lower()
    print(f"VINdecode   → carId {vd_car} | {vd.get('typeName')} | "
          f"{vd_cc}cc / {vd_kw}kW / {vd.get('cylinder')}cyl | manuId={manu} modId={mod}")

    oe = vindecode_oe(VIN)
    if not oe:
        print("VINdecodeOE пусто → доверяем VINdecode"); return vd_car
    spec = parse_oe_engine(oe)
    print(f"VINdecodeOE → {oe.get('naimenovanie')} | {spec['raw']} | рынок {oe.get('rynok')}")

    cc_diff = abs((spec["cc"] or vd_cc) - vd_cc)
    kw_diff = abs((spec["kw"] or vd_kw) - vd_kw)
    mismatch = (spec["cc"] and cc_diff > CC_TOL) or (spec["kw"] and kw_diff > KW_TOL)
    if not mismatch:
        print(f"✓ Двигатели совпадают (Δсм³={cc_diff}, ΔкВт={kw_diff}) → carId {vd_car}")
        return vd_car

    print(f"⚠ РАСХОЖДЕНИЕ (Δсм³={cc_diff}, ΔкВт={kw_diff}) → переопределяем carId через getCars")
    cars = get_cars(manu, mod)
    print(f"  getCars вернул вариантов: {len(cars)}")
    best = pick_car(cars, spec, fwd=fwd)
    if best:
        new_id = int(best.get("carId"))
        print(f"→ Новый carId {new_id} | {best.get('carName')} | "
              f"{best.get('CAPACITY')}cc / {best.get('POWER_KW')}kW")
        return new_id
    print("getCars не нашёл подходящего → откат на VINdecode carId")
    return vd_car

# --------------------------------------------------------------------------- #
def normalize_oem(s):
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())

def parse_oems(field):
    if not field:
        return set()
    if isinstance(field, list):
        text = ", ".join(map(str, field))
    else:
        text = str(field)
    out = set()
    for chunk in text.split(","):
        part = chunk.split(":")[-1]      # убираем префикс бренда "AUDI: ..."
        n = normalize_oem(part)
        if len(n) >= 6:
            out.add(n)
    return out

def parse_height(crit):
    if not crit:
        return None
    text = crit if isinstance(crit, str) else str(crit)
    m = re.search(r"Высота[^:]*:\s*([\d.,]+)", text, re.I) \
        or re.search(r"Height[^:]*:\s*([\d.,]+)", text, re.I)
    if not m:
        return None
    try:
        return round(float(m.group(1).replace(",", ".")))
    except Exception:
        return None

# --------------------------------------------------------------------------- #
def main():
    print("=== РЕЗОЛВ carId по VIN ===")
    car_id = resolve_car_id()
    if not car_id:
        print("Не удалось определить carId"); return

    print(f"\n=== getArticles(carId={car_id}, strId={CAT_STR_ID}) ===")
    arts = get_articles(car_id, CAT_STR_ID)
    print(f"Найдено артикулов: {len(arts)}")
    if not arts:
        print("Пусто — возможно strId не подходит этому carId."); return

    print(f"\n=== Обогащение через getArticle (макс. {MAX_ENRICH}) ===")
    oem_counter, heights, done = Counter(), Counter(), 0
    print(f"\n=== Обогащение через getArticle (макс. {MAX_ENRICH}) ===")
    oem_counter, heights = Counter(), Counter()
    done = attempts = fails = 0
    for row in arts:
        if attempts >= MAX_ENRICH:
            break
        art_num, sup_id = row.get("ART_ARTICLE_NR"), row.get("SUP_ID")
        if not art_num or not sup_id:
            continue
        attempts += 1
        time.sleep(DELAY)
        info = _first(get_article(art_num, sup_id))
        if not info:
            fails += 1
            if fails >= 3:
                print("  3 ошибки подряд (похоже, лимит getArticle на сегодня) — стоп обогащения.")
                break
            continue
        fails = 0
        oems = parse_oems(info.get("OEM_NUMBERS"))
        h = parse_height(info.get("ARTICLE_CRITERIA"))
        done += 1
        brand = info.get("ART_SUP_BRAND") or row.get("ART_SUP_BRAND") or "?"
        print(f"[{done}] {brand} {art_num}: OEM={len(oems)}, высота={h}")
        for o in oems:
            oem_counter[o] += 1
        if h:
            heights[h] += 1

    print("\n=== ИТОГ ===")
    print(f"carId={car_id} | артикулов={len(arts)} | обогащено={done} | уник.OEM={len(oem_counter)}")
    print("\n--- ТОП-10 OEM по частоте (якорь) ---")
    for oem, n in oem_counter.most_common(10):
        print(f"{oem}: {n}")
    print("\n--- Распределение высот (мм) ---")
    for h, n in heights.most_common():
        print(f"{h} мм: {n}")

if __name__ == "__main__":
    main()