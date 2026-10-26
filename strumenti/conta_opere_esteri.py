#!/usr/bin/env python3
"""Quante schede servirebbero davvero per i libri esteri con una descrizione di Open Library: una per opera, non per edizione.

Legge _confronto/testi/testi_ol_<paese>.jsonl.gz dal Drive e conta, per paese e in tutto: libri (edizioni) con descrizione,
opere distinte (ol_opera), libri senza opera, testi distinti. Scrive _confronto/testi/opere_riepilogo.json e le note del lavoro.
Segreto: RCLONE_CONF.
"""
import glob
import gzip
import hashlib
import json
import os
import subprocess

DRIVE = 'drive:Fabula App/cataloghi/_confronto/testi'
LAVORO = os.environ.get('LAVORO', '/tmp/opere')


def main():
    os.makedirs(LAVORO, exist_ok=True)
    subprocess.run(['rclone', 'copy', DRIVE, LAVORO, '--include', 'testi_ol_*.jsonl.gz'], check=True)
    res, tutte_opere, tutti_senza, tutti_testi, tutti_libri = {}, set(), 0, set(), 0
    for f in sorted(glob.glob(os.path.join(LAVORO, 'testi_ol_*.jsonl.gz'))):
        paese = os.path.basename(f)[len('testi_ol_'):-len('.jsonl.gz')]
        libri, senza, opere, testi = 0, 0, set(), set()
        with gzip.open(f, 'rt', encoding='utf-8') as g:
            for l in g:
                x = json.loads(l)
                d = x.get('descrizione')
                if not d:
                    continue
                libri += 1
                if x.get('ol_opera'):
                    opere.add(x['ol_opera'])
                else:
                    senza += 1
                testi.add(hashlib.blake2b(d.strip().lower().encode('utf-8'), digest_size=8).digest())
        res[paese] = {'libri_con_descrizione': libri, 'opere_distinte': len(opere), 'libri_senza_opera': senza,
                      'schede_necessarie': len(opere) + senza, 'testi_distinti': len(testi),
                      'risparmio_pct': round(100 * (1 - (len(opere) + senza) / libri), 1) if libri else 0}
        tutti_libri += libri
        tutti_senza += senza
        tutte_opere |= opere
        tutti_testi |= testi
        print(f"::notice title=opere {paese}::{libri} libri con descrizione -> {len(opere) + senza} schede "
              f"({len(opere)} opere + {senza} senza opera), testi distinti {len(testi)}", flush=True)
    n = len(tutte_opere) + tutti_senza
    res['_totale'] = {'libri_con_descrizione': tutti_libri, 'opere_distinte_tra_paesi': len(tutte_opere),
                      'libri_senza_opera': tutti_senza, 'schede_necessarie': n, 'testi_distinti': len(tutti_testi),
                      'risparmio_pct': round(100 * (1 - n / tutti_libri), 1) if tutti_libri else 0}
    print(f"::notice title=opere totale::{tutti_libri} libri con descrizione -> {n} schede "
          f"({len(tutte_opere)} opere distinte tra tutti i paesi + {tutti_senza} senza opera), testi distinti {len(tutti_testi)}", flush=True)
    with open(os.path.join(LAVORO, 'opere_riepilogo.json'), 'w', encoding='utf-8') as g:
        json.dump(res, g, ensure_ascii=False, indent=1)
    subprocess.run(['rclone', 'copyto', os.path.join(LAVORO, 'opere_riepilogo.json'), f'{DRIVE}/opere_riepilogo.json'], check=True)


if __name__ == '__main__':
    main()
