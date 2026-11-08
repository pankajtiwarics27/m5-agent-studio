/** Deterministically reproduce the CI stale measurement without a server or timer sleeps. */
import assert from 'node:assert/strict';
import { waitForHarnessSidebarLayout } from './sidebar_layout_ready.mjs';

export async function checkSidebarLayoutReadiness(browser, check) {
  const page = await browser.newPage();
  const requests = [];
  page.on('request', request => requests.push(request.url()));
  try {
    await page.setViewport({ width: 1280, height: 720 });
    await page.setContent(`<!doctype html><style>
      :root { --studio-nav-height: 44px; }
      .mode-tabs { position: fixed; top: 56px; left: 0; width: 200px; height: 381px; }
      #harnessSessionsPanel { position: fixed; top: calc(56px + var(--studio-nav-height));
        bottom: 0; left: 0; width: 200px; }
      </style><body data-active-pane="harness">
      <nav class="mode-tabs"></nav><aside id="harnessSessionsPanel"></aside></body>`);
    // A real native observer runs, but hold delivery to the application callback
    // until the test releases it. This is a scheduler latch, not a timed retry.
    await page.evaluate(() => {
      const nav = document.querySelector('.mode-tabs');
      new ResizeObserver(() => {
        window.deliverSidebarMeasurement = () => document.documentElement.style.setProperty(
          '--studio-nav-height', `${nav.getBoundingClientRect().height}px`,
        );
      }).observe(nav);
    });
    await page.waitForFunction(() => typeof window.deliverSidebarMeasurement === 'function');
    const stale = await page.evaluate(() => ({
      sideTop: document.getElementById('harnessSessionsPanel').getBoundingClientRect().top,
      navBottom: document.querySelector('.mode-tabs').getBoundingClientRect().bottom,
    }));
    check('reproduces CI side=100/nav=437 before observer delivery', stale.sideTop === 100 && stale.navBottom === 437, stale);
    check('the original non-overlap assertion rejects the stale geometry', !(stale.sideTop >= stale.navBottom - 1), stale);

    let ready = false;
    const waiting = waitForHarnessSidebarLayout(page).then(() => { ready = true; });
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    check('layout wait remains pending while measurement is held', !ready);
    check('layout wait does not overwrite the stale measurement', await page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue('--studio-nav-height').trim() === '44px'));
    await page.evaluate(() => window.deliverSidebarMeasurement());
    await waiting;
    check('layout wait completes after actual observer measurement', ready && await page.evaluate(() =>
      document.getElementById('harnessSessionsPanel').getBoundingClientRect().top === 437));

    // Missing/broken delivery must still fail closed, not manufacture alignment.
    await page.evaluate(() => document.documentElement.style.setProperty('--studio-nav-height', '44px'));
    await assert.rejects(waitForHarnessSidebarLayout(page, { timeout: 200 }), /Harness sidebar layout did not settle/);
    check('missing observer delivery times out without changing geometry', await page.evaluate(() =>
      document.getElementById('harnessSessionsPanel').getBoundingClientRect().top === 100));
    for (const width of [390, 820]) {
      await page.setViewport({ width, height: 720 });
      await waitForHarnessSidebarLayout(page);
      check(`desktop barrier does not constrain the ${width}px drawer layout`, true);
    }
    await page.setViewport({ width: 1280, height: 720 });
    await page.evaluate(() => { document.body.dataset.activePane = 'start'; });
    await waitForHarnessSidebarLayout(page);
    check('desktop barrier does not constrain another pane', true);
    check('rendering-barrier regression makes no requests', requests.length === 0, requests);
  } finally {
    await page.close();
  }
}
