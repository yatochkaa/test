# -*- coding: utf-8 -*-
"""Рантайм-резолвер «фраза → strId». Данные в parts_str_map.json.

Порядок поиска:
  0) препроцессинг: нормализация + сленг (помпа→водяной насос) +
     отбрасывание слов стороны/позиции (передние/задние/левый…)
  1) HARDCODED / синоним  (точный)
  2) точное совпадение в parts_index
  3) hardcoded-фраза внутри запроса («водяной насос охлаждающей жидкости»)
  4) стемминг-оверлап по дереву (со специфичностью)
  5) fuzzy (опечатки)

API совместим со старым: resolve(query, topn=3) -> list[dict]
  каждый dict: {str_id, method, key, ...}. Первый — лучший.

!!! ВАЖНО для tecdoc.resolve_str_id:
    ранний возврат БЕЗ запроса к дереву машины допустим ТОЛЬКО когда
    method начинается с 'hardcoded' или 'exact'. Остальные (substr/stem/fuzzy)
    служат страховкой и не обходят точный матч по дереву.
"""
import json, re, os, unicodedata, difflib
from collections import defaultdict

_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA = json.load(open(os.path.join(_DIR, "parts_str_map.json"), encoding="utf-8"))
PARTS_INDEX = _DATA["parts_index"]
SYNONYMS = _DATA["synonyms"]
HARDCODED = {k: int(v) for k, v in _DATA["hardcoded"].items()}

# слова стороны/позиции/количества — выкидываем перед сопоставлением
_SIDE = {
    "передний", "передняя", "переднее", "передние", "переднего", "передних", "переднем", "перед",
    "задний", "задняя", "заднее", "задние", "заднего", "задних", "заднем", "зад",
    "левый", "левая", "левое", "левые", "левого", "правый", "правая", "правое", "правые", "правого",
    "верхний", "верхняя", "нижний", "нижняя", "внутренний", "наружный", "внешний",
    "сторона", "стороны", "ось", "оси", "комплект", "кт",
}

# бытовой сленг: одно слово -> каноническая фраза (токены)
_SLANG = {
    "помпа": "водяной насос", "водокачка": "водяной насос",
    "граната": "шрус", "гранату": "шрус", "гранаты": "шрус",
}


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").lower().replace("\u0451", "\u0435")
    s = re.sub(r"[^0-9a-z\u0430-\u044f]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _stem(t):
    """Грубый русский стеммер: срезаем частые окончания, если остаётся >=4 буквы."""
    if len(t) <= 4:
        return t
    for suf in ("иями", "ями", "ами", "ого", "его", "ому", "ему", "ыми", "ими",
                "ой", "ый", "ий", "ая", "яя", "ое", "ее", "ые", "ие",
                "ов", "ев", "ом", "ем", "ах", "ях", "ью",
                "и", "ы", "а", "я", "о", "е", "у", "ю", "ь", "й"):
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            return t[:-len(suf)]
    return t


def _stoks(s):
    """множество стеммированных токенов (>=3 буквы, без слов стороны)"""
    return {_stem(t) for t in s.split() if len(t) >= 3 and t not in _SIDE}


_KEY_STOKS = {k: _stoks(k) for k in PARTS_INDEX}


def _preprocess(query):
    """-> (full, core): full = со сленг-заменой; core = ещё и без слов стороны."""
    q = norm(query)
    toks = []
    for t in q.split():
        toks += _SLANG[t].split() if t in _SLANG else [t]
    full = " ".join(toks)
    core_toks = [t for t in toks if t not in _SIDE]
    core = " ".join(core_toks) if core_toks else full
    return full, core


def resolve(query, topn=3):
    """Возвращает list[dict]: [{str_id, method, key}]. Первый — лучший."""
    full, core = _preprocess(query)
    if not core:
        return []

    # 1) точный hardcoded / синоним / точный индекс (core и full)
    for cand in dict.fromkeys([core, full]):
        canon = SYNONYMS.get(cand, cand)
        if cand in HARDCODED:
            return [{"str_id": HARDCODED[cand], "method": "hardcoded", "key": cand}]
        if canon in HARDCODED:
            return [{"str_id": HARDCODED[canon], "method": "hardcoded/syn", "key": canon}]
        if canon in PARTS_INDEX:
            return [dict(c, method="exact/syn", key=canon) for c in PARTS_INDEX[canon][:topn]]

    canon = SYNONYMS.get(core, core)

    # 3) стемминг-оверлап со специфичностью — ОСНОВНОЙ матч по дереву
    #    сам выбирает самый специфичный ключ по покрытию (бьет общие однословные)
    #    NB: method 'stem' — НЕ обходит дерево в tecdoc (служит страховкой)
    qs = _stoks(canon)
    weak = []
    if qs:
        scored = []
        for k, ks in _KEY_STOKS.items():
            if not ks:
                continue
            ov = len(qs & ks)
            if not ov:
                continue
            # (число совпавших, полнота покрытия ключа, покрытие запроса, длиннее/специфичнее ключ)
            scored.append(((ov, round(ov / len(ks), 3), round(ov / len(qs), 3), len(k)), k))
        scored.sort(key=lambda x: x[0], reverse=True)
        if scored:
            res = []
            for _, k in scored[:topn]:
                res += [dict(c, method="stem", key=k) for c in PARTS_INDEX[k]]
            res = res[:topn]
            best = scored[0][0]
            # уверенный результат: >=2 совпадений или ключ покрыт полностью
            if best[0] >= 2 or best[1] >= 0.999:
                return res
            weak = res  # слабый одиночный токен — придержим на случай пустого fuzzy

    # 4) hardcoded-фраза целиком внутри запроса (самая длинная → самая специфичная)
    hc_in = sorted([h for h in HARDCODED if len(h) >= 5 and h in canon], key=len, reverse=True)
    if hc_in:
        h = hc_in[0]
        return [{"str_id": HARDCODED[h], "method": "substr/hard", "key": h}]

    # 4.5) одиночный токен == точный ключ дерева
    _idx_tok = sorted([t for t in core.split() if len(t) >= 5 and t in PARTS_INDEX],
                      key=len, reverse=True)
    if _idx_tok:
        k = _idx_tok[0]
        return [dict(c, method="idx-token", key=k) for c in PARTS_INDEX[k]][:topn]

    # 5) fuzzy (опечатки) — сначала по дереву, потом по hardcoded
    cm = difflib.get_close_matches(canon, list(PARTS_INDEX), n=topn, cutoff=0.78)
    if cm:
        res = []
        for k in cm:
            res += [dict(c, method="fuzzy", key=k) for c in PARTS_INDEX[k]]
        return res[:topn]
    cmh = difflib.get_close_matches(canon, list(HARDCODED), n=1, cutoff=0.74)
    if cmh:
        h = cmh[0]
        return [{"str_id": HARDCODED[h], "method": "fuzzy/hard", "key": h}]

    return weak


if __name__ == "__main__":
    _T = [
        "Тормозные колодки", "масляный фильтр", "колодки", "маслофильтр", "свечи",
        "салонный фильтр", "тормазные калодки", "передние тормозные колодки",
        "крышка маслозаливной горловины", "прокладка головки блока цилиндров",
        "натяжной ролик ремня ГРМ", "подшипник ступицы передний",
        "стеклоподъемник передний левый", "помпа охлаждающей жидкости",
        "сайлентблок переднего рычага", "лямбда зонд", "опора амортизатора",
        "ремкомплект суппорта", "задние колодки", "стойка амортизатора",
    ]
    for _q in _T:
        _h = resolve(_q, topn=2)
        if _h:
            print(f"{_q!r:42} -> {_h[0].get('str_id')!s:8} [{_h[0].get('method')}] key={_h[0].get('key')!r}")
        else:
            print(f"{_q!r:42} -> (нет совпадений)")
