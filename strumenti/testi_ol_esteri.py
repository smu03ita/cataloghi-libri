#!/usr/bin/env python3
"""Descrizioni e copertine di Open Library per i libri dei cataloghi stranieri (file trovati_<paese>.tsv.gz del confronto).

Si leggono di nuovo i due archivi di Open Library (edizioni e opere), tenendo solo i libri trovati che hanno una descrizione
o una copertina. Per ogni libro: descrizione dell'opera (o, se manca, dell'edizione) e numero della copertina, con
l'indirizzo dell'immagine su Open Library (le immagini non si scaricano: sarebbero milioni, e Open Library chiede di non
scaricarle in massa). Uscita: testi_ol_<paese>.jsonl.gz in USCITA.
Variabili: TROVATI (cartella con trovati_*.tsv.gz), USCITA, LAVORO, OL_EDIZIONI, OL_OPERE (percorsi drive:... o locali).
"""
import csv
import glob
import gzip
import json
import multiprocessing as mp
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from confronto import flusso, isbn13, RX_I13, RX_I10, RX_STR   # noqa: E402

csv.field_size_limit(1 << 30)
TROVATI = os.environ.get('TROVATI', 'trovati')
USCITA = os.environ.get('USCITA', 'uscita')
LAVORO = os.environ.get('LAVORO', 'lavoro')


def testo_ol(x):
    if isinstance(x, dict):
        x = x.get('value')
    return x.strip() if isinstance(x, str) and x.strip() else None


def copertina(j):
    return next((c for c in (j.get('covers') or []) if isinstance(c, int) and c > 0), None)


def righe_trovati():
    for f in sorted(glob.glob(os.path.join(TROVATI, 'trovati_*.tsv.gz'))):
        paese = os.path.basename(f)[len('trovati_'):-len('.tsv.gz')]
        with gzip.open(f, 'rt', encoding='utf-8', newline='') as g:
            for x in csv.DictReader(g, delimiter='\t'):
                if x.get('ol_descrizione') == '1' or x.get('ol_copertina') == '1':
                    yield paese, x


def opera_di(x):
    try:
        w = int(float(x.get('ol_opera') or -1))
    except ValueError:
        return -1
    return w


def leggi_edizioni(percorso, isbn_voluti, dest):
    t0, n, tenute = time.time(), 0, 0
    with gzip.open(dest, 'wt', encoding='utf-8') as out:
        for n, riga in enumerate(flusso(percorso)):
            trovati = []
            for rx in (RX_I13, RX_I10):
                m = rx.search(riga)
                if m:
                    for s in RX_STR.findall(m.group(1)):
                        i = isbn13(s.decode('ascii', 'ignore'))
                        if i and int(i) in isbn_voluti:
                            trovati.append(i)
            if not trovati:
                continue
            try:
                j = json.loads(riga.split(b'\t', 4)[4])
            except Exception:
                continue
            d, c = testo_ol(j.get('description')), copertina(j)
            if d or c:
                for i in set(trovati):
                    out.write(json.dumps({'isbn': i, 'd': d, 'c': c}, ensure_ascii=False) + '\n')
                    tenute += 1
            if n % 10_000_000 == 0 and n:
                print(f'  edizioni: {n:,}, tenute {tenute:,}, {time.time()-t0:.0f} s', flush=True)
    print(f'edizioni lette {n:,}, tenute {tenute:,}, {time.time()-t0:.0f} s', flush=True)


def leggi_opere(percorso, opere_volute, dest):
    t0, n, tenute = time.time(), 0, 0
    with gzip.open(dest, 'wt', encoding='utf-8') as out:
        for n, riga in enumerate(flusso(percorso)):
            parti = riga.split(b'\t', 2)
            if len(parti) < 3 or not parti[1].startswith(b'/works/OL'):
                continue
            try:
                w = int(parti[1][9:-1])
            except ValueError:
                continue
            if w not in opere_volute:
                continue
            try:
                j = json.loads(riga.split(b'\t', 4)[4])
            except Exception:
                continue
            d, c = testo_ol(j.get('description')), copertina(j)
            if d or c:
                out.write(json.dumps({'w': w, 'd': d, 'c': c}, ensure_ascii=False) + '\n')
                tenute += 1
    print(f'opere lette {n:,}, tenute {tenute:,}, {time.time()-t0:.0f} s', flush=True)


def principale():
    os.makedirs(USCITA, exist_ok=True)
    os.makedirs(LAVORO, exist_ok=True)
    isbn_voluti, opere_volute = set(), set()
    for _, x in righe_trovati():
        for i in (x.get('isbn') or '').split('|'):
            if i.isdigit():
                isbn_voluti.add(int(i))
        w = opera_di(x)
        if w >= 0:
            opere_volute.add(w)
    print(f'da cercare: {len(isbn_voluti):,} ISBN, {len(opere_volute):,} opere', flush=True)
    f_ed, f_op = os.path.join(LAVORO, 'ed.jsonl.gz'), os.path.join(LAVORO, 'op.jsonl.gz')
    p1 = mp.Process(target=leggi_edizioni, args=(os.environ['OL_EDIZIONI'], isbn_voluti, f_ed))
    p2 = mp.Process(target=leggi_opere, args=(os.environ['OL_OPERE'], opere_volute, f_op))
    p1.start(); p2.start(); p1.join(); p2.join()
    if p1.exitcode or p2.exitcode:
        sys.exit('lettura di Open Library non riuscita')
    del isbn_voluti
    ed, op = {}, {}
    with gzip.open(f_ed, 'rt', encoding='utf-8') as g:
        for l in g:
            x = json.loads(l)
            ed.setdefault(x['isbn'], (x['d'], x['c']))
    with gzip.open(f_op, 'rt', encoding='utf-8') as g:
        for l in g:
            x = json.loads(l)
            op[x['w']] = (x['d'], x['c'])
    conta = {}
    file_aperti = {}
    for paese, x in righe_trovati():
        w = opera_di(x)
        d_op, c_op = op.get(w, (None, None))
        d_ed = c_ed = None
        for i in (x.get('isbn') or '').split('|'):
            if i in ed:
                d_ed = d_ed or ed[i][0]
                c_ed = c_ed or ed[i][1]
        descr, da = (d_op, 'opera') if d_op else ((d_ed, 'edizione') if d_ed else (None, None))
        cop = c_ed or c_op
        if not descr and not cop:
            continue
        if paese not in file_aperti:
            file_aperti[paese] = gzip.open(os.path.join(USCITA, f'testi_ol_{paese}.jsonl.gz'), 'wt', encoding='utf-8')
            conta[paese] = {'libri': 0, 'descrizione': 0, 'copertina': 0}
        file_aperti[paese].write(json.dumps({
            'id': x.get('id'), 'isbn': x.get('isbn'), 'titolo': x.get('titolo'), 'autore': x.get('autore'),
            'ol_opera': f'OL{w}W' if w >= 0 else None, 'descrizione': descr[:4000] if descr else None, 'descrizione_da': da,
            'copertina_id': cop, 'copertina_url': f'https://covers.openlibrary.org/b/id/{cop}-L.jpg' if cop else None,
        }, ensure_ascii=False) + '\n')
        conta[paese]['libri'] += 1
        conta[paese]['descrizione'] += bool(descr)
        conta[paese]['copertina'] += bool(cop)
    for f in file_aperti.values():
        f.close()
    with open(os.path.join(USCITA, 'testi_ol_riepilogo.json'), 'w') as f:
        json.dump(conta, f, ensure_ascii=False, indent=1)
    print(json.dumps(conta, ensure_ascii=False), flush=True)
    for p, c in conta.items():
        print(f"::notice title=Open Library {p}::{c['libri']} libri, {c['descrizione']} con descrizione, {c['copertina']} con copertina", flush=True)


if __name__ == '__main__':
    principale()
