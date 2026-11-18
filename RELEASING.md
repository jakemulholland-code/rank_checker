# Rank Checker desktop app

The app (`src-tauri/`) is a Tauri 2 window around the Python dashboard, built the same way as Engineer's Notebook. On launch it starts the bundled server (`app.py --app`, built with PyInstaller into `src-tauri/resources/rank-checker-server/`), waits for the `RANK_CHECKER_URL` line, and loads that address. Results live in `%APPDATA%\uk.co.paramountdigital.rank-checker\results`; the server's output is logged to `%LOCALAPPDATA%\uk.co.paramountdigital.rank-checker\logs\server.log`. Checks use the installed Chrome, then Edge, so no browser is bundled.

Links that leave the local dashboard open in the default browser. Downloads land in Downloads and File Explorer opens to show them.

## Prerequisites

- Node 20 or newer, Rust stable, and the [Tauri prerequisites](https://v2.tauri.app/start/prerequisites/) for Windows
- Python 3.9 or newer with the `.venv` set up (double-click `Rank Checker.bat` once), plus PyInstaller:

```bash
.venv/Scripts/python -m pip install -r requirements.txt -r requirements-build.txt
```

```bash
npm install
```

## Development

```bash
npm run tauri dev
```

Dev builds run `app.py` from this folder with `.venv`'s Python, so Python edits only need an app restart. Set `RANK_CHECKER_DATA_DIR` to a scratch folder to keep test checks out of your real results.

Icons come from `icons/app-icon.svg` (`npm run icons`, then delete `src-tauri/icons/android` and `ios`). The favicon is `icons/favicon.svg`, also inlined in the `<head>` of `dashboard_template.html` so exported client pages carry it; keep the two in step.

## Releasing a new version

Installers and updates are published to the **public** repo [rank-checker-releases](https://github.com/jakemulholland-code/rank-checker-releases), because this repo is private and the updater can only read public releases.

1. Bump `version` in `package.json`, `src-tauri/tauri.conf.json` and `src-tauri/Cargo.toml`.
2. Commit, then tag and push:

```bash
git tag v0.1.1
```

```bash
git push origin main --tags
```

3. The `release` workflow builds the installer on `windows-latest`, signs the updater files and attaches everything, including `latest.json`, to a **draft** release in `rank-checker-releases`.
4. Check the draft there, then publish it. Installed apps only see published releases.

### One-off setup

- Create the public repo `jakemulholland-code/rank-checker-releases` with a README (it needs a `main` branch for release tags).
- Create a fine-grained personal access token with **Contents: read and write** on that repo only, and save it in this repo's secrets as `RELEASES_TOKEN`.
- Add the updater signing key as secrets (below).

### Updater signing key

Updates are signed. The private key and its password were generated on Jake's machine at `%USERPROFILE%\.tauri\rank-checker.key` (password in `rank-checker.key.password.txt` beside it). Store them as the repo secrets `TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`, and keep a copy of both in a password manager: if the key is lost, installed copies can no longer be updated and everyone has to reinstall by hand. The public key is in `src-tauri/tauri.conf.json`. Never commit the private key.

To build an installer locally without releasing (PowerShell):

```powershell
$env:TAURI_SIGNING_PRIVATE_KEY = Get-Content "$HOME\.tauri\rank-checker.key" -Raw
$env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = Get-Content "$HOME\.tauri\rank-checker.key.password.txt" -Raw
npx tauri build
```

The installer is written to `src-tauri/target/release/bundle/nsis/`.
