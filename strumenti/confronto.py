#!/usr/bin/env python3
"""Confronto dei cataloghi nazionali con Open Library e Wikidata/Wikipedia.

Legge gli estratti (estratto_*.tsv.gz, fatti da estrai.py) e i loro conteggi (stat_*.json) da ESTRATTI,
i due archivi di Open Library (edizioni, opere) e interroga Wikidata (QLever). Per ogni paese conta, sui libri con ISBN:
  - dal catalogo stesso: quanti hanno una presentazione (520/330) o un collegamento al testo dell'editore;
  - Open Library: edizione trovata, opera, descrizione, copertina;
  - Wikidata: opera trovata per ISBN o per titolo + cognome dell'autore, pagina Wikipedia nella lingua del paese e in inglese.
Scrive in USCITA: confronto_paesi.json, confronto_paesi.md e trovati_<paese>.tsv.gz (i collegamenti trovati, per dopo).

Variabili: ESTRATTI, USCITA, OL_EDIZIONI, OL_OPERE (percorsi locali o drive:..., letti in streaming),
           RIEPILOGO_ITALIA (facoltativo: riepilogo.json della selezione italiana, per la riga di confronto).
"""
import collections
import glob
import gzip
import json
import multiprocessing as mp
import os
import re
import shlex
import subprocess
import sys
import time
import unicodedata
from array import array

import numpy as np
import pandas as pd
import requests

UA = {'User-Agent': 'Mozilla/5.0 (compatible; catalog-check/1.0)'}
ESTRATTI = os.environ.get('ESTRATTI', 'estratti')
USCITA = os.environ.get('USCITA', 'uscita')
LAVORO = os.environ.get('LAVORO', 'lavoro')
os.makedirs(USCITA, exist_ok=True)
os.makedirs(LAVORO, exist_ok=True)

# lingua di Wikipedia del paese
WIKI_PAESE = {'Germania': 'de', 'Francia': 'fr', 'Polonia': 'pl', 'Portogallo': 'pt', 'Estonia': 'et', 'Paesi Bassi': 'nl',
              'Inglese - Library of Congress': 'en', 'Spagna': 'es', 'Repubblica Ceca': 'cs', 'Lettonia': 'lv', 'Italia': 'it'}
# codici lingua dei cataloghi (MARC/UNIMARC) -> lingua delle etichette Wikidata
LINGUA_MARC = {'ger': 'de', 'deu': 'de', 'fre': 'fr', 'fra': 'fr', 'pol': 'pl', 'por': 'pt', 'est': 'et', 'dut': 'nl', 'nld': 'nl',
               'eng': 'en', 'spa': 'es', 'cze': 'cs', 'ces': 'cs', 'ita': 'it', 'lav': 'lv'}
LINGUE_WD = sorted(set(WIKI_PAESE.values()))


# ISBN: le regole comuni di strumenti/isbn.py (isbnlib); tollerante come prima, per confrontare i cataloghi
from isbn import isbn13_tollerante as isbn13  # noqa: E402



def flusso(percorso):
    """Righe (bytes) di un .gz locale o nel Drive, senza copiarlo sul disco."""
    dec = 'pigz -dc' if subprocess.call('which pigz > /dev/null', shell=True) == 0 else 'gzip -dc'
    if percorso.startswith('drive:'):
        cmd = f'rclone cat {shlex.quote(percorso)} | {dec}'
    else:
        cmd = f'{dec} {shlex.quote(percorso)}'
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, bufsize=1 << 22)
    yield from p.stdout
    if p.wait() != 0:
        raise RuntimeError('lettura non riuscita: ' + percorso.rsplit('/', 1)[-1])


# ------------------------------------------------------------------------------------------------------------------
# Open Library: due letture (edizioni, opere) in due processi paralleli; il risultato sono solo array di numeri

RX_WORK = re.compile(rb'"works":\s*\[\s*\{\s*"key":\s*"/works/OL(\d+)W"')
RX_I13 = re.compile(rb'"isbn_13":\s*\[([^\]]*)\]')
RX_I10 = re.compile(rb'"isbn_10":\s*\[([^\]]*)\]')
RX_STR = re.compile(rb'"([^"]*)"')
RX_DESC = re.compile(rb'"description":\s*(?:"(?!")|\{[^}]*"value":\s*"(?!"))')
RX_COVER = re.compile(rb'"covers":\s*\[\s*[1-9]')


def leggi_edizioni(percorso, dest):
    isbn, ed = array('q'), array('i')        # array compatti: decine di milioni di valori
    opera, flag = array('i'), array('b')
    t0 = time.time()
    for n, riga in enumerate(flusso(percorso)):
        mw = RX_WORK.search(riga)
        opera.append(int(mw.group(1)) if mw else -1)
        flag.append((1 if RX_DESC.search(riga) else 0) | (2 if RX_COVER.search(riga) else 0))
        visti = set()
        for rx in (RX_I13, RX_I10):
            m = rx.search(riga)
            if m:
                for s in RX_STR.findall(m.group(1)):
                    i = isbn13(s.decode('ascii', 'ignore'))
                    if i and i not in visti:
                        visti.add(i)
                        isbn.append(int(i))
                        ed.append(n)
        if n % 5_000_000 == 0 and n:
            print(f'  OL edizioni: {n:,} lette, {len(isbn):,} ISBN, {time.time()-t0:.0f} s', flush=True)
    isbn = np.frombuffer(isbn, dtype=np.int64)
    ed = np.frombuffer(ed, dtype=np.int32)
    o = np.argsort(isbn, kind='stable')
    np.savez(dest, isbn=isbn[o], ed=ed[o], opera=np.frombuffer(opera, dtype=np.int32), flag=np.frombuffer(flag, dtype=np.int8))
    print(f'OL edizioni: {len(opera):,} edizioni, {len(isbn):,} ISBN, {time.time()-t0:.0f} s', flush=True)


def leggi_opere(percorso, dest):
    ids, flag = array('i'), array('b')
    t0 = time.time()
    for n, riga in enumerate(flusso(percorso)):
        parti = riga.split(b'\t', 2)
        if len(parti) < 3 or not parti[1].startswith(b'/works/OL'):
            continue
        try:
            w = int(parti[1][9:-1])
        except ValueError:
            continue
        ids.append(w)
        flag.append((1 if RX_DESC.search(riga) else 0) | (2 if RX_COVER.search(riga) else 0))
        if n % 5_000_000 == 0 and n:
            print(f'  OL opere: {n:,} lette, {time.time()-t0:.0f} s', flush=True)
    ids = np.frombuffer(ids, dtype=np.int32)
    fl = np.zeros(int(ids.max()) + 1 if len(ids) else 1, dtype=np.int8)
    fl[ids] = np.frombuffer(flag, dtype=np.int8)
    np.savez(dest, flag=fl)
    print(f'OL opere: {len(ids):,}, {time.time()-t0:.0f} s', flush=True)


# ------------------------------------------------------------------------------------------------------------------
# Wikidata (QLever, copia di Wikidata dell'Università di Friburgo)

PREFISSI = ('PREFIX wdt: <http://www.wikidata.org/prop/direct/>\n'
            'PREFIX wikibase: <http://wikiba.se/ontology#>\n'
            'PREFIX schema: <http://schema.org/>\n'
            'PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n')


def sparql(q, nome):
    for tentativo in range(3):
        try:
            t0 = time.time()
            r = requests.post('https://qlever.cs.uni-freiburg.de/api/wikidata', data={'query': q},
                              headers={**UA, 'Accept': 'text/tab-separated-values'}, timeout=1200)
            r.raise_for_status()
            righe = r.text.split('\n')
            print(f'  Wikidata {nome}: {len(righe)-1:,} righe, {time.time()-t0:.0f} s', flush=True)
            testa = [h.lstrip('?') for h in righe[0].split('\t')]
            return [dict(zip(testa, x.split('\t'))) for x in righe[1:] if x]
        except Exception as e:
            print(f'  Wikidata {nome}: errore ({str(e)[:150]}), tentativo {tentativo + 1}', flush=True)
            time.sleep(30 * (tentativo + 1))
    return None


def valore(v):
    v = (v or '').strip()
    if v.startswith('<') and v.endswith('>'):
        v = v[1:-1]
        return v.rsplit('/', 1)[-1] if '/entity/' in v else v
    m = re.match(r'^"(.*)"(?:@[\w-]+|\^\^<[^>]+>)?$', v, re.S)
    if m:
        return m.group(1).replace('\\"', '"').replace('\\\\', '\\')
    return v


def senza_accenti(s):
    return ''.join(c for c in unicodedata.normalize('NFKD', s) if not unicodedata.combining(c))


ARTICOLI = {'the', 'a', 'an', 'der', 'die', 'das', 'ein', 'eine', 'le', 'la', 'les', 'l', 'un', 'une', 'el', 'los', 'las',
            'lo', 'il', 'gli', 'i', 'uno', 'una', 'o', 'os', 'as', 'um', 'uma', 'de', 'het', 'een'}


def chiave(t):
    t = (t or '').split(' : ')[0].split(' / ')[0].split(' = ')[0]
    p = re.sub(r'[^a-z0-9]+', ' ', senza_accenti(t.lower())).split()
    while len(p) > 1 and p[0] in ARTICOLI:
        p = p[1:]
    return ' '.join(p[:8])


def parole(t):
    return set(re.sub(r'[^a-z0-9]+', ' ', senza_accenti((t or '').lower())).split())


def wikidata():
    """Restituisce (isbn->qid, per lingua: chiave titolo -> [(parole autore, qid)], qid -> pagine per lingua)."""
    out = {'isbn': {}, 'titoli': {}, 'pagine': collections.defaultdict(dict), 'errori': []}
    q = PREFISSI + """SELECT ?isbn ?t WHERE {
  { ?ed wdt:P212 ?isbn } UNION { ?ed wdt:P957 ?isbn }
  OPTIONAL { ?ed wdt:P629 ?opera . }
  BIND(COALESCE(?opera, ?ed) AS ?t)
}"""
    r = sparql(q, 'ISBN')
    if r is None:
        out['errori'].append('ISBN')
    else:
        for b in r:
            i = isbn13(valore(b.get('isbn')))
            if i:
                out['isbn'].setdefault(int(i), valore(b.get('t')))
    for L in LINGUE_WD:
        q = PREFISSI + f"""SELECT ?t ?titolo ?autore WHERE {{
  ?t wdt:P50 ?a .
  ?t wikibase:sitelinks ?n . FILTER(?n > 0)
  ?t rdfs:label ?titolo . FILTER(LANG(?titolo) = "{L}")
  ?a rdfs:label ?autore . FILTER(LANG(?autore) = "{L}" || LANG(?autore) = "en")
}}"""
        r = sparql(q, f'titoli {L}')
        if r is None:
            out['errori'].append(f'titoli {L}')
            continue
        d = collections.defaultdict(list)
        for b in r:
            k = chiave(valore(b.get('titolo')))
            if k:
                d[k].append((parole(valore(b.get('autore'))), valore(b.get('t'))))
        out['titoli'][L] = d
        q = PREFISSI + f"""SELECT ?t ?pagina WHERE {{
  ?s schema:about ?t ; schema:isPartOf <https://{L}.wikipedia.org/> ; schema:name ?pagina .
  {{ ?t wdt:P50 [] }} UNION {{ ?t wdt:P212 [] }} UNION {{ ?t wdt:P957 [] }} UNION {{ ?x wdt:P629 ?t }}
}}"""
        r = sparql(q, f'pagine {L}')
        if r is None:
            out['errori'].append(f'pagine {L}')
            continue
        for b in r:
            out['pagine'][valore(b.get('t'))][L] = valore(b.get('pagina'))
    return out


# ------------------------------------------------------------------------------------------------------------------

def principale():
    t0 = time.time()
    ed_npz, op_npz = os.path.join(LAVORO, 'ol_edizioni.npz'), os.path.join(LAVORO, 'ol_opere.npz')
    procs = []
    if not os.path.exists(ed_npz):
        procs.append(mp.Process(target=leggi_edizioni, args=(os.environ['OL_EDIZIONI'], ed_npz)))
    if not os.path.exists(op_npz):
        procs.append(mp.Process(target=leggi_opere, args=(os.environ['OL_OPERE'], op_npz)))
    for p in procs:
        p.start()
    wd = wikidata()          # intanto, Wikidata
    print(f'Wikidata: {len(wd["isbn"]):,} ISBN, titoli in {len(wd["titoli"])} lingue, {len(wd["pagine"]):,} opere con pagine; errori {wd["errori"]}', flush=True)
    for p in procs:
        p.join()
        if p.exitcode != 0:
            sys.exit('lettura di Open Library non riuscita')
    E = np.load(ed_npz)
    ol_isbn, ol_ed, ol_opera, ol_flag = E['isbn'], E['ed'], E['opera'], E['flag']
    op_flag = np.load(op_npz)['flag']
    wd_isbn_k = np.array(sorted(wd['isbn']), dtype=np.int64)
    wd_isbn_v = np.array([wd['isbn'][k] for k in wd_isbn_k], dtype=object)

    # conteggi della lettura dei cataloghi
    stat_file = collections.defaultdict(list)
    for f in glob.glob(os.path.join(ESTRATTI, '**', 'stat_*.json'), recursive=True):
        s = json.load(open(f, encoding='utf-8'))
        stat_file[s['paese']].append(s)
    file_paese = collections.defaultdict(list)
    for f in glob.glob(os.path.join(ESTRATTI, '**', 'estratto_*.tsv.gz'), recursive=True):
        with gzip.open(f, 'rt', encoding='utf-8') as g:
            g.readline()
            prima = g.readline()
        if prima:
            file_paese[prima.split('\t', 1)[0]].append(f)

    risultati = {}
    for paese in sorted(set(stat_file) | set(file_paese)):
        tp = time.time()
        sf = stat_file.get(paese, [])
        r = {'file_letti': len(sf), 'schede_lette': sum(s.get('schede', 0) for s in sf),
             'formati': sorted({x for s in sf for x in s.get('formati', [])}),
             'errori_lettura': [s.get('file') for s in sf if s.get('errore')]}
        parti = []
        for f in file_paese.get(paese, []):
            parti.append(pd.read_csv(f, sep='\t', dtype=str, keep_default_na=False,
                                     usecols=['isbn', 'libro', 'titolo', 'autore', 'lingua', 'sinossi_len', 'link_testo', 'anno', 'id']))
        if not parti:
            risultati[paese] = r
            continue
        df = pd.concat(parti, ignore_index=True)
        del parti
        df['sinossi_len'] = pd.to_numeric(df['sinossi_len'], errors='coerce').fillna(0).astype(np.int32)
        n = len(df)
        r['con_isbn'] = n
        r['libri_con_isbn'] = int((df['libro'] == '1').sum())
        r['lingue_principali'] = df['lingua'].replace('', '?').value_counts().head(6).to_dict()
        # ISBN -> riga
        esp = df['isbn'].str.split('|').explode()
        riga = esp.index.to_numpy(dtype=np.int64)
        isb = pd.to_numeric(esp, errors='coerce').to_numpy()
        ok = ~np.isnan(isb)
        riga, isb = riga[ok], isb[ok].astype(np.int64)
        r['isbn_unici'] = int(len(np.unique(isb)))
        # Open Library
        pos = np.searchsorted(ol_isbn, isb)
        pos[pos >= len(ol_isbn)] = 0
        hit = ol_isbn[pos] == isb
        ed = ol_ed[pos[hit]]
        rr = riga[hit]
        opera = ol_opera[ed]
        fl_ed = ol_flag[ed]
        fl_op = np.where((opera >= 0) & (opera < len(op_flag)), op_flag[np.clip(opera, 0, len(op_flag) - 1)], 0)
        ol_trov = np.zeros(n, bool); ol_trov[rr] = True
        ol_opera_si = np.zeros(n, bool); ol_opera_si[rr[opera >= 0]] = True
        ol_desc = np.zeros(n, bool); ol_desc[rr[((fl_ed | fl_op) & 1) > 0]] = True
        ol_cop = np.zeros(n, bool); ol_cop[rr[((fl_ed | fl_op) & 2) > 0]] = True
        prima_opera = pd.Series(opera, index=rr).groupby(level=0).first()
        # Wikidata per ISBN
        pos = np.searchsorted(wd_isbn_k, isb)
        pos[pos >= len(wd_isbn_k)] = 0
        hit = (wd_isbn_k[pos] == isb) if len(wd_isbn_k) else np.zeros(len(isb), bool)
        qid = np.full(n, None, dtype=object)
        if hit.any():
            s = pd.Series(wd_isbn_v[pos[hit]], index=riga[hit]).groupby(level=0).first()
            qid[s.index.to_numpy()] = s.to_numpy()
        per_isbn = np.array([q is not None for q in qid])
        # Wikidata per titolo + cognome dell'autore (lingua della scheda, poi quella del paese)
        Lp = WIKI_PAESE.get(paese, 'en')
        per_titolo = np.zeros(n, bool)
        tit, aut, lin = df['titolo'].to_numpy(), df['autore'].to_numpy(), df['lingua'].to_numpy()
        for i in range(n):
            if qid[i] is not None or not aut[i]:
                continue
            cogn = parole(aut[i].split(',')[0])
            if not cogn:
                continue
            k = chiave(tit[i])
            if not k:
                continue
            for L in dict.fromkeys([LINGUA_MARC.get(lin[i], Lp), Lp, 'en']):
                cand = [(len(a), q) for a, q in wd['titoli'].get(L, {}).get(k, []) if cogn <= a]
                if cand:
                    qid[i] = cand[0][1]
                    per_titolo[i] = True
                    break
        pag_loc = np.array([bool(q) and Lp in wd['pagine'].get(q, {}) for q in qid])
        pag_en = np.array([bool(q) and 'en' in wd['pagine'].get(q, {}) for q in qid])
        sinossi_cat = df['sinossi_len'].to_numpy() >= 100
        link_cat = df['link_testo'].to_numpy() == '1'
        testo = sinossi_cat | link_cat | ol_desc | pag_loc | pag_en

        def pc(x):
            return {'n': int(x.sum()), 'pct': round(100 * float(x.mean()), 1)}

        r['catalogo'] = {'sinossi_nel_catalogo': pc(sinossi_cat), 'link_al_testo_editore': pc(link_cat)}
        r['open_library'] = {'edizione_trovata': pc(ol_trov), 'opera': pc(ol_opera_si), 'descrizione': pc(ol_desc), 'copertina': pc(ol_cop)}
        r['wikidata'] = {'trovati': pc(per_isbn | per_titolo), 'per_isbn': pc(per_isbn), 'per_titolo_autore': pc(per_titolo),
                         f'wikipedia_{Lp}': pc(pag_loc), 'wikipedia_en': pc(pag_en)}
        r['almeno_un_testo'] = pc(testo)
        r['secondi'] = round(time.time() - tp)
        risultati[paese] = r
        print(f'{paese}: {json.dumps(r, ensure_ascii=False)}', flush=True)
        # i collegamenti trovati, per dopo
        tro = pd.DataFrame({'id': df['id'], 'isbn': df['isbn'], 'titolo': df['titolo'], 'autore': df['autore'], 'anno': df['anno'],
                            'lingua': df['lingua'], 'sinossi_catalogo': df['sinossi_len'], 'link_testo': df['link_testo'],
                            'ol_opera': pd.Series(prima_opera).reindex(range(n)).to_numpy(), 'ol_descrizione': ol_desc.astype(int),
                            'ol_copertina': ol_cop.astype(int), 'wikidata': qid,
                            f'wikipedia_{Lp}': [wd['pagine'].get(q, {}).get(Lp, '') if q else '' for q in qid],
                            'wikipedia_en': [wd['pagine'].get(q, {}).get('en', '') if q else '' for q in qid]})
        tro = tro[ol_trov | (qid != None) | sinossi_cat | link_cat]  # noqa: E711
        nome = re.sub(r'[^\w-]+', '_', paese)
        tro.to_csv(os.path.join(USCITA, f'trovati_{nome}.tsv.gz'), sep='\t', index=False, compression='gzip')
        del df, tro

    # riga dell'Italia (dalla selezione SBN: conteggi per opera, non per edizione)
    ita = None
    p_ita = os.environ.get('RIEPILOGO_ITALIA')
    if p_ita and os.path.exists(p_ita):
        R = json.load(open(p_ita, encoding='utf-8'))
        T, W = R.get('tutte_le_opere', {}), R.get('wikidata', {})
        tot = T.get('opere_pulite') or 1
        ita = {'nota': 'Italia: conteggi per opera (edizioni accorpate), dalla selezione SBN',
               'opere': tot, 'isbn': R.get('codici_fase2'),
               'open_library': {'edizione_trovata_isbn': R.get('ol_edizioni_trovate'),
                                'opera': {'n': T.get('con_opera_open_library'), 'pct': round(100 * T.get('con_opera_open_library', 0) / tot, 1)},
                                'descrizione': {'n': T.get('con_descrizione_open_library'), 'pct': round(100 * T.get('con_descrizione_open_library', 0) / tot, 1)},
                                'copertina': {'n': T.get('con_copertina_open_library'), 'pct': round(100 * T.get('con_copertina_open_library', 0) / tot, 1)}},
               'wikidata': {'trovati': {'n': W.get('opere_trovate_in_tutto'), 'pct': round(100 * W.get('opere_trovate_in_tutto', 0) / tot, 1)},
                            'wikipedia_it': {'n': W.get('con_pagina_wikipedia_italiana'), 'pct': round(100 * W.get('con_pagina_wikipedia_italiana', 0) / tot, 1)},
                            'wikipedia_en': {'n': W.get('con_pagina_wikipedia_inglese'), 'pct': round(100 * W.get('con_pagina_wikipedia_inglese', 0) / tot, 1)}}}

    tutto = {'paesi': risultati, 'italia': ita, 'wikidata_errori': wd['errori'], 'minuti': round((time.time() - t0) / 60)}
    with open(os.path.join(USCITA, 'confronto_paesi.json'), 'w', encoding='utf-8') as f:
        json.dump(tutto, f, ensure_ascii=False, indent=1)

    def c(d, *k):
        for x in k:
            d = (d or {}).get(x)
        return f"{d['pct']}% ({d['n']:,})".replace(',', '.') if isinstance(d, dict) and d.get('n') is not None else '–'

    righe = ['| Paese | Libri con ISBN | Sinossi nel catalogo | Link testo editore | Open Library: edizione | OL descrizione | OL copertina | Wikidata | Wikipedia lingua del paese | Wikipedia inglese | Almeno un testo |',
             '|---|---|---|---|---|---|---|---|---|---|---|']
    for p, r in risultati.items():
        Lp = WIKI_PAESE.get(p, 'en')
        righe.append(f"| {p} | {r.get('con_isbn', 0):,}".replace(',', '.') + f" | {c(r, 'catalogo', 'sinossi_nel_catalogo')} | {c(r, 'catalogo', 'link_al_testo_editore')} | "
                     f"{c(r, 'open_library', 'edizione_trovata')} | {c(r, 'open_library', 'descrizione')} | {c(r, 'open_library', 'copertina')} | "
                     f"{c(r, 'wikidata', 'trovati')} | {c(r, 'wikidata', f'wikipedia_{Lp}')} | {c(r, 'wikidata', 'wikipedia_en')} | {c(r, 'almeno_un_testo')} |")
    if ita:
        righe.append(f"| Italia (per opera) | {ita['opere']:,}".replace(',', '.') + f" | – | – | – | {c(ita, 'open_library', 'descrizione')} | {c(ita, 'open_library', 'copertina')} | "
                     f"{c(ita, 'wikidata', 'trovati')} | {c(ita, 'wikidata', 'wikipedia_it')} | {c(ita, 'wikidata', 'wikipedia_en')} | – |")
    with open(os.path.join(USCITA, 'confronto_paesi.md'), 'w', encoding='utf-8') as f:
        f.write('# Cataloghi nazionali: cosa trovano Open Library e Wikipedia\n\n' + '\n'.join(righe) + '\n')
    print('\n'.join(righe))
    print(f'Fatto in {tutto["minuti"]} minuti.')


if __name__ == '__main__':
    principale()
