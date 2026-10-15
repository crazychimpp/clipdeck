# Releasing a Clipdeck update

1. Change files in this folder (`server.py`, `static/index.html`, `requirements.txt`).
2. `python windows/release.py ../clipdeck-repo "what changed"` writes `version.json` with a SHA-256 for each file and copies the files into the repo clone.
3. `python windows/build.py ../clipdeck-repo/Clipdeck.cmd` rebuilds the one-file installer.
4. Commit and push `clipdeck-repo` to `main`. Installed copies pick it up within 6 hours, or right away from Settings → Updates → Check for updates now.
