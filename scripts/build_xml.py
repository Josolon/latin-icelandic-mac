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
# no MM- Icelandic slot is ever consulted), and no dual number (no
# _AGREEMENT_NUMBER reuse-the-plural case). This project's main verb table
# still cites only the 1st-singular-indicative form per lexicographic
# convention (icelandic_verb_clause defaults person='1st', number='sg'),
# but the underlying BÍN data (verb_slots) DOES carry the full person x
# number paradigm for present/past, which is what the per-inflected-form
# stub entries below use -- see register_stub / verb_finite_forms.
# Deponent verbs (Latin passive morphology, active meaning) route through
# the ACTIVE germynd Icelandic slots regardless of which Latin table column
# their form happens to be tagged under -- the whole reason this dictionary
# already tracks is_deponent for the Latin-side principal-parts rendering.
# ---------------------------------------------------------------------------

PRONOUN_1SG_IS = 'ég'

# Full person x number pronoun table, for the per-inflected-form stub
# entries (register_stub, below) -- the main verb table itself still only
# ever cites 1sg (PRONOUN_1SG_IS), matching Latin lexicographic convention.
PRONOUNS_IS = {
    ('1st', 'sg'): 'ég', ('2nd', 'sg'): 'þú', ('3rd', 'sg'): 'hann/hún/það',
    ('1st', 'pl'): 'við', ('2nd', 'pl'): 'þið', ('3rd', 'pl'): 'þeir/þær/þau',
}
_PERSON_DIGIT = {'1st': '1', '2nd': '2', '3rd': '3'}

# Auxiliary paradigms, hardcoded (vera/hafa/munu are closed-class; BÍN-
# verified in the Greek project, identical closed-class forms apply here
# unchanged). Keyed by (person, number) so both the main table's 1sg-only
# rendering and the per-form stubs' full paradigm share one table.
_AUX = {
    'vera_pres': {('1st', 'sg'): 'er', ('2nd', 'sg'): 'ert', ('3rd', 'sg'): 'er',
                  ('1st', 'pl'): 'erum', ('2nd', 'pl'): 'eruð', ('3rd', 'pl'): 'eru'},
    'vera_past': {('1st', 'sg'): 'var', ('2nd', 'sg'): 'varst', ('3rd', 'sg'): 'var',
                  ('1st', 'pl'): 'vorum', ('2nd', 'pl'): 'voruð', ('3rd', 'pl'): 'voru'},
    'munu_pres': {('1st', 'sg'): 'mun', ('2nd', 'sg'): 'munt', ('3rd', 'sg'): 'mun',
                  ('1st', 'pl'): 'munum', ('2nd', 'pl'): 'munuð', ('3rd', 'pl'): 'munu'},
    'hafa_pres': {('1st', 'sg'): 'hef', ('2nd', 'sg'): 'hefur', ('3rd', 'sg'): 'hefur',
                  ('1st', 'pl'): 'höfum', ('2nd', 'pl'): 'hafið', ('3rd', 'pl'): 'hafa'},
    'hafa_past': {('1st', 'sg'): 'hafði', ('2nd', 'sg'): 'hafðir', ('3rd', 'sg'): 'hafði',
                  ('1st', 'pl'): 'höfðum', ('2nd', 'pl'): 'höfðuð', ('3rd', 'pl'): 'höfðu'},
}
_AUX_HAFA_INF = 'hafa'

# Past-participle BÍN slot keys for the masculine/feminine/neuter, singular
# AND plural -- see build_is_morphology.py's _PARTICIPLE_TAGS. Plural is
# needed for stubs (a 1pl/2pl/3pl passive form needs the plural participle),
# not just the main table's singular-only 1sg citation.
_PTCP_SLOT = {
    ('masculine', 'sg'): 'ptcp_kk_sg', ('masculine', 'pl'): 'ptcp_kk_pl',
    ('feminine', 'sg'): 'ptcp_kvk_sg', ('feminine', 'pl'): 'ptcp_kvk_pl',
    ('neuter', 'sg'): 'ptcp_hk_sg', ('neuter', 'pl'): 'ptcp_hk_pl',
}


def _passive_periphrasis(tense, person, number, verb_slots):
    """Þolmynd (passive): "ég er/var/mun vera/hef verið + participle" -- no
    Icelandic voice has a synthetic passive (true even where LATIN's own
    passive is synthetic, e.g. present "amor"), so this is always
    periphrastic. The subject's gender isn't known from the Latin form
    (person/number only), so the participle shows all three gender forms
    slash-joined (menntaður/menntuð/menntað) rather than defaulting to
    masculine -- see recap. Returns None if the participle isn't attested at
    all, or the tense has no Icelandic tense mapping."""
    pron = PRONOUNS_IS.get((person, number))
    if not pron:
        return None
    ptcp_forms = [verb_slots.get(_PTCP_SLOT[(g, number)]) for g in ('masculine', 'feminine', 'neuter')]
    if not all(ptcp_forms):
        return None
    ptcp = '/'.join(ptcp_forms)
    pn = (person, number)
    if tense == 'pres':
        return f'{pron} {_AUX["vera_pres"][pn]} {ptcp}'
    if tense == 'imperf':
        return f'{pron} {_AUX["vera_past"][pn]} {ptcp}'
    if tense == 'fut':
        return f'{pron} {_AUX["munu_pres"][pn]} vera {ptcp}'
    if tense == 'perf':
        return f'{pron} {_AUX["hafa_pres"][pn]} verið {ptcp}'
    if tense == 'plup':
        return f'{pron} {_AUX["hafa_past"][pn]} verið {ptcp}'
    if tense == 'futperf':
        return f'{pron} {_AUX["munu_pres"][pn]} hafa verið {ptcp}'
    return None


def _active_periphrasis(tense, person, number, is_word, verb_slots):
    """Germynd (active) indicative Icelandic clause for one Latin tense
    cell, at the given person/number. is_word is the Icelandic gloss verb's
    own citation/infinitive form (also used as the germynd infinitive --
    Icelandic verb citation forms already ARE the infinitive). Returns None
    if the specific BÍN cell needed isn't attested."""
    pron = PRONOUNS_IS.get((person, number))
    pdigit = _PERSON_DIGIT.get(person)
    if not pron or not pdigit:
        return None
    pn = (person, number)
    supine = verb_slots.get('gm_supine')
    if tense == 'pres':
        pres = verb_slots.get(f'gm_ind_pres_{pdigit}{number}')
        if not pres:
            return None
        clause = f'{pron} {pres}'
        if is_word:
            clause += f' / {pron} {_AUX["vera_pres"][pn]} að {is_word}'
        return clause
    if tense == 'imperf':
        return f'{pron} {_AUX["vera_past"][pn]} að {is_word}' if is_word else None
    if tense == 'fut':
        return f'{pron} {_AUX["munu_pres"][pn]} {is_word}' if is_word else None
    if tense == 'perf':
        past = verb_slots.get(f'gm_ind_past_{pdigit}{number}')
        return f'{pron} {past}' if past else None
    if tense == 'plup':
        return f'{pron} {_AUX["hafa_past"][pn]} {supine}' if supine else None
    if tense == 'futperf':
        return f'{pron} {_AUX["munu_pres"][pn]} {_AUX_HAFA_INF} {supine}' if supine else None
    return None


def icelandic_verb_clause(tense, latin_voice, is_deponent, is_word, verb_slots,
                          person='1st', number='sg'):
    """The Icelandic indicative rendering for one (tense, latin_voice) cell
    of the Latin verb table, at the given person/number (defaults to 1sg,
    the main table's citation form; the per-inflected-form stubs pass the
    form's own actual person/number). Deponent verbs are morphologically
    passive in Latin but active in meaning -- routed to the active germynd
    slots regardless of latin_voice, same as the Latin-side principal-parts
    rendering already does (render_principal_parts' is_deponent branch).
    Returns None (render nothing) when verb_slots has no BÍN data at all for
    this gloss word, or the specific cell isn't attested."""
    if not verb_slots:
        return None
    if is_deponent or latin_voice == 'act':
        return _active_periphrasis(tense, person, number, is_word, verb_slots)
    if latin_voice == 'pass':
        return _passive_periphrasis(tense, person, number, verb_slots)
    return None


# ---------------------------------------------------------------------------
# Per-inflected-form stub entries -- ported from ancient-greek-icelandic-mac's
# lsjform_ stub mechanism (see bin-morphology-recap.md for the design). Every
# distinct Latin form of a curated cell (noun/adjective case x number,
# indicative verb tense x voice x person x number) gets its own small
# d:entry, title = the exact attested spelling, linking back to the lemma
# entry and showing that cell's grammatical parse (classical Latin
# abbreviation over an Icelandic one) plus the BÍN-matched Icelandic
# rendering, if any. Deliberately scoped to those two curated grids -- NOT
# infinitives, gerundive, supine, or participles, which stay accessible only
# via the main lemma entry (participles in particular aren't classified into
# any grid at all today; see classify_and_grid's `if 'part' in tokset:
# continue`).
#
# Two parallel grammar tags per cell, same convention as the Greek project:
# the classical/international one (the abbreviations every classicist
# already reads, e.g. "1. sg. ind. imperf. act.") above the Icelandic one
# describing the Icelandic rendering (e.g. "1. p. et. frh. dþt. gm.").
# ---------------------------------------------------------------------------

_CLASSICAL_ABBR = {
    'case': {'nom': 'nom.', 'gen': 'gen.', 'dat': 'dat.', 'acc': 'acc.',
             'abl': 'abl.', 'voc': 'voc.', 'loc': 'loc.'},
    'person': {'1st': '1.', '2nd': '2.', '3rd': '3.'},
    'number': {'sg': 'sg.', 'pl': 'pl.'},
    'mood': {'ind': 'ind.'},
    'tense': {'pres': 'praes.', 'imperf': 'impf.', 'fut': 'fut.',
              'perf': 'perf.', 'plup': 'plusqu.', 'futperf': 'futperf.'},
    'voice': {'act': 'act.', 'pass': 'pass.'},
}
_ICELANDIC_ABBR = {
    'case': {'nom': 'nf.', 'gen': 'ef.', 'dat': 'þgf.', 'acc': 'þf.',
             'abl': 'svf.', 'voc': 'áf.', 'loc': 'staðarf.'},
    'person': {'1st': '1. p.', '2nd': '2. p.', '3rd': '3. p.'},
    'number': {'sg': 'et.', 'pl': 'ft.'},
    'mood': {'ind': 'frh.'},
    'tense': {'pres': 'nt.', 'imperf': 'dþt.', 'fut': 'frt.',
              'perf': 'nlt.', 'plup': 'þlt.', 'futperf': 'þframt.'},
    'voice': {'act': 'gm.', 'pass': 'þm.'},
}


def _grammar_tag(scheme, case=None, person=None, number=None, mood=None,
                 tense=None, voice=None):
    """One space-joined grammar tag in the given abbreviation `scheme`
    (_CLASSICAL_ABBR or _ICELANDIC_ABBR). Canonical order: case, person,
    number, mood, tense, voice (case/person are mutually exclusive --
    nominal vs. verbal cells). Fields absent from a given cell are omitted."""
    def ab(cat, val):
        return scheme[cat].get(val, str(val))
    parts = []
    if case:
        parts.append(ab('case', case))
    if person:
        parts.append(ab('person', person))
    if number:
        parts.append(ab('number', number))
    if mood:
        parts.append(ab('mood', mood))
    if tense:
        parts.append(ab('tense', tense))
    if voice:
        parts.append(ab('voice', voice))
    return ' '.join(parts)


def _dual_tag(**kw):
    """(classical_tag, icelandic_tag) for one nominal cell's parse."""
    return _grammar_tag(_CLASSICAL_ABBR, **kw), _grammar_tag(_ICELANDIC_ABBR, **kw)


def _verb_tag(person, number, tense, latin_voice, is_deponent):
    """(classical_tag, icelandic_tag) for one indicative verb form. The
    classical tag always shows the REAL attested Latin voice (passive for a
    deponent's own morphology); the Icelandic tag shows the voice the
    rendering actually routed through (germynd for a deponent, since
    icelandic_verb_clause renders deponents via the active slots) -- same
    asymmetry the main table's deponent principal-parts label already
    acknowledges."""
    classical = _grammar_tag(_CLASSICAL_ABBR, person=person, number=number,
                             mood='ind', tense=tense, voice=latin_voice)
    icel_voice = 'act' if (is_deponent or latin_voice == 'act') else latin_voice
    icelandic = _grammar_tag(_ICELANDIC_ABBR, person=person, number=number,
                             mood='ind', tense=tense, voice=icel_voice)
    return classical, icelandic


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


_PERSONS = ('1st', '2nd', '3rd')


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
    # raw_form -> set of (tense, voice, person, number) indicative parsings --
    # every attested person/number, not just the 1st-sg citation cell above.
    # This is the basis for the per-inflected-form stub entries (unlike
    # verb_parts/verb_parts_any_person, which only ever need ONE
    # representative form per tense/voice for the main table).
    verb_finite_forms = defaultdict(set)
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
            person = next((p for p in _PERSONS if p in tokset), None)

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
                if person and number:
                    verb_finite_forms[form].add((tense, voice or 'act', person, number))
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
                if person and number:
                    verb_finite_forms[form].add((tense, voice or 'act', person, number))
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
            gerundives, n_nominal, n_verbal, verb_finite_forms)


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
     gerundives, n_nominal, n_verbal, _verb_finite_forms) = classify_and_grid(rows)
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

        # Per-inflected-form stub entries -- see the module docstring above
        # icelandic_verb_clause. raw_form -> list of (lemma_title,
        # lemma_entry_id, classical_tag, icelandic_tag, is_rendering),
        # accumulated across every lemma and written as their own d:entry
        # blocks after the main loop (a form can be attested for more than
        # one lemma, a real homonym collision, and needs merging into one
        # stub listing all of them).
        form_stub_candidates = defaultdict(list)

        def register_stub(raw_form, lemma_title, lemma_entry_id, tags, is_rendering):
            if not raw_form or raw_form.lower() == base_key.lower():
                return  # same spelling as the lemma's own citation form
            classical_tag, icelandic_tag = tags
            form_stub_candidates[raw_form].append(
                (lemma_title, lemma_entry_id, classical_tag, icelandic_tag, is_rendering))

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

            # Per-inflected-form stubs: every attested case/number (nominal)
            # and every attested person/number/tense/voice (finite
            # indicative verb) cell -- see register_stub above and the
            # module docstring near icelandic_verb_clause.
            if morph_rows:
                verb_slots = (verb_forms or {}).get(is_word, {}) if is_word else {}
                noun_slots = (noun_decl or {}).get(is_word, {}) if is_word else {}
                adj_lemma = (adj_form_lemma or {}).get(is_word, is_word) if is_word else None
                adj_slots = (adj_decl or {}).get(adj_lemma, {}) if adj_lemma else {}
                is_adjective = entry_pos == 'Adjective'

                (noun_grid, _verb_parts, _verb_parts_any, _infinitives, _participles,
                 _supines, _gerundives, _n_nominal, _n_verbal, verb_finite_forms) = \
                    classify_and_grid(morph_rows)

                for c, by_number in noun_grid.items():
                    for number, forms in by_number.items():
                        rendering = (icelandic_adj_form(c, number, adj_slots) if is_adjective
                                    else icelandic_noun_form(c, number, noun_slots))
                        tags = _dual_tag(case=c, number=number)
                        for raw_form in forms:
                            register_stub(raw_form, lemma_display, entry_id, tags, rendering)

                for raw_form, parsings in verb_finite_forms.items():
                    for tense, voice, person, number in parsings:
                        rendering = icelandic_verb_clause(
                            tense, voice, is_deponent, is_word, verb_slots,
                            person=person, number=number)
                        tags = _verb_tag(person, number, tense, voice, is_deponent)
                        register_stub(raw_form, lemma_display, entry_id, tags, rendering)

            if (index + 1) % 5000 == 0:
                print(f"   ... Processed {index + 1} / {total} entries")

        print(f"Writing {len(form_stub_candidates)} inflected-form stub entries...")
        for i, (raw_form, parsings) in enumerate(sorted(form_stub_candidates.items())):
            safe_stub_title = sanitize_apple_key(raw_form)
            if not safe_stub_title:
                continue
            stub_id = f"lsform_{i}"
            xml.write(f'    <d:entry id="{stub_id}" d:title="{html.escape(safe_stub_title)}">\n')
            xml.write(f'        <d:index d:value="{html.escape(safe_stub_title)}"/>\n')
            xml.write(f'        <h1 class="entry-lemma">{html.escape(raw_form, quote=False)}</h1>\n')
            xml.write('        <div class="definition">\n')
            xml.write('            <p class="gloss-en"><i>Beygingarmynd / Inflected form</i></p>\n')
            # A form can be attested for more than one lemma (a real homonym
            # collision) or more than one cell of the SAME lemma (a
            # syncretic form, e.g. Latin nom/voc syncretism) -- dedup on
            # (lemma, icelandic-tag) so the stub doesn't repeat a line.
            seen = set()
            for lemma_title, lemma_entry_id, classical_tag, icelandic_tag, is_rendering in parsings:
                dedup_key = (lemma_entry_id, icelandic_tag)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                link = f'<a href="x-dictionary:r:{lemma_entry_id}">{html.escape(lemma_title, quote=False)}</a>'
                cl = f'<i class="tag-cl">{html.escape(classical_tag, quote=False)}</i>' if classical_tag else ''
                isl = html.escape(icelandic_tag, quote=False)
                if is_rendering:
                    line = (f'<b>{html.escape(is_rendering, quote=False)}</b> '
                            f'<i>— af {link}, {isl}</i>')
                else:
                    line = f'<i>af {link}, {isl}</i>'
                if cl:
                    line = f'{cl}<br/>{line}'
                xml.write(f'            <p class="gloss-is">{line}</p>\n')
            xml.write('        </div>\n')
            xml.write('    </d:entry>\n\n')

            if (i + 1) % 20000 == 0:
                print(f"   ... {i + 1}/{len(form_stub_candidates)} stub entries")

        xml.write('</d:dictionary>\n')

    print(f"Success! XML built at {OUTPUT_XML_PATH}")

    ls_conn.close()
    morph_conn.close()
    is_conn.close()


if __name__ == "__main__":
    build_dictionary()
