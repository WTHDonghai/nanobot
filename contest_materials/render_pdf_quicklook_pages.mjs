import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';

const require = createRequire(import.meta.url);
const { PDFDocument } = require('/Users/daniel-wu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/pdf-lib');

const [pdfPath, outputDir, size = '1800'] = process.argv.slice(2);

if (!pdfPath || !outputDir) {
  console.error('usage: render_pdf_quicklook_pages.mjs <input.pdf> <output-dir> [size]');
  process.exit(2);
}

const out = path.resolve(outputDir);
const splitDir = path.join(out, 'single-page-pdfs');
fs.mkdirSync(splitDir, { recursive: true });

const srcBytes = fs.readFileSync(pdfPath);
const srcPdf = await PDFDocument.load(srcBytes);

for (let i = 0; i < srcPdf.getPageCount(); i += 1) {
  const single = await PDFDocument.create();
  const [copiedPage] = await single.copyPages(srcPdf, [i]);
  single.addPage(copiedPage);
  const bytes = await single.save();
  const pageName = `page-${String(i + 1).padStart(2, '0')}.pdf`;
  const pagePdf = path.join(splitDir, pageName);
  fs.writeFileSync(pagePdf, bytes);
  execFileSync('/usr/bin/qlmanage', ['-t', '-s', String(size), '-o', out, pagePdf], {
    stdio: 'ignore',
  });
  const generated = path.join(out, `${pageName}.png`);
  const normalized = path.join(out, `page-${String(i + 1).padStart(2, '0')}.png`);
  if (fs.existsSync(generated)) {
    fs.renameSync(generated, normalized);
  }
  console.log(normalized);
}
