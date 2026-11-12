import { useEffect, useRef, useState } from 'react'
import type { PlanComparison } from './api/plan-alternatives'
import { type AnalysisPlan, type AssetEntry, type AssetRef, type ContextCriteria, type Json, type MethodEntry, type MethodRef, type ModelPreview, type RunArtifacts, type RunStatus, type TaskContext, type TaskRecord, workbenchApi } from './api/workbench'
import { ProblemDetails, requestProblem, type Problem } from './components/errors/ProblemDetails'
import { ArtifactsPanel, artifactTitle } from './features/artifacts/ArtifactsPanel'
import { AssetPanel, assetKey } from './features/assets/AssetPanel'
import { ChecksPanel } from './features/checks/ChecksPanel'
import { MethodCatalogPanel } from './features/methods/MethodCatalogPanel'
import { MethodForm, type MethodFormRequest } from './features/method-form/MethodForm'
import { MapPanel, type MapPreviewLayer } from './features/map/MapPanel'
import { ModelAssistPanel } from './features/model-assistance/ModelAssistPanel'
import { ConfirmPlanButton, StartPlanButton } from './features/plans/PlanActions'
import { AlternativePlansPanel } from './features/plans/AlternativePlansPanel'
import { ClarificationPanel } from './features/plans/ClarificationPanel'
import { PlanPreview } from './features/plans/preview/PlanPreview'
import { isRunActive, RunPanel } from './features/runs/RunPanel'
import { ScriptRunsPage } from './features/scripts/ScriptRunsPage'
import { TaskHistory } from './features/session/TaskHistory'
import { hasWorkbenchLocation, mapLayersLocation, readMapLayers, readWorkbenchSession, SessionLinkError, workbenchLocation } from './features/session/workbench-session'
import { WorkbenchPage } from './pages/WorkbenchPage'

export function App() {
  const view = new URLSearchParams(window.location.search).get('view')
  return view === 'scripts' ? <ScriptRunsPage /> : <AnalysisWorkbench />
}

function AnalysisWorkbench() {
  const [initialSearch] = useState(() => window.location.search)
  const [restoring, setRestoring] = useState(() => hasWorkbenchLocation(initialSearch))
  const [restoreError, setRestoreError] = useState<Problem | null>(null)
  const [assets, setAssets] = useState<AssetEntry[]>([])
  const [visibleAssets, setVisibleAssets] = useState(() => readMapLayers(initialSearch))
  const [assetsLoading, setAssetsLoading] = useState(true)
  const [assetError, setAssetError] = useState<string | null>(null)
  const [methods, setMethods] = useState<MethodEntry[]>([])
  const [selectedMethod, setSelectedMethod] = useState<MethodRef | null>(null)
  const [methodsLoading, setMethodsLoading] = useState(true)
  const [methodError, setMethodError] = useState<string | null>(null)
  const [taskId, setTaskId] = useState<string | null>(null)
  const [taskRecord, setTaskRecord] = useState<TaskRecord | null>(null)
  const [goal, setGoal] = useState('')
  const [inputs, setInputs] = useState<Partial<Record<string, AssetRef>>>({})
  const [options, setOptions] = useState<Partial<Record<string, Json>>>({})
  const [context, setContext] = useState<TaskContext | null>(null)
  const [criteria, setCriteria] = useState<ContextCriteria>({})
  const [plan, setPlan] = useState<AnalysisPlan | null>(null)
  const [draftDirty, setDraftDirty] = useState(false)
  const [preparing, setPreparing] = useState(false)
  const [taskError, setTaskError] = useState<Problem | null>(null)
  const [actionPending, setActionPending] = useState<'confirm' | 'start' | 'answer' | 'alternative' | null>(null)
  const [actionError, setActionError] = useState<Problem | null>(null)
  const methodForm = useRef<HTMLDivElement>(null)
  const methodCatalog = useRef<HTMLDivElement>(null)
  const [modelAvailable, setModelAvailable] = useState<boolean | null>(null)
  const [modelPending, setModelPending] = useState(false)
  const [modelSuggestion, setModelSuggestion] = useState<ModelPreview | null>(null)
  const [modelError, setModelError] = useState<Problem | null>(null)
  const modelRequest = useRef<AbortController | null>(null)
  const [runId, setRunId] = useState<string | null>(null)
  const [runPlan, setRunPlan] = useState<MethodRef | null>(null)
  const [runStatus, setRunStatus] = useState<RunStatus | null>(null)
  const [queuedReport, setQueuedReport] = useState<string>()
  const [recheckSubmitting, setRecheckSubmitting] = useState(false)
  const [recheckError, setRecheckError] = useState<Problem | null>(null)
  const [artifacts, setArtifacts] = useState<RunArtifacts | null>(null)
  const [artifactsLoading, setArtifactsLoading] = useState(false)
  const [artifactError, setArtifactError] = useState<string | null>(null)
  const [focusLayerId, setFocusLayerId] = useState<string>()
  const [focusRevision, setFocusRevision] = useState(0)
  const [selectedArtifactId, setSelectedArtifactId] = useState<string>()

  useEffect(() => {
    if (!hasWorkbenchLocation(initialSearch)) return
    const abort = new AbortController()
    readWorkbenchSession(initialSearch, abort.signal)
      .then((session) => {
        if (abort.signal.aborted || !session) return
        const { task, plan: savedPlan, run } = session
        const latest = task.plans[0]
        const savedContext = !savedPlan || (latest?.id === savedPlan.id && latest.version === savedPlan.version) ? task.context : null
        setTaskId(task.task_id)
        setTaskRecord(task)
        setPlan(savedPlan)
        setGoal((savedPlan?.goal ?? task.goal).summary)
        setContext(savedContext)
        setCriteria(savedContext?.criteria ?? {})
        setSelectedMethod(savedPlan?.method ?? task.context?.method ?? null)
        setInputs(savedPlan?.inputs ?? Object.fromEntries(Object.entries(savedContext?.roles ?? {}).flatMap(
          ([role, assessment]) => assessment.asset ? [[role, assessment.asset]] : [],
        )))
        setOptions(savedPlan?.critical_options ?? {})
        if (run) {
          setRunId(run.run_id)
          setRunPlan({ id: run.plan_id, version: run.plan_version })
        }
        window.history.replaceState(null, '', workbenchLocation(new URL(window.location.href), {
          taskId: task.task_id, plan: savedPlan, runId: run?.run_id,
        }))
      })
      .catch((cause: unknown) => {
        if (abort.signal.aborted) return
        setRestoreError(cause instanceof SessionLinkError
          ? { phase: 'plan', code: 'INVALID_WORKBENCH_LINK', message: cause.message }
          : requestProblem(cause, 'plan', '无法恢复工作台记录。'))
      })
      .finally(() => {
        if (!abort.signal.aborted) setRestoring(false)
      })
    return () => abort.abort()
  }, [initialSearch])

  useEffect(() => {
    const abort = new AbortController()
    workbenchApi.assets(abort.signal)
      .then((catalogue) => setAssets(catalogue.assets))
      .catch(() => {
        if (!abort.signal.aborted) setAssetError('无法读取本机资产目录。')
      })
      .finally(() => {
        if (!abort.signal.aborted) setAssetsLoading(false)
      })
    return () => abort.abort()
  }, [])

  useEffect(() => {
    const abort = new AbortController()
    workbenchApi.modelAvailability(abort.signal)
      .then((status) => {
        if (!abort.signal.aborted) setModelAvailable(status.available)
      })
      .catch(() => {
        if (!abort.signal.aborted) setModelAvailable(false)
      })
    return () => {
      abort.abort()
      modelRequest.current?.abort()
    }
  }, [])

  useEffect(() => {
    if (!runId) return
    const abort = new AbortController()
    setArtifactsLoading(true)
    setArtifactError(null)
    workbenchApi.artifacts(runId, abort.signal)
      .then((record) => {
        if (!abort.signal.aborted) setArtifacts(record)
      })
      .catch(() => {
        if (!abort.signal.aborted) setArtifactError('无法读取本次运行的成果。')
      })
      .finally(() => {
        if (!abort.signal.aborted) setArtifactsLoading(false)
      })
    return () => abort.abort()
  }, [runId, runStatus?.current_step, runStatus?.execution_status, runStatus?.check_status])

  useEffect(() => {
    const abort = new AbortController()
    workbenchApi.methods(abort.signal)
      .then((catalogue) => setMethods(catalogue.methods))
      .catch(() => {
        if (!abort.signal.aborted) setMethodError('无法读取本机方法目录。')
      })
      .finally(() => {
        if (!abort.signal.aborted) setMethodsLoading(false)
      })
    return () => abort.abort()
  }, [])

  function toggleAsset(asset: AssetEntry) {
    const key = assetKey(asset)
    const next = visibleAssets.some((entry) => assetKey(entry) === key)
      ? visibleAssets.filter((entry) => assetKey(entry) !== key) : [...visibleAssets, asset]
    setVisibleAssets(next)
    window.history.replaceState(null, '', mapLayersLocation(new URL(window.location.href), next))
  }

  async function registerAsset(path: string, layer?: string) {
    const asset = await workbenchApi.register(path, layer)
    setAssets((previous) => [asset, ...previous.filter((entry) => assetKey(entry) !== assetKey(asset))])
    setAssetError(null)
    return asset
  }

  function selectMethod(method: MethodRef) {
    if (preparing || actionPending || restoring || restoreError) return
    if (method.id === selectedMethod?.id && method.version === selectedMethod.version) return
    setSelectedMethod(method)
    setInputs({})
    setOptions({})
    setContext(null)
    setCriteria({})
    setTaskError(null)
    setActionError(null)
    clearModelSuggestion()
  }

  async function prepare(request: MethodFormRequest) {
    setPreparing(true)
    setTaskError(null)
    try {
      const id = taskId ?? (await workbenchApi.createTask(request.goal)).task_id
      setTaskId(id)
      const baseline = plan?.task_id === id && plan.method.id === request.method.id && plan.method.version === request.method.version
        ? { base_plan_id: plan.id, base_plan_version: plan.version } : {}
      const draft = await workbenchApi.draft(id, { ...request, ...baseline })
      setPlan(draft)
      setDraftDirty(false)
      setActionError(null)
      window.history.replaceState(null, '', workbenchLocation(new URL(window.location.href), {
        taskId: id, plan: draft, runId,
      }))
      const task = await workbenchApi.task(id)
      setTaskRecord(task)
      setContext(task.context)
    } catch (cause) {
      setTaskError(requestProblem(cause, 'plan', '无法生成方案草稿。'))
    } finally {
      setPreparing(false)
    }
  }

  function locateArtifact(id: string) {
    setSelectedArtifactId(id)
    setFocusLayerId(id)
    setFocusRevision((previous) => previous + 1)
  }

  function editDraft(field?: string) {
    const form = methodForm.current?.querySelector('form')
    if (!form) return
    const parts = field?.split('.') ?? []
    let control: Element | RadioNodeList | null = null
    while (!control && parts.length) {
      control = form.elements.namedItem(parts.join('.'))
      parts.pop()
    }
    const target = control instanceof HTMLElement ? control : form.querySelector('textarea')
    target?.scrollIntoView({ block: 'nearest' })
    target?.focus({ preventScroll: true })
  }

  function clearModelSuggestion() {
    modelRequest.current?.abort()
    modelRequest.current = null
    setModelPending(false)
    setModelSuggestion(null)
    setModelError(null)
  }

  function changeGoal(value: string) {
    clearModelSuggestion()
    setGoal(value)
    setDraftDirty(true)
  }

  async function requestSuggestion() {
    if (!modelAvailable || !goal.trim() || modelPending || restoring || restoreError) return
    const controller = new AbortController()
    modelRequest.current = controller
    setModelPending(true)
    setModelError(null)
    setModelSuggestion(null)
    const selected: Record<string, AssetRef> = {}
    for (const [role, ref] of Object.entries(inputs)) {
      if (ref) selected[role] = ref
    }
    const matchValue = options.match_value
    const protectedLiterals = typeof matchValue === 'string' && matchValue.length > 0 ? [matchValue] : undefined
    try {
      const preview = await workbenchApi.modelSuggestion({
        goal: { summary: goal.trim() }, method: selectedMethod ?? undefined,
        inputs: selected, protected_literals: protectedLiterals,
      }, controller.signal)
      if (!controller.signal.aborted) setModelSuggestion(preview)
    } catch (cause) {
      if (!controller.signal.aborted) setModelError(requestProblem(cause, 'model', '暂时无法获取模型建议。'))
    } finally {
      if (modelRequest.current === controller) {
        modelRequest.current = null
        setModelPending(false)
      }
    }
  }

  function applySuggestion() {
    if (!modelSuggestion || preparing || actionPending) return
    const suggested = methods.find((entry) => entry.definition.id === modelSuggestion.method.id
      && entry.definition.version === modelSuggestion.method.version && entry.available)
    if (!suggested) {
      setModelError({ phase: 'model', code: 'MODEL_CANDIDATE_UNAVAILABLE', message: '当前目录没有该方法版本，可以手工选择其他方法。' })
      return
    }
    const same = suggested.definition.id === selectedMethod?.id && suggested.definition.version === selectedMethod.version
    if (!same) {
      setSelectedMethod(modelSuggestion.method)
      setContext(null)
      setCriteria({})
      setInputs((previous) => Object.fromEntries(Object.entries(previous).filter(([role, value]) =>
        value && role in suggested.definition.inputs
        && method?.definition.inputs[role] === suggested.definition.inputs[role],
      )))
    }
    setOptions((previous) => ({ ...(same ? previous : {}), ...modelSuggestion.options }))
    setDraftDirty(true)
    clearModelSuggestion()
  }

  function continueManually() {
    clearModelSuggestion()
    if (methodForm.current) editDraft()
    else methodCatalog.current?.querySelector('summary')?.focus()
  }

  async function recheck() {
    if (!runId || recheckSubmitting || queuedReport) return
    setRecheckSubmitting(true)
    setRecheckError(null)
    try {
      setQueuedReport((await workbenchApi.recheck(runId)).report_id)
    } catch (cause) {
      setRecheckError(requestProblem(cause, 'check', '无法提交重新检查。'))
    } finally {
      setRecheckSubmitting(false)
    }
  }

  function receiveStatus(status: RunStatus) {
    setRunStatus(status)
    setTaskRecord((record) => record && {
      ...record,
      runs: record.runs.map((run) => run.run_id === status.run_id
        ? { ...run, execution_status: status.execution_status, check_status: status.check_status } : run),
    })
    if (queuedReport && (queuedReport === status.current_report_id || status.error?.code === 'STORAGE_FAILED')) {
      setQueuedReport(undefined)
    }
  }

  async function showSavedDraft(next: AnalysisPlan) {
    clearModelSuggestion()
    setSelectedMethod(next.method)
    setPlan(next)
    setGoal(next.goal.summary)
    setInputs(next.inputs)
    setOptions(next.critical_options)
    setContext(null)
    setDraftDirty(false)
    window.history.replaceState(null, '', workbenchLocation(new URL(window.location.href), {
      taskId: next.task_id, plan: next, runId,
    }))
    try {
      const task = await workbenchApi.task(next.task_id)
      setTaskRecord(task)
      if (task.plans[0]?.id === next.id && task.plans[0].version === next.version) {
        setContext(task.context)
        setCriteria(task.context?.criteria ?? {})
      }
    } catch {
      setActionError({ phase: 'plan', code: 'TASK_REFRESH_FAILED', message: '草稿已保存。请刷新页面以恢复任务记录。' })
    }
  }

  async function answerClarification(ref: AnalysisPlan, field: string, value: Json) {
    if (draftDirty || preparing || actionPending) return
    setActionPending('answer')
    setActionError(null)
    try {
      await showSavedDraft(await workbenchApi.answerClarification(ref.id, ref.version, field, value))
    } catch (cause) {
      setActionError(requestProblem(cause, 'plan', '无法保存澄清回答。'))
    } finally {
      setActionPending(null)
    }
  }

  async function chooseAlternative(base: AnalysisPlan, { plan: choice, criteria: nextCriteria }: PlanComparison) {
    if (draftDirty || preparing || actionPending || choice.status !== 'draft') return
    if (choice.task_id !== base.task_id || choice.id !== base.id || choice.version !== base.version) return
    setActionPending('alternative')
    setActionError(null)
    try {
      const next = await workbenchApi.draft(base.task_id, {
        base_plan_id: base.id, base_plan_version: base.version, method: choice.method,
        goal: base.goal, inputs: choice.inputs, critical_options: choice.critical_options, criteria: nextCriteria,
      })
      await showSavedDraft(next)
    } catch (cause) {
      setActionError(requestProblem(cause, 'plan', '无法保存替代方案草稿。'))
    } finally {
      setActionPending(null)
    }
  }

  async function confirmPlan(ref: MethodRef) {
    setActionPending('confirm')
    setActionError(null)
    try {
      const confirmed = await workbenchApi.confirm(ref.id, ref.version)
      setPlan(confirmed)
      setTaskRecord((record) => record && {
        ...record, plans: record.plans.map((entry) => entry.id === confirmed.id && entry.version === confirmed.version
          ? { ...entry, status: confirmed.status } : entry),
      })
    } catch (cause) {
      setActionError(requestProblem(cause, 'plan', '无法确认方案。'))
    } finally {
      setActionPending(null)
    }
  }

  async function startRun(ref: MethodRef) {
    if (!taskId || actionPending) return
    setActionPending('start')
    setActionError(null)
    try {
      const { run_id: id } = await workbenchApi.start(ref.id, ref.version)
      const task = await workbenchApi.task(taskId)
      const record = task.runs.find((run) => run.run_id === id)
      if (record?.plan_id !== ref.id || record.plan_version !== ref.version) {
        setActionError({
          phase: 'execute', code: 'RUN_SLOT_OCCUPIED', field: 'run_id',
          message: '本机正在处理另一个方案的运行。', suggestion: '等该运行完成后再启动当前方案。',
        })
        return
      }
      if (id === runId) return
      setTaskRecord(task)
      setRunId(id)
      setRunPlan(ref)
      setRunStatus(null)
      setArtifacts(null)
      setSelectedArtifactId(undefined)
      setQueuedReport(undefined)
      setRecheckError(null)
      window.history.replaceState(null, '', workbenchLocation(new URL(window.location.href), {
        taskId, plan, runId: id,
      }))
    } catch (cause) {
      setActionError(requestProblem(cause, 'execute', '无法启动运行。'))
    } finally {
      setActionPending(null)
    }
  }

  const method = methods.find((entry) => entry.definition.id === selectedMethod?.id
    && entry.definition.version === selectedMethod.version)
  const selectedPlan = plan && plan.method.id === selectedMethod?.id && plan.method.version === selectedMethod?.version
    ? plan : null
  const canRecheck = runStatus?.execution_status === 'SUCCEEDED' && runStatus.check_status === 'NOT_CHECKED'
    && Boolean(runStatus.current_report_id)
  const startButton = <StartPlanButton plan={selectedPlan} dirty={draftDirty}
    busy={Boolean(preparing || actionPending || isRunActive(runStatus))} onStart={(ref) => { void startRun(ref) }} />
  const mapLayers: MapPreviewLayer[] = visibleAssets.flatMap((ref) => {
    const asset = assets.find((entry) => assetKey(entry) === assetKey(ref))
    if (asset?.metadata.kind !== 'vector') return []
    return [{
      id: assetKey(ref),
      name: asset.source.layer ?? asset.source.path.split(/[\\/]/).at(-1) ?? asset.asset_id,
      previewUrl: workbenchApi.assetPreviewUrl(ref),
      kind: 'asset',
    }]
  })
  for (const artifact of artifacts?.artifacts ?? []) {
    if (!artifact.readable) continue
    const layer = { id: artifact.id, name: artifactTitle(artifact.role), kind: 'artifact' } as const
    if (artifact.kind === 'vector') {
      mapLayers.push({ ...layer, previewUrl: workbenchApi.artifactPreviewUrl(artifact.id) })
    } else if (artifact.kind === 'raster') {
      mapLayers.push({
        ...layer, previewType: 'raster', previewUrl: workbenchApi.artifactRasterPreviewUrl(artifact.id),
        sourceFingerprint: artifact.fingerprint,
      })
    }
  }
  const scriptsUrl = new URL(window.location.href)
  scriptsUrl.searchParams.set('view', 'scripts')
  const newTaskUrl = workbenchLocation(new URL(window.location.href), {})

  return (
    <WorkbenchPage
      scriptRunsHref={scriptsUrl.pathname + scriptsUrl.search + scriptsUrl.hash}
      assets={<AssetPanel
        assets={assets} visible={visibleAssets} assigned={inputs} loading={assetsLoading} error={assetError}
        onToggle={toggleAsset} onRegister={registerAsset}
        onListLayers={async (path) => (await workbenchApi.layers(path)).layers}
      />}
      map={<MapPanel layers={mapLayers} focusLayerId={focusLayerId} focusRevision={focusRevision} onArtifactSelect={setSelectedArtifactId} />}
      task={<>
        {(taskId || restoreError) && <a href={newTaskUrl.href} className="task-reset">新建任务</a>}
        {taskRecord && <TaskHistory task={taskRecord} currentPlan={plan} currentRunId={runId} />}
        {restoring && <p role="status">正在恢复任务…</p>}
        {restoreError && <ProblemDetails problem={restoreError} />}
        {!restoring && !restoreError && <ModelAssistPanel
          available={modelAvailable} pending={modelPending} goal={goal} showGoal={!method}
          suggestion={modelSuggestion} problem={modelError}
          onGoal={changeGoal} onRequest={() => { void requestSuggestion() }} onApply={applySuggestion} onManual={continueManually}
        />}
        <div ref={methodCatalog}><MethodCatalogPanel
          methods={methods} selected={selectedMethod} onSelect={selectMethod}
          loading={methodsLoading} disabled={preparing || actionPending !== null || restoring || Boolean(restoreError)} error={methodError}
        /></div>
        {method && <div ref={methodForm}><MethodForm
          method={method} assets={assets} goal={goal} inputs={inputs} options={options} context={context}
          criteria={criteria} previewContext={workbenchApi.previewContext}
          onCriteria={(next) => {
            clearModelSuggestion()
            setCriteria(next)
            setDraftDirty(true)
          }}
          pending={preparing || actionPending !== null}
          onGoal={changeGoal}
          onInput={(role, value) => {
            clearModelSuggestion()
            setInputs((previous) => ({ ...previous, [role]: value ?? undefined }))
            setDraftDirty(true)
          }}
          onOption={(name, value) => {
            clearModelSuggestion()
            setOptions((previous) => ({ ...previous, [name]: value }))
            setDraftDirty(true)
          }}
          onPrepare={(request) => { void prepare(request) }}
        /></div>}
        {taskError && <ProblemDetails problem={taskError} actions={{ edit: editDraft }} />}
      </>}
      plan={selectedPlan ? <>
        <PlanPreview plan={selectedPlan} assets={assets} onIssueSelect={editDraft} />
        {selectedPlan.status === 'draft' && selectedPlan.issues.length > 0 && method?.available && <ClarificationPanel
          key={`${selectedPlan.id}@${selectedPlan.version}`} plan={selectedPlan} method={method} assets={assets}
          disabled={draftDirty || preparing || actionPending !== null}
          onAnswer={(field, value) => { void answerClarification(selectedPlan, field, value) }}
        />}
        <AlternativePlansPanel key={`alternatives:${selectedPlan.id}@${selectedPlan.version}`}
          plan={selectedPlan} load={workbenchApi.alternatives}
          disabled={draftDirty || preparing || actionPending !== null}
          onSelect={(choice) => { void chooseAlternative(selectedPlan, choice) }} />
        {draftDirty && <p role="status">表单已修改，请先生成草稿。</p>}
        <ConfirmPlanButton plan={selectedPlan} dirty={draftDirty} busy={preparing || actionPending !== null}
          onConfirm={(ref) => { void confirmPlan(ref) }} />
        {actionPending === 'confirm' && <p role="status">正在确认方案…</p>}
        {actionPending === 'answer' && <p role="status">正在保存回答…</p>}
        {actionPending === 'alternative' && <p role="status">正在生成替代方案草稿…</p>}
        {actionError?.phase === 'plan' && <ProblemDetails problem={actionError} actions={{ edit: editDraft }} />}
        {(actionError?.code === 'PLAN_VERSION_CONFLICT' || actionError?.code === 'PLAN_DRAFT_REQUIRED') && taskId && <a
          href={workbenchLocation(new URL(window.location.href), { taskId }).href}>打开任务最新版本</a>}
      </> : undefined}
      run={<RunPanel
        runId={runId} onStatus={receiveStatus} pendingReportId={queuedReport}
        actions={{
          edit: method ? editDraft : undefined,
          retry: runPlan && !actionPending ? () => { void startRun(runPlan) } : undefined,
          recheck: canRecheck && !recheckSubmitting && !queuedReport ? () => { void recheck() } : undefined,
        }}
      >
        {startButton}
        {runPlan && (runPlan.id !== selectedPlan?.id || runPlan.version !== selectedPlan.version) && (
          <p>本次运行方案 <code>{runPlan.id}</code> · v{runPlan.version}</p>
        )}
        {actionPending === 'start' && <p role="status">正在提交运行…</p>}
        {actionError?.phase === 'execute' && <ProblemDetails problem={actionError} />}
        {(recheckSubmitting || queuedReport) && <p role="status">已提交重新检查，等待后端处理。</p>}
        {recheckError && <ProblemDetails problem={recheckError} actions={{ recheck: () => { void recheck() } }} />}
      </RunPanel>}
      artifacts={runId ? <ArtifactsPanel
        key={runId} record={artifacts} loading={artifactsLoading} error={artifactError}
        selectedArtifactId={selectedArtifactId} onLocate={locateArtifact}
      /> : undefined}
      checks={runId ? <ChecksPanel
        key={runId} runId={runId} checkStatus={runStatus?.check_status} currentReportId={runStatus?.current_report_id}
        onRecheck={canRecheck ? () => { void recheck() } : undefined} recheckPending={Boolean(recheckSubmitting || queuedReport)}
      /> : undefined}
      executionStatus={runStatus?.execution_status}
      checkStatus={runStatus?.check_status}
    />
  )
}
