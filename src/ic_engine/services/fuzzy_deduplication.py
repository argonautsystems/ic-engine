#!/usr/bin/env python3
"""
Fuzzy symbol deduplication for portfolio consolidation.

Identifies symbol variations and duplicates using fuzzy string matching:
- "GOOG" vs "GOOGL" vs "GOOGLE" (different instruments, not duplicates)
- "AAPL" vs "AAPL " vs "aapl" (same holding, whitespace/case variation)
- "BRK.B" vs "BRK-B" (broker format differences)

Uses rapidfuzz for fast fuzzy matching across large portfolios.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Set

try:
    from rapidfuzz import fuzz

    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False

logger = logging.getLogger(__name__)


@dataclass
class FuzzyMatch:
    """Result of fuzzy matching."""

    symbol1: str
    symbol2: str
    similarity: float  # 0.0-1.0
    match_type: str  # 'exact', 'fuzzy', 'case_normalized', 'format_variation'


class FuzzyDeduplicator:
    """
    Detect duplicate symbols with fuzzy matching.

    Handles:
    - Case variations (AAPL, aapl, AaPl)
    - Whitespace (AAPL, AAPL , ` AAPL`)
    - Format variations (BRK.B vs BRK-B)
    - Common ticker aliases (GOOG for Alphabet)
    """

    # Known symbol equivalences (exact matches that aren't true duplicates)
    KNOWN_VARIATIONS = {
        "GOOG": {"GOOGL"},  # GOOG (class C) != GOOGL (class A) — different instruments
        "BRK": {"BRK.A", "BRK-A", "BRK.B", "BRK-B"},  # Same company, different share classes
        "NVDA": {"NVDQ"},  # NVIDIA — different exchanges
    }

    # Format variations that should be treated as duplicates
    FORMAT_VARIATIONS = {
        ".": "-",  # AAPL.PR -> AAPL-PR
        " ": "",  # AAPL PR -> AAPLPR
    }

    SIMILARITY_THRESHOLD = 0.90  # 90% similarity = likely duplicate

    def __init__(self):
        if not RAPIDFUZZ_AVAILABLE:
            logger.warning("rapidfuzz not available; fuzzy deduplication disabled")

    def _normalize_symbol(self, symbol: str) -> str:
        """Normalize symbol for comparison."""
        return symbol.strip().upper()

    def _try_format_variations(self, symbol1: str, symbol2: str) -> bool:
        """Check if symbols match after format variation normalization."""
        norm1 = symbol1
        norm2 = symbol2

        # Try replacing format variations
        for char_from, char_to in self.FORMAT_VARIATIONS.items():
            norm1 = norm1.replace(char_from, char_to)
            norm2 = norm2.replace(char_from, char_to)

        return norm1 == norm2

    def find_duplicates(self, symbols: List[str]) -> List[FuzzyMatch]:
        """
        Find potential duplicate symbols using fuzzy matching.

        Returns list of FuzzyMatch objects with similarity scores.
        """
        if not RAPIDFUZZ_AVAILABLE:
            return []

        matches = []
        normalized = {sym: self._normalize_symbol(sym) for sym in symbols}
        unique_symbols = list(set(normalized.values()))

        # Compare each pair
        for i, sym1 in enumerate(unique_symbols):
            for sym2 in unique_symbols[i + 1 :]:
                if sym1 == sym2:
                    continue

                # Check if known non-duplicate variation
                if self._is_known_variation(sym1, sym2):
                    continue

                # Check format variations
                if self._try_format_variations(sym1, sym2):
                    matches.append(
                        FuzzyMatch(
                            symbol1=sym1,
                            symbol2=sym2,
                            similarity=1.0,
                            match_type="format_variation",
                        )
                    )
                    continue

                # Check case normalization
                if sym1.replace(" ", "") == sym2.replace(" ", ""):
                    matches.append(
                        FuzzyMatch(
                            symbol1=sym1, symbol2=sym2, similarity=1.0, match_type="case_normalized"
                        )
                    )
                    continue

                # Fuzzy string matching
                similarity = fuzz.ratio(sym1, sym2) / 100.0
                if similarity >= self.SIMILARITY_THRESHOLD:
                    matches.append(
                        FuzzyMatch(
                            symbol1=sym1, symbol2=sym2, similarity=similarity, match_type="fuzzy"
                        )
                    )

        return sorted(matches, key=lambda x: x.similarity, reverse=True)

    def _is_known_variation(self, sym1: str, sym2: str) -> bool:
        """Check if symbols are known non-duplicate variations."""
        for base, variations in self.KNOWN_VARIATIONS.items():
            if (sym1 == base and sym2 in variations) or (sym2 == base and sym1 in variations):
                return True
            if sym1 in variations and sym2 in variations:
                return True
        return False

    def group_duplicates(self, symbols: List[str]) -> Dict[str, List[str]]:
        """
        Group symbols into duplicate clusters.

        Returns dict mapping canonical symbol to list of variants.
        """
        matches = self.find_duplicates(symbols)
        if not matches:
            return {sym: [sym] for sym in set(symbols)}

        # Build graph of connections
        graph: Dict[str, Set[str]] = {sym: {sym} for sym in set(symbols)}

        for match in matches:
            # Union-find approach: connect matched symbols
            sym1, sym2 = match.symbol1, match.symbol2
            group1 = graph[sym1]
            group2 = graph[sym2]

            # Merge groups
            merged = group1 | group2
            for sym in merged:
                graph[sym] = merged

        # De-duplicate groups
        unique_groups = {}
        for sym, group in graph.items():
            key = tuple(sorted(group))
            if key not in unique_groups:
                # Pick canonical (alphabetically first)
                canonical = min(group)
                unique_groups[key] = canonical

        # Reverse to canonical -> variants
        result = {}
        for group, canonical in unique_groups.items():
            result[canonical] = sorted(list(group))

        return result

    def recommend_consolidation(
        self, symbols: List[str], asset_types: Dict[str, str]
    ) -> List[Dict]:
        """
        Recommend which duplicate symbols to consolidate.

        Returns list of consolidation recommendations with rationale.
        """
        groups = self.group_duplicates(symbols)
        recommendations = []

        for canonical, variants in groups.items():
            if len(variants) == 1:
                continue  # No duplicates

            # Gather asset type info
            types = list(set(asset_types.get(v, "unknown") for v in variants))

            recommendation = {
                "canonical": canonical,
                "variants": variants,
                "count": len(variants),
                "asset_types": types,
                "action": "consolidate" if len(types) == 1 else "review",
                "reason": (
                    f"Same asset class ({types[0]}), consolidate to {canonical}"
                    if len(types) == 1
                    else f"Different asset classes {types} — review before consolidating"
                ),
            }
            recommendations.append(recommendation)

        return recommendations
