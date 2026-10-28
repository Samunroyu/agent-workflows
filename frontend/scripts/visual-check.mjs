import { spawn } from 'node:child_process'
import { mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { chromium } from 'playwright-core'

const root = resolve(import.meta.dirname, '..')
const projectRoot = resolve(root, '..')
const output = resolve(root, '.visual-check')
const backendRoot = resolve(output, 'backend-data')
const checkConfig = resolve(output, 'vite.check.ts')
const chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
mkdirSync(backendRoot, { recursive: true })
writeFileSync(checkConfig, `import { defineConfig } from 'vite'\nimport base from '../vite.config'\nexport default defineConfig({ ...base, server: { ...base.server, proxy: { '/api': 'http://127.0.0.1:18080' } } })\n`)

const backend = spawn(resolve(projectRoot, '.venv/bin/uvicorn'), ['console.app:app', '--host', '127.0.0.1', '--port', '18080'], {
  cwd: backendRoot,
  env: { ...process.env, CONSOLE_AGENT: 'stub', PYTHONPATH: `${projectRoot}:${process.env.PYTHONPATH || ''}` },
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

async function waitFor(url) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const response = await fetch(url)
      if (response.ok) return
    } catch {}
    await new Promise((resolveWait) => setTimeout(resolveWait, 250))
  }
  throw new Error(`Service did not start: ${url}\n${serverOutput}\n${backendOutput}`)
}

let browser
const pageErrors = []
try {
  await Promise.all([waitFor('http://127.0.0.1:5173/'), waitFor('http://127.0.0.1:18080/api/health')])
  const [health, workflows, runs] = await Promise.all([
    fetch('http://127.0.0.1:5173/api/health').then((response) => response.json()),
    fetch('http://127.0.0.1:5173/api/workflows').then((response) => response.json()),
    fetch('http://127.0.0.1:5173/api/runs').then((response) => response.json()),
  ])
  console.log(`API online: ${health.agent}; workflows: ${workflows.workflows.length}; runs: ${runs.runs.length}`)

  browser = await chromium.launch({ executablePath: chrome, headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 })
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await page.goto('http://127.0.0.1:5173/', { waitUntil: 'domcontentloaded' })
  await page.locator('.b-canvas-card .react-flow__node').first().waitFor({ timeout: 15000 })
  await page.screenshot({ path: resolve(output, 'cloud-console.png'), fullPage: true })
  console.log('Initial console and dynamic workflow rendered')

  const externalResponse = await fetch('http://127.0.0.1:18080/api/workflows', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...workflows.workflows[0], id: 'external_review', name: '外部 Agent 工作流', builtin: false }),
  })
  if (!externalResponse.ok) throw new Error(`External workflow creation failed: ${externalResponse.status}`)
  await page.locator('.workflow-select option[value="external_review"]').waitFor({ state: 'attached', timeout: 10000 })
  console.log('External workflow appeared through polling without reload')

  await page.getByRole('button', { name: '发起新运行' }).click()
  await page.getByRole('dialog', { name: '发起一个新运行' }).waitFor()
  const createButton = page.getByRole('button', { name: '创建并运行' })
  if (await createButton.isDisabled()) throw new Error('Empty task must not disable form submission')
  await createButton.click()
  await page.getByText('请填写任务描述后再创建运行。').waitFor()
  await page.screenshot({ path: resolve(output, 'cloud-console-dialog.png'), fullPage: true })
  await page.getByLabel(/任务描述/).fill('联调测试：验证云流操作台')
  await page.keyboard.press('Escape')
  await page.getByRole('dialog', { name: '发起一个新运行' }).waitFor({ state: 'hidden' })
  console.log('Native dialog, required-field feedback and Escape verified')

  await page.getByRole('button', { name: '发起新运行' }).click()
  await page.getByLabel(/任务描述/).fill('联调测试：验证云流操作台')
  await createButton.click()
  await page.locator('.console-detail-card .detail-title-row').waitFor({ timeout: 15000 })
  await page.getByRole('button', { name: /确认并继续/ }).waitFor({ timeout: 30000 })
  await page.getByRole('tab', { name: '计划', exact: true }).focus()
  await page.keyboard.press('ArrowRight')
  const diffTab = page.getByRole('tab', { name: 'Diff', exact: true })
  if (await diffTab.getAttribute('aria-selected') !== 'true') throw new Error('ArrowRight did not select the Diff tab')
  await page.keyboard.press('ArrowLeft')
  await page.screenshot({ path: resolve(output, 'cloud-console-run.png'), fullPage: true })
  console.log('New run, plan approval and keyboard tabs verified')

  await page.locator('.approval-controls textarea').fill('联调审批通过')
  await page.getByRole('button', { name: /确认并继续/ }).click()
  await page.getByRole('button', { name: '中止运行' }).waitFor({ timeout: 30000 })
  console.log('Plan approved and final approval reached')

  await page.getByRole('button', { name: '中止运行' }).click()
  await page.locator('.console-toast').filter({ hasText: '运行已中止' }).waitFor({ timeout: 15000 })
  await page.getByRole('tab', { name: '事件流' }).click()
  await page.locator('.console-events p').first().waitFor({ timeout: 10000 })
  await page.screenshot({ path: resolve(output, 'cloud-console-events.png'), fullPage: true })
  console.log('Abort action and event stream verified')

  const relevantErrors = pageErrors.filter((message) => !message.includes('ResizeObserver loop'))
  if (relevantErrors.length) throw new Error(`Browser errors:\n${relevantErrors.join('\n')}`)
} catch (error) {
  console.error(error)
  process.exitCode = 1
} finally {
  if (browser) await browser.close()
  server.kill('SIGTERM')
  backend.kill('SIGTERM')
  rmSync(checkConfig, { force: true })
}
