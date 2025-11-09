# core/i18n/glossary_loader.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Iterable

@dataclass
class Glossary:
    keep: Dict[str, bool]           # terms to keep as-is
    map_to: Dict[str, str]          # future use: map 'foo' -> 'bar' (per locale if needed)

    @classmethod
    def from_tsv(cls, path: str) -> "Glossary":
        keep, map_to = {}, {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    cols = line.split("\t")
                    if len(cols) == 1:
                        keep[cols[0]] = True
                    elif len(cols) >= 2:
                        action = cols[1].lower()
                        if action == "keep":
                            keep[cols[0]] = True
                        elif action == "map" and len(cols) >= 3:
                            map_to[cols[0]] = cols[2]
        except Exception:
            pass
        return cls(keep=keep, map_to=map_to)

    def keep_terms(self) -> Dict[str, bool]:
        return self.keep

    def mapping(self) -> Dict[str, str]:
        return self.map_to
