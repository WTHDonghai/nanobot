import fs from 'node:fs';
import path from 'node:path';
import * as pdfjsLib from '/Users/daniel-wu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/pdfjs-dist/legacy/build/pdf.mjs';
import canvasPkg from '/Users/daniel-wu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/@napi-rs/canvas/index.js';

const { createCanvas, DOMMatrix, ImageData, Path2D } = canvasPkg;

globalThis.DOMMatrix = globalThis.DOMMatrix || DOMMatrix;
globalThis.ImageData = globalThis.ImageData || ImageData;
globalThis.Path2D = globalThis.Path2D || Path2D;

const [pdfPath, outputDir] = process.argv.slice(2);

if (!pdfPath || !outputDir) {
  console.error('usage: render_pdf_pages.mjs <input.pdf> <output-dir>');
  process.exit(2);
}

fs.mkdirSync(outputDir, { recursive: true });

const data = new Uint8Array(fs.readFileSync(pdfPath));
const loadingTask = pdfjsLib.getDocument({
  data,
  disableFontFace: false,
  useSystemFonts: true,
});
const pdf = await loadingTask.promise;

for (let pageNo = 1; pageNo <= pdf.numPages; pageNo += 1) {
  const page = await pdf.getPage(pageNo);
  const viewport = page.getViewport({ scale: 2 });
  const canvas = createCanvas(Math.ceil(viewport.width), Math.ceil(viewport.height));
  const context = canvas.getContext('2d');
  context.fillStyle = '#ffffff';
  context.fillRect(0, 0, canvas.width, canvas.height);
  await page.render({ canvasContext: context, viewport }).promise;
  const outPath = path.join(outputDir, `page-${String(pageNo).padStart(2, '0')}.png`);
  fs.writeFileSync(outPath, canvas.toBuffer('image/png'));
  console.log(outPath);
}
