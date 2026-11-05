import { readFile, mkdir, copyFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
execFileSync(process.execPath, ['--check', 'app.js'], {stdio:'inherit'});
const html = await readFile('index.html', 'utf8');
for (const asset of ['app.js', 'style.css']) {
  if (!html.includes(`/${asset}`)) throw new Error(`Missing asset reference: ${asset}`);
}
await mkdir('dist', {recursive:true});
for (const file of ['index.html','app.js','style.css']) await copyFile(file, `dist/${file}`);
console.log('Built Ear demo static assets in dist/');
