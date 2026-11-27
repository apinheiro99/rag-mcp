#!/usr/bin/env python3
"""Fail CI when tracked public files contain likely literal credentials."""
from __future__ import annotations
from pathlib import Path
import re, subprocess
ROOT=Path(__file__).resolve().parents[1]; MAX_TEXT_BYTES=2_000_000
STRONG_PATTERNS=[
 ("private key",re.compile(r"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----")),
 ("GitHub token",re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b")),
 ("AWS access key",re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
 ("OpenAI-style key",re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),]
ASSIGNMENT=re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)\b\s*[:=]\s*[\"']?([^\s\"'#,;]+)")
SAFE_VALUES={"","none","null","false","true","changeme","change-me","change-me-strong-password","change-me-api-token","rag-ci-only","test","example","placeholder"}
SAFE_PREFIXES=("$","${","os.environ","os.getenv","environ[","getenv(")
def tracked_files():
 r=subprocess.run(["git","ls-files"],cwd=ROOT,text=True,capture_output=True,check=True); return r.stdout.splitlines()
def likely_literal_secret(value:str)->bool:
 n=value.strip().strip("\"'").casefold()
 if n in SAFE_VALUES or any(value.startswith(p) for p in SAFE_PREFIXES): return False
 if "<" in value or ">" in value or len(value)<8: return False
 if "://" in value or value.startswith(("/","./","../")): return False
 if any(ch in value for ch in ("(",")","{","}","[","]")): return False
 return True
def main()->int:
 findings=[]; files=tracked_files()
 for rel in files:
  path=ROOT/rel
  if not path.is_file() or path.stat().st_size>MAX_TEXT_BYTES: continue
  try: text=path.read_text(encoding="utf-8")
  except UnicodeDecodeError: continue
  for lineno,line in enumerate(text.splitlines(),1):
   for label,pattern in STRONG_PATTERNS:
    if pattern.search(line): findings.append(f"{label}: {rel}:{lineno}")
   match=ASSIGNMENT.search(line)
   if match and likely_literal_secret(match.group(2)): findings.append(f"literal credential-like value ({match.group(1)}): {rel}:{lineno}")
 if findings:
  print("SECRET SCAN: FAIL")
  for item in sorted(set(findings)): print(f"- {item}")
  return 1
 print(f"SECRET SCAN: PASS ({len(files)} tracked files)"); return 0
if __name__ == "__main__": raise SystemExit(main())
