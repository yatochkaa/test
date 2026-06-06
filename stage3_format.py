# -*- coding: utf-8 -*-
# ============================================================
# ЭТАП 3 — КАЧЕСТВО ВЫДАЧИ (формат ответа пользователю)
# ============================================================
# Самодостаточный форматтер. На вход — meta из
# tecdoc.resolve_oem_detailed(...). На выход — готовый текст для Telegram.
#
# Даёт 4 улучшения:
#   1) разделение ✅ Точно подходят / 📋 Возможные
#   2) OEM-якорь (вероятный оригинал машины) отдельно
#   3) "Сторона установки" и ключевые размеры в строке аналога
#   4) опциональный фильтр по бренду (NGK / BOSCH / ...)
#
# ВСТАВКА (в test_vin_bot.py, внутри cmd_vin):
#   meta = await tecdoc.resolve_oem_detailed(session, vin, cat_id, part_name, keywords)
#   text = format_vin_reply(meta, part_label=part_name)        # весь ответ
#   # если юзер просил конкретный бренд: brand_filter="NGK"
#   await update.message.reply_text(text)
# ============================================================

import re


def _n(s):
    """Нормализация для сравнения брендов/номеров."""
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


# Ключи критериев, которые имеет смысл показывать рядом с аналогом.
_SIDE_KEYS = ("сторона установки", "ось установки", "монтажная сторона")
_SIZE_KEYS = ("высота", "диаметр", "резьба", "ширина", "длина", "толщина")


def _side_of(crit):
    """Из criteria вытаскивает 'Сторону установки' (Передняя ось / Левая...)."""
    if not crit:
        return None
    for name, val in crit.items():
        low = str(name).lower()
        if any(k in low for k in _SIDE_KEYS):
            return str(val)
    return None


def _size_brief(crit, limit=3):
    """Короткая сводка размеров для строки аналога."""
    if not crit:
        return ""
    parts = []
    for name, val in crit.items():
        if any(k in str(name).lower() for k in _SIZE_KEYS):
            parts.append(f"{name}={val}")
        if len(parts) >= limit:
            break
    return "; ".join(parts)


def _line(item):
    """Одна строка аналога: • BRAND ARTICLE — название [сторона; размеры]."""
    crit = item.get("criteria") or {}
    extra = []
    side = _side_of(crit)
    if side:
        extra.append(side)
    sz = _size_brief(crit)
    if sz:
        extra.append(sz)
    tail = f"  [{'; '.join(extra)}]" if extra else ""
    name = item.get("product_name") or ""
    name_s = f" — {name}" if name else ""
    return f"  • {item.get('brand')} {item.get('article')}{name_s}{tail}"


def _brand_match(item, nb):
    return _n(item.get("brand")) == nb or (nb and nb in _n(item.get("brand")))


def format_vin_reply(meta, *, part_label="", brand_filter=None,
                     top_n=5, max_oem=8):
    """meta из resolve_oem_detailed -> готовый текст для Telegram.
    brand_filter: если задан (напр. 'NGK') — в аналогах остаётся только этот бренд.
    """
    head = part_label or "Деталь"
    car = meta.get("car_str") or ""

    # — обработка неудач (разные reason) —
    reason = meta.get("reason")
    if reason == "no_car_id":
        return (f"❌ Не смог определить авто по VIN.\n"
                f"Проверь VIN или попробуй позже.")
    if reason == "no_str_id":
        return (f"🔧 {head}\n"
                f"Авто: {car}\n\n"
                f"⚠️ Не нашёл эту деталь в каталоге для этой машины. "
                f"Уточни название детали.")
    if reason == "no_articles":
        return (f"🔧 {head}\n"
                f"Авто: {car}\n\n"
                f"⚠️ Деталь в каталоге есть, но артикулы по ней не найдены.")

    summary = meta.get("summary") or {}
    exact = list(summary.get("exact") or [])
    top = list(summary.get("top") or [])
    oem = list(summary.get("oem_numbers") or [])
    anchor = list(summary.get("anchor_oems") or [])
    groups = summary.get("groups") or {}

    # — фильтр по бренду (опционально) —
    if brand_filter:
        nb = _n(brand_filter)
        exact = [r for r in exact if _brand_match(r, nb)]
        top = [r for r in top if _brand_match(r, nb)]

    # возможные = top без тех, что уже в exact
    seen = {(_n(r.get("brand")), _n(r.get("article"))) for r in exact}
    maybe = [r for r in top
             if (_n(r.get("brand")), _n(r.get("article"))) not in seen]

    lines = [f"🔧 {head}"]
    if car:
        lines.append(f"Авто: {car}")
    if meta.get("engine_code"):
        lines.append(f"Двигатель: {meta['engine_code']}")
    lines.append("")

    # — OEM (оригинал) —
    if oem:
        lines.append("⚙️ Оригинал (OEM): " + ", ".join(oem[:max_oem]))
        if anchor:
            lines.append("⚓ Вероятный OEM этой машины: " + ", ".join(anchor[:5]))
    else:
        lines.append("⚙️ Оригинал (OEM): — не отдался (проверь ключ getArticle)")
    lines.append("")

    # — ✅ точно подходят —
    if exact:
        lines.append("✅ Точно подходят (по OEM-якорю):")
        for r in exact[:top_n]:
            lines.append(_line(r))
        lines.append("")

    # — 📋 возможные —
    if maybe:
        title = "📋 Возможные аналоги:" if exact else "📋 Подходящие (якорь не найден):"
        lines.append(title)
        for r in maybe[:top_n]:
            lines.append(_line(r))
        lines.append("")

    if not exact and not maybe:
        if brand_filter:
            lines.append(f"⚠️ По бренду {brand_filter} ничего не нашлось. "
                         f"Попробуй без фильтра бренда.")
        else:
            lines.append("⚠️ Не удалось подобрать аналоги.")

    # — 📐 разные размеры/исполнения (подсказка для отсева) —
    if len(groups) > 1:
        lines.append("📐 Есть разные размеры/исполнения — сверьте со старой деталью:")
        for sig, arts in list(groups.items())[:4]:
            ex = ", ".join(str(a) for a in arts[:3])
            lines.append(f"  – {sig}: {len(arts)} шт (напр. {ex})")
        lines.append("")

    # хвост
    lines.append("ℹ️ Сверяйте OEM с маркировкой на старой детали перед покупкой.")
    return "\n".join(lines).rstrip()


# ============================================================
# ОПЦИОНАЛЬНО: распознавание бренда из запроса юзера
# "/vin ... свечи NGK" -> part_name="свечи", brand_filter="NGK"
# ============================================================
_KNOWN_BRANDS = ("NGK", "DENSO", "BOSCH", "BERU", "CHAMPION", "MANN",
                 "MAHLE", "KNECHT", "FILTRON", "HENGST", "SACHS", "BILSTEIN",
                 "KYB", "KAYABA", "MONROE", "FEBI", "SWAG", "MEYLE", "TRW",
                 "ATE", "BREMBO", "TEXTAR", "FERODO", "VALEO", "HELLA",
                 "LEMFORDER", "SKF", "FAG", "INA", "LUK", "GATES", "CONTITECH",
                 "ELRING", "VICTOR REINZ", "DELPHI", "AISIN")


def extract_brand_from_query(text):
    """Из текста запроса выделяет бренд, если юзер его указал.
    Возвращает (очищенный_текст_без_бренда, brand|None)."""
    if not text:
        return text, None
    up = text.upper()
    for b in _KNOWN_BRANDS:
        if re.search(r"\b" + re.escape(b) + r"\b", up):
            cleaned = re.sub(re.escape(b), "", text, flags=re.I).strip()
            cleaned = re.sub(r"\s{2,}", " ", cleaned)
            return cleaned, b
    return text, None
