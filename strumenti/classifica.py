#!/usr/bin/env python3
"""Le prime N opere della classifica della selezione SBN, in una tabella da guardare a occhio (CSV per Google Fogli)."""
import csv, json, os, re, subprocess, sys

DRIVE = 'drive:Fabula App/cataloghi/Italia'
N = int(sys.argv[1]) if len(sys.argv) > 1 else 200


def rclone(*a):
    r = subprocess.run(['rclone', *a], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f'rclone {a[0]}: ' + r.stderr[-300:])
    return r.stdout


cartelle = sorted(x.strip('/') for x in rclone('lsf', DRIVE, '--dirs-only').split('\n') if x.startswith('selezione_20'))
sel = f'{DRIVE}/{cartelle[-1]}'
nomi = rclone('lsf', sel, '--files-only').split('\n')
opere = [json.loads(l) for l in rclone('cat', f'{sel}/opere_00.jsonl').split('\n') if l.strip()]
opere = sorted(opere, key=lambda o: o['posizione'])[:N]
trame = {}
for n in sorted(x for x in nomi if re.match(r'trame_wikipedia_\d+\.jsonl$', x)):
    for l in rclone('cat', f'{sel}/{n}').split('\n'):
        if l.strip():
            t = json.loads(l)
            trame[str(t.get('isbn_principale'))] = t.get('trama_tipo')
f = 'classifica_prime_%d.csv' % N
with open(f, 'w', newline='', encoding='utf-8-sig') as g:
    w = csv.writer(g)
    w.writerow(['posizione', 'titolo', 'autore', 'editore', 'anni', 'edizioni (ISBN)', 'schede SBN', 'lettori Open Library',
                'pagina Wikipedia', 'testo per la scheda', 'copertina', 'punteggio', 'da togliere? (scrivi x)'])
    for o in opere:
        anni = f"{o.get('anno_min') or ''}–{o.get('anno_max') or ''}" if o.get('anno_min') != o.get('anno_max') else str(o.get('anno_max') or '')
        testo = 'Open Library' if o.get('descrizione_ol') else ''
        t = trame.get(str(o.get('isbn_principale')))
        if t:
            testo = (testo + ' + ' if testo else '') + ('trama Wikipedia' if t == 'trama' else 'intro Wikipedia')
        wiki = 'it' if o.get('wikipedia_it') else ('en' if o.get('wikipedia_en') else '')
        w.writerow([o['posizione'], (o.get('titolo') or '') + (f" : {o['sottotitolo']}" if o.get('sottotitolo') else ''), o.get('autore') or '',
                    o.get('editore') or '', anni, o.get('n_isbn'), o.get('n_record'), o.get('ol_letture') or '', wiki, testo or 'nessuno',
                    'sì' if o.get('copertina_ol') else '', round(o.get('punteggio') or 0, 2), ''])
rclone('copyto', f, f'{sel}/{f}')
print(f'{f}: {len(opere)} opere, nel Drive in {cartelle[-1]}')
