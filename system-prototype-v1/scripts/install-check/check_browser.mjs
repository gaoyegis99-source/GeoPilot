import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'

const site = new URL(process.argv[2])
assert.equal(site.protocol, 'http:')
assert.equal(site.hostname, '127.0.0.1')

const { chromium } = await import(process.env.GEOPILOT_PLAYWRIGHT_MODULE ?? 'playwright')
const env = { ...process.env, PATH: '/usr/bin:/bin' }
delete env.GEOPILOT_QGIS_PROCESS
delete env.PYTHONPATH
const lookup = spawnSync('/bin/sh', ['-c', 'if command -v qgis_process; then exit 1; else exit 0; fi'], { env })
assert.equal(lookup.status, 0, 'Browser test environment must not expose a QGIS CLI')

const browser = await chromium.launch({
  headless: true,
  env,
  args: ['--enable-webgl', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
})
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  const page = await context.newPage()
  page.setDefaultTimeout(30000)
  const errors = []
  const externalRequests = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('request', request => {
    const url = new URL(request.url())
    if (['http:', 'https:'].includes(url.protocol) && url.origin !== site.origin) {
      externalRequests.push(url.origin)
    }
  })
  const catalogResponse = page.waitForResponse(response =>
    new URL(response.url()).pathname === '/api/methods' && response.request().method() === 'GET',
  )
  const html = await page.goto(site.href)
  assert.ok(html?.ok(), 'Workbench page did not load')
  await page.getByRole('heading', { level: 1, name: 'GeoPilot 工作台' }).waitFor()
  const response = await catalogResponse
  assert.equal(response.status(), 200)
  const { methods } = await response.json()
  const target = methods.find(({ definition }) => definition.id === 'road-buffer-statistics' && definition.version === 3)
  assert.ok(target?.available, 'Road buffer method v3 is unavailable')

  const item = page.getByRole('region', { name: '方法目录' }).locator('details')
    .filter({ hasText: 'road-buffer-statistics' }).filter({ hasText: '版本 3' })
  await item.locator('summary').click()
  await item.getByRole('button', { name: '选择方法', exact: true }).click()
  await item.locator('summary[aria-current="true"]').waitFor()
  assert.deepEqual(errors, [])
  assert.deepEqual(externalRequests, [])
  console.log(JSON.stringify({
    schema_version: 1,
    status: 'PASS',
    chromium: browser.version(),
    client_qgis_on_path: false,
    fresh_browser_context: true,
    http_status: { workbench: html.status(), methods_proxy: response.status() },
    methods: methods.length,
    available_methods: methods.filter(method => method.available).length,
    selected: { id: target.definition.id, version: target.definition.version },
    page_errors: errors,
    external_requests: externalRequests,
  }, null, 2))
} finally {
  await browser.close()
}
