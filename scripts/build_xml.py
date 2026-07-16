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
# Reconstructed Classical Latin pronunciation guide (Vox Latina, W. Sidney
# Allen), mapped onto Icelandic anchor words -- ported from the identical
# pattern in ancient-greek-icelandic-mac's write_pronunciation_guide_entry
# (see phonology-recap.md in Josolon/ for the phoneme inventory this is
# built from -- the same one scripts/build_xml.py's own latin_to_ipa engine
# in the sibling latin-mac project implements). A hand-authored reference
# entry, not derived from ls.db/morph.db.
#
# Icelandic anchor-word choices, verified against Icelandic phonology (not
# assumed from spelling) rather than reused blind from the Greek table --
# though several genuinely ARE the same anchor, because the underlying
# target sound is identical:
#   - Icelandic has NO voiced stops at all (b/d/g spell voiceless
#     UNASPIRATED [p t k] -- Icelandic contrasts aspiration, not voicing).
#     This cuts both ways: it rules Icelandic out for Latin's genuinely
#     voiced b/d/g/v (foreign anchor needed), but makes Icelandic's own
#     b/d/g the correct anchor for Latin's plain unaspirated p/t/k=c, and
#     Icelandic's (word-initial-aspirated) p/t/k the correct anchor for
#     Latin's aspirated ph/th/ch -- same reasoning the Greek table already
#     applies to π/φ and κ/χ, so those four rows reuse its exact anchors.
#   - Icelandic vowel length is positional, not a separate phoneme per
#     letter (stressed vowel + single following consonant = long; + 2+
#     consonants = short) -- verified via en.wikipedia.org/wiki/Icelandic_orthography.
#     This gives real minimal-pair anchors for Latin's phonemic short/long
#     vowel pairs (aska/aka for a/ā, matching the Greek table's own
#     α/ᾱ choice) without needing a foreign example.
#   - á/æ/ó/au/ei are diphthongized in modern Icelandic (á=[au̯], æ=[ai̯],
#     ó=[ou̯]) -- confirmed against the same source -- which is what makes
#     æ/á excellent native anchors for Latin's ae/au diphthongs, but rules
#     Icelandic out for Latin's plain long ē/ō (Icelandic has no plain
#     [eː]/[oː] monophthong at all) and short y/ȳ (Icelandic y merged onto
#     i=[ɪ], NOT the front-rounded [ʏ]/[yː] Latin y needs -- Icelandic's
#     own short U is the front-rounded [ʏ] instead, an unintuitive
#     letter-swap worth getting right rather than assuming from spelling).
#   - Latin's true-length geminates (pp, ll, ss...) have no Icelandic
#     equivalent: Icelandic's own doubled letters are realized as
#     preaspiration (pp/tt/kk -> [ʰp ʰt ʰk]) or pre-stopping (ll/nn in some
#     environments -> [tl]/[tn]), never held-twice-as-long the way Latin's
#     are -- so no geminate consonant row is included at all, rather than
#     showing a misleading Icelandic "double letter" that isn't actually
#     length. Same conclusion the Greek project's own table reached for
#     its λλ/νν rows (see phonology-recap.md's Icelandic section).
#   - Where Icelandic genuinely has no match (voiced stops, /w/, /ʊ/, the
#     rare Greek-loanword ȳ/z, the oe diphthong), a foreign loanword is
#     used instead, prefixed with its language ("e." enska, "þ." þýska,
#     "fr." franska) -- same convention as the Greek table, several
#     examples reused verbatim where the target IPA value is identical.
# ---------------------------------------------------------------------------

# Descriptive terminology cross-checked against Eiríkur Rögnvaldsson,
# Íslensk hljóðfræði (1990), https://eirikur.hi.is/hljfr.pdf -- the standard
# Icelandic-language phonetics terminology this project uses throughout its
# own dictionaries. Vowel height uses the source's own three-way system
# (nálægt/miðlægt/fjarlægt = close/mid/open) extended with the transparent
# compound "hálfnálægt" (close-mid) for the one height Icelandic's smaller
# vowel inventory doesn't itself distinguish but Latin needs (ē/ō vs. the
# native miðlægt e/o); frammælt/uppmælt (front/back, NOT the "framlægt/
# afturlægt" terms an earlier draft used, which don't appear in the source
# at all) and kringt/ókringt (rounded/unrounded) are used as given.
# Consonant place terms (tvívaramælt, tannvaramælt -- NOT "varatannamælt",
# tannbergsmælt, framgómmælt, uppgómmælt -- NOT the English-calque
# "velarmælt"/"velarlokhljóð" an earlier draft used) and the sveifluhljóð
# category for r (NOT "titurhljóð" -- the source's own r/ɾ row is
# classified as sveifluhljóð, "flap-type", not "trill") are taken directly
# from the source's own consonant chart. qu/v are lip-ROUNDED ([kʷ]/[w]),
# corrected from an earlier draft's backwards "varaglennt" (lip-spread) to
# "varakringt".
_PRONUNCIATION_VOWELS = [
    ("ă", "/a/", "stutt, fjarlægt, uppmælt sérhljóð", "a í aska"),
    ("ā", "/aː/", "langt, fjarlægt, uppmælt sérhljóð", "a í aka"),
    ("ĕ", "/ɛ/", "stutt, miðlægt, frammælt, ókringt sérhljóð", "e í enda"),
    ("ē", "/eː/", "langt, hálfnálægt, frammælt, ókringt sérhljóð (þéttara en ísl. miðlægt e)", "e í þ. Weg"),
    ("ae", "/ai̯/", "tvíhljóð: fjarlægt sérhljóð → nálægt, frammælt sérhljóð", "æ í sæll"),
    ("au", "/au̯/", "tvíhljóð: fjarlægt sérhljóð → nálægt, uppmælt, kringt sérhljóð", "á í sá"),
    ("oe", "/oi̯/", "tvíhljóð: miðlægt, uppmælt, kringt → nálægt, frammælt sérhljóð (sjaldgæft)", "o í e. boy"),
    ("ĭ", "/ɪ/", "stutt, nálægt, frammælt, ókringt sérhljóð", "i í ilma"),
    ("ī", "/iː/", "langt, nálægt, frammælt, ókringt sérhljóð", "í í líta"),
    ("ŏ", "/ɔ/", "stutt, miðlægt, uppmælt, kringt sérhljóð", "o í sofa"),
    ("ō", "/oː/", "langt, hálfnálægt, uppmælt, kringt sérhljóð (þéttara en ísl. miðlægt o)", "o í þ. Boot"),
    ("ŭ", "/ʊ/", "stutt, hálfnálægt, uppmælt, kringt sérhljóð (slakara en ísl. ú)", "u í e. put"),
    ("ū", "/uː/", "langt, nálægt, uppmælt, kringt sérhljóð", "ú í búa"),
    ("y̆", "/ʏ/", "stutt, nálægt, frammælt, kringt sérhljóð (grískt tökuorð)", "u í sund"),
    ("ȳ", "/yː/", "langt, nálægt, frammælt, kringt sérhljóð (grískt tökuorð, sjaldgæft)", "ü í þ. über"),
]
_PRONUNCIATION_CONSONANTS = [
    ("b", "/b/", "raddað tvívaramælt lokhljóð", "b í e. bad"),
    ("d", "/d/", "raddað tannbergsmælt lokhljóð", "d í fr. deux"),
    ("f", "/f/", "óraddað tannvaramælt önghljóð", "f í fara"),
    ("g", "/ɡ/", "raddað uppgómmælt lokhljóð (alltaf hart -- aldrei mýkt fyrir e/i)", "g í fr. garçon"),
    ("h", "/h/", "óraddað raddbandaönghljóð", "h í hestur"),
    ("j", "/j/", "raddað framgómmælt önghljóð", "j í já"),
    ("c/k/qu-", "/k/", "óraddað ófráblásið uppgómmælt lokhljóð", "g í gæti"),
    ("l (fyrir i/y, eða tvöfalt ll)", "/l/", "\"l exilis\": raddað, óvelarað tannbergsmælt hliðarhljóð", "l í lilja"),
    ("l (annars staðar)", "/ɫ/", "\"l pinguis\": raddað, velarað tannbergsmælt hliðarhljóð", "l í e. milk"),
    ("m", "/m/", "raddað tvívaramælt nefhljóð", "m í mæla"),
    ("n", "/n/", "raddað tannbergsmælt nefhljóð", "n í næla"),
    ("n á undan g/qu (gn)", "/ŋ/", "uppgómmælt nefhljóð", "n í langur"),
    ("p", "/p/", "óraddað ófráblásið tvívaramælt lokhljóð", "b í bera"),
    ("ph", "/pʰ/", "óraddað fráblásið tvívaramælt lokhljóð (grískt tökuorð)", "p í pera"),
    ("qu", "/kʷ/", "varakringt, óraddað uppgómmælt lokhljóð", "qu í e. quick"),
    ("r", "/r/", "raddað tannbergsmælt sveifluhljóð", "r í vor"),
    ("s", "/s/", "óraddað tannbergsmælt önghljóð", "s í sofa"),
    ("t", "/t/", "óraddað ófráblásið tannbergsmælt lokhljóð", "d í döf"),
    ("th", "/tʰ/", "óraddað fráblásið tannbergsmælt lokhljóð (grískt tökuorð)", "t í töf"),
    ("v", "/w/", "varakringt, raddað nálgunarhljóð (EKKI ísl./e. v-hljóð)", "v í e. wine"),
    ("x", "/ks/", "óraddað uppgómmælt lokhljóð + tannbergsmælt önghljóð", "x í lax"),
    ("z", "/dz/", "raddað tannbergsmælt affrikata (grískt tökuorð)", ""),
]

# Same "LETTER í WORD" auto-italicization convention as the Greek table --
# see _render_anchor_html.
_LEADING_ANCHOR_RE = re.compile(r'^(\S+) í ')


def _render_anchor_html(text):
    if not text:
        return '—'
    m = _LEADING_ANCHOR_RE.match(text)
    if not m:
        return html.escape(text, quote=False)
    lead = html.escape(m.group(1), quote=False)
    rest = html.escape(text[m.end():], quote=False)
    return f'<i>{lead}</i> í {rest}'


def write_pronunciation_guide_entry(xml):
    """A hand-authored reference entry (not derived from ls.db/morph.db)
    mapping reconstructed Classical Latin pronunciation onto Icelandic
    anchor words -- see _PRONUNCIATION_VOWELS/_PRONUNCIATION_CONSONANTS
    above."""
    entry_id = "pronunciation_guide"
    title = "Framburður latínu"
    xml.write(f'    <d:entry id="{entry_id}" d:title="{html.escape(title)}">\n')
    for keyword in (title, "framburður", "framburður latínu",
                    "íslenskur framburður latínu", "pronunciation",
                    "pronunciation guide", "frambur"):
        xml.write(f'        <d:index d:value="{html.escape(keyword)}"/>\n')
    xml.write(f'        <h1 class="entry-lemma">{html.escape(title)}</h1>\n')
    xml.write('        <p class="entry-preamble">Handbók fyrir íslenskumælandi</p>\n')
    xml.write('        <div class="definition">\n')
    xml.write(
        '            <p class="gloss-is">Endursköpuð klassísk latína (um 1. öld f. Kr. – 1. öld e. Kr.) '
        'skv. W. Sidney Allen, <i>Vox Latina</i>. Aðeins orð sem Lewis &amp; Short merkir með lengdar- '
        'eða styttingarmerki (ā/ă) fá sýndan framburð annars staðar í þessari orðabók -- ómerkt sérhljóð '
        'er of ótryggt til að giska á lengd þess. Íslenska hefur enga raddaða lokhljóða (b/d/g eru í '
        'raun óraddaðir, ófráblásnir [p t k]), svo erlend (ensk/þýsk/frönsk) hjálparorð eru notuð fyrir '
        'latnesku raddhljóðin b/d/g/v. Sérhljóð á undan n eða m sem sjálft stendur í lok orðs, eða á '
        'undan s/f, nefkveðast og samhljóðið fellur brott (t.d. <i>etiam</i> → [ɛ.ti.ãː]); tvöfaldir '
        'samhljóðar (pp, ll, ss o.fl.) eru sannarlega tvöfalt lengri en einfaldir, ólíkt íslenskum '
        'tvöföldum bókstöfum sem tákna forblástur eða forstopp fremur en lengd, svo þeir fá ekki eigin '
        'línu hér. Áhersla fellur á næstsíðasta atkvæði ef það er þungt, annars á þriðja aftast.</p>\n')

    def _write_table(heading, rows):
        xml.write('            <div class="morph-section">\n')
        xml.write(f'                <p class="morph-label">{html.escape(heading)}</p>\n')
        xml.write('                <table class="morphology-table">\n')
        xml.write('                    <tr><th>Tákn</th><th>IPA</th><th>Hljóðlýsing</th>'
                   '<th>Íslenskt hjálpardæmi</th></tr>\n')
        for symbol, ipa, description, anchor in rows:
            xml.write(
                f'                    <tr><td class="case-label">{html.escape(symbol, quote=False)}</td>'
                f'<td>{html.escape(ipa, quote=False)}</td>'
                f'<td>{html.escape(description, quote=False)}</td>'
                f'<td>{_render_anchor_html(anchor)}</td></tr>\n')
        xml.write('                </table>\n')
        xml.write('            </div>\n')

    _write_table("Sérhljóð og tvíhljóð", _PRONUNCIATION_VOWELS)
    _write_table("Samhljóð", _PRONUNCIATION_CONSONANTS)
    xml.write('        </div>\n')
    xml.write('    </d:entry>\n\n')


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


# Icelandic has just one viðtengingarháttur (subjunctive) mood, split into a
# present-stem and a past-stem form -- Latin's four subjunctive tenses have
# to collapse onto that split. Sequence-of-tense convention (this project's
# own judgment call, not independently classicist-verified the way the
# Greek project's subjunctive/optative mapping was): primary sequence
# (present, perfect subjunctive) -> Icelandic present viðtengingarháttur;
# secondary sequence (imperfect, pluperfect subjunctive) -> Icelandic past
# viðtengingarháttur. Spot-check before treating as authoritative.
_SUBJ_SEQUENCE = {'pres': 'nt', 'perf': 'nt', 'imperf': 'th', 'plup': 'th'}

# Closed-class "vera" subjunctive paradigm (sé/séum.../væri/værum...), for
# the þolmynd (passive) subjunctive periphrasis -- "(þótt) hann sé
# menntaður" / "(þótt) hann væri menntaður".
_VERA_SUBJ = {
    'nt': {('1st', 'sg'): 'sé', ('2nd', 'sg'): 'sért', ('3rd', 'sg'): 'sé',
           ('1st', 'pl'): 'séum', ('2nd', 'pl'): 'séuð', ('3rd', 'pl'): 'séu'},
    'th': {('1st', 'sg'): 'væri', ('2nd', 'sg'): 'værir', ('3rd', 'sg'): 'væri',
           ('1st', 'pl'): 'værum', ('2nd', 'pl'): 'væruð', ('3rd', 'pl'): 'væru'},
}
# Greek subjunctive/optative render as Icelandic viðtengingarháttur, which an
# Icelandic subordinator governs -- "(þótt) ég mennti" reads as a real
# subjunctive clause ("though I educate"). Latin's subjunctive is used the
# same way in dependent clauses, so the same convention applies here.
_SUBJ_PREFIX = '(þótt) '


def _subjunctive_periphrasis(tense, latin_voice, person, number, verb_slots, is_deponent):
    """Icelandic viðtengingarháttur rendering for one Latin subjunctive
    (tense, latin_voice, person, number) cell. Returns None if the tense has
    no sequence-of-tense mapping (_SUBJ_SEQUENCE), or the specific BÍN cell
    needed isn't attested."""
    bucket = _SUBJ_SEQUENCE.get(tense)
    pron = PRONOUNS_IS.get((person, number))
    pdigit = _PERSON_DIGIT.get(person)
    if not bucket or not pron or not pdigit:
        return None
    if is_deponent or latin_voice == 'act':
        bin_tense = 'pres' if bucket == 'nt' else 'past'
        form = verb_slots.get(f'gm_subj_{bin_tense}_{pdigit}{number}')
        return f'{_SUBJ_PREFIX}{pron} {form}' if form else None
    if latin_voice == 'pass':
        ptcp_forms = [verb_slots.get(_PTCP_SLOT[(g, number)]) for g in ('masculine', 'feminine', 'neuter')]
        if not all(ptcp_forms):
            return None
        vera_form = _VERA_SUBJ[bucket].get((person, number))
        if not vera_form:
            return None
        return f'{_SUBJ_PREFIX}{pron} {vera_form} {"/".join(ptcp_forms)}'
    return None


def icelandic_verb_clause(tense, latin_voice, is_deponent, is_word, verb_slots,
                          person='1st', number='sg', mood='ind'):
    """The Icelandic rendering for one (mood, tense, latin_voice) cell of
    the Latin verb table, at the given person/number (defaults to 1sg, the
    main table's citation form; the per-inflected-form stubs pass the
    form's own actual person/number). Deponent verbs are morphologically
    passive in Latin but active in meaning -- routed to the active germynd
    slots regardless of latin_voice, same as the Latin-side principal-parts
    rendering already does (render_principal_parts' is_deponent branch).
    Returns None (render nothing) when verb_slots has no BÍN data at all for
    this gloss word, or the specific cell isn't attested."""
    if not verb_slots:
        return None
    if mood == 'subj':
        return _subjunctive_periphrasis(tense, latin_voice, person, number, verb_slots, is_deponent)
    if is_deponent or latin_voice == 'act':
        return _active_periphrasis(tense, person, number, is_word, verb_slots)
    if latin_voice == 'pass':
        return _passive_periphrasis(tense, person, number, verb_slots)
    return None


def icelandic_infinitive_clause(tense, latin_voice, is_deponent, is_word, verb_slots):
    """Icelandic rendering for one Latin infinitive (tense, latin_voice)
    cell. Only present and perfect are ever attested as single-word forms in
    morph.db (Latin's future infinitive is itself periphrastic, "amaturus
    esse", so never appears as one token) -- other tenses simply have no
    infinitives dict entry to call this for. Passive is only rendered for
    the present (Latin's perfect passive infinitive, "amatus esse", is
    likewise periphrastic and never a single attested word)."""
    if is_deponent or latin_voice == 'act':
        if tense == 'pres':
            return f'að {is_word}' if is_word else None
        if tense == 'perf':
            supine = verb_slots.get('gm_supine')
            return f'að hafa {supine}' if supine else None
        return None
    if latin_voice == 'pass' and tense == 'pres':
        ptcp_forms = [verb_slots.get(_PTCP_SLOT[(g, 'sg')]) for g in ('masculine', 'feminine', 'neuter')]
        if not all(ptcp_forms):
            return None
        return f'að vera {"/".join(ptcp_forms)}'
    return None


def icelandic_participle_form(case, gender, number, tense, latin_voice, is_deponent, verb_slots):
    """Icelandic rendering for one Latin participle cell. Only the perfect
    passive participle's NOMINATIVE forms have a matching BÍN slot at all
    (_PTCP_SLOT / build_is_morphology.py's _PARTICIPLE_TAGS only extracts
    the nominative sg/pl per gender, not a full oblique-case declension) --
    other cases, and the present-active/future-active participles (no
    Icelandic morphological equivalent captured by this project's BÍN
    extraction), render tag-only, no guessed form. Deponent verbs' perfect
    participle is ACTIVE in meaning despite passive form ("hortatus" =
    "having urged", not "having been urged") -- the only rendering path
    available assumes a passive sense, so deponents render nothing here
    rather than a wrong-meaning translation."""
    if is_deponent or latin_voice != 'pass' or tense != 'perf' or case != 'nom':
        return None
    is_gender = {'masc': 'masculine', 'fem': 'feminine', 'neut': 'neuter'}.get(gender)
    if not is_gender or not verb_slots:
        return None
    return verb_slots.get(_PTCP_SLOT.get((is_gender, number)))


def icelandic_supine_form(is_purpose, is_word):
    """Icelandic rendering for the purpose ("-um", til að X) supine only --
    the respect ("-u") supine's idiomatic Icelandic equivalent (used with
    adjectives of ease/difficulty, "auðvelt yfirstíga") isn't a fixed
    pronoun+verb construction the way every other rendering in this file is,
    so it's left tag-only rather than guessed."""
    if not is_purpose or not is_word:
        return None
    return f'til að {is_word}'


# ---------------------------------------------------------------------------
# Per-inflected-form stub entries -- ported from ancient-greek-icelandic-mac's
# lsjform_ stub mechanism (see bin-morphology-recap.md for the design). Every
# distinct Latin form of a curated cell -- noun/adjective case x number,
# verb tense x voice x mood x person x number (indicative and subjunctive),
# infinitive tense x voice, gerundive case x gender x number, supine, and
# participle case x gender x number x tense x voice -- gets its own small
# d:entry, title = the exact attested spelling, linking back to the lemma
# entry and showing that cell's grammatical parse (classical Latin
# abbreviation over an Icelandic one) plus the BÍN-matched Icelandic
# rendering, if any (blank where this project has no real inflection data to
# render from, e.g. oblique-case participles -- see icelandic_participle_form).
#
# Two parallel grammar tags per cell, same convention as the Greek project:
# the classical/international one (the abbreviations every classicist
# already reads, e.g. "1. sg. ind. imperf. act.") above the Icelandic one
# describing the Icelandic rendering (e.g. "1. p. et. frh. dþt. gm.").
# ---------------------------------------------------------------------------

_CLASSICAL_ABBR = {
    'case': {'nom': 'nom.', 'gen': 'gen.', 'dat': 'dat.', 'acc': 'acc.',
             'abl': 'abl.', 'voc': 'voc.', 'loc': 'loc.'},
    'gender': {'masc': 'masc.', 'fem': 'fem.', 'neut': 'neut.'},
    'person': {'1st': '1.', '2nd': '2.', '3rd': '3.'},
    'number': {'sg': 'sg.', 'pl': 'pl.'},
    'mood': {'ind': 'ind.', 'subj': 'coni.', 'inf': 'inf.',
             'part': 'part.', 'gerundive': 'ger.'},
    'tense': {'pres': 'praes.', 'imperf': 'impf.', 'fut': 'fut.',
              'perf': 'perf.', 'plup': 'plusqu.', 'futperf': 'futperf.'},
    'voice': {'act': 'act.', 'pass': 'pass.'},
}
_ICELANDIC_ABBR = {
    'case': {'nom': 'nf.', 'gen': 'ef.', 'dat': 'þgf.', 'acc': 'þf.',
             'abl': 'svf.', 'voc': 'áf.', 'loc': 'staðarf.'},
    'gender': {'masc': 'kk.', 'fem': 'kvk.', 'neut': 'hk.'},
    'person': {'1st': '1. p.', '2nd': '2. p.', '3rd': '3. p.'},
    'number': {'sg': 'et.', 'pl': 'ft.'},
    'mood': {'ind': 'frh.', 'subj': 'vth.', 'inf': 'nh.',
             'part': 'lh.', 'gerundive': 'ger.'},
    'tense': {'pres': 'nt.', 'imperf': 'dþt.', 'fut': 'frt.',
              'perf': 'nlt.', 'plup': 'þlt.', 'futperf': 'þframt.'},
    'voice': {'act': 'gm.', 'pass': 'þm.'},
}


def _grammar_tag(scheme, case=None, gender=None, person=None, number=None,
                 mood=None, tense=None, voice=None):
    """One space-joined grammar tag in the given abbreviation `scheme`
    (_CLASSICAL_ABBR or _ICELANDIC_ABBR). Canonical order: case, gender,
    person, number, mood, tense, voice (case/person are mutually exclusive
    -- nominal vs. verbal cells; gender only applies to participles/
    gerundives here, adjectives/nouns don't track gender per cell). Fields
    absent from a given cell are omitted."""
    def ab(cat, val):
        return scheme[cat].get(val, str(val))
    parts = []
    if case:
        parts.append(ab('case', case))
    if gender:
        parts.append(ab('gender', gender))
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


def _verb_tag(mood, person, number, tense, latin_voice, is_deponent):
    """(classical_tag, icelandic_tag) for one finite verb form (indicative
    or subjunctive). The classical tag always shows the REAL attested Latin
    voice (passive for a deponent's own morphology); the Icelandic tag shows
    the voice the rendering actually routed through (germynd for a
    deponent, since icelandic_verb_clause renders deponents via the active
    slots) -- same asymmetry the main table's deponent principal-parts
    label already acknowledges."""
    classical = _grammar_tag(_CLASSICAL_ABBR, person=person, number=number,
                             mood=mood, tense=tense, voice=latin_voice)
    icel_voice = 'act' if (is_deponent or latin_voice == 'act') else latin_voice
    icelandic = _grammar_tag(_ICELANDIC_ABBR, person=person, number=number,
                             mood=mood, tense=tense, voice=icel_voice)
    return classical, icelandic


def _infinitive_tag(tense, latin_voice, is_deponent):
    classical = _grammar_tag(_CLASSICAL_ABBR, mood='inf', tense=tense, voice=latin_voice)
    icel_voice = 'act' if (is_deponent or latin_voice == 'act') else latin_voice
    icelandic = _grammar_tag(_ICELANDIC_ABBR, mood='inf', tense=tense, voice=icel_voice)
    return classical, icelandic


def _participle_tag(case, gender, number, tense, latin_voice, is_deponent):
    classical = _grammar_tag(_CLASSICAL_ABBR, case=case, gender=gender, number=number,
                             mood='part', tense=tense, voice=latin_voice)
    icel_voice = 'act' if (is_deponent or latin_voice == 'act') else latin_voice
    icelandic = _grammar_tag(_ICELANDIC_ABBR, case=case, gender=gender, number=number,
                             mood='part', tense=tense, voice=icel_voice)
    return classical, icelandic


def _gerundive_tag(case, gender, number):
    return (_grammar_tag(_CLASSICAL_ABBR, case=case, gender=gender, number=number, mood='gerundive'),
            _grammar_tag(_ICELANDIC_ABBR, case=case, gender=gender, number=number, mood='gerundive'))


def _supine_tag(is_purpose):
    """Morpheus tags Latin's two supines as if they were nom./dat. of a 4th-
    declension noun (see classify_and_grid's supine_case comment) -- the tag
    line shows the traditional supine terminology instead, since that's
    what a classicist actually expects to read."""
    if is_purpose:
        return 'sup. (-um)', 'sagnb. (tilgangs)'
    return 'sup. (-u)', 'sagnb. (viðmiðunar)'


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
_GENDERS = ('masc', 'fem', 'neut')


def _combined_members(toks, vocab):
    """Union of every '/'-combined token's members that fall in `vocab`
    (e.g. 'nom/voc/acc' against CASES, or 'masc/fem/neut' against
    _GENDERS) -- Morpheus collapses syncretic forms this way rather than
    emitting one row per case/gender."""
    out = set()
    for t in toks:
        if '/' in t:
            out.update(p for p in t.split('/') if p in vocab)
    return out


def classify_and_grid(rows):
    """From (form, analyses) rows, build every grid this dictionary's main
    entries and per-inflected-form stub entries render from. Returns a dict
    (not a positional tuple -- too many fields now to keep two call sites
    in sync by position) with keys:
      noun_grid, verb_parts, verb_parts_any_person, verb_subj_parts,
      verb_subj_parts_any_person, infinitives, participle_forms, supines,
      supine_case, gerundives, gerundive_forms, n_nominal, n_verbal,
      verb_finite_forms.
    """
    form_analyses = {}
    for form, analyses in rows:
        groups = ANALYSIS_GROUP_RE.findall(analyses or '')
        if groups:
            form_analyses.setdefault(form, set()).update(groups)
    form_analyses = drop_enclitic_variants(form_analyses)

    noun_grid = defaultdict(lambda: defaultdict(set))
    verb_parts = defaultdict(set)
    verb_parts_any_person = defaultdict(set)
    # Subjunctive citation cells -- same 1st-sg-preferred/any-person-fallback
    # shape as the indicative verb_parts/verb_parts_any_person pair, feeding
    # the main table's own "Viðtengingarháttur / Subjunctive" block.
    verb_subj_parts = defaultdict(set)
    verb_subj_parts_any_person = defaultdict(set)
    infinitives = defaultdict(set)
    # raw_form -> set of (case, gender, number, tense, voice) -- every
    # attested Latin participle cell (pres.act "-ans/-ens", perf.pass
    # "-us/-a/-um", fut.act "-urus/-a/-um"). Stub-only: not surfaced in the
    # main entry's own morphology table (no participle table exists there).
    participle_forms = defaultdict(set)
    supines = set()
    # raw_form -> set of {'nom', 'dat'} -- Morpheus tags Latin's two supine
    # forms as if they were a 4th-declension noun's nom.sg ("-um", the
    # purpose/"accusative" supine after verbs of motion) and dat.sg ("-u",
    # the respect/"ablative" supine with adjectives of ease) rather than
    # with real supine case names. Kept separate from the flat `supines` set
    # (unchanged, still feeds the main table's single combined row) so stubs
    # can tell which of the two idioms a given form is.
    supine_case = defaultdict(set)
    gerundives = set()
    # raw_form -> set of (case, gender, number) -- every attested gerundive
    # cell, for stubs. `gerundives` above (unchanged) stays scoped to just
    # the masc/nom/sg citation form the main table cites.
    gerundive_forms = defaultdict(set)
    # raw_form -> set of (mood, tense, voice, person, number) finite-verb
    # parsings -- every attested person/number for both indicative and
    # subjunctive, not just the 1st-sg citation cells above. This is the
    # basis for the per-inflected-form stub entries (unlike verb_parts/
    # verb_subj_parts, which only ever need ONE representative form per
    # tense/voice for the main table).
    verb_finite_forms = defaultdict(set)
    n_nominal = n_verbal = 0

    for form, groups in form_analyses.items():
        for g in groups:
            toks = g.split()
            tokset = set(toks)
            case = next((c for c in CASES if c in tokset), None)
            combined_case = _combined_members(toks, CASES)
            number = 'sg' if 'sg' in tokset else ('pl' if 'pl' in tokset else None)
            tense = next((t for t in TENSES if t in tokset), None)
            voice = 'act' if 'act' in tokset else ('pass' if 'pass' in tokset else None)
            person = next((p for p in _PERSONS if p in tokset), None)
            mood = 'subj' if 'subj' in tokset else ('ind' if 'ind' in tokset else None)

            if 'part' in tokset:
                # Adjectival (case/gender/number) AND verbal (tense/voice) --
                # captured for stub tagging only; see participle_forms above.
                part_cases = combined_case or ({case} if case else set())
                part_genders = _combined_members(toks, _GENDERS) or \
                    ({next(gn for gn in _GENDERS if gn in tokset)} if any(gn in tokset for gn in _GENDERS) else set())
                if part_cases and part_genders and number and tense:
                    for c in part_cases:
                        for gd in part_genders:
                            participle_forms[form].add((c, gd, number, tense, voice or 'act'))
                continue
            if 'supine' in tokset:
                supines.add(form)
                if 'nom' in tokset:
                    supine_case[form].add('nom')
                elif 'dat' in tokset:
                    supine_case[form].add('dat')
                continue
            if 'gerundive' in tokset:
                is_masc = 'masc' in tokset or any('masc' in t.split('/') for t in toks if '/' in t)
                is_nom = 'nom' in tokset or any('nom' in t.split('/') for t in toks if '/' in t)
                if is_masc and is_nom and number == 'sg':
                    gerundives.add(form)
                ger_cases = combined_case or ({case} if case else set())
                ger_genders = _combined_members(toks, _GENDERS) or \
                    ({next(gn for gn in _GENDERS if gn in tokset)} if any(gn in tokset for gn in _GENDERS) else {None})
                if ger_cases and number:
                    for c in ger_cases:
                        for gd in ger_genders:
                            gerundive_forms[form].add((c, gd, number))
                continue

            if tense and 'inf' in tokset:
                infinitives[(tense, voice or 'act')].add(form)
                n_verbal += 1
            elif tense and mood == 'ind' and '1st' in tokset and number == 'sg':
                verb_parts[(tense, voice or 'act')].add(form)
                if person and number:
                    verb_finite_forms[form].add(('ind', tense, voice or 'act', person, number))
                n_verbal += 1
            elif tense and mood == 'ind':
                # No 1st-singular form attested for this tense/voice in the
                # corpus (e.g. equito's imperfect is only attested as
                # equitabamus, 1st pl) -- kept separately from verb_parts so
                # citation-style principal parts (which read verb_parts
                # directly) never pick up a non-1sg form, but the tense/voice
                # table can still show the tense instead of silently
                # dropping it.
                verb_parts_any_person[(tense, voice or 'act')].add(form)
                if person and number:
                    verb_finite_forms[form].add(('ind', tense, voice or 'act', person, number))
                n_verbal += 1
            elif tense and mood == 'subj' and '1st' in tokset and number == 'sg':
                verb_subj_parts[(tense, voice or 'act')].add(form)
                if person and number:
                    verb_finite_forms[form].add(('subj', tense, voice or 'act', person, number))
                n_verbal += 1
            elif tense and mood == 'subj':
                verb_subj_parts_any_person[(tense, voice or 'act')].add(form)
                if person and number:
                    verb_finite_forms[form].add(('subj', tense, voice or 'act', person, number))
                n_verbal += 1
            elif tense:
                n_verbal += 1

            if number and not tense:
                if combined_case:
                    for p in combined_case:
                        noun_grid[p][number].add(form)
                        n_nominal += 1
                elif case:
                    noun_grid[case][number].add(form)
                    n_nominal += 1

    return {
        'noun_grid': noun_grid,
        'verb_parts': verb_parts,
        'verb_parts_any_person': verb_parts_any_person,
        'verb_subj_parts': verb_subj_parts,
        'verb_subj_parts_any_person': verb_subj_parts_any_person,
        'infinitives': infinitives,
        'participle_forms': participle_forms,
        'supines': supines,
        'supine_case': supine_case,
        'gerundives': gerundives,
        'gerundive_forms': gerundive_forms,
        'n_nominal': n_nominal,
        'n_verbal': n_verbal,
        'verb_finite_forms': verb_finite_forms,
    }


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


def render_morphology(classified, is_deponent, is_word=None, entry_pos=None,
                      verb_forms=None, noun_decl=None, adj_decl=None,
                      adj_form_lemma=None):
    noun_grid = classified['noun_grid']
    verb_parts = classified['verb_parts']
    verb_parts_any_person = classified['verb_parts_any_person']
    verb_subj_parts = classified['verb_subj_parts']
    verb_subj_parts_any_person = classified['verb_subj_parts_any_person']
    infinitives = classified['infinitives']
    supines = classified['supines']
    gerundives = classified['gerundives']
    n_nominal = classified['n_nominal']
    n_verbal = classified['n_verbal']
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

        subj_rows = []
        for tense in TENSES:
            act = join_forms(verb_subj_parts.get((tense, 'act'), []) or verb_subj_parts_any_person.get((tense, 'act'), []))
            pas = join_forms(verb_subj_parts.get((tense, 'pass'), []) or verb_subj_parts_any_person.get((tense, 'pass'), []))
            if not act and not pas:
                continue
            label = f'{TENSE_LABELS_IS[tense]} – Viðtengingarháttur'
            act_html = (html.escape(act, quote=False) if act else '—') + \
                (_is_equiv_span(icelandic_verb_clause(tense, 'act', is_deponent, is_word, verb_slots, mood='subj')) if act else '')
            pas_html = (html.escape(pas, quote=False) if pas else '—') + \
                (_is_equiv_span(icelandic_verb_clause(tense, 'pass', is_deponent, is_word, verb_slots, mood='subj')) if pas else '')
            subj_rows.append(f'<tr><td class="case-label">{label}</td>'
                             f'<td>{act_html}</td><td>{pas_html}</td></tr>')
        if subj_rows:
            parts.append('<tr class="morph-secondary-header"><td colspan="3">Viðtengingarháttur / Subjunctive</td></tr>')
            parts.extend(subj_rows)

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

        if not any_tense_row and not subj_rows and not inf_rows and not gerundives and not supines and not pp_html:
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
            classified = classify_and_grid(morph_rows) if morph_rows else None
            morph_html = render_morphology(
                classified, is_deponent, is_word=is_word, entry_pos=entry_pos,
                verb_forms=verb_forms, noun_decl=noun_decl,
                adj_decl=adj_decl, adj_form_lemma=adj_form_lemma,
            ) if classified else ''
            if morph_html:
                xml.write(morph_html + '\n')

            xml.write('    </d:entry>\n\n')

            # Per-inflected-form stubs: every attested case/number (nominal),
            # case/gender/number/tense/voice (participle), case/gender/number
            # (gerundive), tense/voice (infinitive), the supine, and every
            # attested mood/person/number/tense/voice (finite indicative and
            # subjunctive verb) cell -- see register_stub above and the
            # module docstring near icelandic_verb_clause.
            if classified:
                verb_slots = (verb_forms or {}).get(is_word, {}) if is_word else {}
                noun_slots = (noun_decl or {}).get(is_word, {}) if is_word else {}
                adj_lemma = (adj_form_lemma or {}).get(is_word, is_word) if is_word else None
                adj_slots = (adj_decl or {}).get(adj_lemma, {}) if adj_lemma else {}
                is_adjective = entry_pos == 'Adjective'

                for c, by_number in classified['noun_grid'].items():
                    for number, forms in by_number.items():
                        rendering = (icelandic_adj_form(c, number, adj_slots) if is_adjective
                                    else icelandic_noun_form(c, number, noun_slots))
                        tags = _dual_tag(case=c, number=number)
                        for raw_form in forms:
                            register_stub(raw_form, lemma_display, entry_id, tags, rendering)

                for raw_form, parsings in classified['verb_finite_forms'].items():
                    for mood, tense, voice, person, number in parsings:
                        rendering = icelandic_verb_clause(
                            tense, voice, is_deponent, is_word, verb_slots,
                            person=person, number=number, mood=mood)
                        tags = _verb_tag(mood, person, number, tense, voice, is_deponent)
                        register_stub(raw_form, lemma_display, entry_id, tags, rendering)

                for (tense, voice), forms in classified['infinitives'].items():
                    rendering = icelandic_infinitive_clause(tense, voice, is_deponent, is_word, verb_slots)
                    tags = _infinitive_tag(tense, voice, is_deponent)
                    for raw_form in forms:
                        register_stub(raw_form, lemma_display, entry_id, tags, rendering)

                for raw_form, parsings in classified['participle_forms'].items():
                    for case_name, gender, number, tense, voice in parsings:
                        rendering = icelandic_participle_form(
                            case_name, gender, number, tense, voice, is_deponent, verb_slots)
                        tags = _participle_tag(case_name, gender, number, tense, voice, is_deponent)
                        register_stub(raw_form, lemma_display, entry_id, tags, rendering)

                for raw_form, parsings in classified['gerundive_forms'].items():
                    for case_name, gender, number in parsings:
                        tags = _gerundive_tag(case_name, gender, number)
                        register_stub(raw_form, lemma_display, entry_id, tags, None)

                for raw_form, case_set in classified['supine_case'].items():
                    for c in case_set:
                        is_purpose = c == 'nom'
                        rendering = icelandic_supine_form(is_purpose, is_word)
                        tags = _supine_tag(is_purpose)
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

        write_pronunciation_guide_entry(xml)

        xml.write('</d:dictionary>\n')

    print(f"Success! XML built at {OUTPUT_XML_PATH}")

    ls_conn.close()
    morph_conn.close()
    is_conn.close()


if __name__ == "__main__":
    build_dictionary()
