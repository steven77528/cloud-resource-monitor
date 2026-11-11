// Requires Playwright; opens only the local synthetic-data preview.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const root = path.resolve(__dirname, '..');
const networkIds = ['network-ecs-intranet', 'network-ecs-all', 'network-rds-all'];

async function canvasPixels(page, selector = 'canvas') {
  return page.locator(selector).evaluateAll(canvases => canvases.map(canvas => {
    const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    let colored = 0;
    for (let offset = 0; offset < pixels.length; offset += 4) {
      if (pixels[offset + 3] > 0 && Math.max(pixels[offset], pixels[offset + 1], pixels[offset + 2]) -
          Math.min(pixels[offset], pixels[offset + 1], pixels[offset + 2]) > 50) colored++;
    }
    return { id: canvas.id, width: canvas.width, height: canvas.height, colored };
  }));
}

async function assertNetworkSelection(page, expected) {
  for (const [index, id] of networkIds.entries()) {
    assert.equal(await page.locator(`#${id}`).getAttribute('data-series-count'), String(expected[index]), id);
    assert.equal(await page.locator(`#${id}-legend span`).count(), expected[index], `${id} legend`);
    const [pixels] = await canvasPixels(page, `#${id}`);
    assert(pixels.width > 100 && pixels.height > 100, `${id} stable canvas dimensions`);
    assert(expected[index] ? pixels.colored > 50 : pixels.colored === 0, `${id} rendered selection`);
  }
}

async function assertResponsiveWidth(page) {
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  const overflow = await page.locator('#network-section h2, #network-section .legend span').evaluateAll(elements =>
    elements.filter(element => element.scrollWidth > element.clientWidth + 1).map(element => element.textContent));
  assert.deepEqual(overflow, [], 'Network headings and legends fit their containers');
}

async function main() {
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
  try {
    for (const [name, width, height] of [['desktop', 1440, 1080], ['mobile', 390, 844]]) {
      const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 });
      const errors = [];
      const externalRequests = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/*', route => {
        if (route.request().url().startsWith('file:')) return route.continue();
        externalRequests.push(route.request().url());
        return route.abort();
      });
      await page.goto(pathToFileURL(path.join(root, 'preview', 'grafana.html')).href);
      await page.locator('#metrics table').waitFor();
      assert.deepEqual(await page.locator('#provider option').evaluateAll(options => options.map(option => option.value)), ['all', 'aliyun', 'huawei']);
      await assertResponsiveWidth(page);
      const rendered = await canvasPixels(page);
      for (const id of ['balance', 'storage', 'cpu', ...networkIds]) {
        const canvas = rendered.find(item => item.id === id);
        assert(canvas && canvas.width > 100 && canvas.height > 100 && canvas.colored > 50, id);
      }
      await assertNetworkSelection(page, [2, 2, 6]);
      for (const id of networkIds) {
        assert.equal(await page.locator(`#${id}`).getAttribute('data-unit'), 'bit/s');
        const legend = await page.locator(`#${id}-legend`).innerText();
        assert(legend.includes('network_in_bps') && legend.includes('network_out_bps') && legend.includes('Mbps'), id);
      }
      assert.equal(await page.locator('#metrics tbody tr').filter({ hasText: /network_(in|out)_bps/ }).count(), 10);
      assert((await page.locator('#network-ecs-all').locator('xpath=ancestor::section[1]').innerText()).includes('\u516c\u7f51\u4e0e\u5185\u7f51'));
      assert((await page.locator('#network-rds-all').locator('xpath=ancestor::section[1]').innerText()).includes('5\u201310'));
      await page.screenshot({ path: path.join(root, 'preview', `grafana-${name}.png`) });
      if (name === 'desktop') {
        await page.screenshot({ path: path.join(root, 'preview', 'grafana-full.png'), fullPage: true });
      }
      await page.locator('#network-section').screenshot({ path: path.join(root, 'preview', `grafana-network-${name}.png`) });
      const network = page.locator('#network-ecs-intranet');
      await network.scrollIntoViewIfNeeded();
      await network.hover({ position: { x: 120, y: 70 } });
      const tooltip = network.locator('..').locator('.chart-tooltip');
      assert.equal(await tooltip.isVisible(), true);
      assert((await tooltip.innerText()).includes('network_in_bps') && (await tooltip.innerText()).includes('Mbps'));
      const initialTooltip = await tooltip.innerText();
      await page.locator('input[name="range"][value="200"]').check({ force: true });
      await network.hover({ position: { x: 120, y: 70 } });
      assert.notEqual(await tooltip.innerText(), initialTooltip, 'Time range updates network timestamps and points');
      assert.equal(await page.locator('input[name="range"]:checked').inputValue(), '200');
      await page.locator('#provider').selectOption('huawei');
      assert.equal(await page.locator('#account option').count(), 2);
      assert.equal(await page.locator('#alerts').innerText().then(text => text.includes('MySQL')), false);
      await assertNetworkSelection(page, [0, 2, 4]);
      assert(!(await page.locator('#network-section .legend').allInnerTexts()).join(' ').includes('aliyun'));
      await page.locator('#resource').selectOption('demo-worker');
      await assertNetworkSelection(page, [0, 2, 0]);
      await page.locator('#resource').selectOption('demo-hw-pg');
      await assertNetworkSelection(page, [0, 0, 2]);
      assert((await page.locator('#network-rds-all-legend').innerText()).includes('PostgreSQL'));
      await page.locator('#resource').selectOption('demo-obs');
      assert.equal(await page.locator('#expiry tbody tr').count(), 1);
      assert((await page.locator('#expiry').innerText()).includes('OBS'));
      await assertNetworkSelection(page, [0, 0, 0]);
      await page.locator('#provider').selectOption('aliyun');
      await assertNetworkSelection(page, [2, 0, 2]);
      await page.locator('#resource').selectOption('demo-pg');
      await assertNetworkSelection(page, [0, 0, 0]);
      await page.locator('#resource').selectOption('demo-api');
      await assertNetworkSelection(page, [2, 0, 0]);
      await page.locator('#provider').selectOption('all');
      await page.locator('#account').selectOption('huawei-demo-ops');
      await assertNetworkSelection(page, [0, 2, 4]);
      await assertResponsiveWidth(page);
      await page.locator('#refresh').click();
      assert.notEqual(await page.locator('#updated').innerText(), '\u89c6\u56fe\u5df2\u5c31\u7eea');
      assert.deepEqual(errors, []);
      assert.deepEqual(externalRequests, [], 'Preview must remain fully offline');
      console.log(`${name}: network canvas pixels, bit/s units, Mbps legends, exclusive source filters, tooltip, time range, responsive width, refresh and offline isolation passed.`);
      await page.close();
    }
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
