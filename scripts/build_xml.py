"""Builds the Apple Dictionary XML for the Latin -> Icelandic bridge
dictionary. Headwords come from latin-mac's Lewis & Short database
(data/ls.db); Icelandic glosses come from data/ls_is.db, produced by
scripts/translate_definitions.py (itself fed by scripts/extract_glosses.py).
Morphology (declension grid / principal parts / gerundive / supine) comes
from latin-mac's Morpheus full-form analyses (data/morph.db).

Adapted from ancient-greek-icelandic-mac/scripts/build_xml.py -- see that
project for the Greek version this is derived from. The morphology-parsing
logic (classify_and_grid, ANALYSIS_GROUP_RE, drop_enclitic_variants,
join_forms) is ported from latin-mac/scripts/build_xml.py, which already
solved parsing morph.db's free-text `analyses` blob format; only the
rendering (Icelandic labels instead of English) is new here.

Unlike LSJ's TEI-XML parsing (which produced many pure duplicate rows for
the same Greek headword), latin-mac's data/ls.db keys each entry uniquely
per TEI @key (verified: 51,636 rows, 51,636 unique keys) -- so no
accent/case merge-group step is needed here the way the Greek project's
build_xml.py has one.
"""
import html
import json
import os
import re
import sqlite3
import unicodedata
import xml.etree.ElementTree as ET
from collections import defaultdict

from latin_normalize import search_variants, norm_key

LS_DB_PATH = 'data/ls.db'
MORPH_DB_PATH = 'data/morph.db'
IS_DB_PATH = 'data/ls_is.db'
OUTPUT_XML_PATH = 'src/LatinIcelandicDictionary.xml'

# ---------------------------------------------------------------------------
# Icelandic grammatical labels (case labels + ablative sub-uses supplied by
# the user; tense/mood labels reused from ancient-greek-icelandic-mac where
# Latin and Greek share the same category; gerundive/supine are Latin-only
# and have no Greek precedent).
# ---------------------------------------------------------------------------
CASES = ['nom', 'gen', 'dat', 'acc', 'abl', 'voc', 'loc']
CASE_LABELS_IS = {
    'nom': 'Nefnifall', 'gen': 'Eignarfall', 'dat': 'Þágufall',
    'acc': 'Þolfall', 'abl': 'Sviptifall', 'voc': 'Ávarpsfall',
    'loc': 'Staðarfall',
}
# Ablative sub-use labels (for future use if L&S usage annotations for
# ablative-of-instrument/time/manner are ever surfaced here; not currently
# mined/rendered anywhere in this build).
ABLATIVE_SUBUSE_LABELS_IS = {
    'instrumentalis': 'Tækisfall', 'temporis': 'Tímafall', 'modi': 'Háttarfall',
}
TENSES = ['pres', 'imperf', 'fut', 'perf', 'plup', 'futperf']
TENSE_LABELS_IS = {
    'pres': 'Nútíð', 'fut': 'Framtíð', 'perf': 'Núliðin tíð',
    'imperf': 'Dvalarþátíð', 'plup': 'Þáliðin tíð', 'futperf': 'Þáframtíð',
}
VOICE_LABELS_IS = {'act': 'Germynd', 'pass': 'Þolmynd'}
GERUNDIVE_LABEL_IS = 'Lýsingarháttur nútíðar'
SUPINE_LABEL_IS = 'Sagnbót'

# Longest first: -que, -ne (incl. elided -n), -ve/-ue, and -st (est contraction)
ENCLITICS = ('que', 'ne', 've', 'ue', 'st', 'n')
ANALYSIS_GROUP_RE = re.compile(r'\(([^()]*)\)')


def clean_text(text):
    if not text:
        return ''
    text = ''.join(ch for ch in text if unicodedata.category(ch) != 'Cc')
    return re.sub(r'\s+', ' ', text.replace('\t', ' ')).strip()


def sanitize_apple_key(text):
    if not text:
        return ''
    kw = unicodedata.normalize('NFC', text.strip())
    while kw and not unicodedata.category(kw[0]).startswith(('L', 'N')):
        kw = kw[1:]
    return kw


# ---------------------------------------------------------------------------
# Morphology parsing -- ported from latin-mac/scripts/build_xml.py
# ---------------------------------------------------------------------------

def drop_enclitic_variants(form_analyses):
    """Remove amoque/amon/amarest-style duplicates when the base form is present."""
    forms = set(form_analyses)
    out = {}
    for form, analyses in form_analyses.items():
        base_hit = False
        for enc in ENCLITICS:
            if form.lower().endswith(enc) and form[:-len(enc)] in forms:
                base_hit = True
                break
        if not base_hit:
            out[form] = analyses
    return out


def join_forms(forms):
    """Join forms for a table cell, collapsing u/v spelling duplicates
    (amaui/amavi) into the v-spelling L&S prints."""
    by_norm = {}
    for f in sorted(forms):
        norm = f.replace('v', 'u').replace('V', 'U')
        cur = by_norm.get(norm)
        if cur is None or ('v' in f and 'v' not in cur):
            by_norm[norm] = f
    return ', '.join(sorted(by_norm.values()))


def classify_and_grid(rows):
    """From (form, analyses) rows build noun grid and verb principal parts."""
    form_analyses = {}
    for form, analyses in rows:
        groups = ANALYSIS_GROUP_RE.findall(analyses or '')
        if groups:
            form_analyses.setdefault(form, set()).update(groups)
    form_analyses = drop_enclitic_variants(form_analyses)

    noun_grid = defaultdict(lambda: defaultdict(set))
    verb_parts = defaultdict(set)
    infinitives = defaultdict(set)
    participles = defaultdict(set)
    supines = set()
    gerundives = set()
    n_nominal = n_verbal = 0

    for form, groups in form_analyses.items():
        for g in groups:
            toks = g.split()
            tokset = set(toks)
            case = next((c for c in CASES if c in tokset), None)
            combined = [t for t in toks if '/' in t and any(p in CASES for p in t.split('/'))]
            number = 'sg' if 'sg' in tokset else ('pl' if 'pl' in tokset else None)
            tense = next((t for t in TENSES if t in tokset), None)
            voice = 'act' if 'act' in tokset else ('pass' if 'pass' in tokset else None)

            if 'part' in tokset:
                continue  # not surfaced in this glossary's morphology section
            if 'supine' in tokset:
                supines.add(form)
                continue
            if 'gerundive' in tokset:
                is_masc = 'masc' in tokset or any('masc' in t.split('/') for t in toks if '/' in t)
                is_nom = 'nom' in tokset or any('nom' in t.split('/') for t in toks if '/' in t)
                if is_masc and is_nom and number == 'sg':
                    gerundives.add(form)
                continue

            if tense and 'inf' in tokset:
                infinitives[(tense, voice or 'act')].add(form)
                n_verbal += 1
            elif tense and 'ind' in tokset and '1st' in tokset and number == 'sg':
                verb_parts[(tense, voice or 'act')].add(form)
                n_verbal += 1
            elif tense:
                n_verbal += 1

            if number and 'part' not in tokset and not tense:
                if combined:
                    for t in combined:
                        for p in t.split('/'):
                            if p in CASES:
                                noun_grid[p][number].add(form)
                                n_nominal += 1
                elif case:
                    noun_grid[case][number].add(form)
                    n_nominal += 1

    return (noun_grid, verb_parts, infinitives, participles, supines,
            gerundives, n_nominal, n_verbal)


def render_principal_parts(verb_parts, infinitives, is_deponent):
    """amo, amare, amavi, amatus -- or for deponents, hortor, hortari,
    hortatus sum. Gracefully omits any part Morpheus doesn't attest."""
    if is_deponent:
        pres = join_forms(verb_parts.get(('pres', 'pass'), []))
        inf = join_forms(infinitives.get(('pres', 'pass'), []))
        forms = [pres, inf]
        labels = ['1. p. et. (nút.)', 'Nafnháttur (nút.)']
    else:
        pres = join_forms(verb_parts.get(('pres', 'act'), []))
        inf = join_forms(infinitives.get(('pres', 'act'), []))
        perf = join_forms(verb_parts.get(('perf', 'act'), []))
        forms = [pres, inf, perf]
        labels = ['1. p. et. (nút.)', 'Nafnháttur (nút.)', '1. p. et. (þát.)']

    if sum(1 for f in forms if f) < 2:
        return ''

    cells = ', '.join(f'<b class="la-word">{html.escape(f)}</b>' if f
                      else '<span class="pp-missing">—</span>' for f in forms)
    label = 'Kennimyndir (germynd) / Principal Parts' if not is_deponent \
        else 'Kennimyndir (þolmynd, samsagnir) / Principal Parts (deponent)'
    return (f'<div class="principal-parts">'
            f'<span class="pp-label">{html.escape(label)}</span> '
            f'<span class="pp-forms">{cells}</span></div>')


def render_morphology(rows, is_deponent):
    (noun_grid, verb_parts, infinitives, participles, supines,
     gerundives, n_nominal, n_verbal) = classify_and_grid(rows)
    parts = []

    if n_verbal > n_nominal and verb_parts:
        pp_html = render_principal_parts(verb_parts, infinitives, is_deponent)
        parts.append('<div class="morph-section">')
        if pp_html:
            parts.append(pp_html)
        parts.append('<p class="morph-label">Sagnbeygingar / Verb Forms</p>')
        parts.append('<table class="morphology-table">')
        parts.append('<tr><th>Tíð</th><th>Germynd</th><th>Þolmynd</th></tr>')
        any_tense_row = False
        for tense in TENSES:
            act = join_forms(verb_parts.get((tense, 'act'), []))
            pas = join_forms(verb_parts.get((tense, 'pass'), []))
            if not act and not pas:
                continue
            any_tense_row = True
            label = TENSE_LABELS_IS[tense]
            parts.append(f'<tr><td class="case-label">{label}</td>'
                         f'<td>{html.escape(act) or "—"}</td><td>{html.escape(pas) or "—"}</td></tr>')

        inf_rows = []
        for tense in TENSES:
            act = join_forms(infinitives.get((tense, 'act'), []))
            pas = join_forms(infinitives.get((tense, 'pass'), []))
            if act or pas:
                label = f'{TENSE_LABELS_IS[tense]} – Nafnháttur'
                inf_rows.append(f'<tr><td class="case-label">{label}</td>'
                                f'<td>{html.escape(act) or "—"}</td><td>{html.escape(pas) or "—"}</td></tr>')
        if inf_rows:
            parts.append('<tr class="morph-secondary-header"><td colspan="3">Nafnhættir / Infinitives</td></tr>')
            parts.extend(inf_rows)

        if gerundives:
            parts.append('<tr class="morph-secondary-header"><td colspan="3">'
                         f'{html.escape(GERUNDIVE_LABEL_IS)} / Gerundive</td></tr>')
            parts.append(f'<tr><td class="case-label">Ksk. et. nf.</td>'
                        f'<td colspan="2">{html.escape(join_forms(gerundives))}</td></tr>')
        if supines:
            parts.append('<tr class="morph-secondary-header"><td colspan="3">'
                         f'{html.escape(SUPINE_LABEL_IS)} / Supine</td></tr>')
            parts.append(f'<tr><td class="case-label">Sagnbót</td>'
                        f'<td colspan="2">{html.escape(join_forms(supines))}</td></tr>')

        if not any_tense_row and not inf_rows and not gerundives and not supines and not pp_html:
            return ''  # nothing attested worth showing
        parts.append('</table></div>')

    elif noun_grid:
        parts.append('<div class="morph-section">')
        parts.append('<p class="morph-label">Beygingar / Declension</p>')
        parts.append('<table class="morphology-table">')
        parts.append('<tr><th>Fall</th><th>Eintala</th><th>Fleirtala</th></tr>')
        for c in CASES:
            if c not in noun_grid:
                continue
            sg = join_forms(noun_grid[c].get('sg', [])) or '—'
            pl = join_forms(noun_grid[c].get('pl', [])) or '—'
            label = CASE_LABELS_IS.get(c, c.capitalize())
            parts.append(f'<tr><td class="case-label">{label}</td>'
                         f'<td>{html.escape(sg)}</td><td>{html.escape(pl)}</td></tr>')
        parts.append('</table></div>')

    return ''.join(parts)


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------

# extract_glosses.py deliberately leaves in stripped-<cit>/<bibl> debris --
# bare asterisks (poetic-quote markers) and runs of leftover separator
# punctuation where a citation used to sit inline (e.g. "et condonatio, * ;
# so ; , §§ 2, 5; ."). translate_definitions.py's apparatus filter only
# applies to the Icelandic-bound copy, so the raw English text shown
# directly in the dictionary (gloss-en) still carries it -- clean it here,
# cosmetically only, without touching the underlying sense content.
_APPARATUS_PUNCT_RE = re.compile(r'\s*(?:[;:.,]\s*){2,}')
_EDGE_PUNCT_RE = re.compile(r'^[\s;:.,]+|[\s;:.,]+$')


def _clean_gloss_text(text):
    text = text.replace('*', '')
    text = _APPARATUS_PUNCT_RE.sub('; ', text)
    text = _EDGE_PUNCT_RE.sub('', text)
    return re.sub(r'\s{2,}', ' ', text).strip()


def render_defs(raw_json):
    try:
        if raw_json.startswith('[') and raw_json.endswith(']'):
            cleaned = (_clean_gloss_text(d) for d in json.loads(raw_json) if d.strip())
            return '; '.join(html.escape(d) for d in cleaned if d)
        return html.escape(_clean_gloss_text(raw_json))
    except (json.JSONDecodeError, AttributeError):
        return html.escape(str(raw_json))


def build_dictionary():
    print("Starting Latin -> Icelandic Apple Dictionary XML generation...")

    for path in (LS_DB_PATH, MORPH_DB_PATH, IS_DB_PATH):
        if not os.path.exists(path):
            print(f"Error: {path} not found.")
            return

    ls_conn = sqlite3.connect(LS_DB_PATH)
    morph_conn = sqlite3.connect(MORPH_DB_PATH)
    is_conn = sqlite3.connect(IS_DB_PATH)
    ls_cursor = ls_conn.cursor()
    morph_cursor = morph_conn.cursor()

    print("Loading Icelandic bridge glosses...")
    is_cursor = is_conn.cursor()
    is_cursor.execute("SELECT id, definitions_en, definitions_is, any_translated FROM definitions_is")
    is_by_id = {row[0]: (row[1], row[2], row[3]) for row in is_cursor.fetchall()}

    with open(OUTPUT_XML_PATH, 'w', encoding='utf-8') as xml:
        xml.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        xml.write('<d:dictionary xmlns="http://www.w3.org/1999/xhtml" xmlns:d="http://www.apple.com/DTDs/DictionaryService-1.0.rng">\n\n')

        print("Fetching L&S entries...")
        ls_cursor.execute("SELECT rowid, key, lemma, xml FROM entries ORDER BY rowid")
        rows = ls_cursor.fetchall()
        total = len(rows)
        print(f"Found {total} entries. Building structures...")

        for index, (rid, key, lemma, fragment) in enumerate(rows):
            base_key = re.sub(r'\d+$', '', key)
            lemma_display = lemma or key

            defs_en, defs_is, any_translated = is_by_id.get(rid, (None, None, 0))

            safe_title = sanitize_apple_key(lemma_display)
            if not safe_title:
                safe_title = sanitize_apple_key(key) or "unknown"

            entry_id = f"ls_{rid}"
            xml.write(f'    <d:entry id="{entry_id}" d:title="{html.escape(safe_title)}">\n')

            search_indices = set()
            search_indices |= search_variants(lemma_display)
            search_indices |= search_variants(base_key)

            morph_cursor.execute(
                'SELECT form, analyses FROM forms WHERE lemma = ? OR lemma = ?',
                (key, base_key)
            )
            morph_rows = morph_cursor.fetchall()
            for form, _ in morph_rows:
                search_indices |= search_variants(form)

            for keyword in search_indices:
                clean_kw = sanitize_apple_key(keyword)
                if clean_kw:
                    xml.write(f'        <d:index d:value="{html.escape(clean_kw)}"/>\n')

            is_deponent = False
            try:
                entry_el = ET.fromstring(fragment)
                for pos_el in entry_el.iter():
                    if pos_el.tag.split('}')[-1] == 'pos':
                        is_deponent = 'dep' in clean_text(''.join(pos_el.itertext())).lower()
                        break
            except ET.ParseError:
                pass

            xml.write(f'        <h1 class="entry-lemma">{html.escape(lemma_display)}</h1>\n')
            xml.write('        <div class="definition">\n')
            if any_translated and defs_is:
                xml.write(f'            <p class="gloss-is"><b>ÍS:</b> {render_defs(defs_is)}</p>\n')
            else:
                xml.write('            <p class="gloss-is gloss-missing">Engin trygg þýðing í orðasafninu.</p>\n')
            if defs_en:
                xml.write(f'            <p class="gloss-en"><b>EN (L&amp;S):</b> {render_defs(defs_en)}</p>\n')
            xml.write('        </div>\n')

            morph_html = render_morphology(morph_rows, is_deponent) if morph_rows else ''
            if morph_html:
                xml.write(morph_html + '\n')

            xml.write('    </d:entry>\n\n')

            if (index + 1) % 5000 == 0:
                print(f"   ... Processed {index + 1} / {total} entries")

        xml.write('</d:dictionary>\n')

    print(f"Success! XML built at {OUTPUT_XML_PATH}")

    ls_conn.close()
    morph_conn.close()
    is_conn.close()


if __name__ == "__main__":
    build_dictionary()
