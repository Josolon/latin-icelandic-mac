"""English -> Icelandic bridge lookup over the CLARIN IS-EN glossary
(data/IS-EN_glossary.tsv, CC BY 4.0), used to gloss-translate LSJ's English
short definitions into Icelandic.

Precision-first design: this module's job is to return an Icelandic
candidate only when the glossary gives real evidence for it, and None
otherwise -- the caller keeps the English text visible for anything we
decline to translate. That is the opposite of the first version of this
script, which translated everything and produced pidgin.

Junk signatures observed in the glossary and filtered here:
  - "eyes" -> "skjár": an inflected English form whose sole candidate came
    from corpus alignment, while the lemma ("eye" -> "auga") has an order of
    magnitude more evidence. Fixed by lemmatizing and preferring the lemma's
    candidates when the surface form's are weak.
  - "channel" -> "Stöð", "word" -> "Word": proper-noun/product-name rows
    (Wikipedia titles) outranking the real word. Fixed by penalizing
    capitalized candidates and EN-POS "Proper noun" rows for lowercase
    English words, and by merging case variants of the same Icelandic word
    ("Stöð"/"stöð") into one pooled-evidence candidate in the first place --
    they aren't two competing translations, they're the same word counted
    twice under different capitalization.
  - "it" -> "upplýsingatækni": acronym expansions. Handled by _OVERRIDES.
  - "shade" -> "hansagardína" (a curtain brand, score 1.0 on 3 hits) beating
    "skuggi" (score 0.14 but 22 hits): a single high score with almost no
    supporting evidence isn't more trustworthy than a lower score with lots
    of it. Fixed by MIN_EVIDENCE, a hard floor applied before ranking, not
    just a soft down-weight.
  - "a tyrant's dwelling" -> "harðstjóri suður bústaður": concatenating
    separately-looked-up words to cover a multi-word phrase produces
    ungrammatical Icelandic even when each word is individually a valid
    translation. Fixed by removing word-by-word reconstruction entirely --
    translate_glossary_phrase() only ever returns a single looked-up word or
    an exact whole-phrase glossary match, never a synthesized combination.

TSV columns (1-indexed per data/IS-EN_glossary.READ.ME):
  1 Icelandic  2 English  3 IS POS  4 EN POS  5 unit-type
  6-11 method hit-counts (embeddings/MT/pivot/parallel/comparable/synthetic)
  12 IS->EN score   13 EN->IS score
"""
import csv
import os
import re
from functools import lru_cache

GLOSSARY_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "IS-EN_glossary.tsv")

WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]+(?:[-'][A-Za-zÀ-ÖØ-öø-ÿ]+)*")

# Hand-pinned translations for closed-class words whose glossary ranking is
# dominated by acronym/proper-noun outliers. Only consulted for words the
# segmenter actually allows through (mostly single-word senses).
_OVERRIDES = {
    "one": "einn", "first": "fyrstur", "word": "orð", "not": "ekki",
    "no": "enginn", "yes": "já", "it": "það", "this": "þessi", "that": "sá",
}

_PLURAL_IRREGULAR = {
    "eyes": "eye", "feet": "foot", "teeth": "tooth", "men": "man",
    "women": "woman", "children": "child", "mice": "mouse", "geese": "goose",
    "oxen": "ox", "wolves": "wolf", "lives": "life", "knives": "knife",
    "leaves": "leaf", "halves": "half", "wives": "wife", "loaves": "loaf",
    "calves": "calf", "hooves": "hoof", "thieves": "thief", "sheaves": "sheaf",
}

_table = None  # english lowercase -> {icelandic_lower: {"score","evidence","en_pos","is_pos","surface","_casing"}}


def _load():
    global _table
    if _table is not None:
        return _table

    _table = {}
    with open(GLOSSARY_PATH, encoding="utf-8") as f:
        for row in csv.reader(f, delimiter="\t"):
            if len(row) < 13:
                continue
            icelandic, english = row[0].strip(), row[1].strip()
            if not icelandic or not english:
                continue
            try:
                score = float(row[12])  # EN -> IS direction
            except ValueError:
                score = 0.0
            try:
                evidence = sum(int(x) for x in row[5:11])
            except ValueError:
                evidence = 0
            is_pos, en_pos = row[2].strip(), row[3].strip()

            bucket = _table.setdefault(english.lower(), {})
            # Dedup key is case-insensitive: "Stöð" and "stöð" for "channel"
            # are the same word, not two different candidates that happen
            # to compete for the top slot -- pool their evidence instead of
            # letting a capitalized Wikipedia-title row silently outrank the
            # ordinary word it's actually the same as.
            dedup_key = icelandic.lower()
            cand = bucket.get(dedup_key)
            if cand is None:
                bucket[dedup_key] = {
                    "score": score, "evidence": evidence,
                    "en_pos": en_pos, "is_pos": is_pos,
                    "surface": icelandic, "_casing": {icelandic: evidence},
                }
            else:
                # Duplicate rows exist with different POS tagging and/or
                # casing; merge: sum evidence, keep max score, the more
                # informative POS, and recompute which casing to surface.
                cand["score"] = max(cand["score"], score)
                cand["evidence"] += evidence
                cand["_casing"][icelandic] = cand["_casing"].get(icelandic, 0) + evidence
                for key, val in (("en_pos", en_pos), ("is_pos", is_pos)):
                    if cand[key] in ("NULL", "", "Proper noun") and val not in ("NULL", ""):
                        cand[key] = val
                # Prefer whichever casing has more evidence behind it; break
                # ties toward lowercase (capitalization in this glossary is
                # far more often a citation/title artifact than a genuine
                # proper noun).
                cand["surface"] = max(
                    cand["_casing"].items(),
                    key=lambda kv: (kv[1], kv[0].islower()),
                )[0]
    return _table


def _lemma_variants(word):
    """Cheap English lemmatizer: yields possible lemma forms, best-guess first."""
    lower = word.lower()
    if lower in _PLURAL_IRREGULAR:
        yield _PLURAL_IRREGULAR[lower]
        return
    if len(lower) > 3 and lower.endswith("ies"):
        yield lower[:-3] + "y"
    elif len(lower) > 4 and lower.endswith(("ches", "shes", "sses", "xes", "zes")):
        yield lower[:-2]
    elif len(lower) > 3 and lower.endswith("s") and not lower.endswith(("ss", "us", "is")):
        yield lower[:-1]
    if len(lower) > 4 and lower.endswith("ied"):
        yield lower[:-3] + "y"
    elif len(lower) > 4 and lower.endswith("ed"):
        yield lower[:-2]
        yield lower[:-1]  # loved -> love
    if len(lower) > 5 and lower.endswith("ing"):
        yield lower[:-3]
        yield lower[:-3] + "e"  # loving -> love


# A candidate needs at least this many independent method-hits to be
# considered at all, regardless of its score. Without this floor, a lone
# corpus-alignment artifact with score 1.0 (e.g. "shade" -> "hansagardína",
# 3 hits) beats a well-attested real translation with a lower score but far
# more evidence ("shade" -> "skuggi", 22 hits, score 0.14) -- damping the
# score alone isn't enough to fix that, evidence has to gate first.
MIN_EVIDENCE = 5


def _ranked(en_word, cands, en_pos_hint=None, min_evidence=MIN_EVIDENCE):
    """Rank a candidate dict by adjusted quality, best first. Candidates
    below min_evidence are dropped outright, not just down-weighted.
    Returns (surface_form, candidate_dict) pairs -- `cands` is keyed by the
    case-insensitive dedup key, not the display spelling; the display
    spelling lives in candidate_dict["surface"]."""
    en_lower_word = en_word[0].islower() if en_word else True
    qualified = [c for c in cands.values() if c["evidence"] >= min_evidence]
    if not qualified:
        return []
    has_lowercase = any(c["surface"][0].islower() for c in qualified)

    def quality(c):
        icelandic = c["surface"]
        q = c["score"]
        if en_lower_word and icelandic[0].isupper() and has_lowercase:
            q *= 0.15
        if en_lower_word and c["en_pos"] == "Proper noun":
            q *= 0.3
        if icelandic.lower() == en_word.lower():
            q *= 0.2
        if en_pos_hint:
            if c["en_pos"] == en_pos_hint:
                q *= 1.4
            elif c["en_pos"] not in ("NULL", ""):
                q *= 0.7
        return q

    return [(c["surface"], c) for c in sorted(qualified, key=quality, reverse=True)]


def _candidates_for(word):
    """Candidates for a surface form, falling back to its lemma when the
    surface form's evidence is weak (the eyes->skjár fix). Returns (dict, form_used)."""
    table = _load()
    lower = word.lower()
    exact = table.get(lower, {})
    exact_strength = max((c["evidence"] for c in exact.values()), default=0)

    if exact_strength >= 10:
        return exact, lower

    for lemma in _lemma_variants(word):
        lemma_cands = table.get(lemma)
        if not lemma_cands:
            continue
        lemma_strength = max(c["evidence"] for c in lemma_cands.values())
        if lemma_strength >= max(2 * exact_strength, 4):
            return lemma_cands, lemma
    return exact, lower


@lru_cache(maxsize=200_000)
def top_candidates(word, en_pos_hint=None, n=2):
    """Up to n Icelandic candidates for one English word, best first.
    Returns a list of (icelandic, is_pos) tuples; empty if nothing trustworthy."""
    lower = word.lower()
    if lower in _OVERRIDES:
        return [(_OVERRIDES[lower], "")]

    cands, _ = _candidates_for(word)
    if not cands:
        return []
    ranked = _ranked(word, cands, en_pos_hint)
    out = []
    best_score = None
    for icelandic, c in ranked:
        if best_score is None:
            best_score = c["score"]
            out.append((icelandic, c["is_pos"]))
        elif c["score"] >= 0.18 and c["score"] >= 0.3 * best_score and icelandic.lower() != out[0][0].lower():
            out.append((icelandic, c["is_pos"]))
        if len(out) >= n:
            break
    return out


@lru_cache(maxsize=200_000)
def phrase_match(phrase, en_pos_hint=None):
    """Whole-phrase glossary match only (no word-by-word reconstruction).
    These are real editorial multiword entries (idioms like "run away" ->
    "strjúka"), so they get a lower evidence bar than single-word lookups --
    but still a bar, to keep out one-off corpus-alignment noise."""
    table = _load()
    key = " ".join(phrase.lower().split())
    cands = table.get(key)
    if not cands:
        return None
    ranked = _ranked(phrase, cands, en_pos_hint, min_evidence=2)
    if not ranked:
        return None
    return ranked[0][0]


_LEADING_ARTICLE_RE = re.compile(r"^(to|an?|the)\s+", re.IGNORECASE)


@lru_cache(maxsize=200_000)
def translate_glossary_phrase(phrase, en_pos_hint=None):
    """Precision-first translation of one short gloss phrase for a glossary
    (not a sentence). Returns Icelandic text, or None if we don't have
    confident enough evidence -- callers should keep the English original in
    that case rather than force a translation.

    Deliberately narrow: after stripping a leading "to/a/an/the" (LSJ
    infinitive/article glosses), this only succeeds for (a) an exact
    whole-phrase glossary match, or (b) a single remaining word. It does
    NOT reconstruct multi-word phrases by concatenating separately-looked-up
    words -- e.g. "retiring" -> "hörfa" (withdraw) and "part" -> "hluti" are
    each individually defensible, but "hörfa hluti" is not grammatical
    Icelandic and nobody chose that combination on purpose. A polysemous
    Greek word's other multi-word senses just don't get an Icelandic gloss
    for that particular sense -- see README.
    """
    stripped = _LEADING_ARTICLE_RE.sub("", phrase.strip())
    if not stripped:
        return None

    tokens = WORD_RE.findall(stripped)
    if len(tokens) == 1:
        # Single remaining word: always go through top_candidates, which
        # applies the full MIN_EVIDENCE bar. phrase_match's lower bar exists
        # for genuine multiword idioms and would let noise back in here
        # (e.g. "shade" matching the same weakly-attested table entry that
        # top_candidates would correctly reject).
        cands = top_candidates(tokens[0], en_pos_hint, n=1)
        return cands[0][0] if cands else None

    if len(tokens) > 1:
        return phrase_match(stripped, en_pos_hint)

    return None


if __name__ == "__main__":
    tests = [
        ("eyes", None), ("eye", None), ("channel", None), ("word", None),
        ("horse", None), ("love", "Verb"), ("love", "Noun"), ("war", None),
        ("inviolable", None), ("wren", None), ("brave", None),
    ]
    for w, hint in tests:
        print(f"{w!r} (hint={hint}): {top_candidates(w, hint)}")
    for p in ("run away", "sea-fish", "regard with affection"):
        print(f"phrase {p!r}: {phrase_match(p)}")
