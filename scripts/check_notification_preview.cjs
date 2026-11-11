/* Offline visual QA. Requires Playwright and a locally installed browser. */
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { chromium } = require('playwright');

async function main() {
  const root = path.resolve(__dirname, '..', 'preview');
  const browser = await chromium.launch({
    headless: true,
    ...(process.platform === 'win32' ? { channel: 'msedge' } : {}),
  });
  let checked = 0;
  try {
    const page = await browser.newPage();
    for (const width of [320, 390, 800, 1440]) {
      await page.setViewportSize({ width, height: 1000 });
      const names = ['index', ...['email', 'dingtalk'].flatMap(kind =>
        [1, 2, 3, 4].map(index => `${kind}-${index}`))];
      for (const name of names) {
        await page.goto(pathToFileURL(path.join(root, `${name}.html`)).href);
        for (const frame of page.frames()) {
          const result = await frame.evaluate(() => ({
            overflow: document.documentElement.scrollWidth > innerWidth,
            images: [...document.images].map(img => ({
              loaded: img.complete && img.naturalWidth === 1200 && img.naturalHeight === 220,
              width: img.getBoundingClientRect().width,
            })),
            scrollHeight: document.documentElement.scrollHeight,
          }));
          assert.equal(result.overflow, false, `${name} overflow at ${width}`);
          if (frame.parentFrame()) {
            const element = await frame.frameElement();
            const height = await element.evaluate(iframe => iframe.clientHeight);
            assert.ok(result.scrollHeight <= height, `${name} iframe crop at ${width}`);
          }
          assert.ok(result.images.every(image => image.loaded && image.width > 0),
            `${name} missing image at ${width}`);
        }
        if ((name === 'index' && [390, 1440].includes(width)) || (name === 'email-3' && width === 320)) {
          // Chromium skips painting distant iframes; bring the whole page into view for capture.
          const height = await page.evaluate(() => document.documentElement.scrollHeight);
          await page.setViewportSize({ width, height });
          await page.screenshot({ path: path.join(root, `notifications-${name}-${width}.png`), fullPage: true });
          await page.setViewportSize({ width, height: 1000 });
        }
        checked++;
      }
    }
    console.log(`Checked ${checked} local previews: images loaded, no horizontal overflow or cropped frames.`);
  } finally {
    await browser.close();
  }
}

main().catch(error => {
  console.error(error.message);
  process.exitCode = 1;
});
