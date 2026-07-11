"""Builds data/wikt_is.db from the kaikki.org machine-readable extract of
English Wiktionary's Icelandic entries (data/kaikki-icelandic.jsonl,
https://kaikki.org/dictionary/Icelandic/ -- CC BY-SA 3.0 / GFDL, see
CREDITS.md). Two tables come out of this:

  en2is(en_key, is_word, is_pos, gloss)
      An EN -> IS lookup inverted from each Icelandic lemma's English
      glosses ("elska" glossed "to love" yields en_key "love" -> elska,
      Verb). This is a second, human-curated evidence source merged into
      bridge_lookup's CLARIN-based candidate table: Wiktionary pairs are
      individually hand-written by lexicographers, so they both add
      coverage (literary/archaic English words the CLARIN glossary lacks)
      and boost confidence in CLARIN candidates they agree with.

  is_pos(word, pos)
      Icelandic word -> part-of-speech, from every Icelandic entry
      (including inflected-form entries, whose POS is still valid POS
      information about the surface word). Used by bridge_lookup to
      POS-validate candidates whose CLARIN row has no IS-POS tag, so that
      the strict headword-POS filter (Latin verb -> Icelandic verb only)
      doesn't have to throw away otherwise-good candidates as unknowable.

Only lemma senses are inverted into en2is: form-of/alt-of senses ("odd" =
accusative of "oddur") gloss the *relationship*, not the meaning, and
proper nouns/characters/affixes aren't glossary material.
"""
import json
import os
import re
import sqlite3

KAIKKI_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "kaikki-icelandic.jsonl")
OUT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "wikt_is.db")

# kaikki pos -> the CLARIN glossary's EN-POS vocabulary (bridge_lookup
# already ranks/filters in those terms). Absent keys are skipped for en2is.
POS_MAP = {
    "noun": "Noun", "verb": "Verb", "adj": "Adjective", "adv": "Adverb",
    "pron": "Pronoun", "num": "Numeral", "intj": "Interjection",
    "prep": "Preposition", "conj": "Conjunction", "det": "Determinant",
    "name": "Proper noun",
}
# POS categories whose senses get inverted into en2is (proper nouns are
# recorded in is_pos for validation but aren't bilingual-glossary material).
EN2IS_POS = {"Noun", "Verb", "Adjective", "Adverb", "Pronoun", "Numeral",
             "Interjection", "Preposition", "Conjunction"}

_PAREN_RE = re.compile(r"\([^)]*\)")
_LEADING_ARTICLE_RE = re.compile(r"^(?:to|an?|the)\s+", re.IGNORECASE)
# Glosses that describe a relationship or usage rather than a meaning.
_META_GLOSS_RE = re.compile(
    r"^(?:abbreviation|initialism|acronym|alternative|obsolete|archaic|"
    r"misspelling|synonym|clipping|contraction|short) (?:of|form|spelling)\b",
    re.IGNORECASE,
)


def _sense_is_form_of(sense):
    tags = sense.get("tags", [])
    return ("form_of" in sense or "alt_of" in sense
            or "form-of" in tags or "alt-of" in tags)


def _en_keys_from_gloss(gloss):
    """Short English lookup keys from one gloss string. "to love, to be
    fond of (poetic)" -> ["love", "be fond of"]. Long definitional
    sentences yield nothing -- only pieces of <= 3 words are invertible
    lookup keys."""
    if _META_GLOSS_RE.match(gloss):
        return []
    text = _PAREN_RE.sub(" ", gloss)
    keys = []
    for piece in re.split(r"[;,]", text):
        piece = _LEADING_ARTICLE_RE.sub("", piece.strip().rstrip(".").strip())
        piece = piece.lower()
        if not piece or any(ch.isdigit() for ch in piece):
            continue
        tokens = piece.split()
        if not 1 <= len(tokens) <= 3:
            continue
        if any(not re.fullmatch(r"[a-z'\-]+", t) for t in tokens):
            continue
        if len(piece) < 2:
            continue
        keys.append(" ".join(tokens))
    return keys


def main():
    if not os.path.exists(KAIKKI_PATH):
        print(f"Error: {KAIKKI_PATH} not found -- download it from "
              "https://kaikki.org/dictionary/Icelandic/ first.")
        return

    conn = sqlite3.connect(OUT_DB_PATH)
    conn.execute("DROP TABLE IF EXISTS en2is")
    conn.execute("DROP TABLE IF EXISTS is_pos")
    conn.execute("""CREATE TABLE en2is (
        en_key TEXT NOT NULL, is_word TEXT NOT NULL,
        is_pos TEXT NOT NULL, gloss TEXT NOT NULL)""")
    conn.execute("CREATE TABLE is_pos (word TEXT NOT NULL, pos TEXT NOT NULL)")

    pairs = set()
    pos_rows = set()
    n_entries = 0
    with open(KAIKKI_PATH, encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            word = entry.get("word", "").strip()
            pos = POS_MAP.get(entry.get("pos"))
            if not word or not pos:
                continue
            n_entries += 1
            pos_rows.add((word.lower(), pos))

            if pos not in EN2IS_POS or pos == "Proper noun":
                continue
            for sense in entry.get("senses", []):
                if _sense_is_form_of(sense):
                    continue
                glosses = sense.get("glosses")
                if not glosses:
                    continue
                # glosses is the full parent>child chain; the last element
                # is the most specific actual definition.
                for key in _en_keys_from_gloss(glosses[-1]):
                    pairs.add((key, word, pos, glosses[-1]))

    conn.executemany("INSERT INTO en2is VALUES (?,?,?,?)", sorted(pairs))
    conn.executemany("INSERT INTO is_pos VALUES (?,?)", sorted(pos_rows))
    conn.execute("CREATE INDEX idx_en2is_key ON en2is(en_key)")
    conn.execute("CREATE INDEX idx_is_pos_word ON is_pos(word)")
    conn.commit()

    n_keys = conn.execute("SELECT COUNT(DISTINCT en_key) FROM en2is").fetchone()[0]
    print(f"Read {n_entries} Icelandic Wiktionary entries.")
    print(f"en2is: {len(pairs)} EN->IS pairs across {n_keys} English keys.")
    print(f"is_pos: {len(pos_rows)} (word, pos) rows.")
    conn.close()


if __name__ == "__main__":
    main()
