# -*- coding: utf-8 -*-
# ============================================================
# ЭТАП 3 — КАЧЕСТВО ВЫДАЧИ (HTML) — v3 (читаемое оформление)
# format_vin_reply(meta, part_label=..., brand_filter=None) -> HTML
# ОТПРАВЛЯТЬ С parse_mode="HTML".
# ============================================================
import re
from html import escape

_VAG = {"AUDI", "VW", "VOLKSWAGEN", "SKODA", "SEAT", "CUPRA", "PORSCHE"}

def _n(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())

def _e(s):
    return escape(str(s or ""), quote=False)

def _code(s):
    return f"<code>{_e(s)}</code>"

def _clean_key(name):
    return re.sub(r"\s*\[[^\]]*\]", "", str(name or "")).strip()

# ── размеры (показываем у каждого аналога) ──
_HEIGHT_RE = re.compile(r"высот", re.I)
_THREAD_RE = re.compile(r"резьб", re.I)
_OD_RE     = re.compile(r"наружн.*диаметр", re.I)

def _crit_find(crit, rx, skip=("упаков",)):
    if not crit:
        return None
    for name, val in crit.items():
        low = str(name).lower()
        if any(s in low for s in skip):
            continue
        if rx.search(low):
            return str(val)
    return None

def _dims_brief(crit):
    """высота · ⌀ нар. · резьба — компактно (у аналогов отличаются)."""
    parts = []
    h = _crit_find(crit, _HEIGHT_RE)
    if h:
        parts.append(f"высота {h}")
    od = _crit_find(crit, _OD_RE)
    if od:
        parts.append(f"⌀ {od}")
    thr = _crit_find(crit, _THREAD_RE)
    if thr:
        parts.append(f"резьба {thr}")
    return " · ".join(parts)

def _line(idx, item):
    brand = _e((item.get("brand") or "").strip())
    art = _code((item.get("article") or "").strip())
    dims = _dims_brief(item.get("criteria") or {})
    head = f"{idx}. <b>{brand}</b> {art}"
    if dims:
        return head + f"\n     <i>{_e(dims)}</i>"
    return head

def _brand_match(item, nb):
    return _n(item.get("brand")) == nb or (nb and nb in _n(item.get("brand")))

def _car_make(meta):
    b = str(meta.get("brand") or "").strip().upper()
    if b:
        return b
    car = str(meta.get("car_str") or "").strip()
    toks = car.split()
    return toks[0].upper() if toks else ""

# ── OEM: дедуп + чистка + схлопывание ──
def _split_oem(s):
    s = str(s or "").strip()
    if ":" in s:
        mk, num = s.split(":", 1)
        return mk.strip().upper(), num.strip()
    return "", s

def _dedupe_oem(oem_list, car_make=""):
    out, order = {}, []
    for raw in oem_list:
        mk, num = _split_oem(raw)
        k = _n(num)
        if not k:
            continue
        if k not in out:
            out[k] = [mk, num]
            order.append(k)
        elif car_make and mk == car_make and out[k][0] != car_make:
            out[k] = [mk, num]
    return [tuple(out[k]) for k in order]

def _nice_oem(nums):
    """Выкидываем мусорные усечённые номера без пробелов (07815561D и т.п.)."""
    nice = [x for x in nums if (" " in x) or len(_n(x)) >= 10]
    return nice or nums

def _collapse_oem(nums):
    order, groups = [], {}
    for num in nums:
        if isinstance(num, (tuple, list)):     # ← добавить
            num = num[-1] if num else ""        # ← из (mk, num) берём num
        num = str(num)                          # ← добавить
        toks = num.split()
        if len(toks) >= 2:
            stem, last = " ".join(toks[:-1]), toks[-1]
            if stem not in groups:
                groups[stem] = []
                order.append(stem)
            if isinstance(groups[stem], list) and last not in groups[stem]:
                groups[stem].append(last)
        else:
            if num not in groups:
                groups[num] = None
                order.append(num)
    out = []
    for key in order:
        if groups[key] is None:
            out.append(key)
        else:
            out.append(f"{key} " + "/".join(groups[key]))
    return out

def format_vin_reply(meta, *, part_label="", brand_filter=None, top_n=5, max_oem=6):
    """meta из resolve_oem_detailed -> готовый HTML-текст для Telegram."""
    head = _e(part_label or "Деталь")
    car = _e(meta.get("car_str") or "")
    car_make = _car_make(meta)

    reason = meta.get("reason")
    if reason == "no_car_id":
        return ("❌ Не смог определить авто по VIN.\n"
                "Проверь VIN или попробуй позже.")
    if reason == "no_str_id":
        tail = ("\n🚗 " + car) if car else ""
        return (f"🔧 <b>{head}</b>{tail}\n\n"
                "⚠️ Не нашёл эту деталь в каталоге для этой машины. "
                "Уточни название детали.")
    if reason == "no_articles":
        tail = ("\n🚗 " + car) if car else ""
        return (f"🔧 <b>{head}</b>{tail}\n\n"
                "⚠️ Деталь в каталоге есть, но артикулы по ней не найдены.")

    summary = meta.get("summary") or {}
    exact = list(summary.get("exact") or [])
    top = list(summary.get("top") or [])
    oem_raw = list(summary.get("oem_numbers") or [])

    if brand_filter:
        nb = _n(brand_filter)
        exact = [r for r in exact if _brand_match(r, nb)]
        top = [r for r in top if _brand_match(r, nb)]

    seen = {(_n(r.get("brand")), _n(r.get("article"))) for r in exact}
    maybe = [r for r in top
             if (_n(r.get("brand")), _n(r.get("article"))) not in seen]

    deduped = _dedupe_oem(oem_raw, car_make)
    anchor = [num for mk, num in deduped if mk == car_make]
    if not anchor and car_make in _VAG:
        anchor = [num for mk, num in deduped if mk in _VAG]
    if not anchor:
        anchor = [num for _mk, num in deduped]
    oem_show = _collapse_oem(_nice_oem(anchor))[:max_oem]

    L = [f"🔧 <b>{head}</b>"]
    if car:
        L.append(f"🚗 <b>{car}</b>")
    if meta.get("engine_code"):
        L.append(f"🛠 Двигатель: {_code(meta['engine_code'])}")
    L.append("━" * 18)

    # --- OEM: сначала ГЛАВНЫЙ (якорь), потом остальные ---
    deduped = _dedupe_oem(summary.get("anchor_oems") or [])
    anchor  = _collapse_oem([num for _mk, num in deduped])[:3]
    rest = [o for o in oem_show if o not in anchor]

    if anchor:
        L.append("⭐ <b>Оригинал (наиболее вероятный):</b>")
        L.append("   " + " · ".join(_code(x) for x in anchor))
        if rest:
            L.append("🔩 <b>Другие OEM-номера:</b>")
            L.append("   " + " · ".join(_code(x) for x in rest))
    elif oem_show:
        L.append("🔩 <b>Оригинал (OEM):</b>")
        L.append("   " + " · ".join(_code(x) for x in oem_show))
    else:
        L.append("🔩 <b>Оригинал (OEM):</b> не определён")
    L.append("")

    if exact:
        L.append("✅ <b>Рекомендуем</b> "
                 "<i>(сверьте размеры — у аналогов отличаются)</i>")
        for i, r in enumerate(exact[:top_n], 1):
            L.append(_line(i, r))
        L.append("")

    if maybe:
        title = ("🔁 <b>Другие аналоги</b>" if exact
                 else "🔁 <b>Подходящие</b> <i>(точный якорь не найден)</i>")
        L.append(title)
        for i, r in enumerate(maybe[:top_n], 1):
            L.append(_line(i, r))
        L.append("")

    if not exact and not maybe:
        if brand_filter:
            L.append(f"⚠️ По бренду {_e(brand_filter)} ничего не нашлось. "
                     "Попробуй без фильтра бренда.")
        else:
            L.append("⚠️ Не удалось подобрать аналоги.")

    L.append("ℹ️ Сверяйте OEM с маркировкой на старой детали перед покупкой.")
    return "\n".join(L).rstrip()

# ============================================================
# Распознавание бренда из запроса (без изменений)
# ============================================================
_KNOWN_BRANDS = ("NGK", "DENSO", "BOSCH", "BERU", "CHAMPION", "MANN",
                 "MAHLE", "KNECHT", "FILTRON", "HENGST", "FEBI", "SACHS",
                 "LEMFORDER", "TRW", "ATE", "VALEO", "SKF", "INA", "LUK",
                 "CONTITECH", "GATES", "ELRING", "BREMBO", "TEXTAR",
                 "ZIMMERMANN", "HELLA", "BLUEPRINT", "JAPANPARTS",
                 "RUVILLE", "VAICO", "SWAG", "MEYLE", "UFI", "PURFLUX",
                 "SOFIMA", "WIX", "FERODO")

def extract_brand_from_query(part_text):
    if not part_text:
        return part_text, None
    tokens = str(part_text).split()
    if not tokens:
        return part_text, None
    nb_last = _n(tokens[-1])
    if len(nb_last) >= 2:
        for b in _KNOWN_BRANDS:
            if _n(b) == nb_last:
                return " ".join(tokens[:-1]).strip(), b
    return part_text, None
