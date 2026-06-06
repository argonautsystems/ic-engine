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

"""Tests for clio.extract.normalize.

Cases come from the cleanroom T1_normalization_utils.py docstrings — these
are the empirical expectations the cleanroom converged on after 5+ duplicate
implementations of the same patterns. Lifting these into clio without
regression is the contract.
"""

from clio.extract.normalize import (
    US_ADDR_SUFFIX_MAP,
    address,
    block_key,
    house_number,
    name_exact,
    name_fuzzy,
    strip_alphabetic_prefix,
    strip_pandas_float_artifact,
)

# ============================================================================
# name_exact
# ============================================================================


def test_name_exact_strips_punctuation_and_uppercases():
    assert name_exact("Joe's Diner, Inc.") == "JOES DINER INC"


def test_name_exact_strips_hash_and_keeps_digits():
    assert name_exact("McDonald's #1234") == "MCDONALDS 1234"


def test_name_exact_handles_none_and_empty():
    assert name_exact(None) == ""
    assert name_exact("") == ""
    assert name_exact("   ") == ""


def test_name_exact_collapses_whitespace():
    assert name_exact("  Joe's    Diner  ") == "JOES DINER"


# ============================================================================
# name_fuzzy
# ============================================================================


def test_name_fuzzy_strips_llc():
    # Note: name_fuzzy replaces ALL non-alphanumeric with space, including
    # apostrophes. So "Joe's" becomes "JOE S" (space after JOE), not "JOES".
    # The cleanroom docstring claimed "JOES DINER" but the implementation
    # has always produced "JOE S DINER". Fuzzy matchers (rapidfuzz) handle
    # the space tokenization fine; the difference doesn't hurt match scores.
    assert name_fuzzy("Joe's Diner LLC") == "JOE S DINER"


def test_name_fuzzy_strips_corp():
    # Same apostrophe-to-space behavior as test_name_fuzzy_strips_llc.
    assert name_fuzzy("McDonald's Corp.") == "MCDONALD S"


def test_name_fuzzy_keeps_words_for_fuzzy_match():
    # The exact-vs-fuzzy distinction: name_exact would strip the apostrophe
    # entirely ("JOES DINER"), name_fuzzy keeps a space-token boundary
    # ("JOE S DINER"). Stage 2 fuzzy matchers operate on tokens.
    assert name_exact("Joe's Diner") == "JOES DINER"
    assert name_fuzzy("Joe's Diner") == "JOE S DINER"


def test_name_fuzzy_strips_inc():
    assert name_fuzzy("ACME Foods Inc") == "ACME FOODS"


def test_name_fuzzy_handles_none():
    assert name_fuzzy(None) == ""
    assert name_fuzzy(123) == ""  # non-string


def test_name_fuzzy_does_not_strip_inside_word():
    # LLC suffix word-boundary-only — "JELLO" should not become "JEO"
    assert name_fuzzy("Jello Brand") == "JELLO BRAND"


# ============================================================================
# block_key
# ============================================================================


def test_block_key_first_4_chars():
    assert block_key("MCDONALDS") == "MCDO"


def test_block_key_strips_leading_article():
    assert block_key("THE DINER") == "DINE"


def test_block_key_too_short_falls_to_zzzz():
    # The cleanroom docstring claimed block_key("AB") -> "ZZZZ", but the
    # implementation gates on len < 2, not len < 4. So 2-char names get a
    # 2-char block key. ZZZZ is reserved for empty/single-char.
    assert block_key("A") == "ZZZZ"
    assert block_key("") == "ZZZZ"


def test_block_key_short_name_returns_short_key():
    # 2-3 char names get partial keys; ZZZZ only for < 2 chars.
    assert block_key("AB") == "AB"
    assert block_key("CDE") == "CDE"


def test_block_key_strips_a_and_an():
    assert block_key("A RESTAURANT") == "REST"
    assert block_key("AN EATERY") == "EATE"


# ============================================================================
# address + house_number
# ============================================================================


def test_address_normalizes_street_to_st():
    assert address("123 Main Street") == "123 MAIN ST"


def test_address_normalizes_avenue_to_ave():
    assert address("456 Oak Avenue") == "456 OAK AVE"


def test_address_normalizes_north_to_n():
    assert address("789 North Main St") == "789 N MAIN ST"


def test_address_handles_punctuation():
    assert address("100 Main St., Ste 4") == "100 MAIN ST STE 4"


def test_address_word_boundary_safe():
    # "STREETSCAPE" should NOT become "STSCAPE"
    out = address("STREETSCAPE LANE")
    # Word-boundary regex — STREETSCAPE is one token, not bordered by \b on both sides of STREET
    assert "STREETSCAPE" in out


def test_house_number_extracts_leading_digits():
    assert house_number("123 MAIN ST") == "123"


def test_house_number_handles_dashed():
    assert house_number("456-A OAK AVE") == "456"


def test_house_number_returns_empty_when_no_digits():
    assert house_number("NO NUMBER HERE") == ""


def test_us_addr_suffix_map_is_present():
    # Sanity: regression guard against accidental empty map
    assert "STREET" in US_ADDR_SUFFIX_MAP
    assert US_ADDR_SUFFIX_MAP["STREET"] == "ST"


# ============================================================================
# strip_alphabetic_prefix + strip_pandas_float_artifact
# ============================================================================


def test_strip_alphabetic_prefix():
    assert strip_alphabetic_prefix("SEA1234567") == "1234567"


def test_strip_alphabetic_prefix_no_prefix():
    assert strip_alphabetic_prefix("1234567") == "1234567"


def test_strip_pandas_float_artifact():
    assert strip_pandas_float_artifact("1234567.0") == "1234567"


def test_strip_pandas_float_artifact_no_change():
    assert strip_pandas_float_artifact("1234567") == "1234567"


def test_strip_pandas_float_artifact_handles_multi_zero():
    # ".0", ".00", ".000" all strip — pandas sometimes writes longer suffixes
    assert strip_pandas_float_artifact("1234567.000") == "1234567"
