"""Gloss-translate every extracted L&S sense phrase (data/ls_defs.db, table
`definitions`, produced by scripts/extract_glosses.py) into an Icelandic
glossary via the EN->IS bridge (scripts/bridge_lookup.py), writing results
into data/ls_is.db.

Adapted from ancient-greek-icelandic-mac/scripts/translate_definitions.py --
see that file for the full design rationale (precision-first: a phrase
translates only via a confident single-word lookup or an exact whole-phrase
glossary match, never a word-by-word reconstruction; a sense with no
confident translation keeps its English text so nothing is silently
dropped).

The apparatus filter here differs from the LSJ version only in the
dialect-abbreviation list: extract_glosses.py's naive, over-inclusive
extraction leaves behind L&S-specific debris -- bare citations that were
never wrapped in <bibl> (e.g. "Gell. ap. Charis p. 40 P."), leftover colons/
semicolons where a <cit> quote was excised, author sigla (Cic., Plin.,
Varr., Quint.), and raw untagged Latin example text. None of that is Greek-
dialect apparatus (LSJ's Ion./Ep./Dor./Att. etc. don't apply to a Latin
lexicon), so that list is replaced with generic cross-reference markers
(cf./v./q.v./etc./sq.) plus the same generic citation-siglum regex (a
capitalized 1-4 letter token immediately followed by a period), digit
filter, and short-token filter -- which already catch the vast majority of
this without enumerating every possible Latin author abbreviation.
"""
import json
import re
import sqlite3
import time

from bridge_lookup import translate_glossary_phrase

DEFS_DB_PATH = "data/ls_defs.db"
OUT_DB_PATH = "data/ls_is.db"

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
    return False


def translate_sense(sense_text, pos_hint=None):
    """Returns (icelandic_or_none, any_translated, all_translated) for one
    extracted L&S sense string. pos_hint (a glossary EN-POS string, e.g.
    "Verb") comes from the headword's own <pos>/<gen> tag in ls.db (see
    extract_glosses.latin_pos_to_glossary_pos) and steers
    translate_glossary_phrase toward the matching word class -- e.g. amo
    ("v. a.") prefers "elska" (Verb) over "samband" (Noun), which also
    glosses some sense of "to love"/"like" in the bridge glossary."""
    phrases = [p for p in _SPLIT_RE.split(sense_text) if p.strip()]
    phrases = [p for p in phrases if not _is_ls_apparatus(p)]
    if not phrases:
        return None, False, False

    translated = []
    hits = 0
    for phrase in phrases:
        is_text = translate_glossary_phrase(phrase, pos_hint)
        if is_text:
            translated.append(is_text)
            hits += 1

    if hits == 0:
        return None, False, False
    return ", ".join(translated), True, hits == len(phrases)


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
    fully_count = 0
    any_count = 0
    none_count = 0
    for i, (rid, lemma, lemma_norm, defs_json, pos_hint) in enumerate(rows):
        try:
            senses = json.loads(defs_json)
        except (json.JSONDecodeError, TypeError):
            senses = [defs_json]

        is_senses = []
        entry_fully = True
        entry_any = False
        for sense in senses:
            is_text, any_ok, all_ok = translate_sense(sense, pos_hint)
            entry_fully = entry_fully and all_ok
            entry_any = entry_any or any_ok
            if is_text:
                is_senses.append(is_text)

        if entry_any:
            any_count += 1
        else:
            none_count += 1
        if entry_fully and entry_any:
            fully_count += 1

        is_json = json.dumps(is_senses, ensure_ascii=False) if is_senses else None
        buffer.append((rid, lemma, lemma_norm, defs_json, is_json, int(entry_fully and entry_any), int(entry_any)))

        if len(buffer) >= 5000:
            out.executemany("INSERT INTO definitions_is VALUES (?,?,?,?,?,?,?)", buffer)
            out.commit()
            buffer.clear()
            elapsed = time.time() - start
            print(f"  ... {i + 1}/{total} ({elapsed:.0f}s elapsed)")

    if buffer:
        out.executemany("INSERT INTO definitions_is VALUES (?,?,?,?,?,?,?)", buffer)
        out.commit()

    print(f"Done in {time.time() - start:.0f}s.")
    print(f"  Fully translated (every sense, every phrase): {fully_count}/{total} ({100*fully_count/total:.1f}%)")
    print(f"  At least partially translated: {any_count}/{total} ({100*any_count/total:.1f}%)")
    print(f"  No confident translation at all (English only): {none_count}/{total} ({100*none_count/total:.1f}%)")

    defs.close()
    out.close()


if __name__ == "__main__":
    main()
