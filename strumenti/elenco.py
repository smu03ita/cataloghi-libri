#!/usr/bin/env python3
"""Elenco dei file dei cataloghi nazionali nel Drive da estrarre (uno per lavoro parallelo).
Salta i file già estratti (c'è già il loro stat_*.json). Scrive in GITHUB_OUTPUT: pezzi=<lista JSON>."""
import json
import os
import re
import subprocess

BASE = 'drive:Fabula App/cataloghi'
ESCLUSI = {'Italia', 'Inglese - Open Library', '_confronto'}
ESTENSIONI = ('.gz', '.mrc', '.marc', '.zip', '.tsv', '.xml', '.utf8', '.trig', '.nt', '.ttl')


def lsjson(percorso, *opz):
    r = subprocess.run(['rclone', 'lsjson', percorso, *opz], capture_output=True, text=True)
    if r.returncode != 0:
        return []
    return json.loads(r.stdout or '[]')


def nome_sicuro(paese, nome):
    return re.sub(r'[^\w.-]+', '_', f'{paese}_{nome}')


pezzi = []
for d in lsjson(BASE, '--dirs-only'):
    paese = d['Name']
    if paese in ESCLUSI:
        continue
    fatti = {x['Name'] for x in lsjson(f'{BASE}/_confronto/estratti/{paese}', '--files-only')}
    for f in lsjson(f'{BASE}/{paese}', '-R', '--files-only'):
        nome = f['Name']
        if not nome.lower().endswith(ESTENSIONI) or re.search(r'checksum|pruefsumme|readme', nome, re.I) or nome.endswith('.parziale'):
            continue
        if f'stat_{nome_sicuro(paese, nome)}.json' in fatti:
            continue
        pezzi.append({'paese': paese, 'percorso': f['Path'], 'nome': nome, 'mb': round(f['Size'] / 1e6)})
pezzi.sort(key=lambda p: -p['mb'])          # prima i più grandi
print(f'{len(pezzi)} file da estrarre:')
for p in pezzi:
    print(f"  {p['paese']}: {p['percorso']} ({p['mb']} MB)")
with open(os.environ.get('GITHUB_OUTPUT', '/dev/null'), 'a') as f:
    f.write('pezzi=' + json.dumps(pezzi, ensure_ascii=False) + '\n')
