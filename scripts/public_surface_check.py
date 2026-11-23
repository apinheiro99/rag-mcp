#!/usr/bin/env python3
from pathlib import Path
import re, subprocess, sys
root=Path(__file__).resolve().parents[1]
tracked=subprocess.run(['git','ls-files'],cwd=root,text=True,capture_output=True,check=True).stdout.splitlines()
errors=[]
for rel in tracked:
    low=rel.lower()
    if low=='.env' or '/.env' in low or low.startswith('rag-private/'):
        errors.append(f'private runtime file tracked: {rel}')
    if low.endswith(('.pem','.key','.p12','.pfx')):
        errors.append(f'private key material tracked: {rel}')
    p=root/rel
    if not p.is_file() or p.stat().st_size>2_000_000: continue
    try: text=p.read_text(encoding='utf-8')
    except UnicodeDecodeError: continue
    for n,line in enumerate(text.splitlines(),1):
        if re.search(r'^POSTGRES_PASSWORD=(?!change-me-strong-password$).+',line):
            errors.append(f'non-placeholder POSTGRES_PASSWORD: {rel}:{n}')
        if re.search(r'^RAG_API_TOKEN=(?!change-me-api-token$).+',line):
            errors.append(f'non-placeholder RAG_API_TOKEN: {rel}:{n}')
        openssh_marker='BEGIN OPENSSH ' + 'PRIVATE KEY'
        generic_marker='BEGIN ' + 'PRIVATE KEY'
        if openssh_marker in line or generic_marker in line:
            errors.append(f'private key content: {rel}:{n}')
if errors:
    print('PUBLIC SURFACE CHECK: FAIL')
    print('\n'.join(f'- {x}' for x in errors))
    sys.exit(1)
print(f'PUBLIC SURFACE CHECK: PASS ({len(tracked)} tracked files)')
