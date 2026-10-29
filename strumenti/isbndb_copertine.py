#!/usr/bin/env python3
"""Copertine dei libri di ISBNdb, salvate nel Drive (DESTINAZIONE/copertine).

Condizioni di ISBNdb (lette il 27/10/2026): i dati si possono conservare solo finché l'abbonamento è attivo; se lo si
disdice vanno cancellati, copertine comprese.

I link delle copertine grandi ("image_original") scadono 2 ore dopo la richiesta a ISBNdb; quelli piccoli ("image",
circa 220 px) no. Per avere le grandi bisogna scaricarle subito. Due modi:
  - durante la raccolta (isbndb_raccolta.py usa la classe Copertine): ogni libro utile appena arrivato va in coda e
    viene scaricato in sottofondo, senza chiamate in più;
  - rinnovo (questo programma, --modo rinnova): per i libri già raccolti si chiedono di nuovo a ISBNdb 100 libri per
    chiamata, così i link sono freschi, e si scaricano subito. Usa la quota del giorno e si ferma alla riserva.
  - solo link (--modo link): i link già salvati nell'elenco, senza chiamate (le grandi di solito sono scadute).

Libri utili: in italiano, non ristampe di massa, non scolastici. Nel rinnovo: prima gli editori, poi gli
autopubblicati, dal più nuovo.
L'immagine deve aprirsi, essere larga almeno MIN_LARGHEZZA e non essere un segnaposto (la stessa immagine per tanti
libri). Si riduce a LARGHEZZA px di larghezza al massimo (JPEG) e va in un pacchetto .tar da PACCHETTO copertine.

Nel Drive:
  copertine/pacchetti/copertine_NNNNN.tar   le immagini, una per libro: <isbn13>.jpg
  copertine/indici/indice_NNNNN.tsv         isbn, esito, larghezza, altezza, byte, impronta, link usato
Un libro con la copertina grande (o larga almeno 400 px) non si rifà; quelle piccole sì, al rinnovo. Se nel Drive restano meno di MIN_LIBERO_GB ci si ferma.
Regole: etichetta neutra, al massimo PARALLELI scaricamenti insieme con una pausa, stop su errori ripetuti.
Segreti: RCLONE_CONF (Drive); ISBNDB_API_KEY solo per il rinnovo.
"""
import argparse
import collections
import gzip
import hashlib
import io
import json
import os
import queue
import re
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from isbndb_raccolta import DEST, RISERVA, Archivio, Isbndb, ora, senza_segreti  # noqa: E402
from isbndb_elenco import RISTAMPE, scheda as scheda_elenco  # noqa: E402

UA = 'Mozilla/5.0 (compatible; catalog-check/1.0)'
PARALLELI = int(os.environ.get('PARALLELI', '12'))
PAUSA = float(os.environ.get('PAUSA_IMMAGINI', '0.2'))   # secondi tra due scaricamenti sulla stessa linea
PACCHETTO = 2000
LARGHEZZA = 600
MIN_LARGHEZZA = 120
SEGNAPOSTO = 3                                            # la stessa immagine per 3 libri o più: segnaposto
MIN_LIBERO_GB = float(os.environ.get('MIN_LIBERO_GB', '3'))
ERRORI_DI_FILA = 200


class Fermati(Exception):
    pass


# ---------------------------------------------------------------- quali libri, quali link
def utile(x):
    """Scheda grezza di ISBNdb: in italiano, non ristampa di massa, non scolastica, ISBN valido."""
    s = scheda_elenco(x)
    return (bool(re.fullmatch(r'97[89]\d{10}', s['isbn13'])) and s['lingua_it'] and not s['scolastico']
            and not RISTAMPE.search(s['editore']))


def link_di(x):
    return [u for u in dict.fromkeys([x.get('image_original'), x.get('image')]) if u]


def isbn_di(x):
    return str(x.get('isbn13') or x.get('isbn') or '').replace('-', '').strip()


# ---------------------------------------------------------------- Drive
def spazio_libero_gb(arch):
    if arch.locale:
        return None
    try:
        d = json.loads(arch._rclone('about', arch.dest.split(':')[0] + ':', '--json') or '{}')
    except Exception as e:
        print('spazio nel Drive non letto:', senza_segreti(e), flush=True)
        return None
    return d['free'] / 1e9 if d.get('free') is not None else None


def elenca(arch, cartella):
    if arch.locale:
        p = os.path.join(arch.dest, cartella)
        return sorted(os.listdir(p)) if os.path.isdir(p) else []
    try:
        return sorted(x for x in arch._rclone('lsf', f'{arch.dest}/{cartella}', '--files-only').split('\n') if x)
    except RuntimeError:
        return []


def gia_fatti(arch):
    """Libri con la copertina già salvata (esito ok) e l'ultimo numero di pacchetto usato."""
    fatti, n = set(), 0
    for nome in elenca(arch, 'copertine/indici'):
        m = re.match(r'indice_(\d+)\.tsv$', nome)
        if not m:
            continue
        n = max(n, int(m.group(1)))
        for riga in arch.leggi(f'copertine/indici/{nome}').decode('utf-8').split('\n')[1:]:
            p = riga.split('\t')
            # fatta = copertina grande (link "original") o comunque larga almeno 400 px; le piccole si rifanno col rinnovo
            if len(p) > 6 and p[1] == 'ok' and ('/original/' in p[6] or int(p[2] or 0) >= 400):
                fatti.add(p[0])
    return fatti, n


def salva_pacchetto(arch, numero, risultati, impronte):
    nome = f'{numero:05d}'
    tar_b = io.BytesIO()
    righe = ['isbn\tesito\tlarghezza\taltezza\tbyte\timpronta\tlink']
    esiti = collections.Counter()
    with tarfile.open(fileobj=tar_b, mode='w') as t:
        for isbn, esito, w, h, jpeg, impronta, url in risultati:
            if esito == 'ok' and impronte[impronta] >= SEGNAPOSTO:
                esito, jpeg = 'segnaposto', None
            if esito == 'ok':
                info = tarfile.TarInfo(f'{isbn}.jpg')
                info.size = len(jpeg)
                info.mtime = int(time.time())
                t.addfile(info, io.BytesIO(jpeg))
            esiti[esito] += 1
            righe.append('\t'.join(str(x) for x in (isbn, esito, w, h, len(jpeg) if jpeg else 0, impronta, url.split('?')[0])))
    # prima il pacchetto, poi l'indice: un libro risulta fatto solo se la sua immagine è già nel Drive
    if esiti['ok']:
        arch.scrivi(f'copertine/pacchetti/copertine_{nome}.tar', tar_b.getvalue())
    arch.scrivi(f'copertine/indici/indice_{nome}.tsv', ('\n'.join(righe) + '\n').encode('utf-8'))
    return esiti, len(tar_b.getvalue()) if esiti['ok'] else 0


# ---------------------------------------------------------------- scaricamento
class Scaricatore:
    def __init__(self):
        self.lock = threading.Lock()
        self.errori_di_fila = 0
        self.ferma = threading.Event()
        self.motivo = ''

    def scarica(self, url):
        for prova in range(3):
            if self.ferma.is_set():
                return None, 'fermato'
            try:
                req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'image/*'})
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.read(), None
            except urllib.error.HTTPError as e:
                if e.code in (400, 401, 403, 404, 410):
                    return None, f'http_{e.code}'        # link scaduto o immagine assente: si prova l'altro link
                time.sleep((30 if e.code == 429 else 5) * (prova + 1))
            except Exception:
                time.sleep(5 * (prova + 1))
        return None, 'errore'

    def libro(self, isbn, link):
        """(isbn, esito, larghezza, altezza, jpeg o None, impronta, link usato); None se fermato."""
        ultimo = 'errore'
        for url in link:
            time.sleep(PAUSA)
            dati, err = self.scarica(url)
            if err == 'fermato':
                return None
            if dati is None:
                ultimo = err
                continue
            try:
                im = Image.open(io.BytesIO(dati))
                im.load()
            except Exception:
                ultimo = 'non_immagine'
                continue
            if im.width < MIN_LARGHEZZA:
                ultimo = 'troppo_piccola'
                continue
            impronta = hashlib.sha1(dati).hexdigest()[:16]
            if im.mode not in ('RGB', 'L'):
                im = im.convert('RGB')
            if im.width > LARGHEZZA:
                im = im.resize((LARGHEZZA, round(im.height * LARGHEZZA / im.width)), Image.LANCZOS)
            out = io.BytesIO()
            im.save(out, 'JPEG', quality=82, optimize=True, progressive=True)
            with self.lock:
                self.errori_di_fila = 0
            return isbn, 'ok', im.width, im.height, out.getvalue(), impronta, url
        with self.lock:
            if ultimo == 'errore':
                self.errori_di_fila += 1
                if self.errori_di_fila >= ERRORI_DI_FILA:
                    self.motivo = f'{ERRORI_DI_FILA} errori di rete di fila'
                    self.ferma.set()
        return isbn, ultimo, 0, 0, None, '', ''


class Copertine:
    """Scarica in sottofondo le copertine dei libri che riceve (offri) e le salva a pacchetti nel Drive."""

    def __init__(self, arch, paralleli=PARALLELI):
        self.arch = arch
        self.fatti, self.numero = gia_fatti(arch)
        self.visti = set()
        self.q = queue.Queue()
        self.s = Scaricatore()
        self.lock = threading.Lock()
        self.risultati = []
        self.impronte = collections.Counter()
        self.tot = collections.Counter()
        self.peso = 0
        self.spazio_ok = True
        libero = spazio_libero_gb(arch)
        if libero is not None:
            print(f'copertine: {libero:.1f} GB liberi nel Drive, {len(self.fatti):,} copertine già salvate', flush=True)
            self.spazio_ok = libero >= MIN_LIBERO_GB
        self.fili = [threading.Thread(target=self._lavora, daemon=True) for _ in range(paralleli)]
        for f in self.fili:
            f.start()

    def offri(self, isbn, link):
        if not self.spazio_ok or not isbn or not link or isbn in self.fatti or isbn in self.visti:
            return
        self.visti.add(isbn)
        self.q.put((isbn, link))

    def offri_scheda(self, x):
        try:
            if utile(x):
                self.offri(isbn_di(x), link_di(x))
        except Exception:
            pass

    def _lavora(self):
        while True:
            lavoro = self.q.get()
            try:
                if lavoro is None:
                    return
                if self.s.ferma.is_set():
                    continue
                r = self.s.libro(*lavoro)
                if not r:
                    continue
                pronti = None
                with self.lock:
                    self.risultati.append(r)
                    if r[1] == 'ok':
                        self.impronte[r[5]] += 1
                    if len(self.risultati) >= PACCHETTO:
                        pronti, self.risultati = self.risultati, []
                        self.numero += 1
                        numero = self.numero
                if pronti:
                    self._salva(numero, pronti)
            except Exception as e:
                print('copertine: errore', senza_segreti(e)[:200], flush=True)
            finally:
                self.q.task_done()

    def _salva(self, numero, pronti):
        esiti, byte = salva_pacchetto(self.arch, numero, pronti, self.impronte)
        with self.lock:
            self.tot.update(esiti)
            self.peso += byte
            for r in pronti:
                if r[1] == 'ok':
                    self.fatti.add(r[0])
        print(f'[{ora()}] copertine: pacchetto {numero}, {esiti["ok"]} su {len(pronti)} · in coda {self.q.qsize():,} · '
              f'totale {dict(self.tot)} · {self.peso / 1e6:.0f} MB', flush=True)
        if numero % 10 == 0:
            libero = spazio_libero_gb(self.arch)
            if libero is not None and libero < MIN_LIBERO_GB:
                self.spazio_ok = False
                self.s.motivo = f'restano {libero:.1f} GB liberi nel Drive'
                self.s.ferma.set()

    def in_coda(self):
        return self.q.unfinished_tasks

    def chiudi(self, scadenza):
        """Aspetta che la coda si svuoti (fino a scadenza, tempo monotonic), poi salva l'ultimo pacchetto."""
        while self.q.unfinished_tasks and time.monotonic() < scadenza and not self.s.ferma.is_set():
            time.sleep(2)
        rimasti = self.q.unfinished_tasks
        self.s.ferma.set()                     # i libri ancora in coda non si scaricano: li riprende il rinnovo
        for _ in self.fili:
            self.q.put(None)
        for f in self.fili:
            f.join(timeout=120)
        with self.lock:
            pronti, self.risultati = self.risultati, []
            if pronti:
                self.numero += 1
            numero = self.numero
        if pronti:
            self._salva(numero, pronti)
        return {'esiti': dict(self.tot), 'MB': round(self.peso / 1e6), 'non_fatti': rimasti,
                'nota': self.s.motivo or ('' if self.spazio_ok else 'poco spazio nel Drive')}


# ---------------------------------------------------------------- libri già raccolti
def da_fare(arch, fatti):
    """[(isbn, [link...])] dei libri utili dell'elenco senza copertina: prima editori, poi autopubblicati; dal più nuovo."""
    righe = gzip.decompress(arch.leggi('elenco/isbndb_italia.jsonl.gz')).decode('utf-8').split('\n')
    libri = []
    for r in righe:
        if not r.strip():
            continue
        s = json.loads(r)
        if not s.get('lingua_it') or s.get('ristampa') or s.get('scolastico') or s['isbn13'] in fatti:
            continue
        link = [u for u in dict.fromkeys([s.get('copertina'), s.get('copertina_base')]) if u]
        libri.append((bool(s.get('autopubblicato')), -(s.get('anno') or 0), s['isbn13'], link))
    libri.sort()
    return [(isbn, link) for _, _, isbn, link in libri]


def prova_rinnovo(isbns):
    """Prova del rinnovo su pochi ISBN: per ogni formato del corpo, stato HTTP, campi della risposta, libri trovati.
    Usa 2 chiamate. Non scrive niente e non stampa mai la chiave."""
    from isbndb_raccolta import API
    chiave = os.environ.get('ISBNDB_API_KEY', '').strip()
    for nome, dati, tipo in (('json', json.dumps({'isbns': isbns}).encode(), 'application/json'),
                             ('form', ('isbns=' + ','.join(isbns)).encode(), 'application/x-www-form-urlencoded')):
        req = urllib.request.Request(API + '/books', data=dati, headers={
            'Authorization': chiave, 'Accept': 'application/json', 'Content-Type': tipo, 'User-Agent': UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                stato, corpo = r.status, r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            stato, corpo = e.code, e.read().decode('utf-8', 'replace')
        try:
            d = json.loads(corpo)
            info = f'campi {sorted(d.keys()) if isinstance(d, dict) else type(d).__name__}'
            libri = (d.get('data') or d.get('books') or []) if isinstance(d, dict) else []
            info += f', libri {len(libri)}'
            if libri and isinstance(libri[0], dict):
                info += f', campi del libro {sorted(libri[0].keys())}'
        except Exception:
            info = 'risposta non JSON: ' + senza_segreti(corpo[:200])
        print(f'::notice title=prova rinnovo {nome}::HTTP {stato} · {info}', flush=True)


def prova_cerca(percorsi):
    """Prova di ricerche GET (es. /publisher/Mondadori?language=it&pageSize=50): stato, totale, lingue dei libri.
    Una chiamata per percorso. Non stampa mai la chiave."""
    from isbndb_raccolta import API
    chiave = os.environ.get('ISBNDB_API_KEY', '').strip()
    for percorso in percorsi:
        req = urllib.request.Request(API + percorso, headers={'Authorization': chiave, 'Accept': 'application/json', 'User-Agent': UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                stato, corpo = r.status, r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            stato, corpo = e.code, e.read().decode('utf-8', 'replace')
        try:
            d = json.loads(corpo)
            libri = d.get('books') or d.get('data') or []
            lingue = collections.Counter(str(x.get('language')) for x in libri if isinstance(x, dict))
            editori = collections.Counter(str(x.get('publisher')) for x in libri if isinstance(x, dict))
            info = f"totale {d.get('total')}, libri {len(libri)}, lingue {dict(lingue.most_common(5))}, editori {dict(editori.most_common(3))}"
            if isinstance(d.get('plan_limit'), dict):
                info = f"quota: {d['plan_limit']}"
        except Exception:
            info = 'risposta non JSON: ' + senza_segreti(corpo[:150])
        print(f'::notice title=prova ricerca::{percorso} · HTTP {stato} · {info}', flush=True)


def main():
    if len(sys.argv) > 2 and sys.argv[1] == '--prova-rinnovo':
        if sys.argv[2].startswith('/'):
            prova_cerca([x for x in sys.argv[2].split(' ') if x])
        else:
            prova_rinnovo([x for x in sys.argv[2].split(',') if x])
        return
    ap = argparse.ArgumentParser()
    ap.add_argument('--modo', choices=('rinnova', 'link'), default='rinnova')
    ap.add_argument('--minuti', type=float, default=320)
    ap.add_argument('--massimo', type=int, default=0, help='prova: al massimo questi libri')
    ap.add_argument('--lavoro', default=os.path.join(os.environ.get('RUNNER_TEMP', '/tmp'), 'copertine'))
    a = ap.parse_args()
    scadenza = time.monotonic() + a.minuti * 60
    arch = Archivio(DEST, a.lavoro)
    cop = Copertine(arch)
    if not cop.spazio_ok:
        print(f'::notice title=copertine::meno di {MIN_LIBERO_GB} GB liberi nel Drive: non scarico niente', flush=True)
        cop.chiudi(time.monotonic())
        return
    lista = da_fare(arch, cop.fatti)
    if a.massimo:
        lista = lista[:a.massimo]
    print(f'{len(lista):,} libri senza copertina, modo {a.modo}', flush=True)
    motivo, chiamate = '', 0
    api = None
    if a.modo == 'rinnova':
        chiave = os.environ.get('ISBNDB_API_KEY', '').strip()
        if not chiave:
            print('Manca il segreto ISBNDB_API_KEY: niente da fare.')
            cop.chiudi(time.monotonic())
            return
        api = Isbndb(chiave)
    quota = None
    riusciti = 0
    for i in range(0, len(lista), 100):
        if time.monotonic() > scadenza - 15 * 60:
            motivo = 'tempo del giro finito'
            break
        if cop.s.ferma.is_set():
            motivo = cop.s.motivo
            break
        while cop.in_coda() > 1500 and not cop.s.ferma.is_set():   # i link freschi si usano subito: non si va avanti troppo
            time.sleep(1)
        gruppo = lista[i:i + 100]
        nuovi = {}
        if api:
            if quota is None or api.chiamate - quota[1] >= 50:
                quota = (api.rimaste()[0], api.chiamate)
            if quota[0] - (api.chiamate - quota[1]) < RISERVA:
                motivo = f'chiamate ISBNdb di oggi quasi finite (riserva {RISERVA})'
                break
            try:
                nuovi = {isbn_di(x): link_di(x) for x in api.molti([g[0] for g in gruppo])}
                chiamate += 1
                if nuovi:
                    riusciti += 1
            except Exception as e:
                print('rinnovo non riuscito per questo gruppo:', senza_segreti(e)[:200], flush=True)
            # controllo di sicurezza: se le prime richieste non danno libri, ci si ferma invece di consumare la quota
            if i // 100 + 1 >= 3 and not riusciti:
                motivo = 'il rinnovo dei link non restituisce libri (prime 3 richieste): da controllare'
                break
        for isbn, vecchi in gruppo:
            cop.offri(isbn, nuovi.get(isbn) or vecchi)
    else:
        motivo = 'finito: tutti i libri provati'
    riepilogo = cop.chiudi(scadenza)
    fatti_ora = sum(riepilogo['esiti'].values())
    print(f'[{ora()}] fine giro: {motivo}. Chiamate ISBNdb: {chiamate}. Copertine: {riepilogo}', flush=True)
    print(f'::notice title=copertine::{riepilogo["esiti"].get("ok", 0)} copertine salvate ({riepilogo["MB"]} MB) su {fatti_ora} '
          f'provate · {chiamate} chiamate · {motivo}', flush=True)
    temp = os.environ.get('RUNNER_TEMP', a.lavoro)
    if a.massimo:
        return
    if motivo == 'tempo del giro finito':
        open(os.path.join(temp, 'continua'), 'w').close()
    elif 'quasi finite' in motivo:
        open(os.path.join(temp, 'riprova'), 'w').close()


if __name__ == '__main__':
    main()
