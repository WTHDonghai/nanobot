import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';

const require = createRequire(import.meta.url);
const { PDFDocument } = require('/Users/daniel-wu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/pdf-lib');

const [pdfPath, outputDir] = process.argv.slice(2);

if (!pdfPath || !outputDir) {
  console.error('usage: split_pdf_pages.mjs <input.pdf> <output-dir>');
  process.exit(2);
}

fs.mkdirSync(outputDir, { recursive: true });
const srcBytes = fs.readFileSync(pdfPath);
const srcPdf = await PDFDocument.load(srcBytes);

for (let i = 0; i < srcPdf.getPageCount(); i += 1) {
  const single = await PDFDocument.create();
  const [copiedPage] = await single.copyPages(srcPdf, [i]);
  single.addPage(copiedPage);
  const bytes = await single.save();
  const pagePdf = path.join(outputDir, `page-${String(i + 1).padStart(2, '0')}.pdf`);
  fs.writeFileSync(pagePdf, bytes);
  console.log(pagePdf);
}
