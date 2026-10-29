#!/usr/bin/env python3
"""Conta le schede complete OPAC SBN scaricate (solo numeri, nessuna scrittura tranne il riepilogo nel Drive).

Legge schede_complete/esiti/*.tsv.gz (codice, esito, presentazione 1/0, opera) e la coda supplementare "88"
(coda_extra_88.tsv.gz). Un codice conta una volta: vale l'esito migliore (fatta > non_trovata > errore).
Scrive schede_complete/riepilogo_opac.json e lo stampa.
"""
import collections
import datetime
import gzip
import json
import os
import subprocess
import tempfile

CARTELLA = 'drive:Fabula App/cataloghi/Italia/schede_complete'
RANGO = {'fatta': 3, 'non_trovata': 2}


def rclone(*args):
    subprocess.run(['rclone', *args], check=True, capture_output=True, timeout=3600)


def main():
    lavoro = tempfile.mkdtemp()
    esiti = os.path.join(lavoro, 'esiti')
    rclone('copy', f'{CARTELLA}/esiti', esiti, '--include', '*.tsv.gz', '--transfers', '16')
    rclone('copy', CARTELLA, lavoro, '--include', 'coda_extra_88.tsv.gz', '--max-depth', '1')

    esito, sinossi, opera = {}, set(), {}
    file_letti = 0
    for nome in os.listdir(esiti):
        with gzip.open(os.path.join(esiti, nome), 'rt', encoding='utf-8') as g:
            righe = [l.rstrip('\n').split('\t') for l in g]
        righe = [p for p in righe if len(p) >= 4 and p[0] != 'codice']
        if len(righe) >= 100 and all(p[1] == 'non_trovata' for p in righe):
            continue            # file del 26/10 con il server che non cercava: non valgono (come in opac_sbn.py)
        file_letti += 1
        for codice, e, s, o in (p[:4] for p in righe):
            if RANGO.get(e, 1) > RANGO.get(esito.get(codice), 0):
                esito[codice] = e
            if e == 'fatta' and s == '1':
                sinossi.add(codice)
            if o:
                opera[codice] = o

    codici_88 = set()
    f88 = os.path.join(lavoro, 'coda_extra_88.tsv.gz')
    if os.path.exists(f88):
        with gzip.open(f88, 'rt', encoding='utf-8') as g:
            codici_88 = {l.split('\t', 1)[0] for l in g if not l.startswith('codice')}

    conta = collections.Counter(esito.values())
    fatte = {c for c, e in esito.items() if e == 'fatta'}
    r = {
        'quando': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
        'file_esiti_letti': file_letti,
        'codici_provati': len(esito),
        'schede_complete': conta['fatta'],
        'non_trovate': conta['non_trovata'],
        'errori_rimasti': sum(v for k, v in conta.items() if k not in RANGO),
        'con_presentazione': len(sinossi),
        'opere_scelte_con_presentazione': len({opera[c] for c in sinossi if c in opera}),
        'coda_88': {
            'in_coda': len(codici_88),
            'schede_complete': len(fatte & codici_88),
            'con_presentazione': len(sinossi & codici_88),
        },
        'coda_principale': {
            'schede_complete': len(fatte - codici_88),
            'con_presentazione': len(sinossi - codici_88),
        },
    }
    testo = json.dumps(r, ensure_ascii=False, indent=1)
    print(testo, flush=True)
    uscita = os.path.join(lavoro, 'riepilogo_opac.json')
    with open(uscita, 'w', encoding='utf-8') as f:
        f.write(testo + '\n')
    rclone('copyto', uscita, f'{CARTELLA}/riepilogo_opac.json')
    print(f"::notice title=OPAC::schede {r['schede_complete']:,} · con presentazione {r['con_presentazione']:,} · "
          f"non trovate {r['non_trovate']:,} · errori {r['errori_rimasti']:,}")


if __name__ == '__main__':
    main()
