"""Build Clipdeck.cmd: a batch file that carries the whole app inside it."""
import base64, hashlib, io, sys, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "Clipdeck.cmd"
FILES = ["server.py", "requirements.txt", "clipdeck.ico", "static/index.html", "version.json"]

buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    for f in FILES:
        z.write(ROOT / f, f)  # no top-level folder: unpacks straight into the app folder
data = buf.getvalue()
version = hashlib.sha256(data).hexdigest()[:12]

header = (
    "@echo off\n"
    "setlocal\n"
    "title Clipdeck\n"
    "rem Clipdeck for Windows. Double-click to install or update, then it opens in your browser.\n"
    "set \"CLIPDECK_SELF=%~f0\"\n"
    "powershell -NoProfile -ExecutionPolicy Bypass -Command \"$t=[IO.File]::ReadAllText($env:CLIPDECK_SELF); "
    "$a=$t.IndexOf('#'+'PS_BEGIN'); $b=$t.IndexOf('#'+'PS_END'); Invoke-Expression $t.Substring($a, $b-$a)\"\n"
    "exit /b %errorlevel%\n"
)
ps = (ROOT / "windows" / "launcher.ps1").read_text().replace("__VERSION__", version)
assert ps.isascii(), "launcher must stay ASCII"
b64 = base64.b64encode(data).decode()
payload = "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))
text = header + ps + "\n::CLIPDECK_PAYLOAD::\n" + payload + "\n"
OUT.write_bytes(text.replace("\n", "\r\n").encode("ascii"))
print(f"{OUT}  version {version}  {OUT.stat().st_size / 1024:.0f} KB")
