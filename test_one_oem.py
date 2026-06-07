# -*- coding: utf-8 -*-
"""
test_one_oem.py (v2) — один главный OEM по VIN + детали, с фильтром по марке.
Запуск:
    py test_one_oem.py                                  # дефолт: VIN + "масляный фильтр"
    py test_one_oem.py WAUBH54B11N111054 масляный фильтр
    py test_one_oem.py WAUBH54B11N111054 100470         # задать strId напрямую
"""
import os, sys, json, time, re
import urllib.parse, urllib.request, urllib.error
from collections import Counter

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BASE = "https://api.partsapi.ru/"
DELAY = 0.6
MAX_CALLS = 25  # сколько артикулов обогащаем (бережём дневной лимит getArticle)

# семейства OE-брендов по марке машины (для фильтра «один OEM, а не куча»)
OE_FAMILIES = {
    "VAG": {"VAG", "AUDI", "VW", "VOLKSWAGEN", "SEAT", "SKODA", "AUDI (FAW)", "PORSCHE"},
    "BMW": {"BMW", "MINI", "ROLLS-ROYCE"},
    "MERCEDES": {"MERCEDES-BENZ", "MERCEDES", "SMART"},
    "FORD": {"FORD"},
    "GM": {"OPEL", "VAUXHALL", "GENERAL MOTORS", "CHEVROLET"},
    "PSA": {"PEUGEOT", "CITROËN", "CITROEN", "DS"},
    "RENAULT": {"RENAULT", "DACIA", "NISSAN", "INFINITI"},
    "TOYOTA": {"TOYOTA", "LEXUS", "DAIHATSU"},
    "FIAT": {"FIAT", "ALFA ROMEO", "LANCIA", "JEEP"},
    "HYUNDAI": {"HYUNDAI", "KIA"},
}
# по какому слову из VINdecode понять семейство
MAKE_HINTS = {
    "AUDI": "VAG", "VOLKSWAGEN": "VAG", "VW": "VAG", "SEAT": "VAG", "SKODA": "VAG", "PORSCHE": "VAG",
    "BMW": "BMW", "MINI": "BMW",
    "MERCEDES": "MERCEDES", "SMART": "MERCEDES",
    "FORD": "FORD",
    "OPEL": "GM", "VAUXHALL": "GM", "CHEVROLET": "GM",
    "PEUGEOT": "PSA", "CITRO": "PSA",
    "RENAULT": "RENAULT", "DACIA": "RENAULT", "NISSAN": "RENAULT",
    "TOYOTA": "TOYOTA", "LEXUS": "TOYOTA",
    "FIAT": "FIAT", "ALFA": "FIAT", "LANCIA": "FIAT",
    "HYUNDAI": "HYUNDAI", "KIA": "HYUNDAI",
}

def env(*names):
    for n in names:
        if os.getenv(n):
            return os.getenv(n)
    return ""

KEY_VIN      = env("PARTSAPI_KEY_VINDECODE21", "PARTSAPI_KEY_VINDECODE")
KEY_TREE     = env("PARTSAPI_KEY_TREE")
KEY_ARTICLES = env("PARTSAPI_KEY_ARTICLES")
KEY_ARTICLE  = env("PARTSAPI_KEY_GETARTICLE")

def call(params, retries=2):
    url = BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read().decode("utf-8", "replace")
            time.sleep(DELAY)
            try:
                return json.loads(body)
            except Exception:
                print(f"   [не JSON] {body[:160]}"); return None
        except Exception as e:
            if attempt < retries:
                time.sleep(1.0); continue
            print(f"   [сеть] {type(e).__name__}: {e}"); return None

def as_objs(data):
    out = []
    def walk(x):
        if isinstance(x, dict):
            if any(k in x for k in ("carId", "STR_ID", "ART_ARTICLE_NR", "OEM_NUMBERS")):
                out.append(x)
            for v in x.values(): walk(v)
        elif isinstance(x, list):
            for v in x: walk(v)
    walk(data)
    return out

def pick(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None

def detect_family(car):
    blob = " ".join(str(v) for v in car.values()).upper()
    for hint, fam in MAKE_HINTS.items():
        if hint in blob:
            return fam
    return None

def step_vin(vin):
    print(f"\n[1] VINdecode {vin}")
    data = call({"method": "VINdecode", "key": KEY_VIN, "vin": vin, "lang": "ru"})
    cars = [o for o in as_objs(data) if pick(o, "carId")]
    if not cars:
        print("   carId не найден:", json.dumps(data, ensure_ascii=False)[:250]); return None, None
    car = cars[0]
    car_id = str(pick(car, "carId"))
    fam = detect_family(car)
    print(f"   carId = {car_id}  ({pick(car,'carName','typeName','modelName') or '—'})  семейство OE: {fam or 'не определено'}")
    return car_id, fam

def step_tree(car_id, part, str_override):
    if str_override:
        print(f"\n[2] strId задан вручную = {str_override}")
        return str_override
    print(f"\n[2] getSearchTree -> ищу точный узел для «{part}»")
    data = call({"method": "getSearchTree", "key": KEY_TREE,
                 "lang": "16", "carId": car_id, "carType": "PC"})
    nodes = [o for o in as_objs(data) if pick(o, "STR_ID")]
    if not nodes:
        print("   дерево пустое."); return None
    toks = [t for t in re.split(r"\s+", part.lower()) if len(t) > 2]
    # ВСЕ слова запроса должны быть в имени узла -> отсекаем широкий «Фильтр»
    full = [n for n in nodes if all(t in str(pick(n,"STR_NODE_NAME","STR_PATH") or "").lower() for t in toks)]
    if full:
        best = min(full, key=lambda n: len(str(pick(n,"STR_NODE_NAME") or "")))  # самый конкретный
        print(f"   strId = {pick(best,'STR_ID')}  ({pick(best,'STR_NODE_NAME')})")
        return str(pick(best, "STR_ID"))
    # не нашли точный — показываем узлы с «фильтр», чтобы выбрать руками
    print("   Точный узел не найден. Узлы со словом «фильтр»:")
    for n in nodes:
        name = str(pick(n,"STR_NODE_NAME") or "")
        if "фильтр" in name.lower():
            print(f"      strId={pick(n,'STR_ID'):<8} {name}")
    print("   -> перезапусти, добавив нужный strId последним аргументом.")
    return None

def step_articles(car_id, str_id):
    print(f"\n[3] getArticles (strId={str_id})")
    data = call({"method": "getArticles", "key": KEY_ARTICLES,
                 "lang": "16", "strId": str_id, "carId": car_id, "carType": "PC"})
    rows = []
    for o in as_objs(data):
        art, sup = pick(o,"ART_ARTICLE_NR","article"), pick(o,"SUP_ID","supId")
        if art and sup:
            rows.append({"brand": pick(o,"ART_SUP_BRAND","brand") or "?",
                         "article": str(art), "sup_id": str(sup)})
    print(f"   артикулов: {len(rows)}")
    return rows

OE_SPLIT = re.compile(r"[;,]")
def parse_oems(s):
    """-> [(brand_upper, number, display)]"""
    out = []
    for part in OE_SPLIT.split(str(s or "")):
        p = part.strip()
        if not p: continue
        brand = ""
        if ":" in p:
            brand, p = p.split(":", 1)
            brand, p = brand.strip().upper(), p.strip()
        if p and p not in ("0", "-"):
            out.append((brand, re.sub(r"[^A-Z0-9]","",p.upper()), p))
    return out

def step_oem(rows, family):
    allowed = OE_FAMILIES.get(family) if family else None
    tag = f"только бренды {family}" if allowed else "без фильтра по марке"
    print(f"\n[4] getArticle по первым {MAX_CALLS} артикулам ({tag})")
    freq, label = Counter(), {}
    for r in rows[:MAX_CALLS]:
        data = call({"method": "getArticle", "key": KEY_ARTICLE,
                     "LANG": "16", "ART_NUM": r["article"], "SUP_ID": r["sup_id"]})
        obj = next(iter(as_objs(data)), None) or (data if isinstance(data, dict) else None)
        oem_str = pick(obj, "OEM_NUMBERS", "oemNumbers") if obj else None
        kept = []
        for brand, k, disp in parse_oems(oem_str):
            if allowed and brand not in allowed:    # фильтр по марке машины
                continue
            if not k: continue
            freq[k] += 1; kept.append(disp)
            if k not in label or (" " in disp and " " not in label[k]):
                label[k] = disp
        print(f"   {r['brand']:<16}{r['article']:<16} OE(марка): {', '.join(kept) or '—'}")
    if not freq:
        print("\n   OEM не собрались (лимит/пусто/всё отфильтровано)."); return
    print("\n========== РЕЗУЛЬТАТ ==========")
    top = freq.most_common(6)
    mk, mc = top[0]
    print(f"  ⭐ ГЛАВНЫЙ OEM: {label[mk]}   (у {mc} деталей)")
    if len(top) > 1:
        print("  другие частые OE:")
        for k, c in top[1:]:
            print(f"     {label[k]}  (x{c})")

def main():
    args = sys.argv[1:]
    vin = args[0] if args else "WAUBH54B11N111054"
    rest = args[1:]
    str_override = None
    if rest and rest[-1].isdigit():
        str_override = rest[-1]; rest = rest[:-1]
    part = " ".join(rest) if rest else "масляный фильтр"
    miss = [n for n,v in [("VINDECODE21",KEY_VIN),("TREE",KEY_TREE),
                          ("ARTICLES",KEY_ARTICLES),("GETARTICLE",KEY_ARTICLE)] if not v]
    if miss:
        print("Нет ключей в .env:", ", ".join("PARTSAPI_KEY_"+m for m in miss)); return
    print(f"VIN={vin}  деталь='{part}'" + (f"  strId={str_override}" if str_override else ""))
    car_id, fam = step_vin(vin)
    if not car_id: return
    str_id = step_tree(car_id, part, str_override)
    if not str_id: return
    rows = step_articles(car_id, str_id)
    if not rows: return
    step_oem(rows, fam)

if __name__ == "__main__":
    main()