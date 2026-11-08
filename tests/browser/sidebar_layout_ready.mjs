/** Test-only rendering barrier; never writes application layout or state. */
export async function waitForHarnessSidebarLayout(page, { timeout = 5000 } = {}) {
  try {
    await page.waitForFunction(() => {
      if (document.body.dataset.activePane !== 'harness' || innerWidth < 1280) return true;
      const nav = document.querySelector('.mode-tabs');
      const side = document.getElementById('harnessSessionsPanel');
      if (!nav || !side) return false;
      const navBox = nav.getBoundingClientRect();
      const sideBox = side.getBoundingClientRect();
      const measuredHeight = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--studio-nav-height'));
      return navBox.height > 0 && sideBox.height > 0
        && Math.abs(measuredHeight - navBox.height) < 1
        && Math.abs(sideBox.top - navBox.bottom) < 1;
    }, { polling: 'raf', timeout });
  } catch (cause) {
    const state = await page.evaluate(() => ({
      pane: document.body.dataset.activePane, width: innerWidth,
      measuredHeight: getComputedStyle(document.documentElement).getPropertyValue('--studio-nav-height'),
      nav: document.querySelector('.mode-tabs')?.getBoundingClientRect().toJSON(),
      side: document.getElementById('harnessSessionsPanel')?.getBoundingClientRect().toJSON(),
    }));
    throw new Error(`Harness sidebar layout did not settle: ${JSON.stringify(state)}`, { cause });
  }
}
