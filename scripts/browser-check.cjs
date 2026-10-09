'use strict';

// All responses are synthetic. No request is allowed to leave this browser context.
// Install Playwright separately, then run: node scripts/browser-check.cjs
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const OUTPUT = process.env.APCC_SCREENSHOTS
  ? path.resolve(process.env.APCC_SCREENSHOTS)
  : path.join(ROOT, 'docs', 'assets');
const ORIGIN = 'https://apcc.example';
const COMMANDS = ['beeper.enable', 'beeper.disable', 'beeper.mute', 'beeper.off', 'beeper.on', 'load.off', 'load.off.delay', 'shutdown.reboot', 'shutdown.stop'];
const PARAMETERS = ['battery.charge.low', 'battery.runtime.low', 'ups.delay.shutdown'];
const DANGEROUS = new Set(['load.off', 'load.off.delay', 'shutdown.reboot']);
const capability = {
  supported: false,
  command: null,
  reason: 'This example driver does not provide a panel or audible test. Enabling alarms does not play a test sound.',
};

function freshFixture() {
  const outlets = Array.from({ length: 9 }, (_, i) => ({
    id: `outlet-${i + 1}`,
    connector: 'Unspecified',
    label: `Outlet ${i + 1}`,
    device: ['Example application server', 'Example storage server', 'Example network switch'][i] || '',
    estimated_w: [180, 220, 60][i] ?? null,
  }));
  return {
    offline: false,
    config: {
      display_name: 'Demo UPS', expected_model: 'Demo UPS', device_ip: '192.0.2.10',
      nut_host: '127.0.0.1', nut_port: 3493, ups: 'demo', expected_serial: 'DEMO-UPS-0001',
      nut_username: 'demo_control', has_nut_password: true, poll_interval: 300,
      username: 'admin', beeper_test: capability, outlets, version: '2.0.0',
    },
    variables: {
      'ups.status': 'OL', 'battery.charge': '97', 'battery.charge.low': '10',
      'battery.voltage': '52.4', 'battery.voltage.nominal': '48',
      'battery.runtime': '1800', 'battery.runtime.low': '150',
      'ups.delay.shutdown': '20', 'ups.beeper.status': 'enabled',
      'device.model': 'Demo UPS', 'device.serial': 'DEMO-UPS-0001',
      'ups.firmware': 'Demo firmware', 'driver.name': 'usbhid-ups',
      'driver.version': 'Demo', 'ups.realpower': '460', 'ups.power': '510', 'ups.load': '18',
    },
    audit: [],
  };
}

function statusFor(fixture) {
  const estimated = fixture.config.outlets.filter((outlet) => outlet.estimated_w !== null);
  return {
    ...fixture.config,
    model: fixture.config.expected_model,
    endpoint: `${fixture.config.ups}@${fixture.config.nut_host}:${fixture.config.nut_port}`,
    variables: fixture.variables,
    supported_commands: COMMANDS,
    supported_variables: PARAMETERS,
    checked_at: '2026-01-01T12:00:00Z',
    audit: fixture.audit,
    consumption: {
      measured_total_w: 460, apparent_power_va: 510, load_percent: 18,
      estimated_total_w: estimated.length ? estimated.reduce((sum, item) => sum + item.estimated_w, 0) : null,
      estimated_count: estimated.length, outlet_count: fixture.config.outlets.length,
      per_outlet_metering: false, outlets: fixture.config.outlets,
    },
  };
}

(async () => {
  fs.mkdirSync(OUTPUT, { recursive: true });
  const launchOptions = { headless: true };
  if (process.env.APCC_BROWSER_EXECUTABLE) launchOptions.executablePath = process.env.APCC_BROWSER_EXECUTABLE;
  const browser = await chromium.launch(launchOptions);
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 }, locale: 'en-US', timezoneId: 'UTC', reducedMotion: 'reduce', serviceWorkers: 'block' });
  let fixture = freshFixture();
  const mutations = [];
  const unexpected = [];
  const browserErrors = [];
  const assets = new Map([
    ['/', ['index.html', 'text/html']],
    ['/static/app.js', ['app.js', 'text/javascript']],
    ['/static/style.css', ['style.css', 'text/css']],
    ['/logo.svg', ['logo.svg', 'image/svg+xml']],
  ]);
  await context.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== ORIGIN) {
      unexpected.push('Blocked unexpected external request');
      await route.abort('blockedbyclient');
      return;
    }
    const asset = assets.get(url.pathname);
    if (asset && request.method() === 'GET') {
      await route.fulfill({ status: 200, contentType: asset[1], body: fs.readFileSync(path.join(ROOT, 'static', asset[0])) });
      return;
    }
    const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (request.method() === 'GET') {
      if (url.pathname === '/api/session') return json({ authenticated: true, username: 'admin', csrf: 'synthetic-browser-fixture-token' });
      if (url.pathname === '/api/config') return json(fixture.config);
      if (url.pathname === '/api/status') return fixture.offline ? json({ error: 'Synthetic fixture: UPS is disconnected.' }, 503) : json(statusFor(fixture));
    }
    if (request.method() === 'POST') {
      const body = request.postDataJSON() || {};
      mutations.push({ path: url.pathname, body });
      if (url.pathname === '/api/action') {
        assert(COMMANDS.includes(body.command), 'Unsupported fixture command');
        if (DANGEROUS.has(body.command)) assert.equal(body.confirmation, 'POWER OFF');
        return json({ message: 'Synthetic command accepted. No hardware was contacted.' });
      }
      if (url.pathname === '/api/setting') {
        assert(PARAMETERS.includes(body.variable), 'Unsupported fixture parameter');
        fixture.variables[body.variable] = String(body.value);
        return json({ message: 'Synthetic parameter saved. No hardware was contacted.' });
      }
      if (url.pathname === '/api/config/test') return json({ message: 'Synthetic connection verified.', serial: 'DEMO-UPS-0001', model: 'Demo UPS', commands: COMMANDS, variables: PARAMETERS });
      if (url.pathname === '/api/config') {
        const { request_id, nut_password, ...settings } = body;
        Object.assign(fixture.config, settings);
        return json({ message: 'Synthetic configuration saved.', config: fixture.config });
      }
      if (url.pathname === '/api/outlets') {
        fixture.config.outlets = body.outlets;
        return json({ message: 'Synthetic outlet estimates saved.', outlets: body.outlets });
      }
      if (url.pathname === '/api/beeper-test') return json({ error: 'The example driver does not expose an audible test.' }, 409);
    }
    unexpected.push(`Unexpected fixture route: ${request.method()} ${url.pathname}`);
    return json({ error: 'This route is not part of the synthetic fixture.' }, 404);
  });

  const page = await context.newPage();
  page.on('pageerror', (error) => browserErrors.push(error.message));
  async function ready() {
    await page.goto(ORIGIN);
    await page.waitForFunction(() => document.getElementById('connection-text').textContent === 'UPS connected');
    await page.waitForFunction(() => !document.getElementById('config-fields').disabled && document.querySelectorAll('.outlet-row').length === 9);
  }
  async function confirmMutation(endpoint) {
    const response = page.waitForResponse((value) => new URL(value.url()).pathname === endpoint && value.request().method() === 'POST');
    await page.locator('#confirm-submit').click();
    assert.equal((await response).status(), 200);
    await page.locator('#confirm-dialog').waitFor({ state: 'hidden' });
    await page.waitForFunction(() => !document.getElementById('refresh-button').disabled);
  }
  try {
    await ready();
    assert.equal(await page.locator('html').getAttribute('lang'), 'en');
    assert.equal(await page.locator('#battery-charge').innerText(), '97');
    assert.equal(await page.locator('#battery-voltage').innerText(), '52.4');
    assert.equal(await page.locator('#measured-total').innerText(), '460.0');
    assert.equal(await page.locator('#estimated-total').innerText(), '460.0');
    assert.equal(await page.locator('#beeper-test-button').isDisabled(), true);
    assert.match(await page.locator('#beeper-test-reason').innerText(), /does not provide/);
    assert.equal(await page.locator('#beeper-actions button, #legacy-actions button, #power-actions button').count(), 9);
    await page.locator('.legacy-section summary').click();
    for (const command of COMMANDS) {
      const button = page.locator(`button[aria-label$="(${command})"]`);
      await button.click();
      await page.locator('#confirm-dialog').waitFor({ state: 'visible' });
      assert.match(await page.locator('#confirmation-detail').innerText(), new RegExp(command.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
      if (DANGEROUS.has(command)) {
        assert.equal(await page.locator('#confirm-submit').isDisabled(), true);
        await page.locator('#confirmation-input').fill('incorrect');
        assert.equal(await page.locator('#confirm-submit').isDisabled(), true);
        await page.locator('#confirmation-input').fill('POWER OFF');
      }
      await confirmMutation('/api/action');
    }
    assert.deepEqual(mutations.filter((item) => item.path === '/api/action').map((item) => item.body.command), COMMANDS);

    const parameterValues = ['15', '240', '25'];
    for (let i = 0; i < PARAMETERS.length; i++) {
      const form = page.locator('#parameter-fields form').nth(i);
      await form.locator('input').fill(parameterValues[i]);
      await form.locator('button').click();
      await confirmMutation('/api/setting');
      assert.equal(fixture.variables[PARAMETERS[i]], parameterValues[i]);
    }

    await page.locator('#config-test-button').click();
    await page.waitForFunction(() => document.getElementById('config-feedback').textContent.includes('Synthetic connection verified.'));
    await page.locator('#config-display-name').fill('Example UPS');
    await page.locator('#config-save-button').click();
    await confirmMutation('/api/config');
    assert.equal(fixture.config.display_name, 'Example UPS');
    assert.equal(await page.locator('#page-device-name').innerText(), 'Example UPS');

    await page.locator('#outlet-device-0').fill('Example laboratory device');
    await page.locator('#outlet-estimated_w-0').fill('125');
    await page.locator('#outlets-save-button').click();
    await confirmMutation('/api/outlets');
    assert.equal(fixture.config.outlets[0].estimated_w, 125);
    assert.equal(await page.locator('#estimated-total').innerText(), '405.0');

    fixture.offline = true;
    await page.locator('#refresh-button').click();
    await page.waitForFunction(() => document.getElementById('connection-text').textContent === 'Disconnected');
    for (const button of await page.locator('#beeper-actions button, #legacy-actions button, #power-actions button').all()) assert.equal(await button.isDisabled(), true);
    await page.waitForFunction(() => !document.getElementById('config-fields').disabled);
    assert.equal(await page.locator('#config-display-name').isEditable(), true);

    // Reset every example value before producing shareable screenshots.
    fixture = freshFixture();
    await ready();
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: path.join(OUTPUT, 'dashboard.png') });
    await page.locator('#consumption').screenshot({ path: path.join(OUTPUT, 'outlets.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    await ready();
    await page.evaluate(() => window.scrollTo(0, 0));
    const layout = await page.evaluate(() => ({ document: document.documentElement.scrollWidth, viewport: window.innerWidth }));
    assert(layout.document <= layout.viewport + 1, 'Mobile layout overflows the viewport');
    await page.screenshot({ path: path.join(OUTPUT, 'mobile.png') });
    assert.deepEqual(browserErrors, []);
    assert.deepEqual(unexpected, []);
    console.log('Browser checks passed: nine commands, three parameters, configuration, outlet estimates, capability limits, offline controls, and mobile layout.');
    console.log('Screenshots contain synthetic example data only. No network request or hardware operation was performed.');
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
