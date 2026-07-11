"""Extract a compact Icelandic noun-declension / verb-form lookup from BÍN
(Beygingarlýsing íslensks nútímamáls / Database of Modern Icelandic
Inflection, CLARIN handle 20.500.12537/5, CC BY-SA 4.0, compiled by Kristín
Bjarnadóttir at the Árni Magnússon Institute for Icelandic Studies -- see
CREDITS.md), writing data/is_noun_declension.tsv, data/is_verb_forms.tsv,
data/is_adj_declension.tsv and data/is_adj_form_lemma.tsv.

Ported from ancient-greek-icelandic-mac/scripts/build_is_morphology.py --
see that file and bin-morphology-recap.md for the full design rationale
(islenska over the raw CSV, MM-SAGNB assimilation, impersonal-verb
detection, the adjective form->lemma normalization and its blocklist
hazard, vocative == nominative in Icelandic, all-three-genders passive
participle). This file only differs where LATIN's own grammar differs from
Greek's:

  - Latin has exactly two voices, active and passive -- no middle. So
    there is no Icelandic miðmynd (MM-) mapping to make at all: Latin
    active -> Icelandic germynd, Latin passive -> Icelandic þolmynd
    (periphrastic, rendered in build_xml.py, not here). Every MM-* row
    this script would have needed for Greek's middle voice simply doesn't
    apply -- only GM-* verb slots and the participle table (needed for the
    þolmynd periphrasis) are extracted.
  - Latin deponent verbs are morphologically passive but active in
    MEANING -- the mapping to Icelandic germynd is a semantic one made in
    build_xml.py (using the entry's own is_deponent flag from L&S's <pos>
    tag), not something this extraction script needs to know about; it
    just extracts the full germynd paradigm for every verb headword,
    deponent or not.
  - No dual number, so no Greek-style "reuse the plural, mark it" case.
  - Latin's own case set is nom/gen/dat/acc/abl/voc (+ a marginal
    locative, not target-mapped here since BÍN nouns don't carry a
    locative slot to map onto). Icelandic vocative == nominative (see
    recap) -- CASES below deliberately omits a separate voc entry, and
    build_xml.py's rendering maps a Latin vocative cell to the Icelandic
    nominative form directly, tagged as vocative so the source parse
    stays honest.

Only extracts forms for words that actually occur as a first, single-word
Icelandic gloss somewhere in data/ls_is.db's definitions_is column -- BÍN
itself covers ~300,000 lemmas, but the dictionary only ever needs to
inflect the handful of thousand words that are themselves glossary output.

Data source: the `islenska` PyPI package (Miðeind ehf., MIT-licensed
wrapper around the same BÍN dataset -- see CREDITS.md), NOT the raw
Sigrúnarsnið CSV dump. Run this script inside the project venv
(`python3 -m venv .venv && .venv/bin/pip install islenska`, then
`source .venv/bin/activate`) -- every other script in this pipeline is
stdlib-only and does NOT need the venv.
"""
import json
import sqlite3

try:
    from islenska import Bin
except ImportError as exc:
    raise SystemExit(
        "islenska is not installed in the current interpreter. Run this "
        "script inside the project venv:\n"
        "  python3 -m venv .venv && .venv/bin/pip install islenska\n"
        "  source .venv/bin/activate && python3 scripts/build_is_morphology.py"
    ) from exc

IS_DB_PATH = "data/ls_is.db"
NOUN_OUT_PATH = "data/is_noun_declension.tsv"
VERB_OUT_PATH = "data/is_verb_forms.tsv"
ADJ_OUT_PATH = "data/is_adj_declension.tsv"
ADJ_LEMMA_OUT_PATH = "data/is_adj_form_lemma.tsv"

NOUN_CLASSES = {"kk", "kvk", "hk"}
VERB_CLASS = "so"
ADJ_CLASS = "lo"

# See ancient-greek-icelandic-mac's ADJ_LEMMA_BLOCKLIST_IDS / recap: BÍN can
# list a spurious/archaic "lo" lemma that wins a form->lemma tie-break
# purely by having a lower id, with nothing in BÍN's own metadata (checked
# via lookup_ksnid) to flag it. Empty until a Latin-project spot-check turns
# one up -- extend by hand if a generated declension table looks wrong for
# a specific gloss word, same as the Greek project's "menntur" discovery.
ADJ_LEMMA_BLOCKLIST_IDS = set()

CASES = {"NF": "nominative", "ÞF": "accusative", "ÞGF": "dative", "EF": "genitive"}
NUMBERS = {"ET": "singular", "FT": "plural"}
GENDERS = {"KK": "masculine", "KVK": "feminine", "HK": "neuter"}

_ADJ_TAG_PREFIX = "FSB-"

# Only GM- (germynd) verb slots -- Latin has no middle voice, so there is
# no MM- mapping to build (see module docstring).
_V_MOOD = {"FH": "ind", "VH": "subj"}
_V_TENSE = {"NT": "pres", "ÞT": "past"}
_V_PERSON = {"1P": "1", "2P": "2", "3P": "3"}
_V_NUMBER = {"ET": "sg", "FT": "pl"}

_V_SUBJ_CASE = {"ÞF": "þf", "ÞGF": "þgf", "EF": "ef", "það": "það"}


def parse_verb_tag(tag):
    """BÍN verb tag -> (slot_key, subj_case), or None for tags we don't
    render. Only germynd (GM-) tags are accepted -- see module docstring."""
    parts = tag.split("-")
    subj_case = None
    force_3sg = False
    if parts and parts[0] == "OP":
        if len(parts) < 3:
            return None
        subj_case = _V_SUBJ_CASE.get(parts[1])
        if subj_case is None:
            return None
        parts = parts[2:]
        force_3sg = True

    if not parts or parts[0] != "GM":
        return None
    parts = parts[1:]

    if len(parts) == 1 and parts[0] == "SAGNB":
        return ("gm_supine", subj_case)
    if len(parts) == 4:
        mood, tense, person, number = parts
        if (mood in _V_MOOD and tense in _V_TENSE
                and person in _V_PERSON and number in _V_NUMBER):
            pn = "3sg" if force_3sg else f"{_V_PERSON[person]}{_V_NUMBER[number]}"
            return (f"gm_{_V_MOOD[mood]}_{_V_TENSE[tense]}_{pn}", subj_case)
    return None


# Past-participle forms, used by build_xml.py's þolmynd (vera + participle)
# periphrasis for Latin passive forms. Voice-agnostic in BÍN (no GM-/MM-
# prefix). All three genders' nominative sg/pl are extracted so the
# periphrasis can slash-join them (see recap: gender-neutral pronoun
# renderings must not default to masculine).
_PARTICIPLE_TAGS = {
    "LHÞT-SB-KK-NFET": "ptcp_kk_sg",
    "LHÞT-SB-KK-NFFT": "ptcp_kk_pl",
    "LHÞT-SB-KVK-NFET": "ptcp_kvk_sg",
    "LHÞT-SB-KVK-NFFT": "ptcp_kvk_pl",
    "LHÞT-SB-HK-NFET": "ptcp_hk_sg",
    "LHÞT-SB-HK-NFFT": "ptcp_hk_pl",
}


def parse_adj_slot(tag):
    """BÍN positive-strong adjective tag (FSB-{gender}-{case}{number}) ->
    (case_name, gender_name, number_name), or None."""
    if not tag.startswith(_ADJ_TAG_PREFIX):
        return None
    rest = tag[len(_ADJ_TAG_PREFIX):]
    parts = rest.split("-")
    if len(parts) != 2:
        return None
    gender_code, cn = parts
    gender = GENDERS.get(gender_code)
    if not gender:
        return None
    case = number = None
    for cc, name in CASES.items():
        if cn.startswith(cc):
            case = name
            number = NUMBERS.get(cn[len(cc):])
            break
    if not case or not number:
        return None
    return (case, gender, number)


def _target_words():
    """Every single-word token that appears in data/ls_is.db's
    definitions_is column -- a flat JSON array of Icelandic words per entry
    (see translate_definitions.py). Multiword glossary entries (e.g. "kunna
    við") are skipped -- BÍN inflects single words, not phrases."""
    conn = sqlite3.connect(IS_DB_PATH)
    words = set()
    for (defs_is,) in conn.execute(
            "SELECT definitions_is FROM definitions_is WHERE definitions_is IS NOT NULL"):
        try:
            entries = json.loads(defs_is)
        except (json.JSONDecodeError, TypeError):
            continue
        for word in entries:
            word = word.strip()
            if word and " " not in word:
                words.add(word)
    conn.close()
    return words


def _choose_voice_slots(pers, op, opcases):
    """Decide whether this verb's germynd is personal or impersonal, same
    logic as the Greek project's _choose_voice_slots but for a single
    voice (gm only -- no mm to iterate)."""
    out_slots, subj = {}, {}

    def is_finite(s):
        return "_ind_" in s or "_subj_" in s

    for src in (pers, op):
        for s, f in src.items():
            if not is_finite(s):
                out_slots.setdefault(s, f)

    finite_personal = {s: f for s, f in pers.items() if is_finite(s)}
    if finite_personal:
        out_slots.update(finite_personal)
        return out_slots, subj

    cases = opcases.get("gm", set())
    oblique = cases & {"þf", "þgf", "ef"}
    chosen = None
    if len(oblique) == 1:
        chosen = next(iter(oblique))
    elif not oblique and "það" in cases:
        chosen = "það"
    if chosen:
        subj["gm"] = chosen
        out_slots.update({s: f for s, f in op.items() if is_finite(s)})
    return out_slots, subj


def main():
    targets = _target_words()
    print(f"Looking up {len(targets)} distinct single-word glosses in BÍN via islenska...")
    bindb = Bin()

    noun_data = {}
    verb_personal = {}
    verb_op = {}
    verb_opcases = {}
    adj_data = {}
    adj_form_lemma = {}

    for headword in sorted(targets):
        _, cands = bindb.lookup_lemmas(headword)
        noun_ids = sorted({e.bin_id for e in cands if e.ofl in NOUN_CLASSES})
        if noun_ids:
            entry_id = noun_ids[0]
            for e in bindb.lookup_id(entry_id):
                if e.ord != headword:
                    continue
                tag = e.mark
                definite = tag.endswith("gr")
                base_tag = tag[:-2] if definite else tag
                for case_code, case_name in CASES.items():
                    if not base_tag.startswith(case_code):
                        continue
                    number_name = NUMBERS.get(base_tag[len(case_code):])
                    if not number_name:
                        continue
                    cell = noun_data.setdefault(headword, {}).setdefault(entry_id, {}) \
                        .setdefault((case_name, number_name), {})
                    cell["def" if definite else "indef"] = e.bmynd
                    break

        verb_ids = sorted({e.bin_id for e in cands if e.ofl == VERB_CLASS})
        if verb_ids:
            entry_id = verb_ids[0]
            pers_sink = verb_personal.setdefault(headword, {}).setdefault(entry_id, {})
            op_sink = verb_op.setdefault(headword, {}).setdefault(entry_id, {})
            opcases_sink = verb_opcases.setdefault(headword, {}).setdefault(entry_id, {})
            for e in bindb.lookup_id(entry_id):
                if e.ord != headword:
                    continue
                tag = e.mark
                if tag in _PARTICIPLE_TAGS:
                    pers_sink[_PARTICIPLE_TAGS[tag]] = e.bmynd
                    continue
                res = parse_verb_tag(tag)
                if not res:
                    continue
                slot_key, subj_case = res
                if subj_case is None:
                    pers_sink[slot_key] = e.bmynd
                else:
                    op_sink[slot_key] = e.bmynd
                    opcases_sink.setdefault("gm", set()).add(subj_case)

    adj_lemma_candidates = set()
    form_reverse = {}

    for word in sorted(targets):
        _, fwd = bindb.lookup_lemmas(word)
        for e in fwd:
            if e.ofl == ADJ_CLASS and e.bin_id not in ADJ_LEMMA_BLOCKLIST_IDS:
                adj_lemma_candidates.add((e.ord, e.bin_id))

        _, rev = bindb.lookup(word)
        cands = sorted(
            {(e.ord, e.bin_id) for e in rev
             if e.ofl == ADJ_CLASS and e.ord != word
             and e.bin_id not in ADJ_LEMMA_BLOCKLIST_IDS},
            key=lambda p: p[1],
        )
        if cands:
            form_reverse[word] = cands[0]
            adj_lemma_candidates.add(cands[0])

    for form, (lemma, entry_id) in form_reverse.items():
        adj_form_lemma[form] = (lemma, entry_id)

    adj_ids_by_lemma = {}
    for lemma, entry_id in adj_lemma_candidates:
        adj_ids_by_lemma.setdefault(lemma, []).append(entry_id)

    for headword, ids in adj_ids_by_lemma.items():
        entry_id = min(ids)
        cells = adj_data.setdefault(headword, {}).setdefault(entry_id, {})
        for e in bindb.lookup_id(entry_id):
            if e.ord != headword:
                continue
            res = parse_adj_slot(e.mark)
            if res:
                cells[res] = e.bmynd

    with open(NOUN_OUT_PATH, "w", encoding="utf-8") as out:
        for headword in sorted(noun_data):
            entry_id = min(noun_data[headword], key=int)
            cells = noun_data[headword][entry_id]
            for (case_name, number_name), forms in sorted(cells.items()):
                indef, definite = forms.get("indef", ""), forms.get("def", "")
                if not indef and not definite:
                    continue
                out.write(f"{headword}\t{case_name}\t{number_name}\t{indef}\t{definite}\n")

    verb_rows = 0
    impersonal_count = 0
    with open(VERB_OUT_PATH, "w", encoding="utf-8") as out:
        all_verbs = set(verb_personal) | set(verb_op)
        for headword in sorted(all_verbs):
            ids = set(verb_personal.get(headword, {})) | set(verb_op.get(headword, {}))
            entry_id = min(ids, key=int)
            pers = verb_personal.get(headword, {}).get(entry_id, {})
            op = verb_op.get(headword, {}).get(entry_id, {})
            opcases = verb_opcases.get(headword, {}).get(entry_id, {})
            slots, subj = _choose_voice_slots(pers, op, opcases)
            for slot in sorted(slots):
                out.write(f"{headword}\t{slot}\t{slots[slot]}\n")
                verb_rows += 1
            for voice in sorted(subj):
                out.write(f"{headword}\t__subj_{voice}\t{subj[voice]}\n")
                verb_rows += 1
                impersonal_count += 1

    adj_rows = 0
    with open(ADJ_OUT_PATH, "w", encoding="utf-8") as out:
        for headword in sorted(adj_data):
            entry_id = min(adj_data[headword], key=int)
            cells = adj_data[headword][entry_id]
            for (case_name, gender_name, number_name) in sorted(cells):
                out.write(f"{headword}\t{case_name}\t{gender_name}\t{number_name}\t"
                          f"{cells[(case_name, gender_name, number_name)]}\n")
                adj_rows += 1

    with open(ADJ_LEMMA_OUT_PATH, "w", encoding="utf-8") as out:
        for form in sorted(adj_form_lemma):
            out.write(f"{form}\t{adj_form_lemma[form][0]}\n")

    print(f"Wrote {len(noun_data)} nouns to {NOUN_OUT_PATH}")
    print(f"Wrote {verb_rows} verb-form rows for {len(all_verbs)} verbs "
          f"({impersonal_count} impersonal voice(s)) to {VERB_OUT_PATH}")
    print(f"Wrote {adj_rows} adjective-form rows for {len(adj_data)} adjectives to {ADJ_OUT_PATH}")
    print(f"Wrote {len(adj_form_lemma)} adjective form->lemma entries to {ADJ_LEMMA_OUT_PATH}")


if __name__ == "__main__":
    main()
