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
