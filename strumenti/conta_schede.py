#!/usr/bin/env python3
"""Conteggio delle schede DeepSeek nel Drive (cataloghi/Italia/schede_deepseek/<selezione>): libri schedati (una volta
sola per ISBN), errori, costo, fonte della sinossi. Solo numeri, nessun testo."""
import collections, json, subprocess

BASE = 'drive:Fabula App/cataloghi/Italia/schede_deepseek'


def rclone(*a):
    r = subprocess.run(['rclone', *a], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f'rclone {a[0]}: ' + r.stderr[-300:])
    return r.stdout


for sel in sorted(x.strip('/') for x in rclone('lsf', BASE, '--dirs-only').split('\n') if x.strip()):
    fatte, errori, costo, fonti, file = {}, 0, 0.0, collections.Counter(), 0
    righe = 0
    for nome in rclone('lsf', f'{BASE}/{sel}', '--files-only').split('\n'):
        if not nome.endswith('.jsonl'):
            continue
        file += 1
        for l in rclone('cat', f'{BASE}/{sel}/{nome}').split('\n'):
            if not l.strip():
                continue
            righe += 1
            x = json.loads(l)
            k = str(x.get('isbn_principale'))
            if x.get('scheda'):
                if k not in fatte:
                    fatte[k] = x
                    costo += x.get('costo_usd') or 0
                    fonti[x.get('sinossi_fonte') or '?'] += 1
            else:
                errori += 1
    testo = (f'{sel}: {len(fatte)} libri schedati ({file} file, {righe} righe), errori {errori}, '
             f'costo {costo:.2f} $, fonti {dict(fonti)}')
    print(testo)
    print('::notice title=schede DeepSeek nel Drive::' + testo)
