#!/usr/bin/env python3
"""File unico dei libri italiani: OPAC SBN + ISBNdb + selezione SBN (Open Library, Wikipedia), senza doppioni.

Legge dal Drive, senza chiamare nessun servizio esterno:
  - ISBNdb:  isbndb/grezzi/parte_*.jsonl.gz e isbndb/copertine/indici/*.tsv (copertine già scaricate)
  - OPAC:    schede_complete/schede_*.jsonl.gz (dati utili delle schede complete UNIMARC)
  - selezione: selezione_<data>/opere_*.jsonl, descrizioni_*.jsonl, wikipedia_*.jsonl, trame_wikipedia_*.jsonl

Come si tolgono i doppioni:
  1. stessa edizione = stesso ISBN-13 (gli ISBN a 10 cifre si convertono). Senza ISBN vale il codice SBN;
  2. stessa opera = edizioni legate da almeno uno di questi:
     - stessa opera nella selezione SBN (stesso autore e titolo, o stessa opera di Open Library, già unite lì);
     - "altri ISBN" della stessa scheda ISBNdb (ebook, tascabile...);
     - stesso titolo (senza sottotitolo) e stesso nome della prima autrice, scritto in qualunque ordine
       ("Rossi, Mario" = "Mario Rossi"), senza accenti né punteggiatura. Non vale senza autore, con "AA.VV." o
       con titoli di meno di 4 lettere.
  La sinossi non serve a riconoscere i libri (due edizioni dello stesso libro possono averla diversa).

Da ISBNdb entrano solo i libri "utili" come in isbndb_elenco.py: in italiano, non ristampe di massa, non scolastici.
Gli autopubblicati entrano, segnati.

Testi per la scheda (sinossi): presentazione OPAC (campo 330), sinossi ISBNdb, trama Wikipedia, descrizione Open
Library; vale un testo di almeno MIN_SINOSSI caratteri. Per ogni opera si tiene il migliore: prima quelli in italiano,
e tra questi la presentazione dell'editore (OPAC o ISBNdb, la più lunga), poi Wikipedia, poi Open Library.

Copertina dell'opera, la migliore tra le sue edizioni:
  grande      scaricata nel Drive, larga almeno 400 px (o link "original" di ISBNdb)
  piccola     scaricata nel Drive, più stretta
  link_isbndb non ancora scaricata, ma c'è il link piccolo di ISBNdb (non scade: si scarica gratis, senza chiamate)
  open_library c'è una copertina su Open Library (non scaricata)
  nessuna

Scrive nel Drive (unico/):
  opere_unite.jsonl.gz        tutte le opere, una per riga, con la sinossi migliore se c'è
  opere_con_sinossi.csv       solo le opere con sinossi, senza il testo, da aprire in Fogli/Excel
  riepilogo_unico.json        i numeri
Prova in locale: FONTI_LOCALI=<cartella con isbndb/, schede_complete/, selezione_x/> DESTINAZIONE_UNICO=<cartella>.
"""
import collections
import csv
import datetime
import glob
import gzip
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from isbndb_elenco import RISTAMPE, TETTO_EDITORE, scheda as scheda_isbndb, unisci as unisci_isbndb  # noqa: E402

ITALIA = os.environ.get('FONTI_LOCALI') or 'drive:Fabula App/cataloghi/Italia'
DESTINAZIONE = os.environ.get('DESTINAZIONE_UNICO') or f'{ITALIA}/unico'
LOCALE = ':' not in ITALIA
MIN_SINOSSI = 120
SENZA_AUTORE = {'aa vv', 'autori vari', 'vari', 'various', 'various authors', 'anonimo', 'anonymous', 'aavv', 'n d', 'nd',
                'autore', 'author', 'unknown', 'sconosciuto'}
RCLONE_PIANO = ['--transfers', '4', '--checkers', '8', '--tpslimit', '8', '--retries', '10', '--low-level-retries', '30']
IT = set('il lo la i gli le di da del della dei delle che e un una uno per non nel nella con sono è alla al si ma come '
         'questo questa suo sua loro anche più tra fra ha hanno era storia romanzo vita'.split())
EN = set('the and of to in is that with for his her was on it as by this from an are be at their who novel story life'.split())
ORDINE_FONTE = {'sbn': 0, 'isbndb': 0, 'wikipedia': 1, 'open_library': 2}
RANGO_COP = {'nessuna': 0, 'open_library': 1, 'link_isbndb': 2, 'piccola': 3, 'grande': 4}


def ora():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def rclone(*a):
    r = subprocess.run(['rclone', *a], capture_output=True, timeout=3 * 3600)
    if r.returncode:
        raise RuntimeError(f'rclone {a[0]}: ' + r.stderr.decode('utf-8', 'replace')[-400:])
    return r.stdout.decode('utf-8', 'replace')


# ---------------------------------------------------------------- testo
def piatto(s):
    """minuscolo, senza accenti né punteggiatura"""
    s = unicodedata.normalize('NFKD', str(s or '')).encode('ascii', 'ignore').decode('ascii').lower()
    return re.sub(r'[^a-z0-9]+', ' ', s).strip()


def isbn13(s):
    d = re.sub(r'[^0-9Xx]', '', str(s or '')).upper()
    if len(d) == 13 and d.isdigit() and d.startswith(('978', '979')):
        return d
    if len(d) == 10 and d[:9].isdigit():
        b = '978' + d[:9]
        return b + str((10 - sum(int(x) * (1 if i % 2 == 0 else 3) for i, x in enumerate(b)) % 10) % 10)
    return None


def chiave(titolo, autore):
    """titolo senza sottotitolo + nome della prima autrice in qualunque ordine; None se non basta per riconoscere l'opera"""
    t = piatto(re.split(r'[:.(\[]| - | / ', str(titolo or ''))[0])
    a = ' '.join(sorted(piatto(autore).split()))
    if len(t.replace(' ', '')) < 4 or not a or a in SENZA_AUTORE or len(a) < 3:
        return None
    return f'{t}|{a}'


def lingua_testo(t):
    parole = re.findall(r"[a-zàèéìòù']+", str(t or '').lower()[:2000])
    it = sum(p in IT for p in parole)
    en = sum(p in EN for p in parole)
    if it >= 4 and it >= en * 1.3:
        return 'it'
    if en >= 4 and en > it:
        return 'en'
    return 'altro'


def pulito(t):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', str(t or ''))).strip()


# ---------------------------------------------------------------- unione (union-find)
class Unione:
    def __init__(self):
        self.p = {}

    def trova(self, x):
        p = self.p
        if x not in p:
            p[x] = x
            return x
        r = x
        while p[r] != r:
            r = p[r]
        while p[x] != r:
            p[x], x = r, p[x]
        return r

    def unisci(self, *nodi):
        nodi = [n for n in nodi if n]
        if not nodi:
            return None
        r = self.trova(nodi[0])
        for n in nodi[1:]:
            q = self.trova(n)
            if q != r:
                self.p[q] = r
        return r


# ---------------------------------------------------------------- lettura
def scarica(lavoro):
    """Copia in locale i file che servono (una copia per cartella: meno richieste al Drive dei 'cat' uno per uno)."""
    if LOCALE:
        sel = sorted(d for d in os.listdir(ITALIA) if d.startswith('selezione_20'))
        return {'isbndb': os.path.join(ITALIA, 'isbndb/grezzi'), 'indici': os.path.join(ITALIA, 'isbndb/copertine/indici'),
                'opac': os.path.join(ITALIA, 'schede_complete'), 'selezione': os.path.join(ITALIA, sel[-1]) if sel else None}
    cartelle = {}
    sel = sorted(x.strip('/') for x in rclone('lsf', ITALIA, '--dirs-only').split() if x.startswith('selezione_20'))
    lavori = [('isbndb', f'{ITALIA}/isbndb/grezzi', ['--include', 'parte_*.jsonl.gz']),
              ('indici', f'{ITALIA}/isbndb/copertine/indici', ['--include', '*.tsv']),
              ('opac', f'{ITALIA}/schede_complete', ['--include', 'schede_*.jsonl.gz', '--max-depth', '1'])]
    if sel:
        lavori.append(('selezione', f'{ITALIA}/{sel[-1]}', ['--include', '*.jsonl', '--max-depth', '1']))
    for nome, da, filtro in lavori:
        a = os.path.join(lavoro, nome)
        os.makedirs(a, exist_ok=True)
        print(f'[{ora()}] copio {da}', flush=True)
        rclone('copy', da, a, *filtro, *RCLONE_PIANO)
        cartelle[nome] = a
        print(f'[{ora()}]   {len(os.listdir(a)):,} file', flush=True)
    cartelle.setdefault('selezione', None)
    return cartelle


def righe_gz(percorso):
    with gzip.open(percorso, 'rt', encoding='utf-8') as g:
        for l in g:
            if l.strip():
                yield json.loads(l)


def righe_jsonl(percorso):
    with open(percorso, encoding='utf-8') as f:
        for l in f:                      # i testi possono contenere separatori Unicode: si legge riga per riga del file
            if l.strip():
                yield json.loads(l)


def leggi_copertine(cartella):
    """isbn -> 'grande' | 'piccola' | 'assente' (come isbndb_elenco.stato_copertine)"""
    out = {}
    if not cartella or not os.path.isdir(cartella):
        return out
    for nome in sorted(os.listdir(cartella)):
        with open(os.path.join(cartella, nome), encoding='utf-8', errors='replace') as f:
            for riga in list(f)[1:]:
                p = riga.rstrip('\n').split('\t')
                if len(p) < 7:
                    continue
                if p[1] == 'ok':
                    v = 'grande' if ('/original/' in p[6] or int(p[2] or 0) >= 400) else 'piccola'
                else:
                    v = 'assente'
                if out.get(p[0]) != 'grande':
                    out[p[0]] = v
    return out


def leggi_isbndb(cartella, conta):
    """Libri utili di ISBNdb (in italiano, non scolastici, non ristampe), uno per ISBN."""
    libri = collections.OrderedDict()
    for nome in sorted(glob.glob(os.path.join(cartella, 'parte_*.jsonl.gz'))):
        for grezzo in righe_gz(nome):
            conta['isbndb_righe_grezze'] += 1
            s = scheda_isbndb(grezzo)
            if not re.fullmatch(r'97[89]\d{10}', s['isbn13']):
                continue
            conta['_isbndb_isbn'].add(s['isbn13'])
            if not s['lingua_it']:
                continue
            if s['scolastico']:
                conta['_isbndb_scolastici'].add(s['isbn13'])
                continue
            s = {k: s[k] for k in ('isbn13', 'titolo', 'autori', 'editore', 'anno', 'sinossi', 'copertina', 'copertina_base',
                                   'soggetti', 'altri_isbn', 'fonti', 'romance', 'autopubblicato')}
            s['soggetti'] = s['soggetti'][:8]
            libri[s['isbn13']] = unisci_isbndb(libri[s['isbn13']], s) if s['isbn13'] in libri else s
    per_editore = collections.Counter(s['editore'].lower() for s in libri.values())
    for isbn in list(libri):
        s = libri[isbn]
        if RISTAMPE.search(s['editore']) or (per_editore[s['editore'].lower()] > TETTO_EDITORE and not s['autopubblicato']):
            conta['isbndb_ristampe'] += 1
            del libri[isbn]
    conta['isbndb_libri_diversi'] = len(conta.pop('_isbndb_isbn'))
    conta['isbndb_scolastici'] = len(conta.pop('_isbndb_scolastici'))
    conta['isbndb_utili'] = len(libri)
    return libri


# ---------------------------------------------------------------- lavoro
def main():
    lavoro = tempfile.mkdtemp(prefix='unico_')
    c = scarica(lavoro)
    conta = collections.Counter()
    conta['_isbndb_isbn'], conta['_isbndb_scolastici'] = set(), set()
    u = Unione()
    # edizioni per fonte: nodo -> dati (nodo = 'i:<isbn13>' o 'c:<codice SBN>')
    ed_opac, ed_isbndb = {}, {}
    sel_opere = {}                                          # 's:<posizione>' -> dati dell'opera nella selezione

    # 1) OPAC
    print(f'[{ora()}] OPAC', flush=True)
    visti = set()
    for nome in sorted(glob.glob(os.path.join(c['opac'], 'schede_*.jsonl.gz'))):
        for x in righe_gz(nome):
            cod = x.get('codice') or x.get('codice_trovato')
            if not cod or cod in visti:
                continue
            visti.add(cod)
            isbn = list(dict.fromkeys(i for i in (isbn13(v) for v in (x.get('isbn') or [])) if i))
            nodi = [f'i:{i}' for i in isbn] + [f'c:{cod}']
            autori = [a for a in (x.get('autori') or []) if a.get('campo') in ('700', '701')] or \
                     [a for a in (x.get('autori') or []) if a.get('campo') in ('702',) and a.get('ruolo') in (None, '070')]
            autore = (autori[0]['nome'] if autori else '') or ''
            k = chiave(x.get('titolo'), autore)
            u.unisci(*nodi, f'k:{k}' if k else None, f"s:{x['opera']}" if x.get('opera') else None)
            sin = pulito(x.get('sinossi'))
            ed_opac[nodi[0]] = (x.get('titolo'), autore, x.get('editore'), x.get('anno'), sin if len(sin) >= MIN_SINOSSI else '',
                                x.get('n_biblioteche') or 0, ','.join(x.get('lingua_originale') or []), (x.get('dewey') or [''])[0],
                                tuple(isbn))
    conta['opac_schede'] = len(visti)
    conta['opac_con_presentazione'] = sum(1 for v in ed_opac.values() if v[4])
    del visti

    # 2) ISBNdb
    print(f'[{ora()}] ISBNdb', flush=True)
    libri = leggi_isbndb(c['isbndb'], conta)
    for isbn, s in libri.items():
        nodo = f'i:{isbn}'
        k = chiave(s['titolo'], (s['autori'] or [''])[0])
        altri = [f'i:{i}' for i in (isbn13(v) for v in (s.get('altri_isbn') or [])) if i and i != isbn]
        u.unisci(nodo, f'k:{k}' if k else None, *altri)
        ed_isbndb[nodo] = s
    del libri
    cop = leggi_copertine(c['indici'])
    conta['copertine_scaricate'] = sum(1 for v in cop.values() if v != 'assente')

    # 3) selezione SBN (Open Library, Wikipedia)
    print(f'[{ora()}] selezione', flush=True)
    trame = {}
    if c['selezione']:
        for nome in sorted(os.listdir(c['selezione'])):
            if not re.match(r'(opere|descrizioni|wikipedia|trame_wikipedia)_\d+\.jsonl$', nome):
                continue
            for o in righe_jsonl(os.path.join(c['selezione'], nome)):
                if o.get('posizione') is None:
                    continue
                s_ = f"s:{o['posizione']}"
                nodi = [s_]
                for e in o.get('edizioni') or []:
                    i = isbn13(e.get('isbn'))
                    nodi += [f'i:{i}' if i else None, f"c:{e['codice_sbn']}" if e.get('codice_sbn') else None]
                k = chiave(o.get('titolo'), o.get('autore'))
                u.unisci(*nodi, f'k:{k}' if k else None)
                d = sel_opere.setdefault(s_, {'titolo': o.get('titolo'), 'autore': o.get('autore'), 'ol': None, 'cop_ol': None})
                if o.get('descrizione_ol') and len(pulito(o['descrizione_ol'])) >= MIN_SINOSSI:
                    d['ol'] = pulito(o['descrizione_ol'])
                if o.get('copertina_ol'):
                    d['cop_ol'] = o['copertina_ol']
                if o.get('trama_wikipedia') and len(pulito(o['trama_wikipedia'])) >= MIN_SINOSSI:
                    trame[s_] = pulito(o['trama_wikipedia'])
    conta['selezione_opere'] = len(sel_opere)

    # 4) opere = gruppi dell'unione
    print(f'[{ora()}] unisco ({len(u.p):,} nodi)', flush=True)
    opere = collections.defaultdict(lambda: {'isbn': set(), 'codici': 0, 'fonti': set(), 'titoli': collections.Counter(),
                                             'autori': collections.Counter(), 'editori': collections.Counter(), 'anni': [],
                                             'testi': [], 'cop': 'nessuna', 'bibl': 0, 'lingua_orig': '', 'dewey': '',
                                             'romance': False, 'autopub': [], 'soggetti': []})
    isbn_opac = set()
    for nodo, v in ed_opac.items():
        o = opere[u.trova(nodo)]
        titolo, autore, editore, anno, sin, bibl, lo, dewey, isbn = v
        o['fonti'].add('sbn'); o['codici'] += 1
        o['isbn'].update(isbn); isbn_opac.update(isbn)
        if titolo:
            o['titoli'][titolo] += 2                            # il titolo delle biblioteche vale di più
        if autore:
            o['autori'][autore] += 2
        if editore:
            o['editori'][editore] += 1
        a = re.search(r'\d{4}', str(anno or ''))
        if a:
            o['anni'].append(int(a.group()))
        if sin:
            o['testi'].append(('sbn', sin))
        o['bibl'] = max(o['bibl'], int(bibl or 0))
        o['lingua_orig'] = o['lingua_orig'] or lo
        o['dewey'] = o['dewey'] or dewey
        for i in isbn:
            st = cop.get(i)
            if st and st != 'assente' and RANGO_COP[st] > RANGO_COP[o['cop']]:
                o['cop'] = st
    del ed_opac
    for nodo, s in ed_isbndb.items():
        o = opere[u.trova(nodo)]
        o['fonti'].add('isbndb')
        o['isbn'].add(s['isbn13'])
        o['titoli'][s['titolo']] += 1
        if s['autori']:
            o['autori'][s['autori'][0]] += 1
        if s['editore']:
            o['editori'][s['editore']] += 1
        if s['anno']:
            o['anni'].append(int(s['anno']))
        if len(s['sinossi']) >= MIN_SINOSSI:
            o['testi'].append(('isbndb', s['sinossi']))
        o['romance'] |= bool(s['romance'])
        o['autopub'].append(bool(s['autopubblicato']))
        if not o['soggetti']:
            o['soggetti'] = s['soggetti'][:5]
        st = cop.get(s['isbn13'])
        if st is None and (s.get('copertina_base') or s.get('copertina')):
            st = 'link_isbndb'
        if st and st != 'assente' and RANGO_COP[st] > RANGO_COP[o['cop']]:
            o['cop'] = st
    conta['isbn_in_opac_e_isbndb'] = sum(1 for n in ed_isbndb if n[2:] in isbn_opac)
    del ed_isbndb, isbn_opac
    for s_, d in sel_opere.items():
        r = u.trova(s_)
        if r not in opere:          # opera della selezione senza edizioni scaricate: non conta come libro nuovo
            conta['selezione_senza_edizioni'] += 1
            continue
        o = opere[r]
        o['fonti'].add('selezione')
        if d['titolo']:
            o['titoli'][d['titolo']] += 1
        if d['autore']:
            o['autori'][d['autore']] += 1
        if d['ol']:
            o['testi'].append(('open_library', d['ol']))
        if s_ in trame:
            o['testi'].append(('wikipedia', trame[s_]))
        if d['cop_ol'] and RANGO_COP['open_library'] > RANGO_COP[o['cop']]:
            o['cop'] = 'open_library'
    del sel_opere, trame, u

    # 5) scrittura e conti
    print(f'[{ora()}] scrivo ({len(opere):,} opere)', flush=True)
    os.makedirs(os.path.join(lavoro, 'out'), exist_ok=True)
    f_jsonl = os.path.join(lavoro, 'out', 'opere_unite.jsonl.gz')
    f_csv = os.path.join(lavoro, 'out', 'opere_con_sinossi.csv')
    R = collections.Counter()
    per_fonti = collections.Counter()
    cop_sin, cop_sin_it = collections.Counter(), collections.Counter()
    testo_da, solo_da = collections.Counter(), collections.Counter()
    campi_csv = ['opera', 'titolo', 'autore', 'editore', 'anno', 'isbn', 'n_isbn', 'fonti', 'sinossi_fonte', 'sinossi_lingua',
                 'testi_disponibili', 'copertina', 'romance', 'autopubblicata', 'biblioteche', 'lingua_originale', 'dewey']
    with gzip.open(f_jsonl, 'wt', encoding='utf-8') as gj, open(f_csv, 'w', encoding='utf-8-sig', newline='') as fc:
        w = csv.DictWriter(fc, fieldnames=campi_csv)
        w.writeheader()
        for n, o in enumerate(sorted(opere.values(), key=lambda o: (-(max(o['anni']) if o['anni'] else 0), -o['bibl'])), 1):
            fonti = sorted(o['fonti'])
            per_fonti['+'.join(f for f in fonti if f != 'selezione') or 'solo_selezione'] += 1
            testi = [(f, t, lingua_testo(t)) for f, t in o['testi']]
            fonti_testo = sorted({f for f, _, _ in testi})
            migliore = min(testi, key=lambda x: (x[2] != 'it', ORDINE_FONTE[x[0]], -len(x[1]))) if testi else None
            autopub = bool(o['autopub']) and all(o['autopub']) and 'sbn' not in o['fonti']
            isbn = sorted(o['isbn'])
            r = {
                'opera': n, 'titolo': o['titoli'].most_common(1)[0][0] if o['titoli'] else None,
                'autore': o['autori'].most_common(1)[0][0] if o['autori'] else None,
                'editore': o['editori'].most_common(1)[0][0] if o['editori'] else None,
                'anno': max(o['anni']) if o['anni'] else None, 'anno_primo': min(o['anni']) if o['anni'] else None,
                'isbn': isbn[:30], 'n_isbn': len(isbn), 'codici_sbn': o['codici'], 'fonti': fonti,
                'sinossi': migliore[1] if migliore else None, 'sinossi_fonte': migliore[0] if migliore else None,
                'sinossi_lingua': migliore[2] if migliore else None, 'testi_disponibili': fonti_testo,
                'copertina': o['cop'], 'romance': o['romance'], 'autopubblicata': autopub, 'biblioteche': o['bibl'],
                'lingua_originale': o['lingua_orig'] or None, 'dewey': o['dewey'] or None, 'soggetti_isbndb': o['soggetti'],
            }
            gj.write(json.dumps(r, ensure_ascii=False) + '\n')
            R['opere'] += 1
            R['edizioni_isbn'] += len(isbn)
            if not migliore:
                continue
            R['con_sinossi'] += 1
            R['con_sinossi_italiana'] += migliore[2] == 'it'
            R['con_sinossi_autopubblicate'] += autopub
            R['con_sinossi_romance'] += o['romance']
            cop_sin[o['cop']] += 1
            if migliore[2] == 'it':
                cop_sin_it[o['cop']] += 1
            for f in fonti_testo:
                testo_da[f] += 1
            if len(fonti_testo) == 1:
                solo_da[fonti_testo[0]] += 1
            w.writerow({**{k: r[k] for k in campi_csv if k in r}, 'isbn': (isbn or [''])[0], 'fonti': ', '.join(fonti),
                        'testi_disponibili': ', '.join(fonti_testo), 'romance': 'sì' if o['romance'] else '',
                        'autopubblicata': 'sì' if autopub else ''})

    def scaricabili(cc):
        return {'grande_scaricata': cc['grande'], 'piccola_scaricata': cc['piccola'],
                'gia_scaricate': cc['grande'] + cc['piccola'],
                'link_isbndb_da_scaricare_gratis': cc['link_isbndb'], 'solo_open_library': cc['open_library'],
                'nessuna': cc['nessuna']}

    riepilogo = {
        'aggiornato': ora(),
        'regole': 'stessa edizione = stesso ISBN-13; stessa opera = stessa opera nella selezione SBN, altri ISBN di ISBNdb, '
                  f'o stesso titolo senza sottotitolo + stesso nome della prima autrice. Sinossi = testo di almeno {MIN_SINOSSI} caratteri. '
                  'Da ISBNdb solo libri in italiano, non scolastici, non ristampe di massa.',
        'fonti': {
            'opac_schede': conta['opac_schede'], 'opac_con_presentazione': conta['opac_con_presentazione'],
            'isbndb_righe_grezze': conta['isbndb_righe_grezze'], 'isbndb_libri_diversi': conta['isbndb_libri_diversi'],
            'isbndb_utili_in_italiano': conta['isbndb_utili'], 'isbndb_scolastici_tolti': conta['isbndb_scolastici'],
            'isbndb_ristampe_tolte': conta['isbndb_ristampe'], 'isbn_sia_in_opac_sia_in_isbndb': conta['isbn_in_opac_e_isbndb'],
            'selezione_opere': conta['selezione_opere'], 'copertine_isbndb_scaricate': conta['copertine_scaricate'],
        },
        'totale': {'opere': R['opere'], 'edizioni_con_isbn': R['edizioni_isbn'], 'opere_per_fonte': dict(per_fonti.most_common())},
        'con_sinossi': {
            'opere': R['con_sinossi'], 'in_italiano': R['con_sinossi_italiana'],
            'di_editori': R['con_sinossi'] - R['con_sinossi_autopubblicate'], 'autopubblicate': R['con_sinossi_autopubblicate'],
            'romance': R['con_sinossi_romance'],
            'testo_da': dict(testo_da.most_common()), 'testo_solo_da': dict(solo_da.most_common()),
        },
        'con_sinossi_e_copertina': scaricabili(cop_sin),
        'con_sinossi_italiana_e_copertina': scaricabili(cop_sin_it),
    }
    testo = json.dumps(riepilogo, ensure_ascii=False, indent=1)
    with open(os.path.join(lavoro, 'out', 'riepilogo_unico.json'), 'w', encoding='utf-8') as f:
        f.write(testo + '\n')
    if ':' in DESTINAZIONE:
        rclone('copy', os.path.join(lavoro, 'out'), DESTINAZIONE, *RCLONE_PIANO)
    else:
        os.makedirs(DESTINAZIONE, exist_ok=True)
        for nome in os.listdir(os.path.join(lavoro, 'out')):
            os.replace(os.path.join(lavoro, 'out', nome), os.path.join(DESTINAZIONE, nome))
    print(testo, flush=True)
    cs = riepilogo['con_sinossi']
    print(f"::notice title=File unico::opere {R['opere']:,} · con sinossi {cs['opere']:,} (in italiano {cs['in_italiano']:,}) · "
          f"con sinossi e copertina già scaricata {riepilogo['con_sinossi_e_copertina']['gia_scaricate']:,}", flush=True)


if __name__ == '__main__':
    main()
