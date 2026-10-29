"""Regole degli ISBN per tutta la pipeline (compito 29/10, sezione 7: isbnlib).

Le stesse regole di isbnlib (github.com/xlcnd/isbnlib), usate anche dall'import CSV dell'app:
- pulizia: via trattini, spazi, punti e la scritta «ISBN»; la X solo come ultima cifra di un ISBN-10;
- ISBN-13: 13 cifre, prefisso 978 o 979, cifra di controllo giusta;
- ISBN-10: 9 cifre più una cifra o X, cifra di controllo giusta, poi convertito in ISBN-13 (978).
Se isbnlib è installato si usa quello (pip install isbnlib); se no, le stesse regole scritte qui.
isbn13_tollerante() tiene anche gli ISBN-13 con la cifra di controllo sbagliata (capita, stampati così sui libri):
serve solo per riconoscere una scheda già nota, mai per aggiungere un ISBN nuovo.
"""
import re

try:
    import isbnlib  # type: ignore
except ImportError:  # le stesse regole, senza il pacchetto
    isbnlib = None


def pulisci(s):
    """«ISBN 978-88-04-34716-3» → «9788804347163» (solo cifre e X maiuscola)."""
    return re.sub(r'[^0-9X]', '', re.sub(r'(?i)isbn(-1[03])?:?', '', s or '').upper())


def _controllo13(d12):
    return str((10 - sum(int(x) * (1 if i % 2 == 0 else 3) for i, x in enumerate(d12)) % 10) % 10)


def _valido10(d):
    if not re.fullmatch(r'\d{9}[\dX]', d):
        return False
    s = sum((10 - i) * (10 if x == 'X' else int(x)) for i, x in enumerate(d))
    return s % 11 == 0


def isbn13(s):
    """ISBN-13 valido (convertendo un ISBN-10 valido), altrimenti None."""
    d = pulisci(s)
    if isbnlib is not None:
        if isbnlib.is_isbn13(d) and d[:3] in ('978', '979'):
            return d
        if isbnlib.is_isbn10(d):
            return isbnlib.to_isbn13(d)
        return None
    if len(d) == 13 and d.isdigit() and d[:3] in ('978', '979'):
        return d if _controllo13(d[:12]) == d[12] else None
    if len(d) == 10 and _valido10(d):
        b = '978' + d[:9]
        return b + _controllo13(b)
    return None


def isbn13_tollerante(s):
    """Come isbn13, ma tiene anche un ISBN-13 978/979 con la cifra di controllo sbagliata e converte un ISBN-10 con le
    prime nove cifre giuste anche se l'ultima è sbagliata (per riconoscere schede già note)."""
    v = isbn13(s)
    if v:
        return v
    d = pulisci(s)
    if len(d) == 13 and d.isdigit() and d[:3] in ('978', '979'):
        return d
    if len(d) == 10 and d[:9].isdigit():
        b = '978' + d[:9]
        return b + _controllo13(b)
    return None
