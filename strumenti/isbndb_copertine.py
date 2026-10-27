#!/usr/bin/env python3
"""Copertine dei libri raccolti da ISBNdb, scaricate dai link dell'elenco e salvate nel Drive.

Condizioni di ISBNdb (lette il 27/10/2026): i dati si possono conservare solo finché l'abbonamento è attivo; se lo si
disdice vanno cancellati, copertine comprese.

Quali libri: quelli utili dell'elenco pulito (isbndb_elenco.py): in italiano, non ristampe di massa, non scolastici.
Prima quelli di editori, poi gli autopubblicati; dentro ogni gruppo dal più nuovo.
Per ogni libro si prova il link grande ("image_original", che può essere scaduto) e poi quello normale ("image").
L'immagine deve aprirsi, essere larga almeno MIN_LARGHEZZA e non essere un segnaposto (la stessa immagine per tanti
libri). Si riduce a LARGHEZZA px di larghezza al massimo (JPEG) e va in un pacchetto .tar da PACCHETTO copertine.

Nel Drive (DESTINAZIONE/copertine):
  pacchetti/copertine_NNNNN.tar   le immagini, una per libro: <isbn13>.jpg
  indici/indice_NNNNN.tsv         isbn, esito, larghezza, altezza, byte, impronta, link usato
Ogni giro riprende da quello che risulta negli indici. Prima di iniziare, e a ogni pacchetto, controlla lo spazio
libero nel Drive e si ferma sotto MIN_LIBERO_GB.
Regole: etichetta neutra, al massimo PARALLELI scaricamenti insieme con una pausa, stop su errori ripetuti.
Segreto: RCLONE_CONF (Drive). Non serve la chiave di ISBNdb: i link delle copertine sono pubblici.
"""
import argparse
import collections
import concurrent.futures as cf
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from isbndb_raccolta import DEST, Archivio, ora, senza_segreti  # noqa: E402

UA = 'Mozilla/5.0 (compatible; catalog-check/1.0)'
PARALLELI = int(os.environ.get('PARALLELI', '8'))
PAUSA = float(os.environ.get('PAUSA', '0.25'))        # secondi tra due scaricamenti sulla stessa linea
PACCHETTO = 2000
LARGHEZZA = 600
MIN_LARGHEZZA = 120
SEGNAPOSTO = 3                                         # la stessa immagine per 3 libri o più: segnaposto
MIN_LIBERO_GB = float(os.environ.get('MIN_LIBERO_GB', '3'))
ERRORI_DI_FILA = 200


class Fermati(Exception):
    pass


def spazio_libero_gb(arch):
    if arch.locale:
        return None
    try:
        d = json.loads(arch._rclone('about', arch.dest.split(':')[0] + ':', '--json') or '{}')
    except Exception as e:
        print('spazio nel Drive non letto:', senza_segreti(e), flush=True)
        return None
    if d.get('free') is None:
        return None
    return d['free'] / 1e9


def elenca(arch, cartella):
    if arch.locale:
        p = os.path.join(arch.dest, cartella)
        return sorted(os.listdir(p)) if os.path.isdir(p) else []
    try:
        return sorted(x for x in arch._rclone('lsf', f'{arch.dest}/{cartella}', '--files-only').split('\n') if x)
    except RuntimeError:
        return []


def gia_fatti(arch):
    fatti, n = set(), 0
    for nome in elenca(arch, 'copertine/indici'):
        m = re.match(r'indice_(\d+)\.tsv$', nome)
        if not m:
            continue
        n = max(n, int(m.group(1)))
        for riga in arch.leggi(f'copertine/indici/{nome}').decode('utf-8').split('\n')[1:]:
            p = riga.split('\t')
            if p and p[0]:
                fatti.add(p[0])
    return fatti, n


def da_fare(arch, fatti):
    """[(isbn, [link...])] nell'ordine: prima editori, poi autopubblicati; dal più nuovo."""
    righe = gzip.decompress(arch.leggi('elenco/isbndb_italia.jsonl.gz')).decode('utf-8').split('\n')
    libri = []
    for r in righe:
        if not r.strip():
            continue
        s = json.loads(r)
        if not s.get('lingua_it') or s.get('ristampa') or s.get('scolastico') or s['isbn13'] in fatti:
            continue
        link = [u for u in dict.fromkeys([s.get('copertina'), s.get('copertina_base')]) if u]
        if link:
            libri.append((bool(s.get('autopubblicato')), -(s.get('anno') or 0), s['isbn13'], link))
    libri.sort()
    return [(isbn, link) for _, _, isbn, link in libri]


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
                if e.code == 429:
                    time.sleep(30 * (prova + 1))
                    continue
                time.sleep(5 * (prova + 1))
            except Exception:
                time.sleep(5 * (prova + 1))
        return None, 'errore'

    def libro(self, isbn, link):
        """(isbn, esito, larghezza, altezza, jpeg o None, impronta, link usato)"""
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
                    self.motivo = f'{ERRORI_DI_FILA} errori di rete di fila: mi fermo'
                    self.ferma.set()
        return isbn, ultimo, 0, 0, None, '', ''


def salva_pacchetto(arch, numero, risultati, impronte):
    nome = f'{numero:05d}'
    tar_b = io.BytesIO()
    righe = ['isbn\tesito\tlarghezza\taltezza\tbyte\timpronta\tlink']
    n_ok = 0
    with tarfile.open(fileobj=tar_b, mode='w') as t:
        for isbn, esito, w, h, jpeg, impronta, url in risultati:
            if esito == 'ok' and impronte[impronta] >= SEGNAPOSTO:
                esito, jpeg = 'segnaposto', None
            if esito == 'ok':
                info = tarfile.TarInfo(f'{isbn}.jpg')
                info.size = len(jpeg)
                info.mtime = int(time.time())
                t.addfile(info, io.BytesIO(jpeg))
                n_ok += 1
            righe.append('\t'.join(str(x) for x in (isbn, esito, w, h, len(jpeg) if jpeg else 0, impronta, url)))
    # prima il pacchetto, poi l'indice: un libro risulta fatto solo se la sua immagine è già nel Drive
    if n_ok:
        arch.scrivi(f'copertine/pacchetti/copertine_{nome}.tar', tar_b.getvalue())
    arch.scrivi(f'copertine/indici/indice_{nome}.tsv', ('\n'.join(righe) + '\n').encode('utf-8'))
    return n_ok, len(tar_b.getvalue())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--minuti', type=float, default=320)
    ap.add_argument('--massimo', type=int, default=0, help='prova: al massimo questi libri')
    ap.add_argument('--lavoro', default=os.path.join(os.environ.get('RUNNER_TEMP', '/tmp'), 'copertine'))
    a = ap.parse_args()
    scadenza = time.monotonic() + a.minuti * 60
    arch = Archivio(DEST, a.lavoro)
    tot = collections.Counter()
    motivo, rimasti = '', 0
    libero = spazio_libero_gb(arch)
    if libero is not None:
        print(f'spazio libero nel Drive: {libero:.1f} GB', flush=True)
        if libero < MIN_LIBERO_GB:
            print(f'::notice title=copertine::meno di {MIN_LIBERO_GB} GB liberi nel Drive: non scarico niente', flush=True)
            return
    fatti, numero = gia_fatti(arch)
    lista = da_fare(arch, fatti)
    if a.massimo:
        lista = lista[:a.massimo]
    print(f'{len(fatti):,} libri già fatti, {len(lista):,} da fare', flush=True)
    s = Scaricatore()
    impronte = collections.Counter()
    peso = 0
    try:
        with cf.ThreadPoolExecutor(PARALLELI) as ex:
            for i in range(0, len(lista), PACCHETTO):
                if time.monotonic() > scadenza - 600:
                    motivo = 'tempo del giro finito'
                    break
                gruppo = lista[i:i + PACCHETTO]
                risultati = [r for r in ex.map(lambda x: s.libro(*x), gruppo) if r]
                for r in risultati:
                    if r[1] == 'ok':
                        impronte[r[5]] += 1
                numero += 1
                n_ok, byte = salva_pacchetto(arch, numero, risultati, impronte)
                peso += byte
                for r in risultati:
                    tot[r[1]] += 1
                tot['segnaposto'] += sum(1 for r in risultati if r[1] == 'ok') - n_ok
                tot['ok'] -= sum(1 for r in risultati if r[1] == 'ok') - n_ok
                print(f'[{ora()}] pacchetto {numero}: {n_ok} copertine su {len(gruppo)} · totale {dict(tot)} · '
                      f'{peso / 1e6:.0f} MB', flush=True)
                if s.ferma.is_set():
                    raise Fermati(s.motivo)
                if numero % 10 == 0:
                    libero = spazio_libero_gb(arch)
                    if libero is not None and libero < MIN_LIBERO_GB:
                        raise Fermati(f'restano {libero:.1f} GB liberi nel Drive: mi fermo')
            else:
                motivo = 'finito: tutte le copertine provate'
    except Fermati as e:
        motivo = str(e)
    rimasti = max(0, len(lista) - sum(tot.values()))
    print(f'[{ora()}] fine giro: {motivo}. Esiti: {dict(tot)}. Rimasti: {rimasti:,}. Peso: {peso / 1e6:.0f} MB', flush=True)
    print(f'::notice title=copertine::{tot["ok"]} copertine salvate ({peso / 1e6:.0f} MB) · rimasti {rimasti:,} · {motivo}', flush=True)
    if motivo == 'tempo del giro finito' and not a.massimo:
        open(os.path.join(os.environ.get('RUNNER_TEMP', '/tmp'), 'continua'), 'w').close()


if __name__ == '__main__':
    main()
