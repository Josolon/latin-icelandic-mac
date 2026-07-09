"""Extract flat English gloss phrases from latin-mac's Lewis & Short TEI-XML
(data/ls.db, table `entries`) into data/ls_defs.db, matching the shape of
ancient-greek-mac's lsj.db `definitions` table (id, lemma, lemma_normalized,
definitions JSON array of short English sense strings) -- so
translate_definitions.py can bridge-translate it exactly the way it does LSJ.

Why this step exists: unlike lsj.db, ls.db stores each entry as a raw
<entryFree> TEI-XML fragment with a nested <sense level=... n=...> hierarchy,
<cit> quotes, <bibl> citations, <foreign> (mostly Greek etymology) and <etym>
spans mixed in with the actual English gloss text -- there is no ready-made
flat list of English sense strings to translate.

For each <sense> node (entry_el.iter(), so every level of the hierarchy,
not just top-level), this collects the node's OWN text -- its direct text
plus any non-excluded child's text -- while skipping the subtrees of
<cit>, <bibl>, <foreign>, <etym>, and nested <sense> children entirely
(those get walked separately when the iterator reaches them, or aren't
English gloss text to begin with). This deliberately does NOT stop at the
first sentence-ending punctuation the way latin-mac's own brief_text()
does for its 150-char overview snippets -- translate_definitions.py needs
the full comma/semicolon-split phrase list, not a truncated preview.

This is a naive, over-inclusive extraction on purpose: leftover noise (a
bare citation like "Gell. ap. Charis p. 40 P." with no <bibl> wrapper, a
speaker abbreviation like "Ph." from an unwrapped play-dialogue quote, a
grammatical note like "gen. plur") is expected and is NOT filtered here --
that's translate_definitions.py's job (citation-siglum regex, digit
filter, short-token filter), exactly mirroring how translate_definitions.py
already handles LSJ's own apparatus leakage. Keeping the two concerns
separate (naive extraction here, precision-first filtering + translation
there) avoids duplicating filtering logic in two places.
"""
import json
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET

from latin_normalize import norm_key

LS_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "ls.db")
OUT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "ls_defs.db")

# Subtrees that are never English gloss text: quoted examples, citations,
# non-English (mostly Greek) etymology cross-references, and etymology
# notes. Nested <sense> children are walked separately by the caller's
# entry_el.iter(), so their content is deliberately excluded here to avoid
# duplicating it into the parent sense's text.
_EXCLUDE_TAGS = {"cit", "bibl", "foreign", "etym", "sense"}


# L&S's own convention for a bracketed etymology aside within a sense's
# running text, e.g. "amo ... ) [cf. Sanscr. kam = to love; ... with the
# radical notion of likeness, union], to like, to love, ..." -- the
# bracketed span is etymology, not part of the English gloss, but it isn't
# wrapped in <etym>/<foreign>/any excludable tag (see _EXCLUDE_TAGS above),
# it's just plain text inside the <sense> node. Left in, it leaked words
# like "union" into the translated Icelandic glossary (e.g. "samband" for
# amo) even after headword-POS filtering, since "union" is a perfectly
# good noun translation for a phrase that just happens to not be part of
# the actual definition. No nested brackets occur in ls.db (verified), so
# a single non-nested pass is sufficient.
_ETYM_BRACKET_RE = re.compile(r"\[[^\[\]]*\]")


def _clean_text(text):
    if not text:
        return ""
    text = _ETYM_BRACKET_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text.replace("\t", " ")).strip()


def _local_tag(elem):
    return elem.tag.split("}")[-1]


def sense_own_text(node):
    """A <sense> node's own gloss text: its direct text plus any
    non-excluded child's full text (and every child's tail, since tail
    text belongs to the surrounding flow regardless of whether the child
    itself was excluded)."""
    parts = []
    if node.text:
        parts.append(node.text)
    for child in node:
        tag = _local_tag(child)
        if tag not in _EXCLUDE_TAGS:
            parts.append("".join(child.itertext()))
        if child.tail:
            parts.append(child.tail)
    return _clean_text("".join(parts))


def extract_entry_senses(entry_el):
    """Returns a list of non-empty English gloss-candidate strings, one per
    <sense> node in the entry (any level), in document order."""
    senses = []
    for node in entry_el.iter():
        if _local_tag(node) != "sense":
            continue
        text = sense_own_text(node)
        if text:
            senses.append(text)
    return senses


# Maps L&S's <pos> abbreviation (e.g. "v. a.", "P. a.") to the CLARIN
# glossary's coarse English POS categories (data/IS-EN_glossary.READ.ME,
# column 4: Noun/Verb/Adjective/Adverb/.../NULL) -- used as en_pos_hint in
# bridge_lookup.top_candidates() so that e.g. amo ("v. a.") prefers the verb
# candidate "elska" over noun candidates like "samband" that also gloss
# some sense of "to love"/"like". Checked in order; the first match wins,
# since e.g. "v. n. and a." must hit the verb rule before anything else.
_VERB_POS_RE = re.compile(r"^v\.\s|^v\.$")
_ADJ_POS = {"adj.", "Adj.", "P. a.", "num. adj.", "pron. adj."}


def latin_pos_to_glossary_pos(pos_text, gen_text):
    """pos_text/gen_text: raw <pos>/<gen> element text (or None). Returns
    one of the glossary's EN-POS strings, or None if no confident mapping
    exists (most commonly: no <pos>/<gen> tag on the entry at all, e.g.
    cross-references like "abax, v. abacus" or bare participle stubs)."""
    if pos_text:
        if _VERB_POS_RE.match(pos_text):
            return "Verb"
        if pos_text in _ADJ_POS:
            return "Adjective"
        if pos_text.startswith("adv."):
            return "Adverb"
        if pos_text == "interj.":
            return "Interjection"
        if pos_text == "prep.":
            return "Preposition"
        if pos_text == "Subst.":
            return "Noun"
    if gen_text in ("f.", "m.", "n.", "comm.", "com."):
        return "Noun"
    return None


def extract_entry_pos(fragment_el):
    """Reads the entry's own top-level <pos>/<gen> child (not a nested
    sense's), mirroring how L&S structures entryFree: pos/gen sit as direct
    siblings of orth/itype, before the sense hierarchy starts."""
    pos_el = fragment_el.find("pos")
    gen_el = fragment_el.find("gen")
    pos_text = (pos_el.text or "").strip() if pos_el is not None else None
    gen_text = (gen_el.text or "").strip() if gen_el is not None else None
    return latin_pos_to_glossary_pos(pos_text, gen_text)


def main():
    if not os.path.exists(LS_DB_PATH):
        print(f"Error: {LS_DB_PATH} not found.")
        return

    ls_conn = sqlite3.connect(LS_DB_PATH)
    out_conn = sqlite3.connect(OUT_DB_PATH)
    out_conn.execute("DROP TABLE IF EXISTS definitions")
    out_conn.execute(
        """CREATE TABLE definitions (
            id INTEGER PRIMARY KEY,
            lemma TEXT NOT NULL,
            lemma_normalized TEXT NOT NULL,
            definitions TEXT NOT NULL,
            pos TEXT
        )"""
    )
    out_conn.execute("CREATE INDEX idx_def_lemma ON definitions(lemma)")

    rows = ls_conn.execute("SELECT key, lemma, xml FROM entries ORDER BY rowid").fetchall()
    total = len(rows)
    print(f"Extracting English gloss phrases from {total} L&S entries...")

    start = time.time()
    buffer = []
    parse_fail = 0
    no_senses = 0
    for i, (key, lemma, fragment) in enumerate(rows):
        pos = None
        try:
            entry_el = ET.fromstring(fragment)
            senses = extract_entry_senses(entry_el)
            pos = extract_entry_pos(entry_el)
        except ET.ParseError:
            parse_fail += 1
            senses = []

        if not senses:
            no_senses += 1

        # `lemma` keeps L&S's own citation spelling (with macra/breves,
        # e.g. "ămo") for display; `lemma_normalized` is the folded search
        # key used for dedup/grouping. Search-index spelling variants
        # (i/j, u/v, macron-stripped) are handled separately in
        # build_xml.py via latin_normalize.search_variants().
        lemma_display = lemma or key
        lemma_normalized = norm_key(lemma or key)
        buffer.append((i + 1, lemma_display, lemma_normalized, json.dumps(senses, ensure_ascii=False), pos))

        if len(buffer) >= 5000:
            out_conn.executemany("INSERT INTO definitions VALUES (?,?,?,?,?)", buffer)
            out_conn.commit()
            buffer.clear()
            elapsed = time.time() - start
            print(f"  ... {i + 1}/{total} ({elapsed:.0f}s elapsed)")

    if buffer:
        out_conn.executemany("INSERT INTO definitions VALUES (?,?,?,?,?)", buffer)
        out_conn.commit()

    print(f"Done in {time.time() - start:.0f}s.")
    print(f"  Entries with at least one gloss candidate: {total - no_senses}/{total}")
    print(f"  Entries with zero sense text: {no_senses}/{total}")
    print(f"  TEI parse failures: {parse_fail}/{total}")

    ls_conn.close()
    out_conn.close()


if __name__ == "__main__":
    main()
