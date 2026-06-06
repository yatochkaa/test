# -*- coding: utf-8 -*-
# ============================================================
# ЭТАП 3 — КАЧЕСТВО ВЫДАЧИ (HTML-формат ответа) — v2
# ============================================================
# format_vin_reply(meta, part_label=..., brand_filter=None) -> готовый HTML.
# ОТПРАВЛЯТЬ С parse_mode="HTML".
#
# v2 изменения:
#   • OEM дедуплицируются по номеру (4B0 698 151 AB не повторяется 3 раза)
#   • "Вероятный OEM" — только по марке авто (чужие ALFA/FIAT убираются)
#   • критерии без "[мм]" и в полезном порядке (Высота/Ширина/...)
# ============================================================

import re
from html import escape

# Группа VAG — взаимозаменяемые марки (один концерн).
_VAG = {"AUDI", "VW", "VOLKSWAGEN", "SKODA", "SEAT", "CUPRA", "PORSCHE"}


def _n(s):
    """Нормализация для сравнения брендов/номеров."""
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def _e(s):
    """HTML-escape произвольного текста."""
    return escape(str(s or ""), quote=False)


def _code(s):
    return f"<code>{_e(s)}</code>"


def _clean_key(name):
    """'Высота [мм]' -> 'Высота'."""
    return re.sub(r"\s*\[[^\]]*\]", "", str(name or "")).strip()


_SIDE_KEYS = ("сторона установки", "ось установки", "монтажная сторона")
_SIZE_KEYS = ("высота", "диаметр", "резьба", "ширина", "длина", "толщина")
# Порядок показа размеров (самое полезное для подбора — первым).
_SIZE_ORDER = ("высота", "ширина", "длина", "диаметр", "толщина", "резьба")


def _side_of(crit):
    if not crit:
        return None
    for name, val in crit.items():
        if any(k in str(name).lower() for k in _SIDE_KEYS):
            return str(val)
    return None


def _size_brief(crit, limit=3):
    if not crit:
        return ""
    found = []
    for name, val in crit.items():
        if any(k in str(name).lower() for k in _SIZE_KEYS):
            found.append((_clean_key(name), str(val)))

    def _rank(item):
        low = item[0].lower()
        for i, k in enumerate(_SIZE_ORDER):
            if k in low:
                return i
        return len(_SIZE_ORDER)

    found.sort(key=_rank)
    return "; ".join(f"{n}={v}" for n, v in found[:limit])


def _line(item):
    """Бренд + <code>артикул</code> + название + [сторона; размеры]."""
    brand = _e(item.get("brand"))
    art = _code(item.get("article"))
    name = str(item.get("product_name") or "")
    if len(name) > 46:
        name = name[:45].rstrip() + "…"
    name_s = f" — {_e(name)}" if name else ""
    crit = item.get("criteria") or {}
    tags = []
    side = _side_of(crit)
    if side:
        tags.append(_e(side))
    size = _size_brief(crit)
    if size:
        tags.append(_e(size))
    tail = f"  [{'; '.join(tags)}]" if tags else ""
    return f"  • <b>{brand}</b> {art}{name_s}{tail}"


def _brand_match(item, nb):
    return _n(item.get("brand")) == nb or (nb and nb in _n(item.get("brand")))


def _car_make(meta):
    b = str(meta.get("brand") or "").strip().upper()
    if b:
        return b
    car = str(meta.get("car_str") or "").strip()
    toks = car.split()
    return toks[0].upper() if toks else ""


def _split_oem(s):
    """'AUDI: 4B0 698 151 AB' -> ('AUDI', '4B0 698 151 AB'); '078...' -> ('', '078...')."""
    s = str(s or "").strip()
    if ":" in s:
        mk, num = s.split(":", 1)
        return mk.strip().upper(), num.strip()
    return "", s


def _dedupe_oem(oem_list, car_make=""):
    """Дедуп по номеру. Возвращает упорядоченный list[(make, num)].
    Если один номер встречается под разными марками — предпочитаем марку авто."""
    out = {}
    order = []
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


def format_vin_reply(meta, *, part_label="", brand_filter=None,
                     top_n=5, max_oem=8):
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
    groups = summary.get("groups") or {}

    if brand_filter:
        nb = _n(brand_filter)
        exact = [r for r in exact if _brand_match(r, nb)]
        top = [r for r in top if _brand_match(r, nb)]

    seen = {(_n(r.get("brand")), _n(r.get("article"))) for r in exact}
    maybe = [r for r in top
             if (_n(r.get("brand")), _n(r.get("article"))) not in seen]

    # — OEM: дедуп по номеру, марка авто вперёд —
    deduped = _dedupe_oem(oem_raw, car_make)

    def _grp_rank(mn):
        mk = mn[0]
        if mk == car_make:
            return 0
        if mk in _VAG and car_make in _VAG:
            return 1
        return 2

    main = sorted(deduped, key=_grp_rank)
    # "Вероятный OEM этой машины" = только номера своей марки.
    anchor = [num for mk, num in deduped if mk == car_make][:5]
    if not anchor and car_make in _VAG:
        anchor = [num for mk, num in deduped if mk in _VAG][:5]

    lines = [f"🔧 <b>{head}</b>"]
    if car:
        lines.append(f"🚗 {car}")
    if meta.get("engine_code"):
        lines.append(f"Двигатель: {_code(meta['engine_code'])}")
    lines.append("─" * 24)

    if main:
        lines.append("⚙️ <b>Оригинал (OEM):</b>")
        lines.append("  " + ", ".join(_code(num) for _mk, num in main[:max_oem]))
        if anchor:
            lines.append("⚓ Вероятный OEM этой машины: "
                         + ", ".join(_code(x) for x in anchor))
    else:
        lines.append("⚙️ <b>Оригинал (OEM):</b> не определён")
    lines.append("")

    if exact:
        lines.append("✅ <b>Точно подходят</b> (по OEM-якорю):")
        for r in exact[:top_n]:
            lines.append(_line(r))
        lines.append("")

    if maybe:
        title = ("📋 <b>Возможные аналоги:</b>" if exact
                 else "📋 <b>Подходящие</b> (якорь не найден):")
        lines.append(title)
        for r in maybe[:top_n]:
            lines.append(_line(r))
        lines.append("")

    if not exact and not maybe:
        if brand_filter:
            lines.append(f"⚠️ По бренду <b>{_e(brand_filter)}</b> ничего не нашлось. "
                         "Попробуй без фильтра бренда.")
        else:
            lines.append("⚠️ Не удалось подобрать аналоги.")

    if len(groups) > 1:
        lines.append("📐 <b>Разные размеры/исполнения</b> — сверьте со старой деталью:")
        for sig, arts in list(groups.items())[:4]:
            ex = ", ".join(_code(a) for a in arts[:3])
            lines.append(f"  – {_e(_clean_key(sig))}: {len(arts)} шт (напр. {ex})")
        lines.append("")

    lines.append("ℹ️ <i>Сверяйте OEM с маркировкой на старой детали перед покупкой.</i>")
    return "\n".join(lines).rstrip()


# ============================================================
# ОПЦИОНАЛЬНО: распознавание бренда из запроса
# "свечи зажигания NGK" -> ("свечи зажигания", "NGK")
# ============================================================
_KNOWN_BRANDS = ("NGK", "DENSO", "BOSCH", "BERU", "CHAMPION", "MANN",
                 "MAHLE", "KNECHT", "FILTRON", "HENGST", "FEBI", "SACHS",
                 "LEMFORDER", "TRW", "ATE", "VALEO", "SKF", "INA", "LUK",
                 "CONTITECH", "GATES", "ELRING", "BREMBO", "TEXTAR",
                 "ZIMMERMANN", "HELLA", "BLUEPRINT", "JAPANPARTS",
                 "RUVILLE", "VAICO", "SWAG", "MEYLE", "UFI", "PURFLUX",
                 "SOFIMA", "WIX", "FERODO")


def extract_brand_from_query(part_text):
    """'свечи зажигания NGK' -> ('свечи зажигания', 'NGK'). Нет бренда -> (text, None)."""
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
