#!/usr/bin/env python3
"""Raccolta da ISBNdb dei libri in italiano per il catalogo di Fabula. Non scrive nel database dell'app: tutto nel Drive.

Cosa raccoglie, sempre dal più recente al più vecchio:
  1. romance: tutti i libri in italiano con "romantasy", "new adult" o "romance" tra i soggetti
     (romance anno per anno dal 2027 al 1990, poi tutto il più vecchio insieme), autopubblicati compresi;
  2. novità dal 2024: tutti i libri in italiano con data di uscita 2024–2027;
  3. editori dal 2024: per ogni editore visto nei passi precedenti (autopubblicati esclusi), tutti i suoi libri dal 2024;
  4. a ritroso, un anno alla volta (2023, 2022, ... fino ad ANNO_MINIMO): novità dell'anno, poi gli editori per quell'anno.
ISBNdb dà al massimo 10.000 risultati per ricerca: se una ricerca ne ha di più, il periodo si divide a metà finché basta.
Ogni chiamata dà 100 libri; il piano Basic ha 5.000 chiamate al giorno, condivise con gli altri lavori: ci si ferma quando
ne restano meno di RISERVA e si riprende al giro successivo.

Nel Drive (cartella DESTINAZIONE):
  stato.json                lavori fatti e da fare, editori visti, conteggi: ogni giro riprende da qui
  grezzi/parte_*.jsonl.gz   i libri come li dà ISBNdb, più "_fase" e "_ricerca" (quale ricerca li ha trovati)
Segreti: ISBNDB_API_KEY (chiave ISBNdb), RCLONE_CONF (Drive). La chiave non viene mai scritta nei registri.
"""
import argparse
import datetime as dt
import gzip
import json
import math
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get('ISBNDB_API', 'https://api2.isbndb.com')
UA = 'Mozilla/5.0 (compatible; catalog-check/1.0)'
DEST = os.environ.get('DESTINAZIONE', 'drive:Fabula App/cataloghi/Italia/isbndb')
TETTO = 10000          # risultati massimi per ricerca
PAGINA = 100           # libri per chiamata
RISERVA = int(os.environ.get('RISERVA', '300'))
ANNO_MINIMO = int(os.environ.get('ANNO_MINIMO', '2000'))
MIN_EDITORE = int(os.environ.get('MIN_EDITORE', '2'))     # libri visti perché un editore abbia le sue ricerche
PAUSA = float(os.environ.get('PAUSA', '1.1'))            # piano Basic: una chiamata al secondo
FINE = '2030-12-31'
# 28/10: le ricerche per editore rendono poco (4.700 chiamate per 25.000 libri utili, contro circa 45 per chiamata delle
# novità anno per anno). Con EDITORI=no si saltano e si va dritti alle novità degli anni precedenti; le ricerche per
# editore già in coda si mettono da parte (stato["editori_rimandati"]) per dopo.
# 29/10 (Paolo: "voglio recuperare i libri usciti nel 2026-2025-2024"): con EDITORI=recenti si rimettono in testa alla
# coda le ricerche per editore dal 2024 messe da parte (ora solo in italiano, language=ita); per gli anni prima del 2024
# le ricerche per editore restano saltate.
MODO_EDITORI = os.environ.get('EDITORI', 'si').lower()
SALTA_EDITORI = MODO_EDITORI in ('no', 'recenti')
# dopo il 2024, a ritroso per blocchi di anni (dal, al)
BLOCCHI = [(2020, 2023), (2015, 2019), (2010, 2014), (2000, 2009), (1900, 1999)]
INIZIO = '1000-01-01'

AUTOPUBBLICATI = re.compile(
    r'independently published|kdp|amazon digital|createspace|youcanprint|ilmiolibro|il mio libro|lulu|streetlib|'
    r'books on demand|bookabook|narcissus|self.?publish|draft2digital|smashwords|babelcube|tektime|pubme|'
    r'pubblicato in proprio|autopubblica|edizione privata|\[s\.n\.\]', re.I)


class Fermati(Exception):
    """Fine ordinata del giro (quota finita, tempo scaduto, chiave non valida)."""


class RicercaRifiutata(Exception):
    """ISBNdb rifiuta una singola ricerca (es. HTTP 400 per un nome strano): si segna e si va avanti."""


def senza_segreti(testo):
    t = str(testo)
    t = re.sub(r'ya29\.[\w\-.]+', 'ya29.[tolto]', t)
    t = re.sub(r'1//[\w\-]+', '1//[tolto]', t)
    t = re.sub(r'[A-Za-z0-9_\-]{32,}', '[tolto]', t)
    return t


def norm(s):
    return re.sub(r'[^a-z0-9]+', ' ', str(s or '').lower()).strip()


def autopubblicato(libro):
    ed = str(libro.get('publisher') or '').strip()
    if not ed or AUTOPUBBLICATI.search(ed):
        return True
    autori = {norm(a) for a in (libro.get('authors') or [])}
    return norm(ed) in autori      # editore = nome dell'autrice: autopubblicato


def ora():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---------------------------------------------------------------- ISBNdb
class Isbndb:
    def __init__(self, chiave, pausa=PAUSA):
        self.chiave = chiave
        self.pausa = pausa
        self.ultima = 0.0
        self.chiamate = 0           # chiamate che contano per la quota (non /key)

    def _get(self, percorso, parametri=None, conta=True, corpo=None, corpo_json=None):
        url = API + percorso + ('?' + urllib.parse.urlencode(parametri) if parametri else '')
        dati_post = urllib.parse.urlencode(corpo).encode('utf-8') if corpo else None
        intestazioni = {'Authorization': self.chiave, 'Accept': 'application/json', 'User-Agent': UA}
        if corpo_json is not None:
            dati_post = json.dumps(corpo_json).encode('utf-8')
            intestazioni['Content-Type'] = 'application/json'
        errore = 'errori ripetuti'
        for prova in range(6):
            attesa = self.pausa - (time.monotonic() - self.ultima)
            if attesa > 0:
                time.sleep(attesa)
            self.ultima = time.monotonic()
            req = urllib.request.Request(url, data=dati_post, headers=intestazioni)
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    dati = json.loads(r.read().decode('utf-8', 'replace') or '{}')
                if conta:
                    self.chiamate += 1
                return dati
            except urllib.error.HTTPError as e:
                if conta:
                    self.chiamate += 1
                if e.code == 404:                       # nessun risultato
                    return {}
                if e.code in (401, 403):
                    raise Fermati(f'ISBNdb rifiuta la chiave (HTTP {e.code}): segreto ISBNDB_API_KEY sbagliato o piano scaduto')
                if e.code == 429:
                    time.sleep(5 * (prova + 1))
                    continue
                if e.code >= 500:
                    errore = f'HTTP {e.code}'
                    time.sleep(10 * (prova + 1))
                    continue
                raise RicercaRifiutata(f'HTTP {e.code}')
            except (urllib.error.URLError, TimeoutError, ValueError, ConnectionError) as e:
                errore = type(e).__name__
                time.sleep(10 * (prova + 1))
        raise RuntimeError('ISBNdb non risponde (' + senza_segreti(errore) + ')')

    def rimaste(self):
        d = self._get('/key', conta=False)
        lim = d.get('plan_limit') or {}
        return int(lim.get('left', 0)), int(lim.get('total', 0))

    formato_molti = None      # 'json' o 'form': quello che ISBNdb accetta, scoperto alla prima chiamata riuscita

    def molti(self, isbns):
        """Fino a 100 ISBN in una chiamata (piano Basic): le schede di nuovo, con i link delle copertine appena creati.
        28/10: la prima prova (corpo "isbns=..." come modulo) non ha dato libri; si prova anche il corpo JSON."""
        formati = [self.formato_molti] if self.formato_molti else ['json', 'form']
        for f in formati:
            if f == 'json':
                d = self._get('/books', corpo_json={'isbns': list(isbns)})
            else:
                d = self._get('/books', corpo={'isbns': ','.join(isbns)})
            libri = [x for x in (d.get('data') or d.get('books') or []) if isinstance(x, dict)]
            if libri:
                self.formato_molti = f
                return libri
        return []

    def pagina(self, lavoro, n):
        q = urllib.parse.quote(lavoro['q'], safe='')
        p = {'page': n, 'pageSize': PAGINA, 'publishedFrom': lavoro['da'], 'publishedTo': lavoro['a']}
        if lavoro['tipo'] == 'books':
            percorso = f'/books/{q}'
            p['language'] = 'it'
            p['column'] = lavoro['col']
        else:
            percorso = f'/publisher/{q}'
            # 28/10: senza lingua la ricerca per editore dà tutti i libri del mondo con quel nome (HarperCollins: 10.000
            # in inglese); qui il filtro funziona solo col codice a tre lettere ("it" dà zero risultati)
            p['language'] = 'ita'
        d = self._get(percorso, p)
        libri = d.get('books') or d.get('data') or []
        totale = d.get('total')
        try:
            totale = int(totale)
        except (TypeError, ValueError):
            totale = len(libri)
        return totale, [x for x in libri if isinstance(x, dict)]


# ---------------------------------------------------------------- Drive (o cartella locale per le prove)
class Archivio:
    def __init__(self, dest, lavoro):
        self.dest = dest.rstrip('/')
        self.locale = ':' not in self.dest
        self.lavoro = lavoro
        os.makedirs(lavoro, exist_ok=True)

    def _rclone(self, *a, binario=False):
        r = subprocess.run(['rclone', *a], capture_output=True, timeout=1800)
        if r.returncode:
            raise RuntimeError('rclone ' + a[0] + ': ' + senza_segreti(r.stderr.decode('utf-8', 'replace')[-300:]))
        return r.stdout if binario else r.stdout.decode('utf-8', 'replace')

    def esiste(self, nome):
        if self.locale:
            return os.path.exists(os.path.join(self.dest, nome))
        cartella, _, file = nome.rpartition('/')
        try:
            elenco = self._rclone('lsf', self.dest + ('/' + cartella if cartella else ''), '--files-only')
        except RuntimeError:
            return False
        return file in elenco.split('\n')

    def leggi(self, nome):
        if self.locale:
            with open(os.path.join(self.dest, nome), 'rb') as f:
                return f.read()
        return self._rclone('cat', f'{self.dest}/{nome}', binario=True)

    def scrivi(self, nome, dati):
        locale = os.path.join(self.lavoro, nome.replace('/', '__'))
        with open(locale, 'wb') as f:
            f.write(dati)
        if self.locale:
            os.makedirs(os.path.dirname(os.path.join(self.dest, nome)), exist_ok=True)
            os.replace(locale, os.path.join(self.dest, nome))
        else:
            self._rclone('copyto', locale, f'{self.dest}/{nome}')
            os.remove(locale)


# ---------------------------------------------------------------- lavori
def lavoro(fase, tipo, q, da, a, col=None):
    return {'id': f'{fase}|{tipo}|{q}|{col or ""}|{da}|{a}', 'fase': fase, 'tipo': tipo, 'q': q, 'col': col, 'da': da, 'a': a}


def dividi(l):
    d0, d1 = dt.date.fromisoformat(l['da']), dt.date.fromisoformat(l['a'])
    if d1 <= d0:
        return None
    meta = d0 + (d1 - d0) // 2
    return [lavoro(l['fase'], l['tipo'], l['q'], l['da'], meta.isoformat(), l['col']),
            lavoro(l['fase'], l['tipo'], l['q'], (meta + dt.timedelta(days=1)).isoformat(), l['a'], l['col'])]


def lavori_editori(stato, fase, da, a):
    nomi = sorted((n for n, c in stato['editori'].items() if c >= MIN_EDITORE), key=lambda n: -stato['editori'][n])
    return [x for x in (lavoro(fase, 'publisher', n, da, a) for n in nomi) if x['id'] not in stato['fatti']]


def prossima_fase(stato):
    """Quando la coda è vuota: aggiunge i lavori del passo successivo. Restituisce False se non c'è più niente da fare."""
    f = stato['fase']
    nuovi = []
    if f == 'inizio':
        nuovi += [lavoro('romance', 'books', s, INIZIO, FINE, 'subjects') for s in ('romantasy', 'new adult')]
        nuovi.append(lavoro('romance', 'books', 'romance', '2027-01-01', FINE, 'subjects'))
        nuovi += [lavoro('romance', 'books', 'romance', f'{y}-01-01', f'{y}-12-31', 'subjects') for y in range(2026, 1989, -1)]
        nuovi.append(lavoro('romance', 'books', 'romance', INIZIO, '1989-12-31', 'subjects'))
        stato['fase'] = 'romance'
    elif f == 'romance':
        nuovi.append(lavoro('novita', 'books', '2027', '2027-01-01', FINE, 'date_published'))
        nuovi += [lavoro('novita', 'books', str(y), f'{y}-01-01', f'{y}-12-31', 'date_published') for y in (2026, 2025, 2024)]
        stato['fase'] = 'novita_recenti'
    elif f == 'novita_recenti':
        stato['fase'] = 'editori_recenti'
        if SALTA_EDITORI:
            return prossima_fase(stato)
        nuovi = lavori_editori(stato, 'editori', '2024-01-01', FINE)
    elif f == 'editori_recenti' or f.startswith('blocco:'):
        # a ritroso per blocchi di anni: prima le novità anno per anno, poi una ricerca per editore su tutto il blocco
        # (per editore anno per anno costerebbe una chiamata per ogni editore e ogni anno, anche quando non c'è niente)
        blocchi = [b for b in BLOCCHI if b[1] >= ANNO_MINIMO]
        if f == 'editori_recenti':
            i, passo = -1, 'editori'
        else:
            _, i, passo = f.split(':')
            i = int(i)
        if passo == 'novita' and not SALTA_EDITORI:
            dal, al = blocchi[i]
            nuovi = lavori_editori(stato, 'editori', f'{max(dal, ANNO_MINIMO)}-01-01', f'{al}-12-31')
            stato['fase'] = f'blocco:{i}:editori'
        else:
            i += 1
            if i >= len(blocchi):
                stato['fase'] = 'finito'
                return False
            dal, al = blocchi[i]
            nuovi = [lavoro('novita', 'books', str(y), f'{y}-01-01', f'{y}-12-31', 'date_published')
                     for y in range(al, max(dal, ANNO_MINIMO) - 1, -1)]
            stato['fase'] = f'blocco:{i}:novita'
    else:
        return False
    stato['coda'] += [x for x in nuovi if x['id'] not in stato['fatti']]
    print(f'[{ora()}] passo "{stato["fase"]}": {len(nuovi)} ricerche', flush=True)
    return True


def stato_nuovo():
    return {'versione': 1, 'creato': ora(), 'aggiornato': ora(), 'fase': 'inizio', 'coda': [], 'fatti': {},
            'editori': {}, 'conteggi': {}, 'parti': 0, 'chiamate_totali': 0, 'giri': 0}


# ---------------------------------------------------------------- giro
class Raccolta:
    def __init__(self, api, archivio, minuti, salva_ogni=20000, salva_minuti=10):
        self.api, self.arch = api, archivio
        self.scadenza = time.monotonic() + minuti * 60
        self.salva_ogni, self.salva_minuti = salva_ogni, salva_minuti
        self.buffer, self.ultimo_salvataggio = [], time.monotonic()
        self.stato = None
        self.parti_iniziali = 0
        self.quota = None
        self.copertine = None      # isbndb_copertine.Copertine: scarica le copertine mentre i link sono validi (2 ore)

    def carica(self):
        self.stato = json.loads(self.arch.leggi('stato.json')) if self.arch.esiste('stato.json') else stato_nuovo()
        self.stato['giri'] += 1
        self.parti_iniziali = self.stato.get('parti', 0)
        if MODO_EDITORI == 'recenti':
            rimandati = self.stato.get('editori_rimandati') or []
            recenti = [l for l in rimandati if l.get('da', '') >= '2024-01-01']
            if recenti:
                ids = {l['id'] for l in recenti}
                self.stato['coda'] = recenti + [l for l in self.stato['coda'] if l['id'] not in ids]
                self.stato['editori_rimandati'] = [l for l in rimandati if l['id'] not in ids]
                print(f'[{ora()}] {len(recenti)} ricerche per editore dal 2024 rimesse in testa alla coda (solo italiano)', flush=True)
        if SALTA_EDITORI:
            da_parte = [l for l in self.stato['coda'] if l.get('fase') == 'editori'
                        and not (MODO_EDITORI == 'recenti' and l.get('da', '') >= '2024-01-01')]
            if da_parte:
                self.stato.setdefault('editori_rimandati', []).extend(da_parte)
                self.stato['coda'] = [l for l in self.stato['coda'] if l.get('fase') != 'editori']
                print(f'[{ora()}] {len(da_parte)} ricerche per editore messe da parte (EDITORI=no)', flush=True)

    def salva(self):
        if self.buffer:
            self.stato['parti'] += 1
            nome = f'grezzi/parte_{self.stato["parti"]:05d}.jsonl.gz'
            dati = gzip.compress(''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in self.buffer).encode('utf-8'))
            self.arch.scrivi(nome, dati)
            print(f'[{ora()}] salvati {len(self.buffer)} libri in {nome}', flush=True)
            self.buffer = []
        self.stato['aggiornato'] = ora()
        self.arch.scrivi('stato.json', json.dumps(self.stato, ensure_ascii=False).encode('utf-8'))
        self.ultimo_salvataggio = time.monotonic()

    def controlla(self):
        if time.monotonic() > self.scadenza:
            raise Fermati('tempo del giro finito')
        if self.quota is None or self.api.chiamate - self.quota[1] >= 50:
            rimaste, _ = self.api.rimaste()
            self.quota = (rimaste, self.api.chiamate)
        rimaste = self.quota[0] - (self.api.chiamate - self.quota[1])
        if rimaste < RISERVA:
            raise Fermati(f'chiamate ISBNdb di oggi quasi finite (ne restano {rimaste}, riserva {RISERVA})')

    def aggiungi(self, l, libri):
        for x in libri:
            x['_fase'], x['_ricerca'] = l['fase'], l['id']
            ed = str(x.get('publisher') or '').strip()
            if ed and not autopubblicato(x) and str(x.get('language', '')).lower() in ('ita', 'it', 'italian', 'italiano'):
                self.stato['editori'][ed] = self.stato['editori'].get(ed, 0) + 1
        self.buffer += libri
        if self.copertine:
            for x in libri:
                self.copertine.offri_scheda(x)
        self.stato['conteggi'][l['fase']] = self.stato['conteggi'].get(l['fase'], 0) + len(libri)

    def esegui(self, l):
        self.controlla()
        try:
            totale, libri = self.api.pagina(l, 1)
        except RicercaRifiutata as e:
            self.stato['fatti'][l['id']] = [0, 0, f'rifiutata: {e}']
            return
        if totale >= TETTO:
            figli = dividi(l)
            if figli:
                self.stato['coda'][0:0] = figli
                self.stato['fatti'][l['id']] = [totale, 0, 'diviso']
                return
            print(f'  {l["q"]} {l["da"]}: oltre {TETTO} risultati in un giorno solo, si prendono i primi {TETTO}', flush=True)
        pagine = min(math.ceil(totale / PAGINA), TETTO // PAGINA)
        presi = list(libri)
        for n in range(2, pagine + 1):
            self.controlla()
            try:
                _, altri = self.api.pagina(l, n)
            except RicercaRifiutata:
                break
            if not altri:
                break
            presi += altri
        self.aggiungi(l, presi)
        self.stato['fatti'][l['id']] = [totale, len(presi)]
        if totale:
            print(f'  {l["fase"]:8} {l["tipo"]:9} {l["q"][:40]:40} {l["da"]}…{l["a"]}: {len(presi)}/{totale}', flush=True)

    def giro(self):
        self.carica()
        motivo = ''
        try:
            while True:
                if not self.stato['coda'] and not prossima_fase(self.stato):
                    motivo = 'finito: tutte le ricerche fatte'
                    break
                l = self.stato['coda'][0]
                if l['id'] in self.stato['fatti']:
                    self.stato['coda'].pop(0)
                    continue
                self.esegui(l)
                if self.stato['coda'] and self.stato['coda'][0] is l:
                    self.stato['coda'].pop(0)
                elif l in self.stato['coda']:
                    self.stato['coda'].remove(l)
                if len(self.buffer) >= self.salva_ogni or time.monotonic() - self.ultimo_salvataggio > self.salva_minuti * 60:
                    self.salva()
        except Fermati as e:
            motivo = str(e)
        finally:
            self.stato['chiamate_totali'] += self.api.chiamate
            self.salva()
        print(f'[{ora()}] fine giro: {motivo}. Chiamate: {self.api.chiamate}. Passo: {self.stato["fase"]}, '
              f'ricerche in coda: {len(self.stato["coda"])}, libri per fase: {self.stato["conteggi"]}', flush=True)
        return motivo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--minuti', type=float, default=330)
    ap.add_argument('--lavoro', default=os.path.join(os.environ.get('RUNNER_TEMP', '/tmp'), 'isbndb'))
    a = ap.parse_args()
    chiave = os.environ.get('ISBNDB_API_KEY', '').strip()
    if not chiave:
        print('Manca il segreto ISBNDB_API_KEY: niente da fare.')
        return
    archivio = Archivio(DEST, a.lavoro)
    r = Raccolta(Isbndb(chiave), archivio, a.minuti)
    if os.environ.get('COPERTINE', 'si') != 'no':
        # 27/10: le copertine grandi hanno link che scadono dopo 2 ore: si scaricano subito, mentre la raccolta va avanti
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from isbndb_copertine import Copertine
        r.copertine = Copertine(archivio)
    motivo = r.giro()
    if r.stato and r.stato.get('parti', 0) > r.parti_iniziali:
        open(os.path.join(a.lavoro, 'nuovi'), 'w').close()      # il workflow rifà l'elenco pulito solo se serve
    if r.copertine:
        print(f'[{ora()}] copertine: finisco quelle in coda', flush=True)
        print(f'[{ora()}] copertine: {r.copertine.chiudi(r.scadenza + 15 * 60)}', flush=True)
    # tempo finito con lavoro ancora da fare e quota disponibile: il workflow fa partire subito un altro giro;
    # quota del giorno finita: il workflow riprova tra un'ora (non si conta sui tentativi programmati, che GitHub salta)
    if motivo == 'tempo del giro finito':
        open(os.path.join(a.lavoro, 'continua'), 'w').close()
    elif 'quasi finite' in motivo:
        open(os.path.join(a.lavoro, 'riprova'), 'w').close()


if __name__ == '__main__':
    main()
