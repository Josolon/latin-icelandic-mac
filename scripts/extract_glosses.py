"""Extract English gloss phrases from latin-mac's Lewis & Short TEI-XML
(data/ls.db, table `entries`) into data/ls_defs.db (id, lemma,
lemma_normalized, definitions JSON array of short English sense strings,
pos) for translate_definitions.py to bridge-translate.

Extraction is ITALIC-FIRST: within each <sense> node, L&S typesets the
actual English definition phrases in italics (<hi rend="ital">to like</hi>,
<hi rend="ital">to love</hi>) while everything in plain text is editorial
commentary, contrast notes, citations, and usage discussion ("both in the
higher and the lower sense, opp. odisse; while diligere designates
esteem..."). Collecting only the italic spans is what turns this from a
wall-of-text extraction into a definition-phrase extraction -- the earlier
naive collect-everything version translated commentary words into the
Icelandic glossary alongside the actual meanings (dictionary soup).

Per sense, the italic spans (own spans only -- not those inside <cit>/
<bibl>/<foreign>/<etym> subtrees or nested <sense> children, which are
walked separately) are joined with ", " into one sense string. Spans
containing macron/breve-marked vowels are dropped: those are quoted Latin
words (ămans, ămanter), not English. Remaining noise (grammar
abbreviations like "inf.", Latin function words like "quod", connectives
like "or") is translate_definitions.py's job to filter -- same division of
labor as before, just starting from a far higher-signal base.

Entries where NO sense has any italic span (rare: mostly cross-references
like "celeriter, v. celer fin.") fall back to the old own-text extraction
so nothing is silently dropped; translate_definitions.py's apparatus
filters handle the extra noise in those.
"""
import json
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET

from latin_normalize import norm_key

LS_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "ls.db")
MORPH_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "morph.db")
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


# Precomposed macron/breve vowels (any case) mark quoted Latin words inside
# italic spans (ămans, ămanter, dī-lĭgo) -- never English gloss text.
_LATIN_MARKED_RE = re.compile("[āăēĕīĭōŏūŭȳÿ]", re.IGNORECASE)


def sense_italic_spans(node):
    """The <sense> node's own <hi rend="ital"> definition phrases, in
    document order -- excluding spans inside excluded subtrees (citations,
    quotes, etymology) and inside nested <sense> children (walked
    separately by the caller), and dropping quoted-Latin spans."""
    spans = []

    def walk(elem, excluded):
        for child in elem:
            tag = _local_tag(child)
            if tag == "hi" and child.get("rend") == "ital" and not excluded:
                text = _clean_text("".join(child.itertext()))
                if text and not _LATIN_MARKED_RE.search(text):
                    spans.append(text)
            if tag != "sense":
                walk(child, excluded or tag in _EXCLUDE_TAGS)

    walk(node, False)
    return spans


def extract_entry_senses(entry_el):
    """Returns a list of [level, text] pairs, one per <sense> node in the
    entry, in document order. `level` is L&S's own sense-hierarchy depth
    (<sense level="1" n="I"> = a principal meaning division; level >= 2 =
    usage/context sub-senses under it) -- translate_definitions.py builds
    the glossary from level-1 senses only, which is what separates
    genuinely distinct meanings (peto I. "to seek" / II. "to beseech")
    from sense 47's courtroom idiom. Missing/garbled level attributes
    default to 1, erring toward treating a sense as a real meaning.

    Italic-first: each sense contributes its italic definition phrases
    joined ", "; only if no sense in the whole entry has any italics does
    the entry fall back to full own-text extraction (see module docstring)."""
    italic_senses = []
    fallback_senses = []
    for node in entry_el.iter():
        if _local_tag(node) != "sense":
            continue
        try:
            level = int(node.get("level", "1"))
        except ValueError:
            level = 1
        spans = sense_italic_spans(node)
        if spans:
            italic_senses.append([level, ", ".join(spans)])
        text = sense_own_text(node)
        if text:
            fallback_senses.append([level, text])
    return italic_senses if italic_senses else fallback_senses


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


_NOUN_GENDERS = ("f.", "m.", "n.", "comm.", "com.")

def _load_morph_verb_lemmas():
    """Set of lemmas Morpheus has conjugated (tense-marked analysis groups
    like "(fut ind act 1st sg)") forms for. Used to infer Verb for entries
    whose L&S XML carries no usable <pos>/<gen> marker at all: if Morpheus
    conjugates the lemma, it's a verb. The reverse inference (declines ->
    noun) is NOT drawn -- adjectives decline too."""
    if not os.path.exists(MORPH_DB_PATH):
        return set()
    conn = sqlite3.connect(MORPH_DB_PATH)
    verbs = {lemma for (lemma,) in conn.execute(
        "SELECT DISTINCT lemma FROM forms WHERE analyses LIKE '%pres%'"
        " OR analyses LIKE '%imperf%' OR analyses LIKE '%fut%'"
        " OR analyses LIKE '%perf%'")}
    conn.close()
    return verbs


def extract_entry_pos(fragment_el):
    """The entry's own part of speech, from the first decisive <pos> or
    <gen> marker in document order. NOT restricted to direct children of
    <entryFree>: L&S often opens a parenthetical note right after the
    headword ("spes, spēi (<sense>...") and the TEI nesting swallows the
    entry's own pos/gen into that first <sense> node. Document order is
    what disambiguates from markers that belong to embedded derived-form
    subentries -- pater's own <gen>m.</gen> sits right after the <itype>,
    long before the "P. a." tag of some participle discussed under sense
    II, so first-decisive-marker-wins picks Noun, not Adjective."""
    for node in fragment_el.iter():
        tag = _local_tag(node)
        if tag == "pos":
            mapped = latin_pos_to_glossary_pos(
                _clean_text("".join(node.itertext())), None)
            if mapped:
                return mapped
        elif tag == "gen":
            if _clean_text("".join(node.itertext())) in _NOUN_GENDERS:
                return "Noun"
    return None


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

    morph_verbs = _load_morph_verb_lemmas()

    start = time.time()
    buffer = []
    parse_fail = 0
    no_senses = 0
    pos_from_morph = 0
    for i, (key, lemma, fragment) in enumerate(rows):
        pos = None
        try:
            entry_el = ET.fromstring(fragment)
            senses = extract_entry_senses(entry_el)
            pos = extract_entry_pos(entry_el)
        except ET.ParseError:
            parse_fail += 1
            senses = []

        if pos is None:
            base_key = re.sub(r"\d+$", "", key)
            if key in morph_verbs or base_key in morph_verbs:
                pos = "Verb"
                pos_from_morph += 1

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
    print(f"  POS inferred from Morpheus conjugation: {pos_from_morph}/{total}")
    print(f"  TEI parse failures: {parse_fail}/{total}")

    ls_conn.close()
    out_conn.close()


if __name__ == "__main__":
    main()
