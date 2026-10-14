// Rebuilds the pictures in docs/images and the PDFs in docs/ from the sources in this folder.
//
//   npm install playwright        (once; any recent version)
//   node docs/src/build.cjs       (from the project folder; needs Python 3 for the web-page screenshot)
//
// figures.html            -> one PNG per element with a data-figure attribute
// demo_server.py          -> web-page.png, a real screenshot of the monitor's page with a pretend D1
// installation-guide.html -> ../installation-guide.pdf
// safe-test-plan.html     -> ../safe-test-plan.pdf

const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');

const SRC = __dirname;
const DOCS = path.dirname(SRC);
const IMAGES = path.join(DOCS, 'images');
const url = f => 'file://' + path.join(SRC, f);

async function figures(browser) {
  const page = await browser.newPage({ deviceScaleFactor: 2 });
  await page.goto(url('figures.html'), { waitUntil: 'networkidle' });
  await page.evaluate(() => document.fonts.ready);
  for (const el of await page.$$('[data-figure]')) {
    const name = await el.getAttribute('data-figure');
    await el.screenshot({ path: path.join(IMAGES, name + '.png'), omitBackground: true });
    console.log('images/' + name + '.png');
  }
  await page.close();
}

async function webPage(browser) {
  const port = 8097;
  const server = spawn('python3', [path.join(SRC, 'demo_server.py'), String(port)], { stdio: ['ignore', 'pipe', 'inherit'] });
  await new Promise((resolve, reject) => {
    server.stdout.on('data', d => { if (String(d).includes('ready')) resolve(); });
    server.on('exit', code => reject(new Error('demo server exited with ' + code)));
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1000, height: 900 }, deviceScaleFactor: 2, colorScheme: 'light' });
    await page.goto('http://127.0.0.1:' + port + '/');
    await page.waitForTimeout(2500);
    // Hover the power chart so the picture shows the crosshair and tooltip.
    const box = await page.locator('.chart svg').first().boundingBox();
    await page.mouse.move(box.x + box.width * 0.72, box.y + 60, { steps: 5 });
    await page.waitForTimeout(300);
    await page.screenshot({ path: path.join(IMAGES, 'web-page.png'), fullPage: true });
    console.log('images/web-page.png');
    await page.close();
  } finally {
    server.kill();
  }
}

async function pdf(browser, name) {
  const page = await browser.newPage();
  await page.goto(url(name + '.html'), { waitUntil: 'networkidle' });
  await page.evaluate(() => document.fonts.ready);
  await page.pdf({
    path: path.join(DOCS, name + '.pdf'),
    format: 'Letter',
    printBackground: true,
    margin: { top: '0.6in', bottom: '0.7in', left: '0.6in', right: '0.6in' },
    displayHeaderFooter: true,
    headerTemplate: '<span></span>',
    footerTemplate: '<div style="width:100%;text-align:center;font-size:8px;color:#888;font-family:sans-serif">' +
      'Page <span class="pageNumber"></span> of <span class="totalPages"></span></div>',
  });
  console.log(name + '.pdf');
  await page.close();
}

(async () => {
  const browser = await chromium.launch();
  try {
    const only = process.argv[2];
    if (!only || only === 'figures') await figures(browser);
    if (!only || only === 'web') await webPage(browser);
    if (!only || only === 'pdf') {
      await pdf(browser, 'installation-guide');
      await pdf(browser, 'safe-test-plan');
    }
  } finally {
    await browser.close();
  }
})();
