"""Toglie dal registro di un giro ogni pezzo che somiglia a un permesso (token) e scrive la fine come nota di GitHub."""
import re
import sys

f = sys.argv[1]
t = open(f, encoding='utf-8', errors='replace').read()
t = re.sub(r'ya29\.[\w.-]+|1//[\w.-]+|"(?:access_token|refresh_token|client_secret)"\s*:\s*"[^"]*"|[A-Za-z0-9_\-+=]{40,}', '[tolto]', t)
open(f, 'w', encoding='utf-8').write(t)
coda = t.strip().splitlines()[-40:]
testo = '%0A'.join(l.replace('%', '%25').replace('\r', '') for l in coda)[-3900:]
print('::notice title=fine del registro::' + testo)
