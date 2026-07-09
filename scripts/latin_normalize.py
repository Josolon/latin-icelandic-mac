"""Shared Latin headword normalization -- the Latin analogue of
ancient-greek-icelandic-mac/scripts/greek_normalize.py.

Unlike LSJ's TEI-XML parsing (which produced many pure duplicate rows for
the same Greek headword under different accent placements), latin-mac's
data/ls.db keys each entry uniquely per TEI @key (e.g. "amo", "rex1",
"rex2" for genuine homonyms) -- so the forward direction here does not need
an accent/case merge-group step the way the Greek project's build_xml.py
does. This module exists for:
  1. lemma_normalized computation (scripts/extract_glosses.py), matching
     the shape of ancient-greek-mac's lsj.db lemma_normalized column.
  2. Search-index spelling variants (macron/breve-insensitive, i/j and u/v
     folded) so "amavi"/"amaui" or "iecur"/"jecur" style spelling doubling
     is all reachable from one Look Up.
  3. dedup_spelling_variants(), used by build_reverse_xml.py to collapse
     genuine pure-orthography duplicates in the Icelandic -> Latin reverse
     index (not homonyms -- those keep distinct dictionary rows to begin
     with, same principle as greek_normalize's accent-only folding).

Ported from latin-mac/scripts/build_xml.py's strip_length_marks/
search_variants/norm_join_key so the logic isn't duplicated by hand.
"""
import unicodedata

# Macron (U+0304) and breve (U+0306) combining marks used throughout L&S's
# headwords to mark vowel length -- not meaningful for search/dedup keys.
_LENGTH_MARKS = (0x0304, 0x0306)


def strip_length_marks(word):
    """amo (amō) -> amo, murus (mūrus) -> murus: drop macra/breves."""
    if not word:
        return ""
    decomposed = unicodedata.normalize("NFD", word)
    filtered = "".join(ch for ch in decomposed if ord(ch) not in _LENGTH_MARKS)
    return unicodedata.normalize("NFC", filtered)


def search_variants(word):
    """All lookup spellings for a Latin word: plain, i-for-j, u-for-v, both."""
    w = strip_length_marks(word).replace("-", "")
    if not w:
        return set()
    variants = {w}
    variants.add(w.replace("j", "i").replace("J", "I"))
    variants.add(w.replace("v", "u").replace("V", "U"))
    variants.add(w.replace("j", "i").replace("J", "I").replace("v", "u").replace("V", "U"))
    return variants


def norm_key(word):
    """Grouping/dedup key: length-marks stripped, lowercased, i/j and u/v
    folded, hyphens and spaces removed -- the Latin equivalent of
    greek_normalize.accent_key(), just folding a different set of pure
    orthographic variants."""
    w = strip_length_marks(word).lower()
    w = w.replace("j", "i").replace("v", "u")
    return w.replace(" ", "").replace("-", "")


def mark_count(word):
    """How many macron/breve combining marks `word` carries -- used to
    prefer the fullest/most standard citation spelling as representative."""
    decomposed = unicodedata.normalize("NFD", word)
    return sum(1 for ch in decomposed if ord(ch) in _LENGTH_MARKS)


def pick_representative(spellings):
    """Best single spelling to display for a group of pure-orthography
    variants: prefers classical v/i consonant spelling (L&S's own
    convention -- see latin-mac's join_forms(), which collapses
    amaui/amavi to the v-spelling) over u/j, then the most fully
    length-marked form, then alphabetical order for determinism."""
    spellings = list(spellings)

    def sort_key(s):
        has_u_not_v = "u" in s.lower() and "v" not in s.lower()
        has_j_not_i = "j" in s.lower() and "i" not in s.lower()
        return (has_u_not_v, has_j_not_i, -mark_count(s), s)

    return sorted(spellings, key=sort_key)[0]


def dedup_spelling_variants(words):
    """Collapse pure orthographic variants (macron placement, i/j, u/v) in
    `words` to one representative each -- genuine homonyms are unaffected
    since they never share a norm_key() to begin with."""
    groups = {}
    for word in words:
        groups.setdefault(norm_key(word), []).append(word)
    return [pick_representative(variants) for variants in groups.values()]
