# Credits

This project builds a Latin <-> Icelandic dictionary (both directions) by
bridging two independent resources: an English lexicon of Latin, and a
bilingual English-Icelandic glossary. No direct Latin-Icelandic dictionary
exists, so every gloss here is a **precision-first word/phrase-substitution
bridge translation** -- a glossary of confident equivalents, not idiomatic
Icelandic prose written by a lexicographer or translated by a language
model. The reverse (Icelandic -> Latin) direction is inverted from the same
generated glossary. See "How this works" in README.md.

## Latin Lexicon (Lewis & Short)

Charlton T. Lewis and Charles Short, *A Latin Dictionary* (1879), via the
Perseus Digital Library TEI-XML edition.
License: CC BY-SA 4.0.
Same source data as the companion [`latin-mac`](https://github.com/Josolon/latin-mac)
project -- see that repo for full sourcing detail.

## Morphology

Latin inflectional morphology (declensions, conjugations, principal parts)
from Morpheus, via the Perseids project. Same source as `latin-mac`.

## English-Icelandic Bridge Glossary

English-Icelandic/Icelandic-English glossary 21.09.
Compiled by Steinþór Steingrímsson, Luke James Obrien, Finnur Ágúst
Ingimundarson, Árni Davíð Magnússon, Þórdís Dröfn Andrésdóttir, and Inga
Guðrún Eiríksdóttir -- The Árni Magnússon Institute for Icelandic Studies /
Reykjavik University, via CLARIN Iceland.
License: CC BY 4.0.
https://repository.clarin.is/repository/xmlui/handle/20.500.12537/144

This is the same glossary used (in the opposite lookup direction) by the
companion [`icelandic-english-dictionary-mac`](https://github.com/Josolon/icelandic-english-dictionary-mac)
project, and by [`ancient-greek-icelandic-mac`](https://github.com/Josolon/ancient-greek-icelandic-mac).

## Wiktionary Icelandic Entries (bridge glossary supplement)

English Wiktionary's Icelandic-language entries, via the [kaikki.org](https://kaikki.org/dictionary/Icelandic/)
machine-readable extract (Tatu Ylonen). Used to supplement the CLARIN
bridge glossary above with additional EN<->IS word pairs and Icelandic
part-of-speech data (`scripts/build_wiktionary_glossary.py`).
License: CC BY-SA 3.0 / GFDL (Wiktionary's own dual license).

## Icelandic Target-Word Morphology (BÍN)

**BÍN** (Beygingarlýsing íslensks nútímamáls / Database of Modern Icelandic
Inflection), compiled by Kristín Bjarnadóttir at the Árni Magnússon
Institute for Icelandic Studies, via CLARIN Iceland.
License: CC BY-SA 4.0.
https://repository.clarin.is/repository/xmlui/handle/20.500.12537/5

Used to inflect the dictionary's own Icelandic gloss words (declension for
noun/adjective glosses, full verb paradigms including the þolmynd passive
periphrasis) so they can be shown matching each attested Latin form's own
case/number/tense/voice -- `scripts/build_is_morphology.py`, rendered in
`scripts/build_xml.py`. Accessed via the [`islenska`](https://github.com/mideind/BinPackage)
PyPI package (Miðeind ehf., MIT-licensed code wrapping the same BÍN data),
not the raw CSV export. Same technique as the companion
[`ancient-greek-icelandic-mac`](https://github.com/Josolon/ancient-greek-icelandic-mac)
project -- see that project's `bin-morphology-recap.md` (in the parent
`Josolon/` directory) for the full design rationale.
