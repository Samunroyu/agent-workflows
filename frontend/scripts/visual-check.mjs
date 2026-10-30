import { spawn, spawnSync } from 'node:child_process'
import { mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { chromium } from 'playwright-core'

const root = resolve(import.meta.dirname, '..')
const projectRoot = resolve(root, '..')
const output = resolve(root, '.visual-check')
const backendRoot = resolve(output, 'backend-data')
const databasePath = resolve(backendRoot, 'console.db')
const checkConfig = resolve(output, 'vite.check.ts')
const chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const backendUrl = 'http://127.0.0.1:18080'
const frontendUrl = 'http://127.0.0.1:5173'
const workflowIds = ['e2e_parallel_gate', 'e2e_cancel_retry', 'e2e_condition_default']
mkdirSync(backendRoot, { recursive: true })
writeFileSync(checkConfig, `import { defineConfig } from 'vite'\nimport base from '../vite.config'\nexport default defineConfig({ ...base, server: { ...base.server, proxy: { '/api': 'http://127.0.0.1:18080' } } })\n`)
const checkpointSetup = spawnSync(resolve(projectRoot, '.venv/bin/python'), [
  '-c',
  'import sqlite3, sys; from langgraph.checkpoint.sqlite import SqliteSaver; connection = sqlite3.connect(sys.argv[1]); SqliteSaver(connection).setup(); connection.close()',
  databasePath,
], { cwd: backendRoot, encoding: 'utf8' })
if (checkpointSetup.status !== 0) throw new Error(`测试数据库初始化失败：${checkpointSetup.stderr}`)

const backend = spawn(resolve(projectRoot, '.venv/bin/uvicorn'), ['console.app:app', '--host', '127.0.0.1', '--port', '18080'], {
  cwd: backendRoot,
  env: {
    ...process.env,
    PYTHONPATH: `${projectRoot}:${process.env.PYTHONPATH || ''}`,
    CONSOLE_DATA_DIR: backendRoot,
    CONSOLE_DB: databasePath,
    CONSOLE_WORKFLOWS_DIR: resolve(backendRoot, 'workflows'),
    CONSOLE_AGENTS_FILE: resolve(backendRoot, 'agents.json'),
    CONSOLE_REPO_DEFAULT: backendRoot,
  },
  stdio: ['ignore', 'pipe', 'pipe'],
})
const server = spawn(process.execPath, [resolve(root, 'node_modules/vite/bin/vite.js'), '--config', checkConfig, '--host', '127.0.0.1', '--port', '5173', '--strictPort'], {
  cwd: root,
  stdio: ['ignore', 'pipe', 'pipe'],
})

let serverOutput = ''
let backendOutput = ''
server.stdout.on('data', (chunk) => { serverOutput += chunk.toString() })
server.stderr.on('data', (chunk) => { serverOutput += chunk.toString() })
backend.stdout.on('data', (chunk) => { backendOutput += chunk.toString() })
backend.stderr.on('data', (chunk) => { backendOutput += chunk.toString() })

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

async function request(path, options = {}) {
  const response = await fetch(`${backendUrl}${path}`, {
    ...options,
    headers: options.body ? { 'Content-Type': 'application/json', ...options.headers } : options.headers,
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(`${options.method || 'GET'} ${path}: ${response.status} ${JSON.stringify(body)}`)
  return body
}

async function waitFor(url) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      const response = await fetch(url)
      if (response.ok) return
    } catch {}
    await new Promise((resolveWait) => setTimeout(resolveWait, 250))
  }
  throw new Error(`Service did not start: ${url}\n${serverOutput}\n${backendOutput}`)
}

async function waitRun(threadId, predicate, description, timeout = 60000) {
  const started = Date.now()
  let latest
  while (Date.now() - started < timeout) {
    latest = await request(`/api/runs/${threadId}`)
    if (predicate(latest)) return latest
    await new Promise((resolveWait) => setTimeout(resolveWait, 250))
  }
  throw new Error(`Timed out: ${description}\n${JSON.stringify(latest, null, 2)}`)
}

function shell(id, label, command, x, y, extra = {}) {
  return {
    id, label, kind: 'shell', description: `${label}测试节点`, position: { x, y },
    worker_id: 'local', command, timeout_sec: 60, continue_on_error: false,
    capture_git_diff: false, max_runs: 8, join_mode: 'auto', ...extra,
  }
}

const parallelWorkflow = {
  id: workflowIds[0], name: 'E2E 并行条件卡点', description: '并行、条件、打回与人工改产出端到端测试', builtin: false,
  inputs: [{ key: 'task', label: '任务描述', kind: 'textarea', required: true, default: '' }],
  entry_point: 'prepare',
  nodes: [
    shell('prepare', '准备', 'printf \'PREPARE:%s\\n\' "{task}"', 0, 180),
    shell('left', '并行左分支', 'printf \'LEFT_START\\n\'; sleep 3; printf \'LEFT_DONE:%s\\n\' "{feedback}"', 260, 40),
    shell('right', '并行右分支', 'printf \'RIGHT_START\\n\'; sleep 3; printf \'RIGHT_DONE\\n\'', 260, 320),
    { id: 'route', label: '条件路由', kind: 'condition', description: '左分支成功则进入人工审批', position: { x: 540, y: 180 }, join_mode: 'all', max_runs: 8 },
    { id: 'gate', label: '人工验收', kind: 'gate', description: '支持打回与在线改产出', position: { x: 800, y: 180 }, instruction: '请检查两个并行分支产出', review_nodes: ['left', 'right'], allow_edit_output: true, rewind_targets: ['left', 'right'], max_runs: 8 },
    shell('finish', '最终汇总', 'printf \'FINAL:%s\\n\' "{left.output}"', 1060, 80),
    shell('failure', '失败分支', 'printf \'UNEXPECTED_FAILURE\\n\'; exit 1', 1060, 320),
  ],
  edges: [
    { id: 'prepare-left', source: 'prepare', target: 'left' },
    { id: 'prepare-right', source: 'prepare', target: 'right' },
    { id: 'left-route', source: 'left', target: 'route' },
    { id: 'right-route', source: 'right', target: 'route' },
    { id: 'route-gate', source: 'route', target: 'gate', label: '成功', condition: { field: 'left.ok', op: '==', value: true }, is_default: false },
    { id: 'route-failure', source: 'route', target: 'failure', label: '默认', condition: null, is_default: true },
    { id: 'gate-finish', source: 'gate', target: 'finish' },
    { id: 'finish-end', source: 'finish', target: 'END' },
    { id: 'failure-end', source: 'failure', target: 'END' },
  ],
}

const cancelWorkflow = {
  id: workflowIds[1], name: 'E2E 强杀重试', description: '运行中强杀并临时改参重试', builtin: false,
  inputs: [{ key: 'task', label: '任务描述', kind: 'textarea', required: true }], entry_point: 'long_shell',
  nodes: [shell('long_shell', '长时间命令', 'printf \'CANCEL_START\\n\'; sleep 30; printf \'UNREACHABLE\\n\'', 100, 100)],
  edges: [{ id: 'long-end', source: 'long_shell', target: 'END' }],
}

const conditionWorkflow = {
  id: workflowIds[2], name: 'E2E 默认条件分支', description: '验证失败结果进入默认分支', builtin: false,
  inputs: [{ key: 'task', label: '任务描述', kind: 'textarea', required: true }], entry_point: 'probe',
  nodes: [
    shell('probe', '失败探测', 'printf \'PROBE_FAIL\\n\'; exit 7', 0, 100),
    { id: 'judge', label: '判断', kind: 'condition', position: { x: 250, y: 100 }, max_runs: 8 },
    shell('pass_sink', '成功接收', 'printf \'PASS_SINK\\n\'', 500, 0),
    shell('fallback_sink', '默认接收', 'printf \'FALLBACK_SINK\\n\'', 500, 220),
  ],
  edges: [
    { id: 'probe-judge', source: 'probe', target: 'judge' },
    { id: 'judge-pass', source: 'judge', target: 'pass_sink', condition: { field: 'probe.ok', op: '==', value: true } },
    { id: 'judge-fallback', source: 'judge', target: 'fallback_sink', is_default: true, condition: null },
    { id: 'pass-end', source: 'pass_sink', target: 'END' },
    { id: 'fallback-end', source: 'fallback_sink', target: 'END' },
  ],
}

let browser
const pageErrors = []
try {
  await Promise.all([waitFor(`${frontendUrl}/`), waitFor(`${backendUrl}/api/health`)])
  for (const workflow of [parallelWorkflow, cancelWorkflow, conditionWorkflow]) {
    await request('/api/workflows', { method: 'POST', body: JSON.stringify(workflow) })
  }
  console.log('✓ 隔离服务启动，3 个测试工作流已创建')

  browser = await chromium.launch({ executablePath: chrome, headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 })
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await page.goto(frontendUrl, { waitUntil: 'domcontentloaded' })
  await page.locator('.workflow-editor .react-flow__node').first().waitFor({ timeout: 15000 })
  await page.locator('#workflow-switcher').selectOption(parallelWorkflow.id)
  await page.locator('.react-flow__node[data-id="left"]').waitFor()
  assert(await page.locator('.react-flow__node:not([data-id="START"]):not([data-id="END"])').count() === parallelWorkflow.nodes.length, '编辑态真实节点渲染数量不正确')

  await page.getByRole('button', { name: 'Agent 管理' }).click()
  await page.getByRole('dialog', { name: 'Agent 管理' }).waitFor()
  await page.keyboard.press('Escape')
  await page.getByRole('dialog', { name: 'Agent 管理' }).waitFor({ state: 'hidden' })

  await page.locator('.node-toolbar button').filter({ hasText: 'Shell 命令' }).click()
  await page.getByRole('button', { name: '删除节点', exact: true }).click()
  assert(await page.locator('.react-flow__node[data-id="left"]').count() === 1, '节点增删后原节点应保持')
  await page.locator('.react-flow__node[data-id="left"]').click()
  const commandEditor = page.locator('#inspector-node-left-command')
  const originalCommand = await commandEditor.inputValue()
  await commandEditor.focus()
  await commandEditor.evaluate((element) => element.setSelectionRange(element.value.length, element.value.length))
  await page.getByRole('button', { name: '{task}', exact: true }).click()
  assert((await commandEditor.inputValue()).endsWith('{task}'), '变量胶囊未插入光标位置')
  await commandEditor.fill(originalCommand)

  const prepareNode = page.locator('.react-flow__node[data-id="prepare"]')
  const beforeDrag = await prepareNode.boundingBox()
  assert(beforeDrag, '无法获取入口节点位置')
  await page.mouse.move(beforeDrag.x + 70, beforeDrag.y + 30)
  await page.mouse.down()
  await page.mouse.move(beforeDrag.x + 115, beforeDrag.y + 65, { steps: 8 })
  await page.mouse.up()
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await page.locator('.console-toast').filter({ hasText: '已保存' }).waitFor({ timeout: 15000 })
  const savedDefinition = await request(`/api/workflows/${parallelWorkflow.id}`)
  assert(savedDefinition.nodes.find((node) => node.id === 'prepare').position.x !== 0, '拖拽位置未保存到 WorkflowSpec')

  await page.locator('.react-flow__node[data-id="route"]').click()
  await page.locator('.inspector-edge-link').first().click()
  assert(await page.locator('select[name="conditionField"]').inputValue() === 'left.ok', '条件连线字段未正确加载')

  await page.getByRole('button', { name: '复制', exact: true }).click()
  await page.getByRole('button', { name: '删除', exact: true }).click()
  await page.getByRole('dialog', { name: '删除工作流' }).getByRole('button', { name: '确认删除' }).click()
  await page.getByRole('dialog', { name: '删除工作流' }).waitFor({ state: 'hidden' })
  console.log('✓ 画布拖拽保存、节点增删、变量插入、条件编辑、复制与本地删除')

  await page.getByRole('button', { name: '发起新运行', exact: true }).click()
  await page.locator('#new-run-workflow').selectOption(parallelWorkflow.id)
  await page.getByLabel(/任务描述/).fill('端到端并行审批测试')
  await page.getByRole('button', { name: '创建并运行', exact: true }).click()
  await page.locator('.run-monitor').waitFor({ timeout: 15000 })
  const parallelThread = await page.evaluate(async () => (await (await fetch('/api/runs')).json()).runs.find((run) => run.workflow_id === 'e2e_parallel_gate').thread_id)
  await page.waitForFunction(() => document.querySelectorAll('.run-canvas-card .flow-node--running').length >= 2, null, { timeout: 12000 })
  assert(await page.locator('.run-canvas-card .react-flow__node').count() === parallelWorkflow.nodes.length + 1, '运行画布未完整渲染全部节点和 END')
  await page.locator('.run-canvas-card .react-flow__node[data-id="finish"]').waitFor()
  await page.locator('.run-canvas-card .react-flow__node[data-id="left"]').click()
  await page.getByText('SSE 实时连接', { exact: true }).waitFor({ timeout: 10000 })
  await page.locator('.log-terminal').filter({ hasText: 'LEFT_START' }).waitFor({ timeout: 10000 })
  await page.getByRole('button', { name: /确认并继续/ }).waitFor({ timeout: 60000 })
  assert(await page.locator('.upstream-snapshot').count() === 2, '人工卡点应展示两个并行上游产出')

  const leftSnapshot = page.locator('.upstream-snapshot').filter({ hasText: 'left' })
  await leftSnapshot.getByRole('button', { name: '在线编辑内容' }).click()
  await leftSnapshot.locator('textarea').fill('LEFT_HUMAN_DRAFT')
  await page.locator('#approval-feedback').fill('请重跑左分支并记录反馈')
  await page.locator('#rewind-target').selectOption('left')
  await page.getByRole('button', { name: /打回重跑/ }).click()
  const revised = await waitRun(parallelThread, (run) => run.status === 'waiting' && run.values?.nodes?.left?.runs >= 2, '左分支打回后二次执行')
  assert(revised.values.nodes.right.runs === 1, '打回左分支不应重跑右分支')
  assert(revised.values.nodes.left.output.includes('请重跑左分支并记录反馈'), '打回反馈未注入重跑节点')
  await page.getByRole('button', { name: /确认并继续/ }).waitFor({ timeout: 30000 })
  const revisedSnapshot = page.locator('.upstream-snapshot').filter({ hasText: 'left' })
  await revisedSnapshot.getByRole('button', { name: '在线编辑内容' }).click()
  await revisedSnapshot.locator('textarea').fill('LEFT_HUMAN_FINAL')
  await page.getByRole('button', { name: /确认并继续/ }).click()
  const completed = await waitRun(parallelThread, (run) => run.status === 'done', '人工编辑产出后放行')
  assert(completed.values.nodes.left.output === 'LEFT_HUMAN_FINAL', '人工修改未写回左分支 output')
  assert(completed.values.nodes.left.edited_by_human === true, '人工修订标记缺失')
  assert(completed.values.nodes.finish.output.includes('LEFT_HUMAN_FINAL'), '下游未引用人工修订后的产出')
  assert(!completed.values.nodes.failure, '成功条件不应执行默认失败分支')
  await page.locator('.run-monitor .status-badge').filter({ hasText: '已完成' }).waitFor({ timeout: 10000 })
  await page.locator('.run-canvas-card .react-flow__node[data-id="left"]').click()
  await page.getByRole('tab', { name: '节点产出', exact: true }).click()
  await page.getByText('LEFT_HUMAN_FINAL', { exact: true }).waitFor()
  await page.waitForFunction(() => {
    const nodes = Array.from(document.querySelectorAll('.run-canvas-card .react-flow__node'))
    return nodes.length > 0 && nodes.every((element) => getComputedStyle(element).visibility === 'visible')
  }, null, { timeout: 5000 })
  await page.waitForFunction((expected) => document.querySelectorAll('.run-canvas-card .react-flow__edge').length === expected, parallelWorkflow.edges.length, { timeout: 5000 })
  await page.screenshot({ path: resolve(output, 'parallel-gate-completed.png'), fullPage: true })
  console.log('✓ 并行高亮、SSE、条件成功分支、定点打回、人工改产出与下游引用')

  await page.getByRole('button', { name: '返回编排编辑' }).click()
  await page.getByRole('button', { name: '发起新运行', exact: true }).click()
  await page.locator('#new-run-workflow').selectOption(cancelWorkflow.id)
  await page.getByLabel(/任务描述/).fill('端到端强杀重试测试')
  await page.getByRole('button', { name: '创建并运行', exact: true }).click()
  const cancelThread = await page.evaluate(async () => (await (await fetch('/api/runs')).json()).runs.find((run) => run.workflow_id === 'e2e_cancel_retry').thread_id)
  await waitRun(cancelThread, (run) => run.status === 'running', '长命令进入运行态')
  await page.locator('.run-canvas-card .react-flow__node[data-id="long_shell"]').click()
  await page.locator('.log-terminal').filter({ hasText: 'CANCEL_START' }).waitFor({ timeout: 15000 })
  await page.locator('.run-monitor .kill-button').click()
  const cancelled = await waitRun(cancelThread, (run) => run.status === 'cancelled', '强杀后保留断点')
  assert(cancelled.next.includes('long_shell') && cancelled.running_nodes.length === 0, '强杀断点或运行节点状态错误')
  await page.getByRole('button', { name: '原地重试该节点' }).click()
  await page.locator('#retry-template').fill("printf 'RETRY_SUCCESS\\n'")
  await page.getByRole('button', { name: '确认原地重试' }).click()
  const retried = await waitRun(cancelThread, (run) => run.status === 'done', '临时改参原地重试')
  assert(retried.values.nodes.long_shell.output.includes('RETRY_SUCCESS'), '重试未使用临时 Shell 命令')
  await page.locator('.run-monitor .status-badge').filter({ hasText: '已完成' }).waitFor({ timeout: 10000 })
  await page.screenshot({ path: resolve(output, 'cancel-retry-completed.png'), fullPage: true })
  console.log('✓ 运行中强杀、断点保留、临时改参原地重试')

  const conditionRun = await request('/api/runs', { method: 'POST', body: JSON.stringify({ workflow_id: conditionWorkflow.id, task: '默认分支', inputs: { task: '默认分支' } }) })
  const conditionDone = await waitRun(conditionRun.thread_id, (run) => run.status === 'done', '默认条件分支')
  assert(conditionDone.values.nodes.probe.ok === false && conditionDone.values.nodes.probe.exit_code === 7, '条件上游失败结果不准确')
  assert(conditionDone.values.nodes.fallback_sink.output.includes('FALLBACK_SINK'), '默认条件分支未执行')
  assert(!conditionDone.values.nodes.pass_sink, '默认分支执行时不应运行成功分支')
  const logs = await request(`/api/runs/${parallelThread}/logs?node_id=left&after_seq=0&limit=1000`)
  assert(logs.logs.length >= 2 && logs.next_seq === logs.logs.at(-1).seq, '节点历史日志或 next_seq 游标不正确')
  console.log('✓ 默认条件分支与节点日志游标')

  const relevantErrors = pageErrors.filter((message) => !message.includes('ResizeObserver loop'))
  assert(relevantErrors.length === 0, `Browser errors:\n${relevantErrors.join('\n')}`)
  console.log('✓ 浏览器无未处理运行时异常；全部 E2E 断言通过')
} catch (error) {
  console.error(error)
  console.error(`\nBackend output:\n${backendOutput.slice(-5000)}\nFrontend output:\n${serverOutput.slice(-3000)}`)
  process.exitCode = 1
} finally {
  if (browser) await browser.close()
  await Promise.all(workflowIds.map((id) => request(`/api/workflows/${id}`, { method: 'DELETE' }).catch(() => undefined)))
  server.kill('SIGTERM')
  backend.kill('SIGTERM')
  rmSync(checkConfig, { force: true })
  rmSync(backendRoot, { recursive: true, force: true })
}
