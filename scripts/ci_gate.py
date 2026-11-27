#!/usr/bin/env python3
"""Run one CI gate and always emit a compact machine/human-readable summary."""
from __future__ import annotations
import json, os, re, subprocess, sys
from pathlib import Path
from datetime import datetime, timezone

def main() -> int:
    if len(sys.argv) < 3:
        print("usage: ci_gate.py <gate-name> <command> [args...]", file=sys.stderr)
        return 2
    name, command = sys.argv[1], sys.argv[2:]
    started = datetime.now(timezone.utc)
    completed = subprocess.run(command, check=False)
    finished = datetime.now(timezone.utc)
    exit_code = int(completed.returncode)
    status = "PASS" if exit_code == 0 else "FAIL"
    out = Path("ci-summary"); out.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "validation"
    duration = max(0, int((finished-started).total_seconds()))
    summary={"gate":name,"status":status,"exit_code":exit_code,"command":command,
      "pipeline_id":os.getenv("CI_PIPELINE_ID"),"pipeline_iid":os.getenv("CI_PIPELINE_IID"),
      "job_id":os.getenv("CI_JOB_ID"),"job_name":os.getenv("CI_JOB_NAME",name),
      "ref":os.getenv("CI_COMMIT_REF_NAME"),"commit_sha":os.getenv("CI_COMMIT_SHA"),
      "started_at":started.isoformat(),"finished_at":finished.isoformat(),"duration_seconds":duration}
    (out/f"{safe}.json").write_text(json.dumps(summary,indent=2)+"\n")
    (out/f"{safe}.txt").write_text("\n".join([
      f"Gate: {name}",f"Status: {status}",f"Exit code: {exit_code}",f"Command: {' '.join(command)}",
      f"Pipeline: {summary['pipeline_id'] or '-'}",f"Job: {summary['job_id'] or '-'}",
      f"Ref: {summary['ref'] or '-'}",f"Commit: {summary['commit_sha'] or '-'}",f"Duration: {duration}s","" ]))
    return exit_code
if __name__ == "__main__": raise SystemExit(main())
