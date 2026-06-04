from __future__ import annotations
import csv
import re
import logging
from pathlib import Path
from typing import Dict, List, Any

logger = logging.getLogger(__name__)


class PartsResolver:
    def __init__(self, data_dir: str = "parts"):
        self.data_dir = Path(data_dir)
        self.alias_map: Dict[str, dict] = {}       # normalized_alias → row
        self.part_name_map: Dict[str, dict] = {}   # part_name → row
        self.synonym_map: Dict[str, dict] = {}     # synonym → row
        self.modifier_map: Dict[str, str] = {}     # modifier → modifier_type
        self._load_all()
        logger.info(f"PartsResolver: {len(self.alias_map)} aliases, "
                    f"{len(self.part_name_map)} parts, "
                    f"{len(self.synonym_map)} synonyms, "
                    f"{len(self.modifier_map)} modifiers")

    def _read_csv(self, filename: str) -> List[Dict[str, str]]:
        path = self.data_dir / filename
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f, delimiter=";"))

    def _norm(self, text: str) -> str:
        text = (text or "").strip().lower()
        text = text.replace("ё", "е")
        text = re.sub(r"[^a-zа-я0-9\s-]", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _tokenize(self, text: str) -> List[str]:
        return [x for x in self._norm(text).split() if x]

    def _load_all(self):
        # 1. parts_dictionary — part_name + synonyms
        for row in self._read_csv("parts_dictionary_v3.csv"):
            pname = self._norm(row.get("part_name", ""))
            if pname:
                self.part_name_map[pname] = row
            for s in (row.get("synonyms") or "").split(","):
                s = self._norm(s)
                if s:
                    self.synonym_map[s] = row

        # 2. query_aliases — главный источник
        for row in self._read_csv("query_aliases_v3.csv"):
            alias = self._norm(row.get("normalized_alias") or row.get("alias") or "")
            if alias:
                self.alias_map[alias] = row

        # 3. query_modifiers
        for row in self._read_csv("query_modifiers_v3.csv"):
            key = self._norm(row.get("modifier", ""))
            if key:
                self.modifier_map[key] = row.get("modifier_type", "attribute")

    def _extract_modifiers(self, text: str) -> Dict[str, List[str]]:
        tokens = self._tokenize(text)
        out: Dict[str, List[str]] = {"side": [], "position": [], "attribute": []}
        for token in tokens:
            t = self._norm(token)
            mtype = self.modifier_map.get(t)
            if t in ("левый", "правый"):
                out["side"].append(t)
            elif t in ("передний", "задний", "верхний", "нижний", "внутренний", "наружный"):
                out["position"].append(t)
            elif mtype:
                out["attribute"].append(t)
        return out

    def _strip_modifiers(self, text: str) -> str:
        tokens = self._tokenize(text)
        return " ".join(t for t in tokens if t not in self.modifier_map).strip()

    def _sorted_key(self, text: str) -> str:
        return " ".join(sorted(self._tokenize(text)))

    def _make_result(self, row: dict, original: str, normalized: str,
                     modifiers: dict, match_type: str, confidence: float) -> dict:
        return {
            "original_query": original,
            "normalized_query": normalized,
            "matched": True,
            "part_name": row.get("part_name", ""),
            "group_name": row.get("group_name", ""),
            "subgroup_name": row.get("subgroup_name", ""),
            "part_key": row.get("part_key", self._norm(row.get("part_name", "")).replace(" ", "_")),
            "modifiers": modifiers,
            "match_type": match_type,
            "confidence": confidence,
        }

    def resolve(self, query: str) -> Dict[str, Any]:
        original = query
        normalized = self._norm(query)
        modifiers = self._extract_modifiers(normalized)
        stripped = self._strip_modifiers(normalized)

        # 1. Точный alias
        if normalized in self.alias_map:
            return self._make_result(self.alias_map[normalized], original, normalized,
                                     modifiers, "alias_exact", 0.99)

        # 2. Alias без модификаторов
        if stripped and stripped in self.alias_map:
            return self._make_result(self.alias_map[stripped], original, normalized,
                                     modifiers, "alias_stripped", 0.95)

        # 3. Точный part_name
        if stripped in self.part_name_map:
            return self._make_result(self.part_name_map[stripped], original, normalized,
                                     modifiers, "part_name_exact", 0.90)

        # 4. Синоним
        if stripped in self.synonym_map:
            return self._make_result(self.synonym_map[stripped], original, normalized,
                                     modifiers, "synonym_exact", 0.85)

        # 5. Alias по отсортированным токенам (порядок слов не важен)
        stripped_sorted = self._sorted_key(stripped)
        for alias, row in self.alias_map.items():
            if self._sorted_key(alias) == stripped_sorted:
                return self._make_result(row, original, normalized,
                                         modifiers, "alias_token_sorted", 0.75)

        # 6. Частичное совпадение по токенам (хотя бы 2 общих слова)
        tokens = set(self._tokenize(stripped))
        best_score, best_row = 0, None
        for alias, row in self.alias_map.items():
            score = len(tokens & set(self._tokenize(alias)))
            if score > best_score and score >= 2:
                best_score, best_row = score, row
        if best_row:
            return self._make_result(best_row, original, normalized,
                                     modifiers, "alias_partial", 0.60)

        # 7. Не найдено
        suggestions = self._suggest(stripped)
        return {
            "original_query": original,
            "normalized_query": normalized,
            "matched": False,
            "part_name": None,
            "group_name": None,
            "subgroup_name": None,
            "part_key": None,
            "modifiers": modifiers,
            "match_type": "not_found",
            "confidence": 0.0,
            "suggestions": suggestions[:5],
        }

    def _suggest(self, text: str) -> List[str]:
        tokens = set(self._tokenize(text))
        scored = []
        for alias in self.alias_map:
            score = len(tokens & set(self._tokenize(alias)))
            if score > 0:
                scored.append((score, alias))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [x[1] for x in scored[:5]]