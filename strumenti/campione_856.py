#!/usr/bin/env python3
"""Controllo: nei libri con ISBN di un catalogo MARC21, quali diciture hanno i collegamenti (campo 856 $3, $x, $q)
e com'è fatto l'indirizzo. Legge dallo standard input le prime N schede."""
import collections, re, sys
sys.path.insert(0, 'strumenti')
from estrai import leggi_iso2709

N = int(sys.argv[1]) if len(sys.argv) > 1 else 300000
diciture, indirizzi, esempi = collections.Counter(), collections.Counter(), []
n = con_isbn = con_856 = 0
for s in leggi_iso2709(sys.stdin.buffer):
    n += 1
    if n > N:
        break
    if not s.sub('020', 'a9'):
        continue
    con_isbn += 1
    campi = [c for c in s.campi.get('856', []) if not isinstance(c, str)]
    if campi:
        con_856 += 1
    for ind, subs in campi:
        d = ' | '.join(f'${k} {v}' for k, v in subs if k in '3xq')
        diciture[d] += 1
        u = next((v for k, v in subs if k == 'u'), '')
        indirizzi[re.sub(r'\d+', 'N', u)[:90]] += 1
        if len(esempi) < 8 and 'nhalt' in d:
            esempi.append(u)
print(f'schede {n}, con ISBN {con_isbn}, con 856 {con_856}')
print('diciture più frequenti:')
for d, k in diciture.most_common(25):
    print(f'  {k:8d}  {d}')
print('indirizzi più frequenti:')
for d, k in indirizzi.most_common(10):
    print(f'  {k:8d}  {d}')
print('esempi:', *esempi, sep='\n  ')
