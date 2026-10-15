"""Make a release: write version.json (with a SHA-256 for every app file) and copy the app files
into a folder that mirrors the GitHub repo. Usage: python windows/release.py <out_dir> "what changed"."""
import datetime as dt, hashlib, json, shutil, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["server.py", "requirements.txt", "clipdeck.ico", "static/index.html"]
out = Path(sys.argv[1]); notes = sys.argv[2] if len(sys.argv) > 2 else ""
now = dt.datetime.now(dt.timezone.utc)
meta = {
    "version": now.strftime("%Y.%m.%d.%H%M"),
    "built": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "notes": notes,
    "files": {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in FILES},
}
for f in FILES:
    (out / f).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / f, out / f)
(out / "version.json").write_text(json.dumps(meta, indent=2) + "\n")
(ROOT / "version.json").write_text(json.dumps(meta, indent=2) + "\n")
print(meta["version"])
