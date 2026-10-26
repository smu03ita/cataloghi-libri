#!/usr/bin/env python3
"""Trame da Wikipedia per le opere della selezione SBN che hanno una pagina Wikipedia (file wikipedia_XX.jsonl nel Drive).

Per ogni opera: pagina italiana se c'è (altrimenti, o se è troppo corta, quella inglese); si tengono l'introduzione e la
sezione della trama (Trama, Sinossi, Contenuto... / Plot, Synopsis...). Le pagine si chiedono a 50 per volta (testo
sorgente della pagina, poi ripulito qui), una richiesta alla volta: poche centinaia di richieste in tutto.
Scrive trame_wikipedia_XX.jsonl e trame_riepilogo.json nella cartella della selezione: tutti i campi dell'opera più
trama_wikipedia, trama_lingua, trama_pagina, trama_tipo ('trama' | 'introduzione').
"""
import json
import os
import re
import subprocess
import time

import mwparserfromhell
import requests

DRIVE = 'drive:Fabula App/cataloghi/Italia'
# Wikimedia chiede un'etichetta che dica chi fa le richieste: qui l'indirizzo pubblico di questi programmi
UA = {'User-Agent': 'catalog-check/1.0 (https://github.com/smu03ita/cataloghi-libri)'}
LAVORO = os.environ.get('LAVORO', '/tmp/trame')
SEZIONI = re.compile(r'^(trama|sinossi|contenuto|riassunto|argomento|plot|synopsis|plot summary|plot synopsis|summary|story)$', re.I)
NON_TESTO = re.compile(r'^(file|image|immagine|categoria|category|media)\s*:', re.I)


def rclone(*a):
    r = subprocess.run(['rclone', *a], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f'rclone {a[0]}: ' + r.stderr[-300:])
    return r.stdout


def chiedi(lingua, titoli, sessione):
    """{titolo richiesto: wikitext} per un gruppo di (al massimo 50) titoli; le pagine di disambiguazione restano fuori."""
    p = {'action': 'query', 'prop': 'revisions|pageprops', 'rvprop': 'content', 'rvslots': 'main', 'ppprop': 'disambiguation',
         'redirects': 1, 'format': 'json', 'formatversion': 2, 'maxlag': 5, 'titles': '|'.join(titoli)}
    out, attesa = {}, 5
    while True:
        for prova in range(6):
            try:
                r = sessione.post(f'https://{lingua}.wikipedia.org/w/api.php', data=p, headers=UA, timeout=120)
                if r.status_code == 429 or r.status_code >= 500 or 'maxlag' in r.text[:200]:
                    time.sleep(int(r.headers.get('Retry-After') or attesa * (prova + 1)))
                    continue
                j = r.json()
                break
            except (requests.RequestException, ValueError):
                time.sleep(attesa * (prova + 1))
        else:
            print(f'  {lingua}: gruppo non riuscito ({len(titoli)} titoli)', flush=True)
            return out
        q = j.get('query', {})
        mappa = {}
        for x in q.get('normalized', []) + q.get('redirects', []):
            mappa[x['to']] = mappa.get(x['from'], x['from'])
        for pg in q.get('pages', []):
            if pg.get('missing') or 'disambiguation' in (pg.get('pageprops') or {}):
                continue
            revs = pg.get('revisions') or []
            if not revs:
                continue
            testo = (revs[0].get('slots', {}).get('main') or {}).get('content')
            if testo:
                t = pg['title']
                originale = mappa.get(t, t)
                originale = mappa.get(originale, originale)
                out[originale] = testo
                out[t] = testo
        if 'continue' not in j:
            return out
        p.update(j['continue'])


def pulisci(wikitext):
    w = mwparserfromhell.parse(wikitext)
    for n in w.filter_wikilinks(recursive=False):
        if NON_TESTO.match(str(n.title).strip()):
            try:
                w.remove(n)
            except ValueError:
                pass
    for n in w.filter_tags(recursive=False):
        if str(n.tag).lower() in ('ref', 'table', 'gallery', 'math', 'references'):
            try:
                w.remove(n)
            except ValueError:
                pass
    t = w.strip_code(normalize=True, collapse=True)
    t = re.sub(r'\[\d+\]', '', t)
    t = re.sub(r'[ \t]+', ' ', t)
    t = re.sub(r' *\n *', '\n', t)
    return re.sub(r'\n\s*\n+', '\n', t).strip()


def taglia(testo, n):
    testo = testo.strip()
    if len(testo) <= n:
        return testo
    t = testo[:n]
    return t[:max(t.rfind('. '), n // 2) + 1].strip()


def trama(wikitext):
    w = mwparserfromhell.parse(wikitext)
    sezioni = w.get_sections(levels=[2])
    primo = w.filter_headings(recursive=False)
    intro = str(w).split(str(primo[0]), 1)[0] if primo else str(w)
    intro = pulisci(intro)
    corpo = ''
    for s in sezioni:
        h = s.filter_headings()[0]
        if SEZIONI.match(pulisci(str(h.title)).strip()):
            corpo = pulisci(str(s).split(str(h), 1)[1])     # con i sottoparagrafi
            break
    if corpo and len(corpo) >= 150:
        return taglia(intro, 1000) + '\n' + taglia(corpo, 2500), 'trama'
    return taglia(intro, 2000), 'introduzione'


def main():
    os.makedirs(LAVORO, exist_ok=True)
    cartelle = sorted(x.strip('/') for x in rclone('lsf', DRIVE, '--dirs-only').split('\n') if x.startswith('selezione_20'))
    sel = f'{DRIVE}/{cartelle[-1]}'
    nomi = sorted(x for x in rclone('lsf', sel, '--files-only').split('\n') if re.match(r'wikipedia_\d+\.jsonl$', x))
    opere = [json.loads(l) for n in nomi for l in rclone('cat', f'{sel}/{n}').split('\n') if l.strip()]
    print(f'{cartelle[-1]}: {len(opere)} opere con pagina Wikipedia', flush=True)
    sessione = requests.Session()
    t0 = time.time()
    risultati = {i: None for i in range(len(opere))}
    for lingua, campo in (('it', 'wikipedia_it'), ('en', 'wikipedia_en')):
        da_fare = [i for i, o in enumerate(opere) if risultati[i] is None and o.get(campo)]
        print(f'{lingua}: {len(da_fare)} pagine da leggere', flush=True)
        for k in range(0, len(da_fare), 50):
            gruppo = da_fare[k:k + 50]
            testi = chiedi(lingua, list(dict.fromkeys(opere[i][campo] for i in gruppo)), sessione)
            for i in gruppo:
                w = testi.get(opere[i][campo]) or testi.get(opere[i][campo].replace('_', ' '))
                if not w:
                    continue
                try:
                    t, tipo = trama(w)
                except Exception:
                    continue
                if len(t) >= 200:
                    risultati[i] = (t, lingua, opere[i][campo], tipo)
            if (k // 50) % 20 == 0:
                print(f'  {lingua}: {k + len(gruppo)}/{len(da_fare)}, {time.time()-t0:.0f} s', flush=True)
            time.sleep(1)
    buone = []
    for i, o in enumerate(opere):
        r = risultati[i]
        if r:
            buone.append(dict(o, trama_wikipedia=r[0], trama_lingua=r[1], trama_pagina=r[2], trama_tipo=r[3]))
    conta = {
        'opere': len(opere),
        'con_testo': len(buone),
        'con_trama': sum(1 for r in buone if r['trama_tipo'] == 'trama'),
        'solo_introduzione': sum(1 for r in buone if r['trama_tipo'] == 'introduzione'),
        'senza_testo': len(opere) - len(buone),
        'in_italiano': sum(1 for r in buone if r['trama_lingua'] == 'it'),
        'in_inglese': sum(1 for r in buone if r['trama_lingua'] == 'en'),
        'secondi': round(time.time() - t0),
    }
    print(json.dumps(conta, ensure_ascii=False, indent=1), flush=True)
    for k in range(0, max(len(buone), 1), 5000):
        nome = f'trame_wikipedia_{k // 5000:02d}.jsonl'
        with open(os.path.join(LAVORO, nome), 'w', encoding='utf-8') as f:
            for r in buone[k:k + 5000]:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
        rclone('copyto', os.path.join(LAVORO, nome), f'{sel}/{nome}')
    with open(os.path.join(LAVORO, 'trame_riepilogo.json'), 'w') as f:
        json.dump(conta, f, ensure_ascii=False, indent=1)
    rclone('copyto', os.path.join(LAVORO, 'trame_riepilogo.json'), f'{sel}/trame_riepilogo.json')


if __name__ == '__main__':
    main()
