# cataloghi-libri

Programmi per leggere cataloghi bibliografici aperti (biblioteche nazionali, Open Library, Wikidata) e confrontarli.
I dati letti e i risultati non stanno qui: vanno in un archivio privato. Qui ci sono solo i programmi e i lavori automatici.

- `strumenti/estrai.py` — da un catalogo nazionale (MARC21, UNIMARC, MARCXML, TSV, RDF) i dati essenziali dei libri con ISBN.
- `strumenti/confronto.py` — per ogni paese: quanti libri hanno una presentazione nel catalogo, quanti si trovano su Open Library
  (con descrizione e copertina) e su Wikidata/Wikipedia.
- `.github/workflows/confronto-cataloghi.yml` — il lavoro completo, con un'estrazione parallela per ogni file.
- `strumenti/opac_sbn.py` e `.github/workflows/opac-sbn.yml` — schede complete dall'OPAC SBN (Z39.50), una richiesta alla volta,
  con pausa tra le richieste; riprende da dove era arrivato.
- `strumenti/isbndb_raccolta.py`, `strumenti/isbndb_elenco.py` e `.github/workflows/isbndb-raccolta.yml` — libri in italiano
  da ISBNdb (tutti i romance, poi le novità dal 2024 e a ritroso), cento per chiamata; elenco pulito con romance,
  autopubblicati e scolastica segnati. Segreti: ISBNDB_API_KEY e RCLONE_CONF.

Fonti: Deutsche Nationalbibliothek, Bibliothèque nationale de France, Biblioteka Narodowa, Biblioteca Nacional de Portugal,
Rahvusraamatukogu (Estonia, CC0), Koninklijke Bibliotheek, Library of Congress, Open Library, Wikidata (tramite QLever).

## Regole degli ISBN (29/10)
Tutti gli strumenti usano `strumenti/isbn.py`, con le regole di [isbnlib](https://github.com/xlcnd/isbnlib) (si usa il
pacchetto se è installato, altrimenti le stesse regole scritte nel modulo): pulizia (trattini, spazi, «ISBN»), cifra di
controllo per ISBN-13 (978/979) e ISBN-10, conversione da 10 a 13 cifre. `isbn13_tollerante()` tiene anche gli ISBN
stampati con la cifra di controllo sbagliata, solo per riconoscere schede già note (OPAC, confronto dei cataloghi).
Le stesse regole valgono nell'import CSV dell'app (azione `normalizzaCsvLibreria`).
