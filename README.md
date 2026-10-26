# cataloghi-libri

Programmi per leggere cataloghi bibliografici aperti (biblioteche nazionali, Open Library, Wikidata) e confrontarli.
I dati letti e i risultati non stanno qui: vanno in un archivio privato. Qui ci sono solo i programmi e i lavori automatici.

- `strumenti/estrai.py` — da un catalogo nazionale (MARC21, UNIMARC, MARCXML, TSV, RDF) i dati essenziali dei libri con ISBN.
- `strumenti/confronto.py` — per ogni paese: quanti libri hanno una presentazione nel catalogo, quanti si trovano su Open Library
  (con descrizione e copertina) e su Wikidata/Wikipedia.
- `.github/workflows/confronto-cataloghi.yml` — il lavoro completo, con un'estrazione parallela per ogni file.
- `strumenti/opac_sbn.py` e `.github/workflows/opac-sbn.yml` — schede complete dall'OPAC SBN (Z39.50), una richiesta alla volta,
  con pausa tra le richieste; riprende da dove era arrivato.

Fonti: Deutsche Nationalbibliothek, Bibliothèque nationale de France, Biblioteka Narodowa, Biblioteca Nacional de Portugal,
Rahvusraamatukogu (Estonia, CC0), Koninklijke Bibliotheek, Library of Congress, Open Library, Wikidata (tramite QLever).
