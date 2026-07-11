"""Gloss-translate the extracted L&S definition phrases (data/ls_defs.db,
table `definitions`, produced by scripts/extract_glosses.py) into a SHORT
Icelandic glossary line per entry via the EN->IS bridge
(scripts/bridge_lookup.py), writing results into data/ls_is.db.

This is a glossary builder, not a definition translator. Three rules keep
the output a glossary (a few trustworthy equivalents) instead of
dictionary soup (every translatable word of every sense):

1. HARD POS GATE. The headword's own part of speech (ls_defs.pos, from
   L&S's <pos>/<gen> tags) is required of every Icelandic candidate, not
   just hinted: a Latin verb gets glossed only by attested Icelandic
   verbs. Applies to Verb/Noun/Adjective/Adverb headwords; the tiny
   closed classes (interjections, prepositions) keep soft-hint behavior
   since the bridge glossary's POS tagging is too sparse there.

2. LEVEL-1 SENSES ONLY. L&S's <sense level="1"> nodes are its principal
   meaning divisions (I, II, III); deeper levels are usage/context
   sub-senses under them. The glossary is built from level-1 senses only
   (a few words each, small entry cap), so genuinely distinct meanings
   all appear (peto: "to seek" and "to beseech") while sense 47's
   courtroom idiom contributes nothing. See translate_entry.

3. CONNECTIVE/APPARATUS STOPLISTS. Even italic-extracted phrases carry
   L&S's connective tissue ("or", "so", "also"), lowercase grammar
   abbreviations ("inf.", "absol.", "fin."), and quoted Latin function
   words ("quod", "ut", "cum"). All are dropped before translation is
   attempted -- translating them is how "einnig"/"því"/"maður" got into
   the old output.

A phrase that can't be confidently translated is still simply omitted; an
entry where nothing survives keeps its English text in the dictionary
display (any_translated = 0). fully_translated now records whether the
glossary came from the level-1 divisions (core meaning glossed) rather
than the all-senses fallback path.
"""
import json
import re
import sqlite3
import time

from bridge_lookup import translate_glossary_phrase

DEFS_DB_PATH = "data/ls_defs.db"
OUT_DB_PATH = "data/ls_is.db"

# Per-sense and per-entry caps. Small on purpose: a glossary line should
# read like "elska, unna, líta" -- not enumerate every sense's every
# synonym.
WORDS_PER_SENSE = 3
HARD_CAP = 6
# In the no-level-1-yield fallback (all senses considered), stop opening
# new senses once this many words are in.
FALLBACK_TARGET = 4

# Headword POS classes that get the hard require_is_pos gate.
_STRICT_POS = {"Verb", "Noun", "Adjective", "Adverb"}

_SPLIT_RE = re.compile(r"\s*[,;]\s*")

_GREEK_RE = re.compile(r"[Ͱ-Ͽἀ-῿]")
# Generic cross-reference markers left over after citations/quotes were
# excised (see extract_glosses.py) -- "cf.", "v." (vide), "q.v.", "etc.",
# "sq./sqq." (following/-s). Not Latin-dialect-specific like LSJ's Ion/Ep/
# Dor/Att list, since L&S has no dialect apparatus of that kind.
_APPARATUS_RE = re.compile(
    r"\b(cf|sqq?|v|q\.v|etc)\.|\bsee\b|\bcf\b", re.IGNORECASE
)
# Author-abbreviation sigla (e.g. "Cic." for Cicero, "Plin." for Pliny) are
# effectively unbounded across L&S's citation apparatus -- rather than
# enumerate them, a capitalized 1-4 letter token immediately followed by a
# period is itself a strong citation signature that basically never occurs
# in a real English gloss. This also catches leftover play-dialogue speaker
# abbreviations (e.g. "Ph.", "Sc.") from untagged quotes.
_CITATION_SIGLUM_RE = re.compile(r"\b[A-Z][a-zA-Z]{0,3}\.")

# Lowercase grammar-apparatus abbreviations L&S italicizes inline ("inf.",
# "absol.", "fin.") -- the capitalized-siglum regex above can't catch these.
# A phrase is dropped when ALL its tokens (period-stripped) are in this set
# or the connective set below.
_GRAMMAR_ABBREVS = {
    "abl", "absol", "abstr", "acc", "adj", "adv", "comp", "concr", "constr",
    "dat", "dep", "dim", "fem", "fin", "freq", "fut", "gen", "imper",
    "imperf", "impers", "inch", "indic", "inf", "init", "intr", "lit",
    "masc", "meton", "neut", "neutr", "nom", "obj", "part", "pass", "perf",
    "plur", "plup", "poet", "pres", "pron", "sing", "subj", "subst", "sup",
    "trop", "transf", "voc",
}
# English connective tissue between italic definition phrases -- never a
# meaning of the headword itself. Translating these is exactly how
# "einnig" (also), "því" (so/therefore) and "maður" (one/man) polluted the
# old glossary lines.
_CONNECTIVES = {
    "a", "again", "also", "an", "and", "as", "besides", "both", "but",
    "even", "hence", "i", "e", "g", "ib", "id", "just", "likewise",
    "moreover", "now", "one", "only", "or", "so", "some", "such", "that",
    "the", "then", "therefore", "this", "thus", "to", "too", "very", "viz",
    "with", "without",
}
# Quoted Latin function words that L&S italicizes when discussing
# construction ("with quod", "amare aliquem de...") -- they survive the
# macron filter in extract_glosses.py because short function words carry
# no length marks in L&S's typography.
_LATIN_FUNCTION_WORDS = {
    "ab", "ad", "aliquem", "aliquid", "cum", "de", "dum", "esse", "est",
    "ex", "in", "ne", "qui", "quae", "quam", "quod", "se", "sese", "si",
    "sub", "ut",
}


def _is_ls_apparatus(phrase):
    """True if `phrase` is L&S editorial/citation debris rather than an
    actual English gloss."""
    if _GREEK_RE.search(phrase):
        return True
    if _APPARATUS_RE.search(phrase):
        return True
    if _CITATION_SIGLUM_RE.search(phrase):
        return True
    if any(ch.isdigit() for ch in phrase):
        return True
    letters = re.sub(r"[^A-Za-z]", "", phrase)
    if len(letters) <= 2:
        return True
    tokens = [t.strip(".").lower() for t in phrase.split()]
    tokens = [t for t in tokens if t]
    if tokens and all(t in _GRAMMAR_ABBREVS or t in _CONNECTIVES for t in tokens):
        return True
    if len(tokens) == 1 and tokens[0] in _LATIN_FUNCTION_WORDS:
        return True
    return False


def _translate_sense_words(sense_text, pos_hint, require, seen, cap):
    """Up to `cap` new Icelandic words from one sense string, deduped
    against `seen` (mutated)."""
    out = []
    phrases = [p for p in _SPLIT_RE.split(sense_text) if p.strip()]
    for phrase in phrases:
        if _is_ls_apparatus(phrase):
            continue
        is_text = translate_glossary_phrase(phrase, pos_hint, require)
        if is_text and is_text.lower() not in seen:
            seen.add(is_text.lower())
            out.append(is_text)
        if len(out) >= cap:
            break
    return out


def translate_entry(senses, pos_hint):
    """One glossary line for one L&S entry: an ordered, deduplicated list
    of at most HARD_CAP Icelandic words. `senses` is extract_glosses.py's
    list of [level, text] pairs.

    Level-1 senses are L&S's principal meaning divisions -- they, and only
    they, feed the glossary (up to WORDS_PER_SENSE each, HARD_CAP total),
    so genuinely distinct meanings all show up (peto: "to seek" and "to
    beseech") while usage sub-senses ("the season of roses" under rosa)
    contribute nothing. Only when no level-1 sense yields a single word
    does the entry fall back to considering every sense in order, stopping
    at FALLBACK_TARGET -- better a contextual gloss than none at all.

    Returns (words, core_glossed): core_glossed is True when the glossary
    came from the level-1 divisions rather than the fallback path."""
    require = pos_hint if pos_hint in _STRICT_POS else None
    words = []
    seen = set()

    for level, sense_text in senses:
        if level != 1:
            continue
        if len(words) >= HARD_CAP:
            break
        cap = min(WORDS_PER_SENSE, HARD_CAP - len(words))
        words.extend(_translate_sense_words(sense_text, pos_hint, require, seen, cap))

    if words:
        return words, True

    for level, sense_text in senses:
        if len(words) >= FALLBACK_TARGET:
            break
        cap = HARD_CAP - len(words)
        words.extend(_translate_sense_words(sense_text, pos_hint, require, seen, cap))

    return words, False


def main():
    defs = sqlite3.connect(DEFS_DB_PATH)
    out = sqlite3.connect(OUT_DB_PATH)
    out.execute("DROP TABLE IF EXISTS definitions_is")
    out.execute(
        """CREATE TABLE definitions_is (
            id INTEGER PRIMARY KEY,
            lemma TEXT NOT NULL,
            lemma_normalized TEXT NOT NULL,
            definitions_en TEXT NOT NULL,
            definitions_is TEXT,
            pos TEXT,
            fully_translated INTEGER NOT NULL,
            any_translated INTEGER NOT NULL
        )"""
    )
    out.execute("CREATE INDEX idx_def_is_lemma ON definitions_is(lemma)")

    rows = defs.execute("SELECT id, lemma, lemma_normalized, definitions, pos FROM definitions").fetchall()
    total = len(rows)
    print(f"Building Icelandic glossary for {total} L&S entries...")

    start = time.time()
    buffer = []
    core_count = 0
    any_count = 0
    none_count = 0
    for i, (rid, lemma, lemma_norm, defs_json, pos_hint) in enumerate(rows):
        try:
            senses = json.loads(defs_json)
        except (json.JSONDecodeError, TypeError):
            senses = [[1, defs_json]]

        words, core_glossed = translate_entry(senses, pos_hint)

        if words:
            any_count += 1
        else:
            none_count += 1
        if core_glossed:
            core_count += 1

        is_json = json.dumps(words, ensure_ascii=False) if words else None
        buffer.append((rid, lemma, lemma_norm, defs_json, is_json, pos_hint,
                       int(core_glossed), int(bool(words))))

        if len(buffer) >= 5000:
            out.executemany("INSERT INTO definitions_is VALUES (?,?,?,?,?,?,?,?)", buffer)
            out.commit()
            buffer.clear()
            elapsed = time.time() - start
            print(f"  ... {i + 1}/{total} ({elapsed:.0f}s elapsed)")

    if buffer:
        out.executemany("INSERT INTO definitions_is VALUES (?,?,?,?,?,?,?,?)", buffer)
        out.commit()

    print(f"Done in {time.time() - start:.0f}s.")
    print(f"  Core meaning glossed (from level-1 divisions): {core_count}/{total} ({100*core_count/total:.1f}%)")
    print(f"  At least one Icelandic word: {any_count}/{total} ({100*any_count/total:.1f}%)")
    print(f"  No confident translation at all (English only): {none_count}/{total} ({100*none_count/total:.1f}%)")

    defs.close()
    out.close()


if __name__ == "__main__":
    main()
