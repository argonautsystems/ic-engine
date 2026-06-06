# Copyright 2026 clio Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""clio.extract.normalize — name and address normalization for matching.

Two normalization grades:
  * Exact: aggressive normalization for dictionary lookups. Removes ALL
    non-alphanumeric characters. Used as Stage-1 of two-stage matching
    (e.g. License# -> Sunbiz exact via normalized Licensee Name).
  * Fuzzy: moderate normalization for similarity scoring. Preserves word
    boundaries but strips common US business suffixes (LLC, INC, CORP,
    etc.). Used as Stage-2 fuzzy fallback (rapidfuzz) for misses on the
    exact stage.

Plus blocking-key generation (4-char prefix, articles stripped) for
candidate pre-filtering in fuzzy matching, address normalization (US
suffix abbreviations), house-number extraction, and a couple of pandas/
polars column-whitespace helpers.

These are framework-agnostic string transforms (with the exception of the
column-whitespace helpers) — they work as element-wise transforms in any
DataFrame library, or standalone over Python strings.

Lifted verbatim (with renames + de-FL-DBPR-ification) from the RiskyEats
cleanroom T1_normalization_utils.py. The patterns there were converged
from 5+ duplicate normalize_name implementations across the cleanroom
T-stage matchers; this is the canonical version.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import pandas as pd
    import polars as pl


# US business-name suffixes stripped by name_fuzzy. Generic to US business
# naming conventions, not state-specific. Add ", " in the regex word boundary
# so "Joe's, LLC" gets stripped cleanly.
_US_BIZ_SUFFIX_RE = re.compile(r"\b(LLC|INC|CORP|LTD|PA|PL)\b")

# English-language leading articles stripped before computing block keys, so
# "The Diner" and "Diner" land in the same block.
_LEADING_ARTICLE_RE = re.compile(r"^(THE|A|AN)\s+")

# Strip-all-non-alphanumeric pattern for exact normalization (uppercased).
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9 ]")

# Strip-non-alphanumeric-but-keep-spaces pattern for fuzzy normalization.
_NON_ALNUM_KEEPSPACE_RE = re.compile(r"[^A-Z0-9\s]")

# Whitespace-collapse.
_WHITESPACE_RE = re.compile(r"\s+")

# Leading-digits extractor for house numbers.
_LEADING_DIGITS_RE = re.compile(r"^(\d+)")

# Leading-letters strip for license-number-style strings ("SEA1234567" ->
# "1234567"). The regex is generic; the use case is opaque-prefix removal.
_LEADING_ALPHA_RE = re.compile(r"^[A-Z]+")

# Trailing-".0" strip for pandas-cast-as-float artifacts ("1234567.0" ->
# "1234567").
_TRAILING_FLOAT_DOTZERO_RE = re.compile(r"\.0+$")


# US street-address suffix abbreviation map. Long-form -> standard
# abbreviation. Used by address() to standardize so "123 Main Street"
# and "123 Main St" hash the same. Non-US apps can pass their own map.
US_ADDR_SUFFIX_MAP: dict[str, str] = {
    "STREET": "ST",
    "AVENUE": "AVE",
    "BOULEVARD": "BLVD",
    "ROAD": "RD",
    "DRIVE": "DR",
    "LANE": "LN",
    "COURT": "CT",
    "CIRCLE": "CIR",
    "PARKWAY": "PKWY",
    "CAUSEWAY": "CSWY",
    "HIGHWAY": "HWY",
    "PLACE": "PL",
    "PLAZA": "PLZ",
    "SQUARE": "SQ",
    "BUILDING": "BLDG",
    "SUITE": "STE",
    "UNIT": "APT",
    "APARTMENT": "APT",
    "NORTH": "N",
    "SOUTH": "S",
    "EAST": "E",
    "WEST": "W",
}


# ============================================================================
# Name normalization
# ============================================================================


def name_exact(s: Optional[str]) -> str:
    """Aggressive normalization for exact-match dictionary lookups.

    Removes ALL non-alphanumeric characters (keeping spaces, then collapsing).
    Use as Stage 1 of two-stage matching where a normalized form is the join
    key into a pre-built lookup.

    Examples:
        >>> name_exact("Joe's Diner, Inc.")
        'JOES DINER INC'
        >>> name_exact("McDonald's #1234")
        'MCDONALDS 1234'
        >>> name_exact(None)
        ''
    """
    if s is None or not isinstance(s, str):
        # Also handles pandas NaN floats that fail isinstance(str)
        try:
            import pandas as pd

            if pd.isna(s):
                return ""
        except ImportError:
            pass
        if not isinstance(s, str):
            return ""
    upper = s.upper().strip()
    cleaned = _NON_ALNUM_RE.sub("", upper)
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def name_fuzzy(raw: Optional[str]) -> str:
    """Moderate normalization for fuzzy similarity scoring.

    Preserves word boundaries (replaces ALL non-alphanumeric punctuation
    including apostrophes with spaces, then collapses whitespace) and
    strips common US business suffixes (LLC, INC, CORP, LTD, PA, PL).
    Used as Stage 2 fuzzy fallback (rapidfuzz / Levenshtein) for matches
    the exact stage missed.

    Note: apostrophes become spaces (NOT removed entirely). "Joe's" becomes
    "JOE S" not "JOES". This is the intentional difference from name_exact
    — fuzzy matchers operate on tokens, so preserving the word boundary
    after an apostrophe matches better than collapsing it. Use name_exact
    if you need the apostrophe collapsed for dictionary-key lookups.

    Examples:
        >>> name_fuzzy("Joe's Diner LLC")
        'JOE S DINER'
        >>> name_fuzzy("McDonald's Corp.")
        'MCDONALD S'
        >>> name_fuzzy("ACME Foods Inc")
        'ACME FOODS'
        >>> name_fuzzy(None)
        ''
    """
    if not isinstance(raw, str):
        return ""
    x = raw.upper()
    x = _NON_ALNUM_KEEPSPACE_RE.sub(" ", x)
    x = _US_BIZ_SUFFIX_RE.sub("", x)
    return _WHITESPACE_RE.sub(" ", x).strip()


def block_key(name: str) -> str:
    """Generate a 4-character blocking key for fuzzy-match pre-filtering.

    Strips leading English articles (THE/A/AN), removes spaces, returns the
    first 4 characters. Names shorter than 2 characters fall into the
    "ZZZZ" catch-all block. Used to partition large candidate sets into
    smaller buckets so rapidfuzz only compares within the same block —
    typical workload reduction is 1000x.

    Examples:
        >>> block_key("MCDONALDS")
        'MCDO'
        >>> block_key("THE DINER")
        'DINE'
        >>> block_key("AB")
        'AB'
        >>> block_key("A")
        'ZZZZ'
    """
    stripped = _LEADING_ARTICLE_RE.sub("", name)
    no_space = stripped.replace(" ", "")
    return no_space[:4] if len(no_space) >= 2 else "ZZZZ"


# ============================================================================
# Address normalization
# ============================================================================


def address(raw: Optional[str], suffix_map: Optional[dict[str, str]] = None) -> str:
    """Normalize a street address for fuzzy matching.

    Standardizes word-by-word using a suffix-abbreviation map (default is
    the US US_ADDR_SUFFIX_MAP). Removes punctuation. Uppercases. Collapses
    whitespace. Non-US apps should pass their own suffix_map.

    Examples:
        >>> address("123 Main Street")
        '123 MAIN ST'
        >>> address("456 Oak Avenue")
        '456 OAK AVE'
    """
    if not isinstance(raw, str):
        return ""
    table = suffix_map if suffix_map is not None else US_ADDR_SUFFIX_MAP
    x = raw.upper()
    x = _NON_ALNUM_KEEPSPACE_RE.sub(" ", x)
    for long_form, short_form in table.items():
        # Word-boundary match so "STREETSIDE" doesn't become "STSIDE"
        x = re.sub(r"\b" + re.escape(long_form) + r"\b", short_form, x)
    return _WHITESPACE_RE.sub(" ", x).strip()


def house_number(addr: str) -> str:
    """Extract the leading numeric house number from a normalized address.

    Used for disambiguating same-street businesses (e.g. "100 Main St" vs
    "200 Main St" should not fuzzy-match each other despite identical
    street tokens).

    Examples:
        >>> house_number("123 MAIN ST")
        '123'
        >>> house_number("456-A OAK AVE")
        '456'
        >>> house_number("NO NUMBER HERE")
        ''
    """
    m = _LEADING_DIGITS_RE.match(addr)
    return m.group(1) if m else ""


# ============================================================================
# Generic string-cleaning utilities
# ============================================================================


def strip_alphabetic_prefix(s) -> str:
    """Strip a leading run of capital letters from a string.

    Generic regex strip-leading-alpha. Use case in the cleanroom was license
    numbers carrying an opaque prefix (e.g. "SEA1234567" needs to join to
    bare "1234567" in another file). The implementation is generic — any
    "alphabetic-prefix attached to a numeric body" string benefits.

    Examples:
        >>> strip_alphabetic_prefix("SEA1234567")
        '1234567'
        >>> strip_alphabetic_prefix("1234567")
        '1234567'
    """
    return _LEADING_ALPHA_RE.sub("", str(s))


def strip_pandas_float_artifact(s) -> str:
    """Strip a trailing ".0" from a string.

    Pandas reads integers-with-NaN columns as float64 and stringifies them
    with a trailing ".0" (e.g. "1234567.0"). This is a very common ETL
    artifact when integer keys cross through pandas; the same string
    needs to join back to "1234567" in another file. Generic strip-".0$".

    Examples:
        >>> strip_pandas_float_artifact("1234567.0")
        '1234567'
        >>> strip_pandas_float_artifact("1234567")
        '1234567'
    """
    return _TRAILING_FLOAT_DOTZERO_RE.sub("", str(s))


# ============================================================================
# DataFrame column-whitespace helpers (framework-specific)
# ============================================================================


def strip_column_whitespace_pandas(df: "pd.DataFrame") -> "pd.DataFrame":
    """Strip leading/trailing whitespace from pandas DataFrame column names.

    In-place modification per pandas convention; also returns the frame for
    chaining. Some upstream CSV files have column names with leading/
    trailing spaces that break exact-match column lookups.
    """
    df.columns = df.columns.str.strip()
    return df


def strip_column_whitespace_polars(df: "pl.DataFrame") -> "pl.DataFrame":
    """Strip leading/trailing whitespace from polars DataFrame column names.

    Returns a new DataFrame (Polars convention; columns are immutable in
    place). Renames only the columns that need it; columns already clean
    are passed through unchanged.
    """
    return df.rename({c: c.strip() for c in df.columns if c != c.strip()})


__all__ = [
    # Name normalization
    "name_exact",
    "name_fuzzy",
    "block_key",
    # Address normalization
    "address",
    "house_number",
    "US_ADDR_SUFFIX_MAP",
    # Generic string cleaning
    "strip_alphabetic_prefix",
    "strip_pandas_float_artifact",
    # DataFrame column helpers
    "strip_column_whitespace_pandas",
    "strip_column_whitespace_polars",
]
