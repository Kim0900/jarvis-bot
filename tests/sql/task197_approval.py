#!/usr/bin/env python3
"""TASK#197 authenticated authorization for a live rollback rehearsal (CASSANDRA #5673 P0-A, P1-F).

A live run needs an APPROVAL FILE signed by MAGI's Ed25519 key.  The runner verifies, before it opens any database connection:

  1. the public key it holds hashes to TRUST_ANCHOR_PUBKEY_SHA256 (a constant in this reviewed file; None = no anchor = every live run refused);
  2. the Ed25519 signature over the canonical payload;
  3. the payload is for task 197, this exact git commit (clean checkout), and the SHA256 of EVERY file that decides what runs
     (runner, this module, the DO statement, the expect file, the snapshot SQL, the generator);
  4. the fixed production manifest in the payload equals the manifest compiled into the runner (3000 / 250 / 2000 ms, 8 s);
  5. now is inside [not_before, not_after] (window at most 30 minutes) and the signed claims say the MAGI #5486 HOLD is cleared for this
     window and the window is upload-free;
  6. approval_id is a UUID that has not been used by a previous run on this machine (local used-ids file; MAGI also tracks it).

Everything fails closed: any missing piece, parse error or mismatch is a refusal with a reason; there is no text-token success.

Stated limits (not hidden): code cannot protect itself from an operator who edits the runner (an edited runner can skip these checks).  The
real controls are (a) MAGI signs the commit and file hashes it reviewed, so an edited runner does not match what was approved and CASSANDRA /
MAGI can re-hash the checkout; (b) the database credential for a live run is handed over only together with the approval.  The signing key
never enters this repository; registry rows alone are NOT an authorization (actor names are spoofable, #202).

MAGI tooling:  task197_approval.py keygen --out-dir D   |   sign --key D/private.pem --payload payload.json --out approval.json   |   verify --approval approval.json
"""
import argparse, base64, hashlib, json, os, re, subprocess, sys, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ROOT = Path(__file__).resolve().parents[2]

# set by MAGI in a reviewed commit: sha256 (hex) of the DER SubjectPublicKeyInfo of MAGI's approval key.  None => live runs are impossible.
TRUST_ANCHOR_PUBKEY_SHA256 = None

# the production manifest: the ONLY limits a live run may use (mirrors the runner constants; checked against the signed payload)
PROD_MANIFEST = {"statement_timeout_ms": 3000, "lock_timeout_ms": 250, "idle_tx_timeout_ms": 2000, "watchdog_s": 8, "mode": "begin"}
MAX_WINDOW = timedelta(minutes=30)
SCHEMA = "task197-approval-v1"
GUARDED_FILES = (
    "tests/sql/task197_safe_runner.py",
    "tests/sql/task197_approval.py",
    "tests/sql/task197_prod_rollback_rehearsal.sql",
    "tests/sql/task197_prod_rollback_rehearsal.expect.json",
    "tests/sql/task197_prod_rollback_snapshot.sql",
    "tests/sql/task197_rollback_rehearsal_build.py",
)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class Refused(Exception):
    """authorization missing / invalid: the run must not start"""


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def pubkey_fingerprint(pub: Ed25519PublicKey) -> str:
    der = pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()


def file_hashes(root: Path, names=GUARDED_FILES) -> dict:
    return {n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in names}


GIT_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
THROWAWAY_DB = re.compile(r"^(t_[a-z0-9_]+|gate[a-z0-9_]*|task197_throwaway_[a-z0-9_]+)$")
LOCAL_HOSTS = ("", "localhost", "127.0.0.1", "::1")


def git_bin() -> str:
    """git is looked up at fixed system paths only, never through the caller's PATH, and runs with a scrubbed environment
    (no GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE / config from the caller): a hostile environment cannot make it report another repository."""
    for c in ("/usr/bin/git", "/usr/local/bin/git", "/bin/git"):
        if os.access(c, os.X_OK):
            return c
    raise Refused("git not found at a fixed system path (cannot prove the commit)")


def default_state_dir() -> Path:
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".task197"


def state_dir(host: str, dbname: str, environ=None) -> Path:
    """Where single-use approval ids are recorded.  TASK197_STATE_DIR is honoured ONLY for the isolated test setup (TASK197_TEST_MODE +
    local host + throwaway database name); in production it is ignored, so the caller cannot point the replay record at an empty directory."""
    environ = os.environ if environ is None else environ
    local = host in LOCAL_HOSTS or str(host).startswith("/")
    if environ.get("TASK197_TEST_MODE") == "throwaway-server" and local and THROWAWAY_DB.match(dbname or "") and environ.get("TASK197_STATE_DIR"):
        return Path(environ["TASK197_STATE_DIR"])
    return default_state_dir()


def claimed(used_ids_file, key) -> bool:
    used = Path(used_ids_file)
    return used.exists() and key in set(used.read_text().split())


def git_state(root: Path) -> dict:
    """{'commit': <40 hex>, 'dirty': bool}.  Raises Refused if this is not a git checkout (a live run needs a provable commit)."""
    def run(*a):
        r = subprocess.run([git_bin(), "-c", f"safe.directory={root}", "-C", str(root), *a], capture_output=True, text=True, timeout=20, env=GIT_ENV)
        if r.returncode:
            raise Refused("not a git checkout (cannot prove the commit): " + r.stderr.strip()[:120])
        return r.stdout.strip()
    commit = run("rev-parse", "HEAD")
    dirty = bool(run("status", "--porcelain", "--untracked-files=no"))
    return {"commit": commit, "dirty": dirty}


def _parse_utc(s, name):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        raise Refused(f"{name} is not an ISO timestamp")
    if d.tzinfo is None:
        raise Refused(f"{name} has no timezone")
    return d.astimezone(timezone.utc)


def verify(approval: dict, pubkey_pem: bytes, root: Path, *, anchor=None, now=None, git=None, used_ids_file=None, manifest=None) -> dict:
    """Return the verified payload or raise Refused.  `anchor` defaults to the module constant (tests pass their own)."""
    anchor = TRUST_ANCHOR_PUBKEY_SHA256 if anchor is None else anchor
    manifest = PROD_MANIFEST if manifest is None else manifest
    now = now or datetime.now(timezone.utc)
    if not anchor or not re.fullmatch(r"[0-9a-f]{64}", str(anchor)):
        raise Refused("no trust anchor is configured in this checkout: live runs are not possible")
    try:
        pub = serialization.load_pem_public_key(pubkey_pem)
    except Exception:
        raise Refused("approval public key unreadable")
    if not isinstance(pub, Ed25519PublicKey):
        raise Refused("approval public key is not Ed25519")
    if pubkey_fingerprint(pub) != anchor:
        raise Refused("approval public key does not match the trust anchor")
    if not isinstance(approval, dict) or set(approval) != {"payload", "sig"} or not isinstance(approval["payload"], dict):
        raise Refused("approval file malformed")
    try:
        pub.verify(base64.b64decode(approval["sig"], validate=True), canonical(approval["payload"]))
    except (InvalidSignature, ValueError, TypeError):
        raise Refused("approval signature invalid")
    p = approval["payload"]
    if p.get("schema") != SCHEMA or p.get("task") != 197:
        raise Refused("approval is not a task-197 approval (schema/task)")
    g = git or git_state(root)
    if g["dirty"]:
        raise Refused("working tree has uncommitted changes to tracked files")
    if p.get("commit") != g["commit"] or not re.fullmatch(r"[0-9a-f]{40}", str(p.get("commit"))):
        raise Refused("approval is for a different commit")
    want = p.get("files")
    have = file_hashes(root)
    if not isinstance(want, dict) or set(want) != set(GUARDED_FILES):
        raise Refused("approval does not list exactly the guarded files")
    for n in GUARDED_FILES:
        if want[n] != have[n]:
            raise Refused(f"file differs from the approved hash: {n}")
    if p.get("manifest") != manifest:
        raise Refused("approval manifest differs from the fixed production manifest")
    nb, na = _parse_utc(p.get("not_before"), "not_before"), _parse_utc(p.get("not_after"), "not_after")
    if not (na - nb) > timedelta(0) or (na - nb) > MAX_WINDOW:
        raise Refused("approval window is empty or longer than 30 minutes")
    if not (nb <= now <= na):
        raise Refused("outside the approved time window")
    if p.get("hold_5486_cleared") is not True or p.get("upload_free_window") is not True:
        raise Refused("approval does not state that the #5486 HOLD is cleared for an upload-free window")
    if not isinstance(p.get("operator"), str) or not p["operator"].strip():
        raise Refused("approval names no operator")
    aid = str(p.get("approval_id", ""))
    if not UUID_RE.match(aid):
        raise Refused("approval_id is not a UUID")
    if used_ids_file is not None:
        used = Path(used_ids_file)
        seen = set(used.read_text().split()) if used.exists() else set()
        if aid in seen:
            raise Refused("approval_id was already used (single use)")
    return p


def consume_approval_id(used_ids_file, approval_id) -> bool:
    """Record the id as used (single use).  Returns False if it was already there.  Call only once the run is really admitted."""
    used = Path(used_ids_file)
    used.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(used, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        existing = set(os.pread(fd, 1 << 20, 0).decode().split())
        if approval_id in existing:
            return False
        os.write(fd, (approval_id + "\n").encode())
        return True
    finally:
        os.close(fd)


def make_payload(root: Path, commit: str, not_before, not_after, operator, approval_id=None, *, manifest=None):
    return {"schema": SCHEMA, "task": 197, "commit": commit, "files": file_hashes(root), "manifest": dict(manifest or PROD_MANIFEST),
            "not_before": not_before.astimezone(timezone.utc).isoformat(), "not_after": not_after.astimezone(timezone.utc).isoformat(),
            "operator": operator, "hold_5486_cleared": True, "upload_free_window": True, "approval_id": approval_id or str(uuid.uuid4())}


def sign(payload: dict, private_pem: bytes) -> dict:
    key = serialization.load_pem_private_key(private_pem, password=None)
    return {"payload": payload, "sig": base64.b64encode(key.sign(canonical(payload))).decode()}


def keygen(out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    k = Ed25519PrivateKey.generate()
    (out_dir / "private.pem").write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    os.chmod(out_dir / "private.pem", 0o600)
    pub = k.public_key()
    (out_dir / "public.pem").write_bytes(pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return pubkey_fingerprint(pub)


def main(argv=None):
    ap = argparse.ArgumentParser(description="MAGI-side tooling for the task197 approval (never run by the live runner)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen"); k.add_argument("--out-dir", required=True)
    s = sub.add_parser("sign"); s.add_argument("--key", required=True); s.add_argument("--payload", required=True); s.add_argument("--out", required=True)
    m = sub.add_parser("make-payload"); m.add_argument("--commit", required=True); m.add_argument("--not-before", required=True); m.add_argument("--not-after", required=True); m.add_argument("--operator", required=True); m.add_argument("--out", required=True)
    v = sub.add_parser("verify"); v.add_argument("--approval", required=True); v.add_argument("--pubkey", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "keygen":
        print("trust anchor (put this sha256 into TRUST_ANCHOR_PUBKEY_SHA256 in a reviewed commit):", keygen(Path(a.out_dir)))
    elif a.cmd == "make-payload":
        pl = make_payload(ROOT, a.commit, _parse_utc(a.not_before, "not_before"), _parse_utc(a.not_after, "not_after"), a.operator)
        Path(a.out).write_text(json.dumps(pl, indent=1, sort_keys=True) + "\n")
    elif a.cmd == "sign":
        Path(a.out).write_text(json.dumps(sign(json.loads(Path(a.payload).read_text()), Path(a.key).read_bytes()), indent=1) + "\n")
    else:
        try:
            p = verify(json.loads(Path(a.approval).read_text()), Path(a.pubkey).read_bytes(), ROOT)
            print("VALID until", p["not_after"], "operator", p["operator"], "id", p["approval_id"])
        except Refused as e:
            print("REFUSED:", e); return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
