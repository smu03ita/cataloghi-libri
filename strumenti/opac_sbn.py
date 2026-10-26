#!/usr/bin/env python3
"""Schede complete dall'OPAC SBN (UNIMARC via Z39.50), una richiesta alla volta, di giorno. Tutto nel Drive.

Gira ogni ora (.github/workflows/opac-sbn.yml) e riprende sempre da dove era arrivato:
  1. l'ordine dei libri viene dalla selezione nel Drive (cataloghi/Italia/selezione_<data>): prima l'edizione più
     recente di ogni opera scelta, poi le altre edizioni delle opere scelte (saltando le opere che hanno già una
     presentazione), poi tutti gli altri libri dal più nuovo. L'ordine si calcola una volta e si salva nel Drive
     (schede_complete/coda_<selezione>.tsv.gz);
  2. cosa è già fatto sta nel Drive (schede_complete/esiti/*.tsv.gz: codice, esito, presentazione sì/no, opera);
  3. per ogni codice: ricerca per codice SBN (se non trova, per ISBN) e scheda UNIMARC completa;
  4. ogni 2.000 libri: schede grezze (.mrc.gz) e dati utili (.jsonl.gz) nel Drive, poi il file degli esiti;
  5. ogni giro lascia un riepilogo in schede_complete/giri/ (si vedono gli orari in cui il server risponde).
Regole: una connessione e una richiesta alla volta, pausa tra le richieste, etichetta neutra, stop su errori ripetuti.
Segreto: RCLONE_CONF (Drive).
"""
import argparse
import collections
import datetime
import gzip
import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time

from pymarc import MARCReader

HOST = 'tcp:opac.sbn.it:2100/nopac'
DRIVE_ITALIA = 'drive:Fabula App/cataloghi/Italia'
CARTELLA = f'{DRIVE_ITALIA}/schede_complete'
PAUSA = 0.35            # secondi tra una richiesta e l'altra
LOTTO = 200             # libri per sessione (alla fine della sessione le schede vengono lette e salvate)
PARTE = 2000            # libri per file nel Drive

def isbn13(s):
    d = re.sub(r'[^0-9Xx]', '', s or '').upper()
    if len(d) == 13 and d.isdigit():
        return d
    if len(d) == 10 and d[:9].isdigit():   # la X può essere solo l'ultima cifra
        b = '978' + d[:9]
        return b + str((10 - sum(int(x) * (1 if i % 2 == 0 else 3) for i, x in enumerate(b)) % 10) % 10)
    return None


# ---------------------------------------------------------------- lettura delle schede UNIMARC
def _sf(r, tag, *codici):
    out = []
    for f in r.get_fields(tag):
        try:
            out += [v.strip() for v in f.get_subfields(*codici) if v and v.strip()]
        except Exception:
            pass
    return out


def scheda(r):
    c = r.get_fields('001')
    codice = c[0].data.strip() if c else None
    autori = []
    for tag in ('700', '701', '702', '710', '711', '712'):
        for f in r.get_fields(tag):
            nome = ' '.join(f.get_subfields('a', 'b')).replace(' ,', ',').strip()
            if nome:
                autori.append({'nome': re.sub(r'\s+', ' ', nome), 'ruolo': (f.get_subfields('4') or [None])[0], 'campo': tag})
    collana = None
    for f in r.get_fields('410', '225'):
        a = f.get_subfields('a')
        if a:
            v = f.get_subfields('v')
            collana = a[0].strip() + (f' ; {v[0].strip()}' if v else '')
            break
    soggetti = [' - '.join(x.strip() for x in f.get_subfields('a', 'x', 'y', 'z') if x.strip()) for f in r.get_fields('606', '607', '608')]
    biblioteche = {(f.get_subfields('1') or [''])[0] for f in r.get_fields('899')}
    return {
        'codice': codice,
        'isbn': [i for i in (isbn13(x) for x in _sf(r, '010', 'a')) if i],
        'titolo': (_sf(r, '200', 'a') or [None])[0],
        'complemento': ' : '.join(_sf(r, '200', 'e')) or None,
        'responsabilita': ' ; '.join(_sf(r, '200', 'f', 'g')) or None,
        'edizione': (_sf(r, '205', 'a') or [None])[0],
        'luogo': ((_sf(r, '210', 'a') + _sf(r, '214', 'a')) or [None])[0],
        'editore': ((_sf(r, '210', 'c') + _sf(r, '214', 'c')) or [None])[0],
        'anno': ((_sf(r, '210', 'd') + _sf(r, '214', 'd')) or [None])[0],
        'pagine': (_sf(r, '215', 'a') or [None])[0],
        'collana': collana,
        'lingua': _sf(r, '101', 'a'),
        'lingua_originale': _sf(r, '101', 'c'),
        'paese': (_sf(r, '102', 'a') or [None])[0],
        'dewey': _sf(r, '676', 'a'),
        'soggetti': [s for s in soggetti if s][:20],
        'sinossi': '\n'.join(_sf(r, '330', 'a'))[:6000] or None,
        'titolo_uniforme': (_sf(r, '500', 'a') or [None])[0],
        'autori': autori[:20],
        'n_biblioteche': len(biblioteche - {''}) if biblioteche else 0,
        'note': _sf(r, '300', 'a')[:10],
    }


def leggi_marc(dati):
    out = []
    for r in MARCReader(io.BytesIO(dati), to_unicode=True, force_utf8=True, utf8_handling='replace', permissive=True):
        if r is not None:
            out.append(r)
    return out


# ---------------------------------------------------------------- sessione Z39.50 (yaz-client)
class Fermati(Exception):
    pass


class Sessione:
    """Una connessione yaz-client; ogni comando aspetta la risposta del server ("Elapsed:") prima del successivo."""

    def __init__(self, file_marc, comando='yaz-client'):
        self.p = subprocess.Popen([comando, '-m', file_marc], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, bufsize=1, encoding='utf-8', errors='replace')
        self.q = queue.Queue()
        threading.Thread(target=self._leggi, daemon=True).start()
        try:
            righe = self.cmd(f'open {HOST}', attesa=40)
        except TimeoutError:
            self.chiudi()
            raise Fermati('il server non risponde alla connessione (di notte è normale)')
        if not any('Connection accepted' in x for x in righe):
            self.chiudi()
            raise Fermati('connessione non accettata: ' + ' | '.join(righe[-3:]))
        self.p.stdin.write('format unimarc\n'); self.p.stdin.flush()
        time.sleep(0.2)

    def _leggi(self):
        for riga in self.p.stdout:
            self.q.put(riga.rstrip('\n'))
        self.q.put(None)

    def cmd(self, c, attesa=30):
        self.p.stdin.write(c + '\n'); self.p.stdin.flush()
        righe, t0 = [], time.time()
        while time.time() - t0 < attesa:
            try:
                x = self.q.get(timeout=1)
            except queue.Empty:
                continue
            if x is None:
                break
            righe.append(x)
            if x.startswith('Elapsed:') or 'Elapsed:' in x:
                return righe
        raise TimeoutError(f'nessuna risposta a "{c}" in {attesa} s')

    def hits(self, query):
        righe = self.cmd(f'find {query}')
        for x in righe:
            m = re.search(r'Number of hits:\s*(\d+)', x)
            if m:
                return int(m.group(1))
        return 0

    def chiudi(self):
        try:
            self.p.stdin.write('quit\n'); self.p.stdin.flush()
            self.p.wait(timeout=20)
        except Exception:
            self.p.kill()


def scarica_lotto(codici, file_marc, comando='yaz-client'):
    """Restituisce {codice: 'fatta'|'non_trovata'|'errore'} e i byte delle schede UNIMARC del lotto."""
    if os.path.exists(file_marc):
        os.remove(file_marc)
    esiti, timeout_di_fila = {}, 0
    s = Sessione(file_marc, comando)
    try:
        for c in codici:
            codice, isbn = c['codice'], c.get('isbn')
            if not codice.startswith('IT\\ICCU\\'):
                esiti[codice] = 'non_trovata'; continue
            breve = codice.replace('IT\\ICCU\\', '').replace('\\', '')
            try:
                n = s.hits(f'@attr 1=1032 {breve}')
                time.sleep(PAUSA)
                if n == 0 and isbn:
                    n = s.hits(f'@attr 1=7 {isbn}')
                    time.sleep(PAUSA)
                if n == 0:
                    esiti[codice] = 'non_trovata'; continue
                s.cmd('show 1')
                esiti[codice] = 'presa'
                timeout_di_fila = 0
                time.sleep(PAUSA)
            except TimeoutError:
                esiti[codice] = 'errore'
                timeout_di_fila += 1
                if timeout_di_fila >= 3:
                    raise Fermati('il server non risponde (3 volte di fila)')
    finally:
        s.chiudi()   # alla chiusura yaz-client scrive tutte le schede nel file
    dati = open(file_marc, 'rb').read() if os.path.exists(file_marc) else b''
    return esiti, dati



# ---------------------------------------------------------------- Drive
def senza_segreti(testo):
    # mai scrivere nei registri pezzi di permessi o chiavi (es. righe di rclone.conf negli errori)
    t = str(testo)
    t = re.sub(r'ya29\.[\w\-.]+', 'ya29.[tolto]', t)
    t = re.sub(r'1//[\w\-]+', '1//[tolto]', t)
    t = re.sub(r'"(access_token|refresh_token|client_secret|token)"\s*:\s*"[^"]*"', r'"\1":"[tolto]"', t)
    t = re.sub(r'[A-Za-z0-9_\-]{40,}', '[tolto]', t)
    return t


def rclone(*args, binario=False):
    r = subprocess.run(['rclone', *args], capture_output=True, timeout=1800)
    if r.returncode != 0:
        errore = r.stderr.decode('utf-8', 'replace')
        if 'could not parse line' in errore or 'Failed to load config' in errore:
            raise RuntimeError('rclone non riesce a leggere il segreto RCLONE_CONF: va incollato tutto, '
                               'copiato con "rclone config show drive | pbcopy" (una riga per voce)')
        raise RuntimeError('rclone ' + ' '.join(args[:1]) + ': ' + senza_segreti(errore[-300:]))
    return r.stdout if binario else r.stdout.decode('utf-8', 'replace')


def selezione_pronta():
    """La cartella della selezione nel Drive, solo se completa (riepilogo presente e nessun file scritto negli ultimi 15 minuti)."""
    cartelle = sorted(x.strip('/') for x in rclone('lsf', DRIVE_ITALIA, '--dirs-only').split())
    # SBN_SELEZIONE_DA: le selezioni più vecchie di questa non si usano (es. dopo un cambio delle regole)
    sel = [c for c in cartelle if c.startswith('selezione_20') and c >= os.environ.get('SBN_SELEZIONE_DA', '')]
    if not sel:
        return None
    file = json.loads(rclone('lsjson', f'{DRIVE_ITALIA}/{sel[-1]}', '--files-only') or '[]')
    nomi = {f.get('Name') for f in file}
    if 'riepilogo.json' not in nomi or 'codici_sbn_dal_piu_nuovo.csv.gz' not in nomi:
        return None
    adesso = datetime.datetime.now(datetime.timezone.utc)
    for f in file:
        m = re.match(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?$', f.get('ModTime') or '')
        if not m:
            continue
        t = datetime.datetime.fromisoformat(m.group(1) + ('+00:00' if m.group(2) in (None, 'Z') else m.group(2)))
        if adesso - t < datetime.timedelta(minutes=15):
            return None
    return f'{DRIVE_ITALIA}/{sel[-1]}'


# ---------------------------------------------------------------- ordine dei libri e cose già fatte

def elenca(percorso):
    try:
        return [x for x in rclone('lsf', percorso, '--files-only').split('\n') if x]
    except RuntimeError as e:
        if 'not found' in str(e).lower():
            return []
        raise


def coda_ordinata(lavoro):
    """[(codice, isbn, opera)] nell'ordine in cui vanno scaricati. Calcolata una volta per selezione e salvata nel Drive."""
    sel = selezione_pronta()
    if not sel:
        raise Fermati('nel Drive non c\'è una selezione completa (cataloghi/Italia/selezione_<data>)')
    nome_sel = sel.rsplit('/', 1)[-1]
    nome_coda = f'coda_{nome_sel}.tsv.gz'
    if nome_coda in elenca(CARTELLA):
        testo = gzip.decompress(rclone('cat', f'{CARTELLA}/{nome_coda}', binario=True)).decode('utf-8')
        righe = [tuple(x.split('\t')) for x in testo.split('\n')[1:] if x]
        return [(c, i or None, o or None) for c, i, o in righe], nome_sel
    opere = []
    for nome in sorted(x for x in rclone('lsf', sel, '--files-only').split('\n') if re.match(r'opere_\d+\.jsonl$', x)):
        for l in rclone('cat', f'{sel}/{nome}').split('\n'):       # non splitlines: i testi possono contenere separatori Unicode
            if not l.strip():
                continue
            o = json.loads(l)
            eds = [(e['codice_sbn'], e.get('isbn')) for e in (o.get('edizioni') or []) if e.get('codice_sbn')]
            if eds:
                opere.append((int(o.get('posizione') or 10 ** 9), str(o.get('posizione') or ''), eds))
    opere.sort(key=lambda x: x[0])
    righe, visti = [], set()

    def metti(c, i, o):
        if c and c not in visti:
            visti.add(c)
            righe.append((c, i or '', o or ''))

    for _, op, eds in opere:              # 1) l'edizione più recente di ogni opera scelta
        metti(eds[0][0], eds[0][1], op)
    for _, op, eds in opere:              # 2) le altre edizioni delle opere scelte
        for c, i in eds[1:]:
            metti(c, i, op)
    testo = gzip.decompress(rclone('cat', f'{sel}/codici_sbn_dal_piu_nuovo.csv.gz', binario=True)).decode('utf-8', 'replace')
    for l in testo.split('\n')[1:]:       # 3) tutti gli altri libri, dal più nuovo
        p = l.split(',')
        if len(p) >= 2:
            metti(p[0], p[1], '')
    f = os.path.join(lavoro, nome_coda)
    with gzip.open(f, 'wt', encoding='utf-8') as g:
        g.write('codice\tisbn\topera\n')
        for r in righe:
            g.write('\t'.join(r) + '\n')
    rclone('copyto', f, f'{CARTELLA}/{nome_coda}')
    print(f'coda calcolata da {nome_sel}: {len(righe):,} libri ({len(opere):,} opere scelte)', flush=True)
    return [(c, i or None, o or None) for c, i, o in righe], nome_sel


def gia_fatti(lavoro):
    """Codici già fatti (o provati e falliti due volte) e opere che hanno già una presentazione."""
    dest = os.path.join(lavoro, 'esiti')
    os.makedirs(dest, exist_ok=True)
    if elenca(f'{CARTELLA}/esiti'):
        rclone('copy', f'{CARTELLA}/esiti', dest, '--include', '*.tsv.gz')
    fatti, errori, con_sinossi = set(), collections.Counter(), set()
    for nome in os.listdir(dest):
        with gzip.open(os.path.join(dest, nome), 'rt', encoding='utf-8') as g:
            for l in g:
                p = l.rstrip('\n').split('\t')
                if len(p) < 4 or p[0] == 'codice':
                    continue
                codice, esito, sinossi, opera = p[:4]
                if esito in ('fatta', 'non_trovata'):
                    fatti.add(codice)
                else:
                    errori[codice] += 1
                if sinossi == '1' and opera:
                    con_sinossi.add(opera)
    fatti |= {c for c, n in errori.items() if n >= 2}
    return fatti, con_sinossi


# ---------------------------------------------------------------- file nel Drive

class Parte:
    """Libri del giro in attesa di andare nel Drive: ogni PARTE libri, schede grezze + dati utili, poi gli esiti."""
    def __init__(self, lavoro, giro, locale=None):
        self.lavoro, self.giro, self.locale = lavoro, giro, locale
        self.n, self.mrc, self.righe, self.esiti = 0, bytearray(), [], []

    def aggiungi(self, dati, salvare, esiti):
        self.mrc += dati; self.righe += salvare; self.esiti += esiti

    def piena(self):
        return len(self.esiti) >= PARTE

    def scarica(self):
        if not self.esiti:
            return
        nome = f"schede_{datetime.datetime.now(datetime.timezone.utc):%Y%m%d_%H%M%S}_{self.giro}_{self.n:03d}"
        cartella = os.path.join(self.lavoro, 'parti')
        os.makedirs(cartella, exist_ok=True)
        with gzip.open(os.path.join(cartella, nome + '.mrc.gz'), 'wb') as g:
            g.write(bytes(self.mrc))
        with gzip.open(os.path.join(cartella, nome + '.jsonl.gz'), 'wt', encoding='utf-8') as g:
            for x in self.righe:
                g.write(json.dumps(x, ensure_ascii=False) + '\n')
        with gzip.open(os.path.join(cartella, 'esiti_' + nome + '.tsv.gz'), 'wt', encoding='utf-8') as g:
            g.write('codice\tesito\tsinossi\topera\n')
            for r in self.esiti:
                g.write('\t'.join(r) + '\n')
        try:
            if self.locale:
                for sotto, nomi in (('', (nome + '.mrc.gz', nome + '.jsonl.gz')), ('esiti', ('esiti_' + nome + '.tsv.gz',))):
                    os.makedirs(os.path.join(self.locale, sotto), exist_ok=True)
                    for n in nomi:
                        os.replace(os.path.join(cartella, n), os.path.join(self.locale, sotto, n))
            else:
                # prima le schede, poi gli esiti: un libro risulta fatto solo se le sue schede sono già nel Drive
                rclone('copy', cartella, CARTELLA, '--include', nome + '.*')
                rclone('copyto', os.path.join(cartella, 'esiti_' + nome + '.tsv.gz'), f'{CARTELLA}/esiti/esiti_{nome}.tsv.gz')
        except Exception as e:
            self.mrc, self.righe, self.esiti = bytearray(), [], []
            raise Fermati(senza_segreti(f'file nel Drive non riuscito (i libri si rifanno al prossimo giro): {e}')[:300])
        for f in os.listdir(cartella):
            os.remove(os.path.join(cartella, f))
        self.n += 1
        self.mrc, self.righe, self.esiti = bytearray(), [], []


def scrivi_giro(lavoro, giro, dati, locale=None):
    f = os.path.join(lavoro, f'giro_{giro}.json')
    with open(f, 'w', encoding='utf-8') as g:
        json.dump(dati, g, ensure_ascii=False, indent=1)
    try:
        if locale:
            os.makedirs(os.path.join(locale, 'giri'), exist_ok=True)
            os.replace(f, os.path.join(locale, 'giri', f'giro_{giro}.json'))
        else:
            rclone('copyto', f, f'{CARTELLA}/giri/giro_{giro}.json')
    except Exception as e:
        print('riepilogo del giro non salvato:', senza_segreti(e), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--minuti', type=float, default=50, help='durata massima del giro')
    ap.add_argument('--lavoro', default=os.environ.get('RUNNER_TEMP', '/tmp'))
    ap.add_argument('--yaz', default='yaz-client')
    ap.add_argument('--locale', metavar='CARTELLA', help='prova: i file vanno in questa cartella locale, non nel Drive')
    a = ap.parse_args()
    inizio = datetime.datetime.now(datetime.timezone.utc)
    giro = f'{inizio:%Y%m%d_%H%M%S}'
    fine = time.time() + a.minuti * 60
    tot = {'schede': 0, 'con_sinossi': 0, 'non_trovate': 0, 'errori': 0, 'opere_saltate': 0}
    parte = Parte(a.lavoro, giro, a.locale)
    nota = ''
    try:
        coda, nome_sel = coda_ordinata(a.lavoro)
        fatti, con_sinossi = gia_fatti(a.lavoro)
        print(f'{nome_sel}: {len(coda):,} libri in coda, {len(fatti):,} già fatti, {len(con_sinossi):,} opere con presentazione', flush=True)
        tot['in_coda'], tot['gia_fatti'] = len(coda), len(fatti)
        pos = 0
        while time.time() < fine - 120:
            presi = []
            while pos < len(coda) and len(presi) < LOTTO:
                c, i, o = coda[pos]
                pos += 1
                if c in fatti:
                    continue
                if o and o in con_sinossi:      # opera scelta che ha già una presentazione: le altre edizioni aspettano
                    tot['opere_saltate'] += 1
                    continue
                presi.append({'codice': c, 'isbn': i, 'opera': o})
            if not presi:
                nota = 'coda finita'
                break
            esiti, dati = scarica_lotto(presi, os.path.join(a.lavoro, 'lotto.mrc'), a.yaz)
            schede, per_isbn = {}, {}
            for r in leggi_marc(dati):
                x = scheda(r)
                if x['codice']:
                    schede[x['codice']] = x
                    for i in x['isbn']:
                        per_isbn.setdefault(i, x)
            salvare, righe_esiti = [], []
            for c in presi:
                e = esiti.get(c['codice'], 'errore')
                sinossi = '0'
                if e == 'presa':
                    x = schede.get(c['codice']) or per_isbn.get(isbn13(c.get('isbn')) or '')
                    if x:
                        x = dict(x, codice=c['codice'], codice_trovato=x['codice'], opera=c['opera'])
                        salvare.append(x)
                        e = 'fatta'
                        if x.get('sinossi'):
                            sinossi = '1'
                            if c['opera']:
                                con_sinossi.add(c['opera'])
                    else:
                        e = 'errore'
                righe_esiti.append((c['codice'], e, sinossi, c['opera'] or ''))
                fatti.add(c['codice'])
            parte.aggiungi(dati, salvare, righe_esiti)
            tot['schede'] += sum(1 for r in righe_esiti if r[1] == 'fatta')
            tot['con_sinossi'] += sum(1 for r in righe_esiti if r[2] == '1')
            tot['non_trovate'] += sum(1 for r in righe_esiti if r[1] == 'non_trovata')
            tot['errori'] += sum(1 for r in righe_esiti if r[1] == 'errore')
            if parte.piena():
                parte.scarica()
            print(f"{datetime.datetime.now():%H:%M} schede {tot['schede']} (con presentazione {tot['con_sinossi']}), "
                  f"non trovate {tot['non_trovate']}, errori {tot['errori']}", flush=True)
    except Fermati as e:
        nota = (nota + ' · ' if nota else '') + f'fermato: {e}'
    except Exception as e:
        nota = (nota + ' · ' if nota else '') + senza_segreti(f'errore: {e!r}')[:500]
    finally:
        try:
            parte.scarica()
        except Exception as e:
            nota = (nota + ' · ' if nota else '') + senza_segreti(str(e))[:300]
        nota = senza_segreti(nota)
        dati = dict(tot, inizio=inizio.isoformat(timespec='seconds'),
                    fine=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'), nota=nota or 'ok')
        scrivi_giro(a.lavoro, giro, dati, a.locale)
        print('fine:', json.dumps(dati, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
