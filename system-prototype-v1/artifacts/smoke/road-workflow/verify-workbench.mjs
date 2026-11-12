import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFile, writeFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'

const { chromium } = await import(process.env.GEOPILOT_PLAYWRIGHT_MODULE ?? 'playwright')
assert.ok(process.argv[2], 'Pass a case directory with projected and geodesic GeoPackages and expected.json')
const directory = resolve(process.argv[2])
const site = new URL(process.env.GEOPILOT_WORKBENCH_URL ?? 'http://127.0.0.1:5192')
const selectedScenario = process.argv[3]
const scenarios = JSON.parse(await readFile(join(directory, 'expected.json'), 'utf8'))
  .filter(scenario => !selectedScenario || scenario.name === selectedScenario)
assert.ok(scenarios.length, 'The requested scenario is not in expected.json')
const browser = await chromium.launch({
  headless: true,
  args: ['--enable-webgl', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
})
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex')
const roleNames = { buffer_extent: '道路缓冲区', selected_targets: '目标对象', zone_statistics: '分区统计' }
const stepKeys = [
  'expand_analysis_extent', 'select_roads_from_expanded_extent', 'choose_measurement_method', 'create_buffer',
  'apply_spatial_relation', 'clip_back_to_analysis_extent', 'aggregate_by_zone',
]

try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1060 }, acceptDownloads: true })
  page.setDefaultTimeout(30000)
  const errors = []
  const external = []
  function noteOrigin(address) {
    const url = new URL(address)
    if (['http:', 'https:', 'ws:', 'wss:'].includes(url.protocol) && url.host !== site.host) external.push(address)
  }
  page.on('request', request => noteOrigin(request.url()))
  page.on('websocket', socket => noteOrigin(socket.url()))
  page.on('pageerror', error => errors.push(error.message))

  async function get(path) {
    const response = await page.request.get(new URL(path, site).href)
    assert.ok(response.ok(), await response.text())
    return response
  }

  async function getJson(path) {
    return (await get(path)).json()
  }

  function posted(matches) {
    return page.waitForResponse(response => response.request().method() === 'POST' && matches(new URL(response.url()).pathname))
  }

  async function register(path) {
    const refs = {}
    for (const role of ['analysis_extent', 'roads', 'targets', 'zones']) {
      await page.getByRole('button', { name: '导入数据', exact: true }).click()
      await page.getByLabel('本机文件路径', { exact: true }).fill(path)
      await page.getByRole('button', { name: '读取图层', exact: true }).click()
      await page.getByRole('combobox', { name: 'GeoPackage 图层', exact: true }).selectOption(role)
      const pending = posted(route => route === '/api/assets')
      await page.getByRole('button', { name: '登记资产', exact: true }).click()
      const response = await pending
      assert.equal(response.status(), 200, await response.text())
      const asset = await response.json()
      assert.equal(asset.source.layer, role)
      refs[role] = { asset_id: asset.asset_id, asset_version: asset.asset_version }
    }
    return refs
  }

  async function prepare(scenario, refs) {
    const entry = page.locator('details.method-catalog__entry').filter({ hasText: 'road-buffer-statistics' })
      .filter({ hasText: `版本 ${scenario.version}` })
    await entry.locator('summary').click()
    await entry.getByRole('button', { name: '选择方法', exact: true }).click()
    const form = page.getByRole('form', { name: '方法表单' })
    const field = name => form.locator(`[name="critical_options.${name}"]`)
    await form.getByLabel('任务目标', { exact: true }).fill(`独立验收道路缓冲 ${scenario.name}`)
    await field('distance_method').selectOption('local_projected')
    await field('distance_crs_ref').fill(scenario.distance_crs_ref ?? 'EPSG:32650')
    if (scenario.mode === 'geodesic') {
      await field('distance_method').selectOption('geodesic')
      await field('distance_crs_ref').waitFor({ state: 'detached' })
    }
    await field('distance_m').fill('500')
    const choices = {
      spatial_relation: scenario.predicate ?? 'intersects', boundary_assignment: 'centroid', boundary_tie: 'unassigned',
      statistic: 'count', output_scope: 'clipped_to_analysis_extent',
    }
    for (const [name, value] of Object.entries(choices)) await field(name).selectOption(value)
    for (const [role, ref] of Object.entries(refs)) {
      await form.locator(`select[name="inputs.${role}"]`).selectOption(`${ref.asset_id}@${ref.asset_version}`)
    }
    const pendingDraft = posted(route => /^\/api\/tasks\/[^/]+\/plans$/.test(route))
    await form.getByRole('button', { name: '生成草稿', exact: true }).click()
    const draftResponse = await pendingDraft
    assert.equal(draftResponse.status(), 200, await draftResponse.text())
    const draft = await draftResponse.json()
    const options = { ...choices, distance_m: 500, distance_method: scenario.mode }
    if (scenario.mode === 'local_projected') options.distance_crs_ref = scenario.distance_crs_ref
    assert.deepEqual(draft.critical_options, options)
    assert.deepEqual(draft.inputs, refs)
    assert.deepEqual(draft.issues, [], JSON.stringify(draft.issues))
    assert.deepEqual(draft.steps.map(step => step.key), stepKeys)
    const expectedTools = scenario.mode === 'geodesic'
      ? ['python-expand-wgs84-analysis-extent', 'python-select-wgs84-roads', 'python-prepare-wgs84-measurement-lines', 'python-wgs84-geodesic-buffer']
      : ['qgis-expand-analysis-extent', 'qgis-select-roads-by-location', 'python-prepare-measurement-lines', 'qgis-buffer']
    assert.deepEqual(draft.steps.slice(0, 4).map(step => step.tool.id), expectedTools)
    assert.equal(await page.getByRole('button', { name: '开始运行', exact: true }).isDisabled(), true)
    const pendingConfirm = posted(route => route === `/api/plans/${draft.id}/confirm`)
    await page.getByRole('button', { name: '确认方案', exact: true }).click()
    const confirmation = await pendingConfirm
    assert.equal(confirmation.status(), 200, await confirmation.text())
    const plan = await confirmation.json()
    assert.equal(plan.status, 'confirmed')
    assert.deepEqual(plan.steps, draft.steps)
    assert.deepEqual((await getJson(`/api/tasks/${plan.task_id}`)).runs, [])
    return plan
  }

  async function run(plan) {
    const pending = posted(route => route === `/api/plans/${plan.id}/runs`)
    await page.getByRole('button', { name: '开始运行', exact: true }).click()
    const response = await pending
    assert.equal(response.status(), 202, await response.text())
    const { run_id: runId } = await response.json()
    const base = `/api/runs/${runId}`
    const deadline = Date.now() + 120000
    let state
    do {
      state = await getJson(base)
      if (state.current_report_id || ['FAILED', 'CANCELLED'].includes(state.execution_status)) break
      await new Promise(resolvePoll => setTimeout(resolvePoll, 250))
    } while (Date.now() < deadline)
    assert.deepEqual([state.execution_status, state.check_status], ['SUCCEEDED', 'PASSED'], JSON.stringify(state))
    const [origin, steps, registered, checks] = await Promise.all(
      ['/origin', '/steps', '/artifacts', '/checks'].map(suffix => getJson(base + suffix)),
    )
    assert.deepEqual(origin, { run_id: runId, task_id: plan.task_id, plan_id: plan.id, plan_version: plan.version })
    assert.deepEqual(steps.steps.map(step => step.step_key), stepKeys)
    assert.ok(steps.steps.every(step => step.status === 'SUCCEEDED' && step.log_available))
    assert.deepEqual(registered.artifacts.map(item => item.role).sort(), Object.keys(roleNames).sort())
    assert.equal(checks.current_report.id, state.current_report_id)
    assert.ok(Object.values(checks.current_report.report.rules).every(rule => rule.passed))
    await page.getByText('运行成功', { exact: true }).first().waitFor()
    await page.getByText('规则检查通过', { exact: true }).first().waitFor()
    return { origin, state, plan, artifacts: registered.artifacts, report: checks.current_report }
  }

  function artifactRow(role) {
    return page.getByRole('list', { name: '正式成果' }).getByRole('listitem').filter({ hasText: roleNames[role] })
  }

  async function checkArtifacts(scenario, result) {
    const artifacts = Object.fromEntries(result.artifacts.map(item => [item.role, item]))
    const downloads = {}
    for (const [role, artifact] of Object.entries(artifacts)) {
      assert.ok(artifact.readable, role)
      const file = `${scenario.name}-${role}.${role === 'zone_statistics' ? 'csv' : 'gpkg'}`
      const pending = page.waitForEvent('download')
      await artifactRow(role).getByRole('link', { name: '下载原文件', exact: true }).click()
      await (await pending).saveAs(join(directory, file))
      const bytes = await readFile(join(directory, file))
      assert.ok(bytes.equals(await (await get(`/api/artifacts/${artifact.id}/file`)).body()))
      downloads[role] = { file, id: artifact.id, sha256: sha256(bytes), fingerprint: artifact.fingerprint }
    }

    const table = await getJson(`/api/artifacts/${artifacts.zone_statistics.id}/table`)
    assert.equal(table.truncated, false)
    const rows = table.rows.map(row => Object.fromEntries(row.map((value, index) => [table.columns[index], value])))
    const counts = Object.fromEntries(rows.map(row => [row.zone_id || 'unassigned', Number(row.count)]))
    counts.total = rows.reduce((total, row) => total + Number(row.count), 0)
    assert.deepEqual(counts, scenario.counts)
    const ids = rows.flatMap(row => JSON.parse(row.source_feature_ids)).sort()
    assert.deepEqual(ids, [...scenario.selected_ids].sort())
    assert.deepEqual(result.report.report.rules.outside_roads_within_influence_included.observed.actual_ids, scenario.candidate_ids)
    assert.deepEqual(result.report.report.rules.output_clipped_to_analysis_extent.observed.outside_ids, [])

    const steps = page.getByRole('list', { name: '运行步骤' })
    const bufferStep = steps.getByRole('listitem').filter({ hasText: 'create_buffer' })
    await bufferStep.getByRole('button', { name: '查看日志', exact: true }).click()
    const log = page.locator('.run-panel__log pre')
    await log.waitFor()
    assert.match(await log.innerText(), /exit_code: 0/)
    const href = await page.getByRole('link', { name: '查看完整日志', exact: true }).getAttribute('href')
    const algorithm = scenario.mode === 'geodesic' ? /wgs84_geodesic_buffer\.py/ : /native:buffer/
    assert.match(await (await get(href)).text(), algorithm)
    await artifactRow('zone_statistics').getByRole('button', { name: '查看统计', exact: true }).click()
    await artifactRow('zone_statistics').getByText(rows[0].zone_id, { exact: true }).waitFor()
    await artifactRow('buffer_extent').getByRole('button', { name: '地图定位', exact: true }).click()
    await page.locator('canvas.maplibregl-canvas').waitFor()
    await page.screenshot({ path: join(directory, `${scenario.name}.png`) })
    return { counts, ids, downloads }
  }

  const results = {}
  let legacyReplay
  if (!selectedScenario) {
    const baselinePath = join(directory, 'browser-local-projected-v2.json')
    const baselineBytes = await readFile(baselinePath).catch(error => {
      if (error.code === 'ENOENT') return null
      throw error
    })
    if (baselineBytes) {
      const baseline = JSON.parse(baselineBytes).results['local-projected-v2']
      const legacyURL = new URL(site)
      legacyURL.searchParams.set('run', baseline.origin.run_id)
      await page.goto(legacyURL.href)
      await artifactRow('buffer_extent').waitFor()
      const saved = await getJson(`/api/plans/${baseline.plan.id}?version=${baseline.plan.version}`)
      assert.deepEqual(saved, baseline.plan)
      const replay = await run(saved)
      assert.notEqual(replay.origin.run_id, baseline.origin.run_id)
      assert.deepEqual(replay.plan, baseline.plan)
      const scenario = { ...scenarios.find(item => item.name === 'local-projected-v2'), name: 'local-projected-v2-replay' }
      assert.equal(sha256(await readFile(join(directory, scenario.file))), baseline.original_sha256)
      legacyReplay = {
        ...replay, ...await checkArtifacts(scenario, replay), original_run_id: baseline.origin.run_id,
        original_sha256: baseline.original_sha256,
      }
      console.log(`PASS local-projected-v2-replay: ${legacyReplay.origin.run_id}`)
    }
  }
  const refsByFile = new Map()
  for (const scenario of scenarios) {
    await page.goto(site.href)
    const path = join(directory, scenario.file)
    const original = sha256(await readFile(path))
    if (!refsByFile.has(path)) refsByFile.set(path, await register(path))
    const plan = await prepare(scenario, refsByFile.get(path))
    const result = await run(plan)
    const evidence = await checkArtifacts(scenario, result)
    assert.equal(sha256(await readFile(path)), original)
    const restored = new URL(site)
    restored.searchParams.set('run', result.origin.run_id)
    await page.goto(restored.href)
    await artifactRow('buffer_extent').waitFor()
    assert.equal(new URL(page.url()).searchParams.get('version'), String(plan.version))
    results[scenario.name] = { ...result, ...evidence, original_sha256: original }
    console.log(`PASS ${scenario.name}: run ${result.origin.run_id}, counts ${JSON.stringify(evidence.counts)}`)
  }
  assert.equal(new Set(Object.values(results).map(item => item.origin.run_id)).size, scenarios.length)
  assert.deepEqual(errors, [])
  assert.deepEqual(external, [])
  await writeFile(join(directory, selectedScenario ? `browser-${selectedScenario}.json` : 'browser.json'), JSON.stringify({ chromium: browser.version(), results, legacy_replay: legacyReplay, errors, external }, null, 2) + '\n')
} finally {
  await browser.close()
}
