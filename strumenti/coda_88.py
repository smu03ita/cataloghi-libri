#!/usr/bin/env python3
"""Coda supplementare per le schede complete dell'OPAC: i libri dell'elenco SBN con ISBN "88" (fette 880-889 del Colab,
cartella Italia/2026-10-25_v2/88X) che non sono già nella coda principale (selezione del 26/10, solo 978-88 e 979-12).

Toglie i doppioni con il codice SBN (molti libri compaiono sia con l'ISBN a 10 sia con quello a 13 cifre), tiene solo
le monografie a stampa, scarta scolastica, concorsi e autopubblicati riconoscibili dall'editore, ordina dal più recente.
Scrive schede_complete/coda_extra_88.tsv.gz (codice, isbn, opera vuota): opac_sbn.py la mette in fondo alla coda.
Segreto: RCLONE_CONF (Drive).
"""
import collections
import gzip
import json
import re
import subprocess

ITALIA = 'drive:Fabula App/cataloghi/Italia'
ELENCO = f'{ITALIA}/2026-10-25_v2'
CARTELLA = f'{ITALIA}/schede_complete'
SCARTA_EDITORE = re.compile(
    r'zanichelli|loescher|pearson|sanoma|paravia|petrini|la scuola|edises|simone|maggioli|alpha test|giunti scuola|'
    r'dea scuola|mondadori education|rizzoli education|elmedi|ardea|raffaello|capitello|lattes|bulgarini|cetem|'
    r'youcanprint|ilmiolibro|il mio libro|lulu|streetlib|narcissus|createspace|independently published|kdp|'
    r'gruppo albatros|aletti|booksprint|edizioni del faro', re.I)


def rclone(*a, binario=False):
    r = subprocess.run(['rclone', *a], capture_output=True, timeout=1800)
    if r.returncode:
        raise RuntimeError('rclone ' + a[0] + ': ' + r.stderr.decode('utf-8', 'replace')[-300:])
    return r.stdout if binario else r.stdout.decode('utf-8', 'replace')


def main():
    code = sorted(x for x in rclone('lsf', CARTELLA, '--files-only').split('\n') if re.match(r'coda_selezione_.*\.tsv\.gz$', x))
    in_coda = set()
    for c in code:
        testo = gzip.decompress(rclone('cat', f'{CARTELLA}/{c}', binario=True)).decode('utf-8')
        in_coda |= {l.split('\t', 1)[0] for l in testo.split('\n')[1:] if l}
    print(f'codici già in coda: {len(in_coda):,}', flush=True)
    visti, nuovi, conta = set(), [], collections.Counter()
    for fetta in [f'88{d}' for d in range(10)]:
        try:
            file = sorted(x for x in rclone('lsf', f'{ELENCO}/{fetta}', '--files-only').split('\n') if x.endswith('.jsonl.gz'))
        except RuntimeError:
            print(f'{fetta}: cartella non trovata', flush=True)
            continue
        for f in file:
            for riga in gzip.decompress(rclone('cat', f'{ELENCO}/{fetta}/{f}', binario=True)).decode('utf-8').split('\n'):
                if not riga.strip():
                    continue
                r = json.loads(riga)
                conta['schede'] += 1
                codice = r.get('codiceIdentificativo') or ''
                if not codice or codice in visti:
                    conta['doppione_88'] += 1
                    continue
                visti.add(codice)
                if codice in in_coda:
                    conta['già_in_coda'] += 1
                    continue
                if r.get('tipo') != 'Testo a stampa' or r.get('livello') != 'Monografia':
                    conta['non_monografia_a_stampa'] += 1
                    continue
                pub = r.get('pubblicazione') or ''
                if SCARTA_EDITORE.search(pub):
                    conta['scuola_concorsi_autopubblicati'] += 1
                    continue
                anni = re.findall(r'(1[5-9]\d\d|20[0-2]\d)', pub)
                anno = int(anni[-1]) if anni else 0
                isbn = re.sub(r'[^0-9Xx]', '', r.get('isbn') or '')
                nuovi.append((anno, codice, isbn))
                conta['nuovi'] += 1
        print(f'{fetta}: {dict(conta)}', flush=True)
    nuovi.sort(key=lambda x: -x[0])
    with gzip.open('coda_extra_88.tsv.gz', 'wt', encoding='utf-8') as g:
        g.write('codice\tisbn\topera\n')
        for _, c, i in nuovi:
            g.write(f'{c}\t{i}\t\n')
    rclone('copyto', 'coda_extra_88.tsv.gz', f'{CARTELLA}/coda_extra_88.tsv.gz')
    per_decennio = collections.Counter(f'{a // 10 * 10}' if a else 'senza anno' for a, _, _ in nuovi)
    print(f'::notice title=coda 88::{conta["nuovi"]:,} libri nuovi in coda (su {conta["schede"]:,} schede 88) · '
          f'già in coda {conta["già_in_coda"]:,} · doppioni {conta["doppione_88"]:,} · scartati '
          f'{conta["non_monografia_a_stampa"] + conta["scuola_concorsi_autopubblicati"]:,} · per decennio '
          f'{dict(sorted(per_decennio.items(), reverse=True))}', flush=True)


if __name__ == '__main__':
    main()
