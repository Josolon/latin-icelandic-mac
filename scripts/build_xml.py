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
NOUN_DECL_PATH = 'data/is_noun_declension.tsv'
VERB_FORMS_PATH = 'data/is_verb_forms.tsv'
ADJ_DECL_PATH = 'data/is_adj_declension.tsv'
ADJ_LEMMA_PATH = 'data/is_adj_form_lemma.tsv'
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

# ---------------------------------------------------------------------------
# BÍN-sourced Icelandic-gloss-word morphology matching -- ported from
# ancient-greek-icelandic-mac/scripts/build_xml.py (see that project and
# bin-morphology-recap.md for the full design rationale: real inflection
# data over suffix rules, gaps left blank rather than guessed, vocative ==
# nominative in Icelandic, all-three-genders passive participle).
#
# LATIN's grammar is simpler than Greek's in exactly the ways that let this
# be a much shorter port: only two voices (active/passive, no middle -- so
# no MM- Icelandic slot is ever consulted), no dual number (no
# _AGREEMENT_NUMBER reuse-the-plural case), and this project's own Latin
# declension/verb tables (classify_and_grid above) already collapse each
# finite verb cell to a single 1st-person-singular-indicative citation form
# rather than a full person x number paradigm -- so the Icelandic
# annotation only ever needs to render "ég ..." (1sg), never build a full
# pronoun table the way the Greek per-attested-form dictionary entries did.
# Deponent verbs (Latin passive morphology, active meaning) route through
# the ACTIVE germynd Icelandic slots regardless of which Latin table column
# their form happens to be tagged under -- the whole reason this dictionary
# already tracks is_deponent for the Latin-side principal-parts rendering.
# ---------------------------------------------------------------------------

PRONOUN_1SG_IS = 'ég'

# Auxiliary paradigms, hardcoded (vera/hafa/munu are closed-class). Only the
# 1st-singular cell is needed -- see note above. BÍN-verified in the Greek
# project; identical closed-class forms apply here unchanged.
_AUX_1SG = {
    'vera_pres': 'er', 'vera_past': 'var',
    'munu_pres': 'mun',
    'hafa_pres': 'hef', 'hafa_past': 'hafði',
}
_AUX_HAFA_INF = 'hafa'

# Past-participle BÍN slot keys for the masculine/feminine/neuter nominative
# singular -- see build_is_morphology.py's _PARTICIPLE_TAGS. Singular only:
# this dictionary's Latin verb table cites 1sg, so the periphrasis subject
# is always singular.
_PTCP_SLOT_SG = {'masculine': 'ptcp_kk_sg', 'feminine': 'ptcp_kvk_sg', 'neuter': 'ptcp_hk_sg'}


def _passive_periphrasis(tense, verb_slots):
    """Þolmynd (passive): "ég er/var/mun vera/hef verið + participle" -- no
    Icelandic voice has a synthetic passive (true even where LATIN's own
    passive is synthetic, e.g. present "amor"), so this is always
    periphrastic. The subject's gender isn't known from the Latin form (the
    citation is bare 1sg, "ég"), so the participle shows all three gender
    forms slash-joined (menntaður/menntuð/menntað) rather than defaulting to
    masculine -- see recap. Returns None if the participle isn't attested at
    all, or the tense has no Icelandic tense mapping."""
    ptcp_forms = [verb_slots.get(_PTCP_SLOT_SG[g]) for g in ('masculine', 'feminine', 'neuter')]
    if not all(ptcp_forms):
        return None
    ptcp = '/'.join(ptcp_forms)
    pron = PRONOUN_1SG_IS
    if tense == 'pres':
        return f'{pron} {_AUX_1SG["vera_pres"]} {ptcp}'
    if tense == 'imperf':
        return f'{pron} {_AUX_1SG["vera_past"]} {ptcp}'
    if tense == 'fut':
        return f'{pron} {_AUX_1SG["munu_pres"]} vera {ptcp}'
    if tense == 'perf':
        return f'{pron} {_AUX_1SG["hafa_pres"]} verið {ptcp}'
    if tense == 'plup':
        return f'{pron} {_AUX_1SG["hafa_past"]} verið {ptcp}'
    if tense == 'futperf':
        return f'{pron} {_AUX_1SG["munu_pres"]} hafa verið {ptcp}'
    return None


def _active_periphrasis(tense, is_word, verb_slots):
    """Germynd (active) 1sg-indicative Icelandic clause for one Latin tense
    cell. is_word is the Icelandic gloss verb's own citation/infinitive
    form (also used as the germynd infinitive -- Icelandic verb citation
    forms already ARE the infinitive). Returns None if the specific BÍN
    cell needed isn't attested."""
    pron = PRONOUN_1SG_IS
    supine = verb_slots.get('gm_supine')
    if tense == 'pres':
        pres = verb_slots.get('gm_ind_pres_1sg')
        if not pres:
            return None
        clause = f'{pron} {pres}'
        if is_word:
            clause += f' / {pron} {_AUX_1SG["vera_pres"]} að {is_word}'
        return clause
    if tense == 'imperf':
        return f'{pron} {_AUX_1SG["vera_past"]} að {is_word}' if is_word else None
    if tense == 'fut':
        return f'{pron} {_AUX_1SG["munu_pres"]} {is_word}' if is_word else None
    if tense == 'perf':
        past = verb_slots.get('gm_ind_past_1sg')
        return f'{pron} {past}' if past else None
    if tense == 'plup':
        return f'{pron} {_AUX_1SG["hafa_past"]} {supine}' if supine else None
    if tense == 'futperf':
        return f'{pron} {_AUX_1SG["munu_pres"]} {_AUX_HAFA_INF} {supine}' if supine else None
    return None


def icelandic_verb_clause(tense, latin_voice, is_deponent, is_word, verb_slots):
    """The Icelandic 1sg-indicative rendering for one (tense, latin_voice)
    cell of the Latin verb table. Deponent verbs are morphologically
    passive in Latin but active in meaning -- routed to the active germynd
    slots regardless of latin_voice, same as the Latin-side principal-parts
    rendering already does (render_principal_parts' is_deponent branch).
    Returns None (render nothing) when verb_slots has no BÍN data at all for
    this gloss word, or the specific cell isn't attested."""
    if not verb_slots:
        return None
    if is_deponent or latin_voice == 'act':
        return _active_periphrasis(tense, is_word, verb_slots)
    if latin_voice == 'pass':
        return _passive_periphrasis(tense, verb_slots)
    return None


def _is_definite_suffix_form(indef, definite):
    """Format as 'indef(suffix)' when the definite form is a straightforward
    suffixed extension of the indefinite one (hestur + inn -> hesturinn).
    Falls back to showing both forms separated by " / " for the irregular
    minority where the definite form isn't a clean suffix, rather than
    fabricating a misleading parenthetical."""
    if not indef:
        return definite
    if not definite:
        return indef
    if definite.startswith(indef):
        suffix = definite[len(indef):]
        return f'{indef}({suffix})' if suffix else indef
    return f'{indef} / {definite}'


# Latin case -> the (case_name, ...) key used by is_noun_declension.tsv /
# is_adj_declension.tsv (English names, matching build_is_morphology.py).
# Icelandic has no distinct vocative inflection at all -- ávarpsfall is
# always identical to the nominative (a real grammatical fact, not a gap;
# see recap) -- so a Latin vocative cell maps to the SAME Icelandic
# nominative lookup as a Latin nominative cell. Latin's marginal locative
# has no Icelandic case to map onto (BÍN nouns don't carry a locative slot)
# and is intentionally left out.
_CASE_TO_IS = {
    'nom': 'nominative', 'voc': 'nominative', 'gen': 'genitive',
    'dat': 'dative', 'acc': 'accusative',
}
_NUMBER_TO_IS = {'sg': 'singular', 'pl': 'plural'}


def icelandic_noun_form(case, number, noun_slots):
    """Icelandic indef(def) declined form for one Latin (case, number)
    cell, or None if not attested / not a mappable case."""
    is_case = _CASE_TO_IS.get(case)
    is_number = _NUMBER_TO_IS.get(number)
    if not is_case or not is_number or not noun_slots:
        return None
    cell = noun_slots.get((is_case, is_number))
    if not cell:
        return None
    indef, definite = cell
    return _is_definite_suffix_form(indef, definite)


def icelandic_adj_form(case, number, adj_slots):
    """Icelandic masculine-declined form for one Latin (case, number) cell.
    Latin's own declension grid (classify_and_grid) doesn't track gender
    per cell, so masculine is used as the citation gender throughout --
    same convention this dictionary already uses elsewhere for renderings
    that don't specify gender (see recap: 'masculine is the citation
    default'). Returns None if not attested."""
    is_case = _CASE_TO_IS.get(case)
    is_number = _NUMBER_TO_IS.get(number)
    if not is_case or not is_number or not adj_slots:
        return None
    return adj_slots.get((is_case, 'masculine', is_number))


def load_is_noun_declension():
    data = defaultdict(dict)
    if not os.path.exists(NOUN_DECL_PATH):
        return data
    with open(NOUN_DECL_PATH, encoding='utf-8') as f:
        for line in f:
            parts = line.rstrip('\n').split('\t')
            if len(parts) != 5:
                continue
            lemma, case_name, number, indef, definite = parts
            data[lemma][(case_name, number)] = (indef, definite)
    return data


def load_is_verb_forms():
    """Long-format data/is_verb_forms.tsv (lemma, slot, form) ->
    {lemma: {slot: form}}. Slots are keys like 'gm_ind_pres_1sg',
    'gm_supine', 'ptcp_kk_sg' -- see build_is_morphology.py. Latin has no
    middle voice, so unlike the Greek project every slot here is a gm_/
    ptcp_ key; there is no mm_ prefix to filter."""
    data = defaultdict(dict)
    if not os.path.exists(VERB_FORMS_PATH):
        return data
    with open(VERB_FORMS_PATH, encoding='utf-8') as f:
        for line in f:
            parts = line.rstrip('\n').split('\t')
            if len(parts) != 3:
                continue
            lemma, slot, form = parts
            if slot.startswith('__subj_'):
                continue  # impersonal-verb marker; not consulted for the 1sg citation rendering
            data[lemma][slot] = form
    return data


def load_is_adj_declension():
    data = defaultdict(dict)
    if not os.path.exists(ADJ_DECL_PATH):
        return data
    with open(ADJ_DECL_PATH, encoding='utf-8') as f:
        for line in f:
            parts = line.rstrip('\n').split('\t')
            if len(parts) != 5:
                continue
            lemma, case_name, gender_name, number, form = parts
            data[lemma][(case_name, gender_name, number)] = form
    return data


def load_is_adj_form_lemma():
    """gloss-word form -> adjective lemma (e.g. 'gott' -> 'góður'), for
    normalizing a gloss word that's an inflected adjective form (whatever
    gender/case/number the bridge glossary happened to produce) to its
    citation lemma before declension lookup."""
    data = {}
    if not os.path.exists(ADJ_LEMMA_PATH):
        return data
    with open(ADJ_LEMMA_PATH, encoding='utf-8') as f:
        for line in f:
            parts = line.rstrip('\n').split('\t')
            if len(parts) != 2:
                continue
            form, lemma = parts
            data[form] = lemma
    return data


def _primary_gloss_word(defs_is_json):
    """The first single-word token of the Icelandic glossary line --  the
    word this dictionary inflects for the morphology annotations. A
    multiword entry (e.g. "kunna við") is skipped in favor of the next
    word, not treated as a dead end."""
    try:
        words = json.loads(defs_is_json)
    except (json.JSONDecodeError, TypeError):
        return None
    for word in words or []:
        word = word.strip()
        if word and ' ' not in word:
            return word
    return None


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
    verb_parts_any_person = defaultdict(set)
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
            elif tense and 'ind' in tokset:
                # No 1st-singular form attested for this tense/voice in the
                # corpus (e.g. equito's imperfect is only attested as
                # equitabamus, 1st pl) -- kept separately from verb_parts so
                # citation-style principal parts (which read verb_parts
                # directly) never pick up a non-1sg form, but the tense/voice
                # table can still show the tense instead of silently
                # dropping it.
                verb_parts_any_person[(tense, voice or 'act')].add(form)
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

    return (noun_grid, verb_parts, verb_parts_any_person, infinitives, participles, supines,
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

    cells = ', '.join(f'<b class="la-word">{html.escape(f, quote=False)}</b>' if f
                      else '<span class="pp-missing">—</span>' for f in forms)
    label = 'Kennimyndir (germynd) / Principal Parts' if not is_deponent \
        else 'Kennimyndir (þolmynd, samsagnir) / Principal Parts (deponent)'
    return (f'<div class="principal-parts">'
            f'<span class="pp-label">{html.escape(label, quote=False)}</span> '
            f'<span class="pp-forms">{cells}</span></div>')


def _is_equiv_span(is_form):
    """Small inline annotation appended after a Latin form cell, showing
    the BÍN-matched Icelandic equivalent inflected the same way. Renders
    nothing (returns '') when no Icelandic form is attested for that
    cell -- gaps are left blank, never guessed."""
    if not is_form:
        return ''
    return f' <span class="is-equiv">→ {html.escape(is_form, quote=False)}</span>'


def render_morphology(rows, is_deponent, is_word=None, entry_pos=None,
                      verb_forms=None, noun_decl=None, adj_decl=None,
                      adj_form_lemma=None):
    (noun_grid, verb_parts, verb_parts_any_person, infinitives, participles, supines,
     gerundives, n_nominal, n_verbal) = classify_and_grid(rows)
    parts = []

    # BÍN slots for this entry's own Icelandic gloss word, if it has any.
    # Adjectives normalize through the form->lemma map first (a gloss word
    # like "gott" is itself an inflected form of "góður", not a citation
    # lemma) -- see icelandic_adj_form / load_is_adj_form_lemma.
    verb_slots = (verb_forms or {}).get(is_word, {}) if is_word else {}
    noun_slots = (noun_decl or {}).get(is_word, {}) if is_word else {}
    adj_lemma = (adj_form_lemma or {}).get(is_word, is_word) if is_word else None
    adj_slots = (adj_decl or {}).get(adj_lemma, {}) if adj_lemma else {}

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
            act = join_forms(verb_parts.get((tense, 'act'), []) or verb_parts_any_person.get((tense, 'act'), []))
            pas = join_forms(verb_parts.get((tense, 'pass'), []) or verb_parts_any_person.get((tense, 'pass'), []))
            if not act and not pas:
                continue
            any_tense_row = True
            label = TENSE_LABELS_IS[tense]
            act_html = (html.escape(act, quote=False) if act else '—') + \
                (_is_equiv_span(icelandic_verb_clause(tense, 'act', is_deponent, is_word, verb_slots)) if act else '')
            pas_html = (html.escape(pas, quote=False) if pas else '—') + \
                (_is_equiv_span(icelandic_verb_clause(tense, 'pass', is_deponent, is_word, verb_slots)) if pas else '')
            parts.append(f'<tr><td class="case-label">{label}</td>'
                         f'<td>{act_html}</td><td>{pas_html}</td></tr>')

        inf_rows = []
        for tense in TENSES:
            act = join_forms(infinitives.get((tense, 'act'), []))
            pas = join_forms(infinitives.get((tense, 'pass'), []))
            if act or pas:
                label = f'{TENSE_LABELS_IS[tense]} – Nafnháttur'
                inf_rows.append(f'<tr><td class="case-label">{label}</td>'
                                f'<td>{html.escape(act, quote=False) or "—"}</td><td>{html.escape(pas, quote=False) or "—"}</td></tr>')
        if inf_rows:
            parts.append('<tr class="morph-secondary-header"><td colspan="3">Nafnhættir / Infinitives</td></tr>')
            parts.extend(inf_rows)

        if gerundives:
            parts.append('<tr class="morph-secondary-header"><td colspan="3">'
                         f'{html.escape(GERUNDIVE_LABEL_IS, quote=False)} / Gerundive</td></tr>')
            parts.append(f'<tr><td class="case-label">Ksk. et. nf.</td>'
                        f'<td colspan="2">{html.escape(join_forms(gerundives), quote=False)}</td></tr>')
        if supines:
            parts.append('<tr class="morph-secondary-header"><td colspan="3">'
                         f'{html.escape(SUPINE_LABEL_IS, quote=False)} / Supine</td></tr>')
            parts.append(f'<tr><td class="case-label">Sagnbót</td>'
                        f'<td colspan="2">{html.escape(join_forms(supines), quote=False)}</td></tr>')

        if not any_tense_row and not inf_rows and not gerundives and not supines and not pp_html:
            return ''  # nothing attested worth showing
        parts.append('</table></div>')

    elif noun_grid:
        is_adjective = entry_pos == 'Adjective'
        parts.append('<div class="morph-section">')
        parts.append('<p class="morph-label">Beygingar / Declension</p>')
        parts.append('<table class="morphology-table">')
        parts.append('<tr><th>Fall</th><th>Eintala</th><th>Fleirtala</th></tr>')
        for c in CASES:
            if c not in noun_grid:
                continue
            sg = join_forms(noun_grid[c].get('sg', []))
            pl = join_forms(noun_grid[c].get('pl', []))
            label = CASE_LABELS_IS.get(c, c.capitalize())
            if is_adjective:
                sg_is = icelandic_adj_form(c, 'sg', adj_slots)
                pl_is = icelandic_adj_form(c, 'pl', adj_slots)
            else:
                sg_is = icelandic_noun_form(c, 'sg', noun_slots)
                pl_is = icelandic_noun_form(c, 'pl', noun_slots)
            sg_html = (html.escape(sg, quote=False) if sg else '—') + (_is_equiv_span(sg_is) if sg else '')
            pl_html = (html.escape(pl, quote=False) if pl else '—') + (_is_equiv_span(pl_is) if pl else '')
            parts.append(f'<tr><td class="case-label">{label}</td>'
                         f'<td>{sg_html}</td><td>{pl_html}</td></tr>')
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

# extract_glosses.py extracts each <sense> node's own text in isolation, but
# some L&S entries open a parenthetical remark in the entry preamble (before
# any <sense> starts) and only close it inside the first <sense> -- e.g. amo:
# "<pos>v. a.</pos> (amasso = ... Mull.: <sense>...) [cf. ... union], to
# like, to love, ...". The extracted sense then starts with a dangling ")",
# often behind other leftover separator punctuation (": ; ; ) , to like").
# A leading ")" is unambiguously an orphan -- a genuine balanced parenthetical
# note (810 senses start with a real "(...)" remark, e.g. "(rare) to run")
# always starts with "(", never ")" -- so only ")" (not "(") is stripped from
# the front, and only "(" (not ")") from the back, interleaved with ordinary
# separator punctuation so both can be peeled together in one pass.
_LEADING_ORPHAN_PAREN_RE = re.compile(r'^(?:[\s;:.,]|\))+')
_TRAILING_ORPHAN_PAREN_RE = re.compile(r'(?:[\s;:.,]|\()+$')


def _clean_gloss_text(text):
    text = text.replace('*', '')
    text = _LEADING_ORPHAN_PAREN_RE.sub('', text)
    text = _TRAILING_ORPHAN_PAREN_RE.sub('', text)
    text = _APPARATUS_PUNCT_RE.sub('; ', text)
    text = _EDGE_PUNCT_RE.sub('', text)
    return re.sub(r'\s{2,}', ' ', text).strip()


def render_defs(raw_json):
    """The EN (L&S) reference line. definitions_en holds extract_glosses's
    [level, text] sense pairs; only the level-1 senses -- L&S's principal
    meaning divisions -- are shown, matching what the Icelandic glossary
    line was built from. Entries with no level-1 sense at all (italic-less
    fallback extractions) show everything they have."""
    try:
        senses = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return html.escape(str(raw_json), quote=False)
    if not isinstance(senses, list):
        return html.escape(_clean_gloss_text(str(senses)), quote=False)
    pairs = [s for s in senses if isinstance(s, list) and len(s) == 2]
    texts = [t for lvl, t in pairs if lvl == 1] or [t for lvl, t in pairs]
    cleaned = (_clean_gloss_text(t) for t in texts if t and t.strip())
    return '; '.join(html.escape(t, quote=False) for t in cleaned if t)


def render_glossary_is(defs_is_json, pos):
    """The Icelandic glossary line: definitions_is is a flat ranked list of
    words (translate_definitions.py caps and dedups it). Verbs are shown in
    the Icelandic citation form with the infinitive marker ("að elska") --
    safe to prefix unconditionally for verb headwords because the hard POS
    gate guarantees every listed word actually is a verb."""
    try:
        words = json.loads(defs_is_json)
    except (json.JSONDecodeError, TypeError):
        return html.escape(str(defs_is_json), quote=False)
    if pos == 'Verb':
        words = [w if w.startswith('að ') else f'að {w}' for w in words]
    return ', '.join(html.escape(w, quote=False) for w in words)


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
    is_cursor.execute("SELECT id, definitions_en, definitions_is, pos, any_translated FROM definitions_is")
    is_by_id = {row[0]: (row[1], row[2], row[3], row[4]) for row in is_cursor.fetchall()}

    print("Loading BÍN-sourced Icelandic morphology (gitignored, generated by build_is_morphology.py)...")
    noun_decl = load_is_noun_declension()
    verb_forms = load_is_verb_forms()
    adj_decl = load_is_adj_declension()
    adj_form_lemma = load_is_adj_form_lemma()
    print(f"  {len(noun_decl)} nouns, {len(verb_forms)} verbs, {len(adj_decl)} adjectives "
          f"({len(adj_form_lemma)} form->lemma entries)")

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

            defs_en, defs_is, entry_pos, any_translated = is_by_id.get(rid, (None, None, None, 0))

            safe_title = sanitize_apple_key(lemma_display)
            if not safe_title:
                safe_title = sanitize_apple_key(key) or "unknown"

            entry_id = f"ls_{rid}"
            xml.write(f'    <d:entry id="{entry_id}" d:title="{html.escape(safe_title)}">\n')

            is_deponent = False
            try:
                entry_el = ET.fromstring(fragment)
                for pos_el in entry_el.iter():
                    if pos_el.tag.split('}')[-1] == 'pos':
                        is_deponent = 'dep' in clean_text(''.join(pos_el.itertext())).lower()
                        break
            except ET.ParseError:
                pass

            search_indices = set()
            search_indices |= search_variants(lemma_display)
            search_indices |= search_variants(base_key)

            # Morpheus lemmatizes some deponents (hortor -> horto, osculor ->
            # osculo1) under the "notional active" 1st-principal-part spelling
            # instead of the passive citation form L&S keys on, and some
            # entries collide with an unrelated homonym's disambiguation digit
            # (moror2 -> moror1) -- see deponents_recap.md in Josolon/. Only
            # widen the lookup for entries L&S itself marks deponent, so this
            # never changes matching for ordinary verbs.
            morph_query = 'SELECT form, analyses FROM forms WHERE lemma = ? OR lemma = ?'
            morph_params = [key, base_key]
            if is_deponent:
                morph_query += ' OR lemma GLOB ?'
                morph_params.append(base_key + '[0-9]')
                if base_key.endswith('or'):
                    dep_candidate = base_key[:-1]
                    morph_query += ' OR lemma = ? OR lemma GLOB ?'
                    morph_params += [dep_candidate, dep_candidate + '[0-9]']
            morph_cursor.execute(morph_query, morph_params)
            morph_rows = morph_cursor.fetchall()
            for form, _ in morph_rows:
                search_indices |= search_variants(form)

            for keyword in search_indices:
                clean_kw = sanitize_apple_key(keyword)
                if clean_kw:
                    xml.write(f'        <d:index d:value="{html.escape(clean_kw)}"/>\n')

            xml.write(f'        <h1 class="entry-lemma">{html.escape(lemma_display, quote=False)}</h1>\n')
            xml.write('        <div class="definition">\n')
            if any_translated and defs_is:
                xml.write(f'            <p class="gloss-is"><b>ÍS:</b> {render_glossary_is(defs_is, entry_pos)}</p>\n')
            else:
                xml.write('            <p class="gloss-is gloss-missing">Engin trygg þýðing í orðasafninu.</p>\n')
            if defs_en:
                xml.write(f'            <p class="gloss-en"><b>EN (L&amp;S):</b> {render_defs(defs_en)}</p>\n')
            xml.write('        </div>\n')

            is_word = _primary_gloss_word(defs_is) if (any_translated and defs_is) else None
            morph_html = render_morphology(
                morph_rows, is_deponent, is_word=is_word, entry_pos=entry_pos,
                verb_forms=verb_forms, noun_decl=noun_decl,
                adj_decl=adj_decl, adj_form_lemma=adj_form_lemma,
            ) if morph_rows else ''
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
