// Builds the Python dashboard server into a standalone folder with PyInstaller, at
// src-tauri/resources/rank-checker-server/, which the installer ships as "server/".
// Runs before every `tauri build`. Uses .venv's Python when there is one (local builds),
// otherwise `python` (CI). That Python needs: pip install -r requirements.txt -r requirements-build.txt
import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';

const root = join(import.meta.dirname, '..');
const venv = process.platform === 'win32'
  ? join(root, '.venv', 'Scripts', 'python.exe')
  : join(root, '.venv', 'bin', 'python');
const python = existsSync(venv) ? venv : 'python';

const run = (args) => execFileSync(python, args, { cwd: root, stdio: 'inherit' });

try {
  run(['-c', 'import PyInstaller, playwright']);
} catch {
  console.error(`\nThe server build needs PyInstaller and Playwright in ${python}. Run:\n` +
    `  ${python} -m pip install -r requirements.txt -r requirements-build.txt\n`);
  process.exit(1);
}

run([
  '-m', 'PyInstaller', 'app.py',
  '--name', 'rank-checker-server',
  '--noconfirm', '--clean', '--log-level', 'WARN',
  '--distpath', 'src-tauri/resources',
  '--workpath', 'build/pyinstaller',
  '--specpath', 'build/pyinstaller',
  '--add-data', `${join(root, 'dashboard_template.html')}:.`,
  '--add-data', `${join(root, 'icons', 'favicon.svg')}:icons`,
  // Only the Chrome/Edge channels are used, so no Playwright browser is bundled.
  '--collect-data', 'playwright',
]);
console.log('Server built in src-tauri/resources/rank-checker-server');
