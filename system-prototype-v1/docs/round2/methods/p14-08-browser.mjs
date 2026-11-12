import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { readFile, realpath } from 'node:fs/promises'
import { join } from 'node:path'

const site = new URL(process.argv[2])
assert.equal(site.protocol, 'http:')
assert.equal(site.hostname, '127.0.0.1')
const fixtures = await realpath(process.argv[3])
const { chromium } = await import(process.env.GEOPILOT_PLAYWRIGHT_MODULE ?? 'playwright')
const env = { ...process.env, PATH: '/usr/bin:/bin' }
delete env.GEOPILOT_QGIS_PROCESS
delete env.PYTHONPATH
assert.equal(spawnSync('/bin/sh', ['-c', 'if command -v qgis_process; then exit 1; else exit 0; fi'], { env }).status, 0)

const browser = await chromium.launch({
  headless: true,
  env,
  args: ['--enable-webgl', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
})
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true })
  const page = await context.newPage()
  page.setDefaultTimeout(30000)
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  const started = performance.now()

  async function post(path, action, status) {
    const [response] = await Promise.all([
      page.waitForResponse(item => item.request().method() === 'POST' && path.test(new URL(item.url()).pathname)),
      action(),
    ])
    assert.equal(response.status(), status, await response.text())
    return response.json()
  }

  assert.ok((await page.goto(site.href))?.ok())
  const registered = {}
  for (const role of ['network', 'start', 'end']) {
    await page.getByRole('button', { name: '导入数据', exact: true }).click()
    await page.getByLabel('本机文件路径').fill(join(fixtures, 'inputs', `${role}.geojson`))
    const asset = await post(/^\/api\/assets$/, () => page.getByRole('button', { name: '登记资产', exact: true }).click(), 200)
    assert.equal(asset.asset_version, 1)
    registered[role] = { asset_id: asset.asset_id, asset_version: asset.asset_version }
  }

  const method = page.getByRole('region', { name: '方法目录' }).locator('details').filter({ hasText: '路网最短路径' })
  await method.locator('summary').click()
  await method.getByRole('button', { name: '选择方法', exact: true }).click()
  const form = page.getByRole('form', { name: '方法表单' })
  await form.getByLabel('任务目标').fill('求起点到终点的最短路网路径')
  for (const [role, label] of Object.entries({ network: '已节点化路网', start: '起点', end: '终点' })) {
    await form.getByLabel(label, { exact: true }).selectOption({ label: `${role}.geojson · v1` })
  }
  await form.getByLabel('米制投影坐标参考系').fill('EPSG:32650')
  await form.getByLabel('最大吸附距离').fill('5')
  const draft = await post(/^\/api\/tasks\/[^/]+\/plans$/, () => form.getByRole('button', { name: '生成草稿' }).click(), 200)
  assert.equal(draft.status, 'draft')
  assert.deepEqual(draft.method, { id: 'road-network-shortest-path', version: 1 })
  assert.deepEqual(draft.inputs, registered)
  assert.deepEqual(draft.issues, [])
  const confirmed = await post(/^\/api\/plans\/[^/]+\/confirm$/,
    () => page.getByRole('button', { name: '确认方案', exact: true }).click(), 200)
  assert.equal(confirmed.status, 'confirmed')
  const run = await post(/^\/api\/plans\/[^/]+\/runs$/,
    () => page.getByRole('button', { name: '开始运行', exact: true }).click(), 202)
  await page.getByRole('region', { name: '运行' }).getByText('运行成功', { exact: true }).first().waitFor()
  await page.getByText('规则检查通过', { exact: true }).first().waitFor()

  const results = page.getByRole('list', { name: '正式成果' })
  await results.getByText('最短路径路段', { exact: true }).waitFor()
  const summary = results.getByRole('listitem').filter({ hasText: '最短路径摘要' })
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    summary.getByRole('link', { name: '下载原文件' }).click(),
  ])
  assert.equal(await download.failure(), null)
  const document = JSON.parse(await readFile(await download.path(), 'utf8'))
  assert.equal(document.crs, 'EPSG:32650')
  assert.deepEqual(document.edge_ids, ['west', 'east'])
  assert.equal(document.total_distance_m, 60)
  assert.equal(document.start.offset_m, 5)
  assert.equal(document.end.offset_m, 5)
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({
    status: 'PASS', chromium: browser.version(), method: confirmed.method, run_id: run.run_id,
    execution_status: 'SUCCEEDED', check_status: 'PASSED', route_summary: document,
    browser_elapsed_ms: Math.round(performance.now() - started), page_errors: errors,
  }, null, 2))
} finally {
  await browser.close()
}
