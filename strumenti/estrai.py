#!/usr/bin/env python3
"""Estrae da un catalogo nazionale (un file) i dati essenziali dei libri con ISBN, in un formato unico.

Uso:  python3 estrai.py PAESE NOME_FILE USCITA_DIR  < flusso del file (già decompresso se .gz)
      (per i .zip si passa il percorso del file con --zip PERCORSO)

Formati riconosciuti da soli: MARC21 e UNIMARC binari (ISO 2709), MARC in XML (MARCXML / marcxchange),
MARC in testo (=LDR ...), TSV con intestazione (Estonia), RDF TriG/Turtle (Paesi Bassi).

Esce:
  estratto_<paese>_<file>.tsv.gz  una riga per scheda con almeno un ISBN valido
  stat_<paese>_<file>.json        conteggi della lettura (schede, libri, ISBN, campi presenti, lingue...)
"""
import collections
import csv
import gzip
import io
import json
import os
import re
import sys
import time
import unicodedata
import zipfile

csv.field_size_limit(1 << 30)

COLONNE = ['paese', 'file', 'id', 'isbn', 'libro', 'formato', 'titolo', 'autore', 'anno', 'lingua', 'lingua_orig',
           'editore', 'titolo_orig', 'sinossi_len', 'link_testo', 'link_indice']


# ISBN: le regole comuni di strumenti/isbn.py (isbnlib), con la cifra di controllo
from isbn import isbn13  # noqa: E402



RX_ISBN = re.compile(r'(?:97[89][\d\- ]{10,16}|\d[\d\- ]{8,12}[\dXx])')


def isbn_da_testo(t):
    trovati = []
    for m in RX_ISBN.findall(t or ''):
        i = isbn13(m)
        if i and i not in trovati:
            trovati.append(i)
    return trovati


def pulisci(t):
    if t is None:
        return ''
    # caratteri di "non ordinamento" (articoli) di UNIMARC e MARC: \x88 \x89 \x98 \x9c
    t = re.sub(r'[\x88\x89\x98\x9c\u0088\u0089\u0098\u009c]', '', t)
    t = re.sub(r'[\t\r\n]+', ' ', t)
    return t.strip().strip(' /:;,.=').strip()


# ---------------------------------------------------------------------------------------------------------------
# Scheda in forma comune: tag -> lista di campi; campo = (ind, [(codice, testo), ...]) oppure stringa (campi di controllo)

class Scheda:
    __slots__ = ('leader', 'campi')

    def __init__(self, leader):
        self.leader = leader or ''
        self.campi = collections.defaultdict(list)

    def ctrl(self, tag):
        v = self.campi.get(tag)
        return v[0] if v and isinstance(v[0], str) else ''

    def sub(self, tag, codici):
        out = []
        for c in self.campi.get(tag, []):
            if isinstance(c, str):
                continue
            for k, v in c[1]:
                if k in codici:
                    out.append(v)
        return out

    def primo(self, tag, codice):
        x = self.sub(tag, codice)
        return x[0] if x else ''

    def ind(self, tag):
        for c in self.campi.get(tag, []):
            if not isinstance(c, str):
                return c[0]
        return '  '


def dati_scheda(s, paese, nome):
    """Dalla scheda (MARC21 o UNIMARC) i dati comuni. None se non c'è nessun ISBN valido."""
    unimarc = '200' in s.campi and '245' not in s.campi
    ld = s.leader.ljust(24)
    libro = 1 if (ld[6] in 'at' and ld[7] in 'm') else 0
    if unimarc:
        isbn = []
        for v in s.sub('010', 'a'):
            for i in isbn_da_testo(v):
                if i not in isbn:
                    isbn.append(i)
        if not isbn:
            return None
        titolo = pulisci(s.primo('200', 'a'))
        resto = pulisci(s.primo('200', 'e'))
        a = s.sub('700', 'ab') or s.sub('701', 'ab') or s.sub('710', 'a')
        autore = pulisci(', '.join(a[:2])) if a else ''
        c100 = s.primo('100', 'a')
        anno = c100[9:13] if len(c100) >= 13 and c100[9:13].isdigit() else ''
        if not anno:
            m = re.search(r'\d{4}', s.primo('210', 'd') or s.primo('214', 'd') or '')
            anno = m.group(0) if m else ''
        lingua = s.primo('101', 'a')
        lingua_orig = s.primo('101', 'c')
        editore = pulisci(s.primo('210', 'c') or s.primo('214', 'c'))
        titolo_orig = pulisci(s.primo('454', 't') or s.primo('500', 'a'))
        sinossi = ' '.join(s.sub('330', 'a'))
        link = ' '.join(s.sub('856', 'z23u'))
        formato = 'UNIMARC'
    else:
        isbn = []
        for v in s.sub('020', 'a9'):
            for i in isbn_da_testo(v):
                if i not in isbn:
                    isbn.append(i)
        if not isbn:
            return None
        ind2 = s.ind('245')[1:2]
        titolo = s.primo('245', 'a')
        if ind2.isdigit() and int(ind2) > 0:
            titolo = titolo[int(ind2):]
        titolo = pulisci(titolo)
        resto = pulisci(s.primo('245', 'b'))
        autore = pulisci(s.primo('100', 'a') or s.primo('110', 'a') or s.primo('700', 'a'))
        c008 = s.ctrl('008')
        anno = c008[7:11] if len(c008) >= 11 and c008[7:11].isdigit() else ''
        if not anno:
            m = re.search(r'\d{4}', s.primo('264', 'c') or s.primo('260', 'c') or '')
            anno = m.group(0) if m else ''
        lingua = c008[35:38].strip() if len(c008) >= 38 else ''
        if not lingua or lingua in ('|||', '   '):
            lingua = s.primo('041', 'a')
        lingua_orig = s.primo('041', 'h')
        editore = pulisci(s.primo('264', 'b') or s.primo('260', 'b'))
        titolo_orig = pulisci(s.primo('240', 'a') or s.primo('130', 'a') or s.primo('765', 't'))
        sinossi = ' '.join(s.sub('520', 'a'))
        link = ' '.join(s.sub('856', '3xyzu'))
        formato = 'MARC21'
    if resto and titolo and len(titolo) < 200:
        titolo_completo = titolo + ' : ' + resto
    else:
        titolo_completo = titolo
    l = link.lower()
    return {
        'paese': paese, 'file': nome, 'id': (s.ctrl('001') or '')[:40], 'isbn': '|'.join(isbn), 'libro': libro,
        'formato': formato, 'titolo': titolo_completo[:300], 'autore': autore[:120], 'anno': anno, 'lingua': (lingua or '')[:3],
        'lingua_orig': (lingua_orig or '')[:3], 'editore': editore[:120], 'titolo_orig': titolo_orig[:200],
        'sinossi_len': len(sinossi.strip()),
        # DNB e altri: collegamento al testo dell'editore (Inhaltstext / Klappentext) o all'indice
        'link_testo': 1 if re.search(r'inhaltstext|klappentext|beschreibung|description|résumé|resume|abstract|quarta', l) else 0,
        'link_indice': 1 if re.search(r'inhaltsverzeichnis|table of contents|sommaire|indice|spis tre', l) else 0,
    }


# ---------------------------------------------------------------------------------------------------------------
# Lettori dei formati

def leggi_iso2709(flusso):
    """Schede binarie ISO 2709, divise sul terminatore di scheda (0x1D). Robusto anche se le lunghezze sono sbagliate:
    i campi si prendono nell'ordine della directory, divisi sul terminatore di campo (0x1E)."""
    resto = b''
    while True:
        blocco = flusso.read(1 << 22)
        if not blocco:
            break
        dati = resto + blocco
        pezzi = dati.split(b'\x1d')
        resto = pezzi.pop()
        for r in pezzi:
            s = scheda_iso(r)
            if s is not None:
                yield s
    if resto.strip():
        s = scheda_iso(resto)
        if s is not None:
            yield s


def scheda_iso(r):
    r = r.lstrip(b'\r\n ')
    if len(r) < 30:
        return None
    try:
        base = int(r[12:17])
    except ValueError:
        return None
    leader = r[:24].decode('latin-1')
    fine_dir = r.find(b'\x1e', 24)
    if fine_dir < 0:
        return None
    directory = r[24:fine_dir]
    tags = [directory[i:i + 3].decode('latin-1') for i in range(0, len(directory) - len(directory) % 12, 12)]
    campi = r[fine_dir + 1:].split(b'\x1e')
    s = Scheda(leader)
    # solo i campi che servono: si decodificano solo quelli
    for tag, c in zip(tags, campi):
        if tag not in VOLUTI:
            continue
        if tag < '010':
            s.campi[tag].append(c.decode('utf-8', 'replace'))
            continue
        parti = c.split(b'\x1f')
        ind = parti[0].decode('latin-1')[:2]
        subs = [(p[:1].decode('latin-1'), p[1:].decode('utf-8', 'replace')) for p in parti[1:] if p]
        s.campi[tag].append((ind, subs))
    return s


VOLUTI = {'001', '008', '010', '020', '041', '100', '101', '110', '130', '200', '210', '214', '240', '245', '260', '264',
          '330', '454', '500', '520', '700', '701', '710', '765', '856'}


def leggi_mrk(flusso):
    """MARC in testo: =LDR  ..., =245  10$aTitolo$bsotto..."""
    s = None
    for riga in io.TextIOWrapper(flusso, encoding='utf-8', errors='replace'):
        riga = riga.rstrip('\r\n')
        if not riga.strip():
            if s is not None:
                yield s
                s = None
            continue
        if not riga.startswith('='):
            continue
        tag, val = riga[1:4], riga[6:]
        if tag == 'LDR':
            if s is not None:
                yield s
            s = Scheda(val)
            continue
        if s is None or tag not in VOLUTI:
            continue
        if tag < '010':
            s.campi[tag].append(val)
        else:
            ind = val[:2].replace('\\', ' ')
            subs = [(p[:1], p[1:]) for p in val[2:].split('$')[1:] if p]
            s.campi[tag].append((ind, subs))
    if s is not None:
        yield s


def leggi_xml(flusso):
    """MARCXML / marcxchange (con o senza spazio dei nomi)."""
    from lxml import etree
    for _, el in etree.iterparse(flusso, events=('end',), huge_tree=True, recover=True):
        nome = etree.QName(el).localname if isinstance(el.tag, str) else ''
        if nome != 'record':
            continue
        s = Scheda('')
        for f in el:
            if not isinstance(f.tag, str):
                continue
            n = etree.QName(f).localname
            if n == 'leader':
                s.leader = f.text or ''
            elif n == 'controlfield':
                tag = f.get('tag', '')
                if tag in VOLUTI:
                    s.campi[tag].append(f.text or '')
            elif n == 'datafield':
                tag = f.get('tag', '')
                if tag in VOLUTI:
                    ind = (f.get('ind1') or ' ')[:1] + (f.get('ind2') or ' ')[:1]
                    subs = [(sf.get('code', ''), sf.text or '') for sf in f if isinstance(sf.tag, str)]
                    s.campi[tag].append((ind, subs))
        el.clear()
        while el.getprevious() is not None:
            del el.getparent()[0]
        yield s


def righe_tsv(flusso, paese, nome, stat):
    """TSV con intestazione (Estonia: MARC21XML convertito in colonne). Si cercano le colonne per nome."""
    testo = io.TextIOWrapper(flusso, encoding='utf-8', errors='replace', newline='')
    r = csv.reader(testo, delimiter='\t')
    testa = next(r)
    stat['intestazione'] = testa[:200]
    low = [h.lower() for h in testa]

    def col(*chiavi, esclusi=()):
        for k in chiavi:
            for i, h in enumerate(low):
                if k in h and not any(e in h for e in esclusi):
                    return i
        return None

    c = {
        'isbn': col('020$a', '020a', '020_a', 'isbn'),
        'titolo': col('245$a', '245a', '245_a', 'title', 'pealkiri', esclusi=('uniform', '246', '240')),
        'resto': col('245$b', '245b', '245_b', 'subtitle'),
        'autore': col('100$a', '100a', '100_a', 'author', 'autor'),
        'anno': col('008', 'year', 'date', 'aasta'),
        'lingua': col('041$a', '041a', 'language', 'keel'),
        'editore': col('264$b', '260$b', '264b', '260b', 'publisher', 'kirjastus'),
        'sinossi': col('520$a', '520a', '520_a', 'summary', 'abstract', 'annotatsioon'),
        'id': col('001', 'id'),
        'leader': col('leader', 'ldr'),
    }
    stat['colonne_usate'] = {k: (testa[v] if v is not None else None) for k, v in c.items()}

    def v(row, k):
        i = c[k]
        return row[i] if i is not None and i < len(row) else ''

    for row in r:
        stat['schede'] += 1
        isbn = isbn_da_testo(v(row, 'isbn'))
        if not isbn:
            continue
        ld = v(row, 'leader').ljust(8)
        a008 = v(row, 'anno')
        if len(a008) >= 11 and a008[7:11].isdigit():
            anno = a008[7:11]
        else:
            m = re.search(r'1[5-9]\d\d|20\d\d', a008)
            anno = m.group(0) if m else ''
        titolo = pulisci(v(row, 'titolo'))
        resto = pulisci(v(row, 'resto'))
        yield {'paese': paese, 'file': nome, 'id': v(row, 'id')[:40], 'isbn': '|'.join(isbn),
               'libro': 1 if (ld[6:7] in ('a', 't') and ld[7:8] == 'm') or c['leader'] is None else 0,
               'formato': 'TSV', 'titolo': (titolo + (' : ' + resto if resto else ''))[:300],
               'autore': pulisci(v(row, 'autore'))[:120], 'anno': anno, 'lingua': v(row, 'lingua')[:3],
               'lingua_orig': '', 'editore': pulisci(v(row, 'editore'))[:120], 'titolo_orig': '',
               'sinossi_len': len(v(row, 'sinossi').strip()), 'link_testo': 0, 'link_indice': 0}


def righe_rdf(flusso, paese, nome, stat):
    """RDF in TriG/Turtle scritto 'a blocchi' (Paesi Bassi, KB). Lettura a righe: per ogni soggetto si raccolgono
    ISBN, nome/titolo, descrizione, autori (collegamenti) e tipo; i nomi delle persone si risolvono alla fine."""
    prefissi = {}
    soggetti_libri = []          # (soggetto, dict)
    nomi = {}                    # soggetto -> nome (per le persone)
    corrente, dati, continua = None, None, False
    esempio = []
    RX_PREF = re.compile(r'@prefix\s+([\w-]*):\s*<([^>]+)>', re.I)
    RX_SOGG = re.compile(r'^\s*(<[^>]+>|[\w-]*:[^\s;,]+|_:\S+)\s+(.*)$')
    RX_LIT = re.compile(r'"((?:[^"\\]|\\.)*)"')

    def espandi(t):
        t = t.strip()
        if t.startswith('<') and t.endswith('>'):
            return t[1:-1]
        if ':' in t and not t.startswith('"'):
            p, _, loc = t.partition(':')
            if p in prefissi:
                return prefissi[p] + loc
        return t

    def chiudi():
        if corrente is None or dati is None:
            return
        if dati.get('nome') and (dati.get('persona') or not dati.get('isbn')):
            nomi[corrente] = dati['nome']
        if dati.get('isbn'):
            soggetti_libri.append((corrente, dati))

    def predicato_oggetto(testo):
        nonlocal dati
        testo = testo.strip()
        if not testo:
            return
        pred, _, ogg = testo.partition(' ')
        p = espandi(pred) if pred != 'a' else 'type'
        pl = p.lower()
        o = ogg.strip().rstrip(' ;,.').strip()
        if pl == 'type' or pl.endswith('rdf-syntax-ns#type'):
            ol = espandi(o).lower()
            if 'person' in ol:
                dati['persona'] = True
            if 'book' in ol:
                dati['libro'] = True
        elif pl.endswith('isbn') or pl.endswith('isbn13') or pl.endswith('isbn10'):
            for lit in RX_LIT.findall(o) or [o]:
                for i in isbn_da_testo(lit):
                    dati.setdefault('isbn', [])
                    if i not in dati['isbn']:
                        dati['isbn'].append(i)
        elif pl.endswith('/name') or pl.endswith('#title') or pl.endswith('/title') or pl.endswith('#label') or pl.endswith('/headline'):
            lit = RX_LIT.findall(o)
            if lit and 'nome' not in dati:
                dati['nome'] = lit[0]
        elif pl.endswith('/description') or pl.endswith('/abstract'):
            lit = RX_LIT.findall(o)
            if lit:
                dati['descr'] = max(dati.get('descr', 0), len(lit[0]))
        elif pl.endswith('/author') or pl.endswith('/creator') or pl.endswith('/contributor'):
            dati.setdefault('autori', []).append(espandi(o.split(',')[0].strip()))
        elif pl.endswith('/datepublished') or pl.endswith('/issued') or pl.endswith('/date'):
            m = re.search(r'1[5-9]\d\d|20\d\d', o)
            if m:
                dati.setdefault('anno', m.group(0))
        elif pl.endswith('/publisher'):
            lit = RX_LIT.findall(o)
            if lit:
                dati.setdefault('editore', lit[0])
        elif pl.endswith('/inlanguage') or pl.endswith('/language'):
            lit = RX_LIT.findall(o)
            dati.setdefault('lingua', (lit[0] if lit else espandi(o).rsplit('/', 1)[-1])[:3])

    for b in io.TextIOWrapper(flusso, encoding='utf-8', errors='replace'):
        if len(esempio) < 80:
            esempio.append(b.rstrip('\n')[:200])
        riga = b.rstrip('\n')
        st = riga.strip()
        if not st or st.startswith('#'):
            continue
        m = RX_PREF.search(st)
        if m:
            prefissi[m.group(1)] = m.group(2)
            continue
        if st.endswith('{'):
            continue
        if st == '}':
            chiudi(); corrente, dati, continua = None, None, False
            continue
        if not continua:
            chiudi()
            ms = RX_SOGG.match(st)
            if not ms:
                corrente, dati = None, None
                continue
            corrente, dati = espandi(ms.group(1)), {}
            stat['schede'] += 1
            resto = ms.group(2)
        else:
            resto = st
        if dati is not None:
            for pezzo in re.split(r'\s;\s*(?=\S)', resto):
                predicato_oggetto(pezzo)
        continua = not (st.endswith(' .') or st == '.' or st.endswith('}'))
    chiudi()
    stat['esempio_righe'] = esempio
    for sogg, d in soggetti_libri:
        autori = [nomi.get(a, '') for a in d.get('autori', [])]
        yield {'paese': paese, 'file': nome, 'id': sogg[-40:], 'isbn': '|'.join(d['isbn']), 'libro': 1, 'formato': 'RDF',
               'titolo': pulisci(d.get('nome', ''))[:300], 'autore': pulisci(next((a for a in autori if a), ''))[:120],
               'anno': d.get('anno', ''), 'lingua': d.get('lingua', ''), 'lingua_orig': '', 'editore': pulisci(d.get('editore', ''))[:120],
               'titolo_orig': '', 'sinossi_len': d.get('descr', 0), 'link_testo': 0, 'link_indice': 0}


# ---------------------------------------------------------------------------------------------------------------

def formato(testa):
    t = testa.lstrip(b'\xef\xbb\xbf\r\n\t ')
    if t.startswith(b'<?xml') or t.startswith(b'<') and (b'<record' in t[:20000] or b':record' in t[:20000] or b'<collection' in t[:20000]):
        return 'xml'
    if t.startswith(b'@prefix') or t.startswith(b'@base') or t.startswith(b'<http') or b'@prefix' in t[:5000]:
        return 'rdf'
    if t.startswith(b'=LDR'):
        return 'mrk'
    if re.match(rb'^\d{5}[a-z ]', t):
        return 'iso'
    if b'\t' in t.split(b'\n', 1)[0]:
        return 'tsv'
    return 'sconosciuto'


class Testa(io.RawIOBase):
    """Legge i primi byte per capire il formato, poi li restituisce a chi legge il flusso."""
    def __init__(self, f, n=65536):
        self.f = f
        self.inizio = f.read(n)
        self.buf = self.inizio

    def readable(self):
        return True

    def readinto(self, b):
        n = len(b)
        if self.buf:
            d, self.buf = self.buf[:n], self.buf[n:]
        else:
            d = self.f.read(n)
        b[:len(d)] = d
        return len(d)


def principale():
    paese, nome, uscita = sys.argv[1], sys.argv[2], sys.argv[3]
    percorso_zip = None
    if '--zip' in sys.argv:
        percorso_zip = sys.argv[sys.argv.index('--zip') + 1]
    os.makedirs(uscita, exist_ok=True)
    base = re.sub(r'[^\w.-]+', '_', f'{paese}_{nome}')
    stat = collections.Counter()
    info = {'paese': paese, 'file': nome}
    lingue, tipi, formati = collections.Counter(), collections.Counter(), collections.Counter()
    t0 = time.time()

    def flussi():
        if percorso_zip:
            with zipfile.ZipFile(percorso_zip) as z:
                for zi in z.infolist():
                    if zi.is_dir() or zi.file_size == 0:
                        continue
                    info.setdefault('file_nello_zip', []).append(zi.filename)
                    with z.open(zi) as f:
                        yield io.BufferedReader(f, 1 << 20)
        else:
            yield sys.stdin.buffer

    out_path = os.path.join(uscita, f'estratto_{base}.tsv.gz')
    with gzip.open(out_path, 'wt', encoding='utf-8', compresslevel=6, newline='') as out:
        w = csv.DictWriter(out, fieldnames=COLONNE, delimiter='\t', quoting=csv.QUOTE_MINIMAL, lineterminator='\n')
        w.writeheader()
        for f in flussi():
            testa = Testa(f)
            fm = formato(testa.inizio)
            t = io.BufferedReader(testa, 1 << 22)
            info.setdefault('formati', []).append(fm)
            st = {'schede': 0}
            if fm in ('iso', 'mrk', 'xml'):
                lettore = {'iso': leggi_iso2709, 'mrk': leggi_mrk, 'xml': leggi_xml}[fm](t)

                def gen():
                    for s in lettore:
                        st['schede'] += 1
                        tipi[s.leader[6:8] if len(s.leader) >= 8 else '??'] += 1
                        d = dati_scheda(s, paese, nome)
                        if d:
                            yield d
                righe = gen()
            elif fm == 'tsv':
                righe = righe_tsv(t, paese, nome, st)
            elif fm == 'rdf':
                righe = righe_rdf(t, paese, nome, st)
            else:
                info['errore'] = 'formato non riconosciuto'
                info['inizio_file'] = testa.inizio[:300].decode('utf-8', 'replace')
                righe = iter(())
            for d in righe:
                w.writerow(d)
                stat['con_isbn'] += 1
                stat['libri_con_isbn'] += d['libro']
                stat['isbn_valori'] += d['isbn'].count('|') + 1
                if d['sinossi_len'] >= 100:
                    stat['con_sinossi_catalogo'] += 1
                if d['link_testo']:
                    stat['con_link_testo_editore'] += 1
                if d['link_indice']:
                    stat['con_link_indice'] += 1
                if d['titolo_orig'] or d['lingua_orig']:
                    stat['con_titolo_o_lingua_originale'] += 1
                if d['autore']:
                    stat['con_autore'] += 1
                lingue[d['lingua'] or '?'] += 1
                formati[d['formato']] += 1
                if stat['con_isbn'] % 500000 == 0:
                    print(f'  {paese} {nome}: {st["schede"]:,} schede, {stat["con_isbn"]:,} con ISBN, {time.time()-t0:.0f} s', flush=True)
            stat['schede'] += st['schede']
            for k in ('intestazione', 'colonne_usate', 'esempio_righe'):
                if k in st:
                    info[k] = st[k]
    info.update(stat)
    info['tipi_scheda'] = dict(tipi.most_common(12))
    info['lingue'] = dict(lingue.most_common(15))
    info['formato_schede'] = dict(formati)
    info['secondi'] = round(time.time() - t0)
    with open(os.path.join(uscita, f'stat_{base}.json'), 'w', encoding='utf-8') as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in info.items() if k not in ('intestazione', 'esempio_righe')}, ensure_ascii=False), flush=True)
    if 'esempio_righe' in info:
        print('prime righe del file:\n' + '\n'.join(info['esempio_righe'][:40]))
    if 'intestazione' in info:
        print('intestazione:', info['intestazione'][:80])


if __name__ == '__main__':
    principale()
