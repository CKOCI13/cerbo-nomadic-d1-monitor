// Renders the wiring pictures in docs/images from docs/src/wiring.html (2x scale).
//
//   npm install playwright        (once; any recent version)
//   node docs/src/build-wiring.cjs
//
// wiring.html is the same in all four repositories except data-model on <body> (D1 or D2).

const { chromium } = require('playwright');
const path = require('path');

const SRC = __dirname;
const IMAGES = path.join(path.dirname(SRC), 'images');

(async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ deviceScaleFactor: 2 });
    await page.goto('file://' + path.join(SRC, 'wiring.html'), { waitUntil: 'load' });
    await page.evaluate(() => document.fonts.ready);
    for (const el of await page.$$('[data-figure]')) {
      const name = await el.getAttribute('data-figure');
      await el.screenshot({ path: path.join(IMAGES, name + '.png') });
      console.log(name + '.png');
    }
  } finally {
    await browser.close();
  }
})();
