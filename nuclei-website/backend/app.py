"""
Backend for the custom Nuclei scanning dashboard.

Flow:
  Frontend -> POST /api/scans (target + profile name)
  Backend  -> creates a scan row (status=queued), schedules a background job
  Worker   -> runs `nuclei` CLI with a FIXED set of arguments per profile
  Backend  -> parses the JSONL output, stores findings in SQLite
  Frontend -> polls GET /api/scans/{id} and /api/scans/{id}/findings

Users never send raw Nuclei arguments. They only pick a target + a profile
name, and the profile -> arguments mapping lives in profiles.json on the
server. This is what keeps the HTTP surface safe (no command injection via
free-form CLI flags).
"""

import json
import sqlite3
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator

BASE_DIR = Path(__file__).parent
RESULTS_DIR = BASE_DIR / "results"
DB_PATH = BASE_DIR / "scans.db"
PROFILES_PATH = BASE_DIR / "profiles.json"

RESULTS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Nuclei Scan Dashboard API")

# Allow the local frontend (served from a different port/file) to call this API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scans (
                scan_id TEXT PRIMARY KEY,
                target TEXT NOT NULL,
                profile TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                total_findings INTEGER DEFAULT 0,
                started_at TEXT,
                finished_at TEXT,
                error TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT NOT NULL,
                template_id TEXT,
                name TEXT,
                severity TEXT,
                matched_at TEXT,
                description TEXT,
                raw_json TEXT,
                FOREIGN KEY (scan_id) REFERENCES scans (scan_id)
            )
            """
        )


init_db()


def load_profiles() -> dict:
    with open(PROFILES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class ScanCreateRequest(BaseModel):
    target: str
    profile: str

    @field_validator("target")
    @classmethod
    def target_must_be_http_url(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("target must start with http:// or https://")
        return v


# --------------------------------------------------------------------------
# Nuclei execution
# --------------------------------------------------------------------------

def build_nuclei_command(target: str, profile_name: str, profile: dict, output_path: Path) -> list[str]:
    """Translate a profile name into a FIXED nuclei command. The caller never
    supplies raw nuclei flags -- only `target` and `profile` (validated above
    and looked up against profiles.json)."""

    cmd = ["nuclei", "-u", target, "-jsonl-export", str(output_path)]

    if "template" in profile:
        cmd += ["-t", profile["template"]]
    if "tags" in profile:
        cmd += ["-tags", ",".join(profile["tags"])]
    if "severity" in profile:
        cmd += ["-severity", ",".join(profile["severity"])]

    cmd += [
        "-rl", str(profile.get("rate_limit", 5)),
        "-c", str(profile.get("concurrency", 2)),
        "-bs", str(profile.get("bulk_size", 1)),
        "-timeout", str(profile.get("timeout", 10)),
        "-retries", str(profile.get("retries", 1)),
        "-no-color",
        "-silent",
    ]
    return cmd


def run_scan(scan_id: str, target: str, profile_name: str):
    profiles = load_profiles()
    profile = profiles.get(profile_name)
    output_path = RESULTS_DIR / f"{scan_id}.jsonl"

    with get_db() as conn:
        conn.execute(
            "UPDATE scans SET status='running', started_at=? WHERE scan_id=?",
            (datetime.now(timezone.utc).isoformat(), scan_id),
        )

    if profile is None:
        with get_db() as conn:
            conn.execute(
                "UPDATE scans SET status='failed', error=?, finished_at=? WHERE scan_id=?",
                (f"unknown profile '{profile_name}'", datetime.now(timezone.utc).isoformat(), scan_id),
            )
        return

    cmd = build_nuclei_command(target, profile_name, profile, output_path)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=900,  # 15 min hard ceiling for a prototype
        )
        error_text = None
        if result.returncode not in (0, 1):
            # nuclei's exit codes aren't strictly 0/1 across all versions;
            # treat a nonzero code with no output file as a real failure.
            if not output_path.exists():
                error_text = result.stderr[-2000:] if result.stderr else "nuclei exited with an error"
    except subprocess.TimeoutExpired:
        error_text = "scan timed out"
    except FileNotFoundError:
        error_text = "nuclei binary not found on PATH"

    findings = []
    if output_path.exists():
        with open(output_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    findings.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    with get_db() as conn:
        for finding in findings:
            info = finding.get("info", {})
            conn.execute(
                """
                INSERT INTO findings (scan_id, template_id, name, severity, matched_at, description, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_id,
                    finding.get("template-id") or finding.get("template_id"),
                    info.get("name"),
                    info.get("severity"),
                    finding.get("matched-at") or finding.get("host"),
                    info.get("description"),
                    json.dumps(finding),
                ),
            )
        status = "failed" if error_text and not findings else "completed"
        conn.execute(
            """
            UPDATE scans
            SET status=?, total_findings=?, finished_at=?, error=?
            WHERE scan_id=?
            """,
            (status, len(findings), datetime.now(timezone.utc).isoformat(), error_text, scan_id),
        )


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/api/profiles")
def list_profiles():
    return load_profiles()


@app.post("/api/scans")
def create_scan(req: ScanCreateRequest, background_tasks: BackgroundTasks):
    profiles = load_profiles()
    if req.profile not in profiles:
        raise HTTPException(400, f"unknown profile '{req.profile}'. Available: {list(profiles.keys())}")

    scan_id = f"scan-{uuid.uuid4().hex[:8]}"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO scans (scan_id, target, profile, status) VALUES (?, ?, ?, 'queued')",
            (scan_id, req.target, req.profile),
        )

    background_tasks.add_task(run_scan, scan_id, req.target, req.profile)
    return {"scan_id": scan_id, "status": "queued"}


@app.get("/api/scans")
def list_scans():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM scans ORDER BY started_at DESC").fetchall()
        return [dict(r) for r in rows]


@app.get("/api/scans/{scan_id}")
def get_scan(scan_id: str):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM scans WHERE scan_id=?", (scan_id,)).fetchone()
        if not row:
            raise HTTPException(404, "scan not found")
        return dict(row)


@app.get("/api/scans/{scan_id}/findings")
def get_findings(scan_id: str, severity: Optional[str] = None):
    with get_db() as conn:
        if severity:
            rows = conn.execute(
                "SELECT * FROM findings WHERE scan_id=? AND severity=? ORDER BY id",
                (scan_id, severity),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM findings WHERE scan_id=? ORDER BY id", (scan_id,)
            ).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/scans/{scan_id}/results")
def download_results(scan_id: str, fmt: str = "jsonl"):
    ext_map = {"jsonl": ".jsonl", "json": ".json", "sarif": ".sarif"}
    if fmt not in ext_map:
        raise HTTPException(400, "fmt must be one of jsonl, json, sarif")
    path = RESULTS_DIR / f"{scan_id}{ext_map[fmt]}"
    if not path.exists():
        raise HTTPException(404, f"no {fmt} export found for this scan")
    return FileResponse(path, filename=path.name)


@app.get("/api/scans/{scan_id}/report")
def get_report(scan_id: str):
    """Simple text/markdown-style report assembled from stored findings.
    Swap this out for the AI-analyzer step described in the design doc."""
    with get_db() as conn:
        scan = conn.execute("SELECT * FROM scans WHERE scan_id=?", (scan_id,)).fetchone()
        if not scan:
            raise HTTPException(404, "scan not found")
        findings = conn.execute(
            "SELECT * FROM findings WHERE scan_id=? ORDER BY severity", (scan_id,)
        ).fetchall()

    lines = [f"# Scan report: {scan_id}", "", f"Target: {scan['target']}", f"Profile: {scan['profile']}",
              f"Status: {scan['status']}", f"Total findings: {scan['total_findings']}", "", "## Findings", ""]
    for f in findings:
        lines.append(f"- **[{f['severity']}] {f['name']}** ({f['template_id']}) — {f['matched_at']}")
    return {"report_markdown": "\n".join(lines)}
