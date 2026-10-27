#!/usr/bin/env python3
"""Elenco pulito dei libri raccolti da ISBNdb (vedi isbndb_raccolta.py), nel Drive accanto ai grezzi.

Unisce tutti i grezzi/parte_*.jsonl.gz, toglie i doppioni (stesso ISBN: si tiene la scheda con la sinossi più lunga e si
uniscono le fonti), pulisce i titoli ("(Italian Edition)" e simili) e SEGNA, senza togliere niente:
  romance        soggetti romance/romantasy/new adult, o trovato dalla ricerca dei romance
  autopubblicato editore assente, piattaforme di autopubblicazione, o editore uguale all'autrice
  scolastico     libri di scuola, eserciziari, test e concorsi (editori, soggetti, parole del titolo)
  ristampa       ristampe di massa e stampa su richiesta: editori noti per questo (RISTAMPE) o con più di
                 TETTO_EDITORE titoli nella raccolta (nessun editore italiano vero ne fa tanti: Mondadori ne ha meno di 5.000)
  lingua_it      la lingua della scheda è l'italiano
Scrive in elenco/:
  isbndb_italia.jsonl.gz     tutti i campi utili per caricarli in Fabula, sinossi compresa
  isbndb_italia_elenco.csv   lo stesso senza sinossi, da aprire in Fogli/Excel
  riepilogo.json             quanti libri per anno, fonte, editore; quanti romance, autopubblicati, con sinossi
"""
import csv
import gzip
import io
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from isbndb_raccolta import DEST, Archivio, autopubblicato, ora  # noqa: E402

TITOLO_SPORCO = re.compile(r'\s*[\(\[](italian edition|edizione italiana|versione italiana|italian version|in italiano|italian)[\)\]]\s*', re.I)
ROMANCE = re.compile(r'\b(romance|romantasy|new adult|love & romance)\b', re.I)
SCUOLA_SOGG = re.compile(r'study aids|test preparation|textbook|foreign language study|education|examinations', re.I)
SCUOLA_TIT = re.compile(r'\b(esercizi|eserciziario|eserciziari|per la scuola|scuola (primaria|secondaria|media|elementare)|'
                        r'classe [1-5]|con espansione online|quiz|concors[oi]|test di ammissione|manuale di preparazione|'
                        r'prove invalsi|verifiche|libro di testo)\b', re.I)
SCUOLA_ED = re.compile(r'zanichelli|loescher|pearson|sanoma|paravia|petrini|lattes|la scuola|dea scuola|giunti scuola|'
                       r'mondadori education|rizzoli education|edises|simone|maggioli|alpha test|hoepli scuola|'
                       r'cetem|ardea|raffaello|elmedi|edizioni il capitello|minerva scuola|bulgarini|'
                       r'garzanti scuola|einaudi scuola|cto editorial', re.I)
RISTAMPE = re.compile(r'creative media partners|hutson street|legare street|fb&c|forgotten books|1kg limited|antigonos|'
                      r'linguas classics|grey eagle|blurb|epubli|kessinger|nabu press|wentworth press|alpha editions', re.I)
TETTO_EDITORE = 5000
CAMPI_CSV = ['isbn13', 'titolo', 'autori', 'editore', 'anno', 'data', 'pagine', 'rilegatura', 'romance',
             'autopubblicato', 'scolastico', 'ristampa', 'lingua_it', 'con_sinossi', 'soggetti', 'copertina', 'fonti']


def pulisci_titolo(t):
    t = TITOLO_SPORCO.sub(' ', str(t or '')).strip(' -–:;,')
    return re.sub(r'\s{2,}', ' ', t)


def testo(x):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', str(x or ''))).strip()


def scheda(x):
    isbn = str(x.get('isbn13') or x.get('isbn') or '').replace('-', '').strip()
    soggetti = [str(s) for s in (x.get('subjects') or []) if s and 'Arborist' not in str(s) and str(s) != 'Self Service']
    titolo = pulisci_titolo(x.get('title'))
    completo = pulisci_titolo(x.get('title_long') or x.get('title'))
    data = str(x.get('date_published') or '')
    anno = re.match(r'(\d{4})', data)
    sinossi = testo(x.get('synopsis') or x.get('overview'))
    ed = str(x.get('publisher') or '').strip()
    return {
        'isbn13': isbn, 'isbn10': str(x.get('isbn10') or ''), 'titolo': titolo, 'titolo_completo': completo,
        'autori': [str(a) for a in (x.get('authors') or [])], 'editore': ed, 'data': data,
        'anno': int(anno.group(1)) if anno else None, 'pagine': x.get('pages'), 'rilegatura': x.get('binding'),
        'lingua': x.get('language'), 'copertina': x.get('image_original') or x.get('image'), 'sinossi': sinossi,
        'soggetti': soggetti, 'altri_isbn': [o.get('isbn') for o in (x.get('other_isbns') or []) if isinstance(o, dict)],
        'fonti': [x.get('_fase', '?')],
        'romance': bool(x.get('_fase') == 'romance' or any(ROMANCE.search(s) for s in soggetti)),
        'autopubblicato': autopubblicato(x),
        'scolastico': bool(SCUOLA_ED.search(ed) or SCUOLA_TIT.search(completo) or any(SCUOLA_SOGG.search(s) for s in soggetti)),
        'lingua_it': str(x.get('language', '')).lower() in ('ita', 'it', 'italian', 'italiano'),
        'con_sinossi': len(sinossi) >= 120,
    }


def unisci(a, b):
    """Due schede con lo stesso ISBN: si tiene quella più ricca, con le fonti e i soggetti di entrambe."""
    migliore, altra = (a, b) if len(a['sinossi']) >= len(b['sinossi']) else (b, a)
    for k, v in altra.items():
        if migliore.get(k) in (None, '', []) and v not in (None, '', []):
            migliore[k] = v
    migliore['fonti'] = sorted(set(a['fonti']) | set(b['fonti']))
    migliore['soggetti'] = list(dict.fromkeys(a['soggetti'] + b['soggetti']))
    migliore['romance'] = a['romance'] or b['romance']
    return migliore


def main(lavoro):
    arch = Archivio(DEST, lavoro)
    if arch.locale:
        cartella = os.path.join(arch.dest, 'grezzi')
        parti = sorted(os.listdir(cartella)) if os.path.isdir(cartella) else []
    else:
        try:
            parti = sorted(x for x in arch._rclone('lsf', f'{arch.dest}/grezzi', '--files-only').split('\n') if x.endswith('.jsonl.gz'))
        except RuntimeError:
            parti = []
    if not parti:
        print('nessun file grezzo: niente da fare')
        return
    libri, righe = {}, 0
    for p in parti:
        for riga in gzip.decompress(arch.leggi(f'grezzi/{p}')).decode('utf-8').split('\n'):
            if not riga.strip():
                continue
            righe += 1
            s = scheda(json.loads(riga))
            if not re.fullmatch(r'97[89]\d{10}', s['isbn13']):
                continue
            libri[s['isbn13']] = unisci(libri[s['isbn13']], s) if s['isbn13'] in libri else s
    elenco = sorted(libri.values(), key=lambda s: (-(s['anno'] or 0), s['titolo'].lower()))
    per_editore = Counter(s['editore'].lower() for s in elenco)
    for s in elenco:
        s['ristampa'] = bool(RISTAMPE.search(s['editore']) or per_editore[s['editore'].lower()] > TETTO_EDITORE)

    arch.scrivi('elenco/isbndb_italia.jsonl.gz', gzip.compress(
        ''.join(json.dumps(s, ensure_ascii=False) + '\n' for s in elenco).encode('utf-8')))
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=CAMPI_CSV, extrasaction='ignore')
    w.writeheader()
    for s in elenco:
        w.writerow({**s, 'autori': '; '.join(s['autori']), 'soggetti': '; '.join(s['soggetti'][:8]), 'fonti': ', '.join(s['fonti']),
                    **{k: 'sì' if s[k] else '' for k in ('romance', 'autopubblicato', 'scolastico', 'ristampa', 'lingua_it', 'con_sinossi')}})
    arch.scrivi('elenco/isbndb_italia_elenco.csv', ('﻿' + out.getvalue()).encode('utf-8'))   # BOM: Excel legge gli accenti

    rom = [s for s in elenco if s['romance']]
    # utili: in italiano, non ristampe di massa, non scolastici (gli autopubblicati restano: entrano se hanno lettori)
    ut = [s for s in elenco if s['lingua_it'] and not s['ristampa'] and not s['scolastico']]
    ut_rom = [s for s in ut if s['romance']]
    riepilogo = {
        'aggiornato': ora(), 'righe_grezze': righe, 'libri_diversi': len(elenco),
        'con_sinossi': sum(s['con_sinossi'] for s in elenco), 'con_copertina': sum(bool(s['copertina']) for s in elenco),
        'autopubblicati': sum(s['autopubblicato'] for s in elenco), 'scolastici': sum(s['scolastico'] for s in elenco),
        'non_in_italiano': sum(not s['lingua_it'] for s in elenco),
        'ristampe': sum(s['ristampa'] for s in elenco),
        'ristampe_editori': sorted({s['editore'] for s in elenco if s['ristampa']}, key=lambda e: -per_editore[e.lower()])[:30],
        'utili': {'totale': len(ut), 'con_sinossi': sum(s['con_sinossi'] for s in ut), 'con_copertina': sum(bool(s['copertina']) for s in ut),
                  'autopubblicati': sum(s['autopubblicato'] for s in ut),
                  'di_editori_con_sinossi': sum(s['con_sinossi'] and not s['autopubblicato'] for s in ut),
                  'romance': len(ut_rom), 'romance_con_sinossi': sum(s['con_sinossi'] for s in ut_rom),
                  'romance_di_editori': sum(not s['autopubblicato'] for s in ut_rom)},
        'romance': {'totale': len(rom), 'di_editori': sum(not s['autopubblicato'] for s in rom),
                    'autopubblicati': sum(s['autopubblicato'] for s in rom), 'con_sinossi': sum(s['con_sinossi'] for s in rom),
                    'in_italiano': sum(s['lingua_it'] for s in rom), 'scolastici': sum(s['scolastico'] for s in rom),
                    'per_anno': dict(sorted(Counter(str(s['anno'] or 'senza') for s in rom).items(), reverse=True)),
                    'editori_principali': Counter(s['editore'] for s in rom if not s['autopubblicato']).most_common(40)},
        'per_anno': dict(sorted(Counter(str(s['anno'] or 'senza') for s in elenco).items(), reverse=True)),
        'per_fonte': dict(Counter(f for s in elenco for f in s['fonti'])),
        'editori_principali': Counter(s['editore'] for s in elenco if not s['autopubblicato']).most_common(60),
    }
    arch.scrivi('elenco/riepilogo.json', json.dumps(riepilogo, ensure_ascii=False, indent=1).encode('utf-8'))
    print(f'elenco: {len(elenco)} libri ({riepilogo["con_sinossi"]} con sinossi), romance {len(rom)}, da {righe} righe in {len(parti)} file')


if __name__ == '__main__':
    main(os.path.join(os.environ.get('RUNNER_TEMP', '/tmp'), 'isbndb_elenco'))
