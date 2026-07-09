"""Builds the reverse-direction Apple Dictionary XML: Icelandic headword ->
Latin word(s). Inverts data/ls_is.db's forward glossary (Latin ->
Icelandic), the same way ancient-greek-icelandic-mac inverts lsj_is.db for
its is2gk bundle.

Coarser than the forward direction by construction:
  1. Only single-word Icelandic glosses become reverse headwords -- a
     multi-word phrase like "aðskilnaður, forða" as a sense doesn't have
     one natural "headword" to invert on, so each comma-split single word
     is indexed separately (both words point back to the Latin word that
     produced that sense).
  2. latin-mac's data/ls.db keys each entry uniquely per TEI @key (no
     LSJ-style accent-duplicate rows), but pure orthography variants
     (macron placement, i/j, u/v -- e.g. amaui/amavi) still collapse to one
     representative headword via latin_normalize.dedup_spelling_variants(),
     the Latin analogue of greek_normalize.dedup_accent_variants().
No morphology tables in this direction -- the headword is Icelandic, not
Latin, so Morpheus declension/principal-part data doesn't apply (same scope
decision as ancient-greek-icelandic-mac's is2gk bundle).
"""
import html
import json
import os
import sqlite3
import unicodedata
from collections import defaultdict

from latin_normalize import dedup_spelling_variants

IS_DB_PATH = "data/ls_is.db"
OUTPUT_XML_PATH = "src/IcelandicLatinDictionary.xml"


def sanitize_apple_key(text):
    if not text:
        return ""
    kw = text.strip()
    kw = unicodedata.normalize("NFC", kw)
    while kw and not unicodedata.category(kw[0]).startswith(("L", "N")):
        kw = kw[1:]
    return kw


def build_reverse_index():
    if not os.path.exists(IS_DB_PATH):
        print(f"Error: {IS_DB_PATH} not found -- run translate_definitions.py first.")
        return

    conn = sqlite3.connect(IS_DB_PATH)
    rows = conn.execute(
        "SELECT lemma, definitions_is FROM definitions_is WHERE any_translated = 1"
    ).fetchall()
    conn.close()

    print(f"Inverting {len(rows)} Latin entries with Icelandic glosses...")

    is_to_latin = defaultdict(set)
    for lemma, defs_json in rows:
        try:
            senses = json.loads(defs_json)
        except (json.JSONDecodeError, TypeError):
            continue
        for sense in senses:
            for word in sense.split(","):
                word = word.strip()
                if word and " " not in word:
                    is_to_latin[word.lower()].add(lemma)

    print(f"Built {len(is_to_latin)} Icelandic headwords.")

    with open(OUTPUT_XML_PATH, "w", encoding="utf-8") as xml:
        xml.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        xml.write('<d:dictionary xmlns="http://www.w3.org/1999/xhtml" xmlns:d="http://www.apple.com/DTDs/DictionaryService-1.0.rng">\n\n')

        for i, (is_word, latin_lemmas) in enumerate(sorted(is_to_latin.items())):
            entry_id = f"is2la_{i}"
            safe_title = sanitize_apple_key(is_word)
            if not safe_title:
                continue

            xml.write(f'    <d:entry id="{entry_id}" d:title="{html.escape(safe_title)}">\n')
            xml.write(f'        <d:index d:value="{html.escape(safe_title)}"/>\n')

            deduped = dedup_spelling_variants(latin_lemmas)

            xml.write(f'        <h1 class="entry-lemma">{html.escape(is_word)}</h1>\n')
            xml.write('        <div class="definition">\n')
            xml.write('            <p class="gloss-en"><i>Latnesk orð / Latin words:</i></p>\n')
            xml.write('            <p class="gloss-is">')
            xml.write(", ".join(f'<b class="la-word">{html.escape(la)}</b>' for la in sorted(deduped)))
            xml.write('</p>\n')
            xml.write('        </div>\n')
            xml.write('    </d:entry>\n\n')

            if (i + 1) % 2000 == 0:
                print(f"   ... {i + 1}/{len(is_to_latin)}")

        xml.write('</d:dictionary>\n')

    print(f"Success! XML built at {OUTPUT_XML_PATH}")


if __name__ == "__main__":
    build_reverse_index()
