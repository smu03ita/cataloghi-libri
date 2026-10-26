#!/usr/bin/env python3
"""Trame da Wikipedia per i libri dei cataloghi stranieri trovati su Wikidata (file trovati_<paese>.tsv.gz del confronto).

Una lingua per volta (argomento: de, fr, en, pl, pt, nl, et...): si raccolgono le pagine di quella lingua da tutti i paesi,
si chiedono a 50 per volta (testo sorgente, ripulito qui) e si tiene l'introduzione e la sezione della trama.
Uscita: trame_wikipedia_<lingua>.jsonl.gz (pagina, lingua, testo, tipo) in USCITA.
"""
import csv
import glob
import gzip
import json
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trame_wikipedia as tw   # noqa: E402  (chiedi, pulisci, taglia)

import mwparserfromhell   # noqa: E402

csv.field_size_limit(1 << 30)
SEZIONI = {
    'de': r'handlung|inhalt|inhaltsangabe|zusammenfassung|plot|geschichte',
    'fr': r'r[ée]sum[ée]|intrigue|synopsis|histoire|argument|r[ée]sum[ée] de l.intrigue',
    'pl': r'fabu[łl]a|tre[śs][ćc]|opis fabu[łl]y|streszczenie|zarys fabu[łl]y',
    'pt': r'enredo|sinopse|resumo|trama|argumento|hist[óo]ria',
    'nl': r'verhaal|inhoud|plot|samenvatting|verhaallijn',
    'et': r'sisu|s[üu][žz]ee',
    'en': r'plot|synopsis|plot summary|plot synopsis|summary|story|premise',
    'es': r'argumento|sinopsis|trama|resumen',
    'cs': r'd[ěe]j|obsah|p[řr][íi]b[ěe]h',
}


def trama(wikitext, lingua):
    rx = re.compile(r'^(' + SEZIONI.get(lingua, SEZIONI['en']) + r')$', re.I)
    w = mwparserfromhell.parse(wikitext)
    primo = w.filter_headings(recursive=False)
    intro = tw.pulisci(str(w).split(str(primo[0]), 1)[0] if primo else str(w))
    corpo = ''
    for s in w.get_sections(levels=[2]):
        h = s.filter_headings()[0]
        if rx.match(tw.pulisci(str(h.title)).strip()):
            corpo = tw.pulisci(str(s).split(str(h), 1)[1])
            break
    if corpo and len(corpo) >= 150:
        return tw.taglia(intro, 1000) + '\n' + tw.taglia(corpo, 2500), 'trama'
    return tw.taglia(intro, 2000), 'introduzione'


def main():
    lingua, cartella, uscita = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(uscita, exist_ok=True)
    pagine = set()
    for f in glob.glob(os.path.join(cartella, 'trovati_*.tsv.gz')):
        with gzip.open(f, 'rt', encoding='utf-8', newline='') as g:
            r = csv.DictReader(g, delimiter='\t')
            col = f'wikipedia_{lingua}'
            if col not in (r.fieldnames or []):
                continue
            for x in r:
                if x.get(col):
                    pagine.add(x[col])
    pagine = sorted(pagine)
    print(f'{lingua}: {len(pagine)} pagine da leggere', flush=True)
    sessione = requests.Session()
    t0, fatte, con_trama = time.time(), 0, 0
    nome = os.path.join(uscita, f'trame_wikipedia_{lingua}.jsonl.gz')
    with gzip.open(nome, 'wt', encoding='utf-8') as out:
        for k in range(0, len(pagine), 50):
            gruppo = pagine[k:k + 50]
            testi = tw.chiedi(lingua, gruppo, sessione)
            for p in gruppo:
                w = testi.get(p) or testi.get(p.replace('_', ' '))
                if not w:
                    continue
                try:
                    t, tipo = trama(w, lingua)
                except Exception:
                    continue
                if len(t) >= 200:
                    out.write(json.dumps({'pagina': p, 'lingua': lingua, 'testo': t, 'tipo': tipo}, ensure_ascii=False) + '\n')
                    fatte += 1
                    con_trama += tipo == 'trama'
            if (k // 50) % 40 == 0:
                print(f'  {lingua}: {k + len(gruppo)}/{len(pagine)}, testi {fatte}, {time.time()-t0:.0f} s', flush=True)
            time.sleep(0.5)
    riep = {'lingua': lingua, 'pagine': len(pagine), 'con_testo': fatte, 'con_trama': con_trama, 'solo_introduzione': fatte - con_trama,
            'secondi': round(time.time() - t0)}
    with open(os.path.join(uscita, f'trame_wikipedia_{lingua}.json'), 'w') as f:
        json.dump(riep, f)
    print(json.dumps(riep), flush=True)
    print(f"::notice title=trame {lingua}::{riep['pagine']} pagine, {riep['con_testo']} con testo ({riep['con_trama']} con la trama)", flush=True)


if __name__ == '__main__':
    main()
