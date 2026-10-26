#!/usr/bin/env python3
"""Scrive ~/.config/rclone/rclone.conf dal segreto RCLONE_CONF. Se il testo è stato copiato dallo schermo, le righe
lunghe (il token) possono essere spezzate: si riuniscono. Non stampa mai il contenuto."""
import os

righe = []
for l in os.environ.get('RCLONE_CONF', '').replace('\r', '').split('\n'):
    l = l.rstrip()
    if not l.strip():
        continue
    if l.lstrip().startswith('[') or ' = ' in l or l.endswith(' =') or not righe:
        righe.append(l.strip())
    else:
        righe[-1] += l.strip()
os.makedirs(os.path.expanduser('~/.config/rclone'), exist_ok=True)
with open(os.path.expanduser('~/.config/rclone/rclone.conf'), 'w') as f:
    f.write('\n'.join(righe) + '\n')
print('configurazione rclone scritta:', len(righe), 'righe')
