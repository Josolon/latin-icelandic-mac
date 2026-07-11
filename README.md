# Latin ↔ Icelandic Dictionary

> **⚠️ Work in progress — not ready for general use.** This is an early,
> honest-effort attempt at a hard problem (there is no existing Latin-
> Icelandic dictionary to build from), not a finished reference work.
> Please read the whole "How this works" section below before relying on
> anything it outputs. In short:
> - It's a **glossary of word-level equivalents**, not a dictionary of
>   fluent Icelandic definitions.
> - Coverage is partial by design: ~42% of entries get at least one
>   confident Icelandic word, ~7% get every sense translated, and the rest
>   fall back to English-only because nothing confident was found.
> - Even where it does return an Icelandic word, it can be the **wrong
>   sense** of a polysemous headword (the two independent resources this
>   bridges don't share any notion of "the same sense") — always cross-check
>   against the English Lewis & Short gloss shown alongside it.
> - The reverse (Icelandic → Latin) direction is coarser still: only
>   single-word Icelandic glosses become headwords, and pure spelling
>   variants (macron placement, i/j, u/v) are collapsed into one entry.
>
> Contributions on translation quality, phrase segmentation, and citation
> filtering are very welcome — see Contributing below.

Two `.dictionary` plugins for the native macOS Dictionary app and
system-wide "Look Up" feature, built from the **Lewis & Short Latin
dictionary** (51,636 entries):

* **LatinIcelandicDictionary** — Latin headword → Icelandic glossary
  (+ full English Lewis & Short gloss for reference), with declension/
  conjugation tables and principal parts (kennimyndir).
* **IcelandicLatinDictionary** — Icelandic headword → Latin word(s),
  a reverse index inverted from the forward glossary.

## How this works — read this before relying on a gloss

There is no existing Latin–Icelandic dictionary to build from, so this
project bridges two independent resources:

1. **Lewis & Short**, which glosses Latin headwords into short English
   definitions — reused from the companion
   [`latin-mac`](https://github.com/Josolon/latin-mac) project.
2. The **CLARIN IS-EN glossary**, a bilingual English↔Icelandic word list
   with per-pair confidence scores and method-evidence counts — reused from
   [`icelandic-english-dictionary-mac`](https://github.com/Josolon/icelandic-english-dictionary-mac).

This is deliberately built as **a glossary, not a dictionary**: the goal is
a list of trustworthy Icelandic word equivalents for each Latin headword,
not fluent Icelandic prose. `scripts/translate_definitions.py` is
precision-first — it returns an Icelandic candidate only when the glossary
gives real evidence for it. When it isn't confident, it returns nothing
rather than guessing.

Each Lewis & Short sense is split into short phrases, and each phrase is
translated independently. Two hard rules keep this from regressing into
pidgin:

1. **No word-by-word phrase reconstruction.** A phrase translates only if
   it's a single word, or an exact whole-phrase match already established
   in the glossary — never a concatenation of separately-looked-up words.
   This means a polysemous word's multi-word senses often don't get an
   Icelandic gloss at all — the glossary favors dropping a sense over
   fabricating one.
2. **Lewis & Short's own apparatus is filtered out before translation is
   attempted.** The short-definition field also carries cross-references to
   other headwords, citation sigla, and page/chapter numbers — none of
   that is an actual English gloss, and translating it produces nonsense
   results. These are detected and skipped outright before the Icelandic
   glossary is built. (The raw English gloss shown alongside it still gets
   only a light cosmetic cleanup of leftover citation-stripping punctuation
   — see `_clean_gloss_text` in `scripts/build_xml.py` — since the
   underlying extraction is deliberately naive/over-inclusive by design.)

A phrase that can't be confidently translated is simply **omitted from the
Icelandic side**. If a sense has no confident translation at all, the
original English is kept so no information is silently dropped; the
compiled dictionary always shows the full English Lewis & Short gloss
beneath the Icelandic glossary for reference and to catch dropped senses.

Coverage, from the current build:
- 6.8% of entries: every sense, every phrase, translated
- 42.4% of entries: at least a partial Icelandic glossary
- 57.6% of entries: no confident translation at all (English-only fallback)

**Use the Icelandic side as a fast way to recognize a Latin word's rough
meaning, not as a citable definition.** The English Lewis & Short gloss
alongside it is the authoritative source.

### The reverse direction (Icelandic → Latin) is coarser still

`scripts/build_reverse_xml.py` inverts the forward glossary: every
single-word Icelandic gloss becomes a headword pointing back to the Latin
word(s) that produced it (multi-word glosses aren't invertible onto one
headword, so only single words are indexed). `latin_normalize.dedup_spelling_variants()`
folds pure orthography-variant duplicates (macron placement, i/j, u/v —
e.g. amaui/amavi) into one representative headword. No morphology tables in
this direction — the headword is Icelandic, not Latin, so declension/
principal-part data doesn't apply (same scope decision as
`ancient-greek-icelandic-mac`'s reverse bundle).

## ✨ Features

* **51,636 Lewis & Short entries**, glossed into Icelandic where the
  bridge is confident.
* **8,031 Icelandic headwords** in the reverse index.
* **System Integration:** works natively with macOS "Look Up".
* **Morphology tables** (forward direction only): declension, conjugation,
  and principal parts (kennimyndir), reused directly from `latin-mac`'s
  Morpheus data, with Icelandic grammatical labels.
* **Transparency:** the full English Lewis & Short gloss is always shown
  alongside the Icelandic glossary, so nothing is silently hidden behind a
  translation guess.
* **Icelandic-gloss-word morphology matching:** where the dictionary's own
  Icelandic gloss word has BÍN inflection data, its declined/conjugated
  form is shown inline next to the matching Latin case/tense/voice cell
  (e.g. Latin dative singular *hesti* alongside Icelandic dative singular
  *"hesti(num)"* for a noun; a Latin passive verb form alongside an
  Icelandic *"ég er ... -aður/-uð/-að"* þolmynd periphrasis) — see BÍN below.

## 📦 Installation (For End Users)

1. Download the latest release from the [Releases](https://github.com/Josolon/latin-icelandic-mac/releases) page.
2. Unzip to get `LatinIcelandicDictionary.dictionary` and/or `IcelandicLatinDictionary.dictionary`.
3. Open Finder, press `Cmd+Shift+G`, navigate to `~/Library/Dictionaries/`.
4. Drag the `.dictionary` folder(s) there.
5. Open Dictionary.app → Settings → enable "Latína (L&S) - Íslenska" and/or "Íslenska - Latína (L&S, öfug leit)".

## 🛠️ Building from Source

### Prerequisites
* Python 3.x
* [Dictionary Development Kit](https://developer.apple.com/download/all/) (Apple's "Additional Tools for Xcode")
* The `data/ls.db` / `data/morph.db` SQLite databases from
  [`latin-mac`](https://github.com/Josolon/latin-mac) (gitignored here, copy them over)
* `data/IS-EN_glossary.tsv` from CLARIN Iceland (gitignored here) —
  https://repository.clarin.is/repository/xmlui/handle/20.500.12537/144
* `data/kaikki-icelandic.jsonl` from kaikki.org (gitignored here, optional
  supplement) — https://kaikki.org/dictionary/Icelandic/
* [`islenska`](https://pypi.org/project/islenska/) (BÍN wrapper), installed
  in a **project-local venv** — only `scripts/build_is_morphology.py` needs
  it; every other script here is plain stdlib and runs with system Python:
  ```bash
  python3 -m venv .venv && .venv/bin/pip install islenska
  ```

### Build steps

```bash
# 1. Extract raw English gloss senses from Lewis & Short
python3 scripts/extract_glosses.py

# 2. (Optional) Build the Wiktionary EN<->IS supplement, merged into the
#    bridge glossary automatically if data/wikt_is.db exists
python3 scripts/build_wiktionary_glossary.py

# 3. Build the Icelandic glossary from the extracted English definitions
python3 scripts/translate_definitions.py

# 4. Extract Icelandic-gloss-word morphology from BÍN (inside the venv)
source .venv/bin/activate && python3 scripts/build_is_morphology.py && deactivate

# 5. Generate both Apple Dictionary XML sources
python3 scripts/build_xml.py            # forward: Latin -> Icelandic
python3 scripts/build_reverse_xml.py    # reverse: Icelandic -> Latin

# 6. Compile and install both bundles
cd src && make install
```

Note: Apple's `build_dict.sh` fetches `PropertyList-1.0.dtd` from apple.com
on every invocation; a transient network hiccup can abort a build mid-way
with an "unable to parse dict.plist" error. Just re-run `make install`.

## 📁 Project Structure

```
latin-icelandic-mac/
├── data/
│   ├── ls.db                   # Lewis & Short entries [gitignored, from latin-mac]
│   ├── morph.db                # Morpheus morphology [gitignored, from latin-mac]
│   ├── IS-EN_glossary.tsv      # EN<->IS bridge glossary [gitignored, from CLARIN]
│   ├── ls_defs.db              # Generated: raw English gloss senses [gitignored]
│   ├── ls_is.db                # Generated: Icelandic glossary [gitignored]
│   ├── is_noun_declension.tsv  # Generated: BÍN noun declension for gloss words
│   ├── is_verb_forms.tsv       # Generated: BÍN verb paradigms for gloss words
│   ├── is_adj_declension.tsv   # Generated: BÍN adjective declension for gloss words
│   └── is_adj_form_lemma.tsv   # Generated: inflected-gloss-form -> adjective lemma
├── scripts/
│   ├── latin_normalize.py      # Orthography-variant folding (macrons, i/j, u/v)
│   ├── extract_glosses.py      # Extracts raw English gloss senses from ls.db
│   ├── translate_definitions.py # Builds the Icelandic glossary sense-by-sense
│   ├── build_wiktionary_glossary.py # Optional EN<->IS supplement from kaikki.org
│   ├── build_is_morphology.py  # Extracts BÍN morphology for gloss words [needs .venv]
│   ├── build_xml.py            # Forward direction: Latin -> Icelandic XML
│   └── build_reverse_xml.py    # Reverse direction: Icelandic -> Latin XML
├── src/
│   ├── LatinIcelandicDictionary.{xml,css,plist}      # forward bundle [xml gitignored]
│   ├── IcelandicLatinDictionary.{xml,plist}          # reverse bundle [xml gitignored]
│   ├── Makefile                # builds + installs both bundles
│   └── objects/                # Build artifacts [gitignored]
└── README.md
```

## 🤝 Contributing

* **Translation quality:** the biggest opportunity — extending phrase
  segmentation and translation confidence directly improves coverage and
  precision.
* **Weird/broken entries:** same caveat as `latin-mac` — 51k
  auto-generated entries will have edge cases.

**Not in scope:** Lewis & Short headwords/definitions and morphology data
themselves are maintained upstream (Perseus Digital Library / Perseids) —
report issues with the *source* English gloss there, not here.

## 📚 Data Sources

See [CREDITS.md](CREDITS.md) for full attribution.

* **Lewis & Short Lexicon & Morphology:** same sources as `latin-mac`.
* **Bridge glossary:** CLARIN Iceland, English-Icelandic/Icelandic-English glossary 21.09.

## 📄 License

Dual-license, same structure as `latin-mac`: code under MIT, data
under CC BY-SA 4.0 (Lewis & Short) / CC BY 4.0 (glossary) / CC BY-SA 4.0
(this project's generated Icelandic glossary and reverse index, since they
derive from CC BY-SA Lewis & Short text). See [LICENSE](LICENSE) for full
details.
