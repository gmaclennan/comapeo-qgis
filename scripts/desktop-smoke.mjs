// Full-chain smoke check: packaged CoMapeo Desktop (local API on through its
// settings screen, test data from its test-data screen) and a real QGIS window
// with the CoMapeo plugin, which finds desktop through the standard discovery
// folder under a throwaway HOME.
//
//   node scripts/desktop-smoke.mjs --desktop <desktop checkout with a build in out/>
//     [--plugin <dir holding comapeo_qgis, default this repo>]
//     --out <evidence dir> --work <scratch dir, emptied first> [--qgis-version 4.2.3]
//
// Only an internal build ("CoMapeo Desktop Internal") is accepted, and it runs on
// a user data folder under the throwaway HOME. macOS only. Adapted from
// scripts/smoke_e2e.py. Exits non-zero if a check fails.

import { spawn, execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync, renameSync, symlinkSync, readdirSync } from 'node:fs'
import { createConnection } from 'node:net'
import { createRequire } from 'node:module'
import { homedir } from 'node:os'
import { join, dirname, resolve } from 'node:path'
import { parseArgs } from 'node:util'
import { fileURLToPath } from 'node:url'

const { values: args } = parseArgs({
	options: {
		desktop: { type: 'string' },
		plugin: { type: 'string' },
		out: { type: 'string' },
		work: { type: 'string' },
		'qgis-version': { type: 'string', default: '4.2.3' },
		observations: { type: 'string', default: '40' },
		timeout: { type: 'string', default: '420' },
		'reuse-desktop-data': { type: 'boolean', default: false },
	},
})

const DESKTOP = resolve(args.desktop)
const PLUGIN_SRC = resolve(args.plugin ?? join(dirname(fileURLToPath(import.meta.url)), '..'))
const OUT = resolve(args.out)
const WORK = resolve(args.work)
const QGIS_VERSION = args['qgis-version']
const PREFIX = `desktop-qgis-${QGIS_VERSION}`
const IN_QGIS = join(dirname(fileURLToPath(import.meta.url)), 'desktop_smoke_in_qgis.py')
const REAL_HOME = homedir()
const FAKE_HOME = join(WORK, 'home')
const USER_DATA = join(FAKE_HOME, 'Library', 'Application Support', 'CoMapeo Desktop Internal')
const DISCOVERY_FILE = join(USER_DATA, 'comapeo', 'comapeo-local-api.json')
const PROFILES = join(WORK, 'p')
const WORK_MARKER = '.desktop-smoke-work'
const PROJECT_NAME = 'River survey (test data)'
const OBSERVATIONS = Number(args.observations)

const require = createRequire(join(DESKTOP, 'package.json'))
const { _electron: electron } = require('@playwright/test')
const { findLatestBuild, parseElectronApp } = require('electron-playwright-helpers')

const checks = []
function check(name, ok, detail) {
	checks.push({ check: name, ok: !!ok, detail: detail ?? null })
	console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail !== undefined ? `: ${JSON.stringify(detail)}` : ''}`)
	return !!ok
}

function scrub(text) {
	return text.split(WORK).join('<work>').split(REAL_HOME).join('~')
}

function writeJson(path, data) {
	const tmp = `${path}.tmp`
	writeFileSync(tmp, scrub(JSON.stringify(data, null, 2)) + '\n')
	renameSync(tmp, path)
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function poll(fn, what, timeoutMs = 60_000, intervalMs = 200) {
	const deadline = Date.now() + timeoutMs
	for (;;) {
		const value = await fn()
		if (value) return value
		if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`)
		await sleep(intervalMs)
	}
}

// Local API client, as in desktop's Playwright spec
class LocalApiClient {
	#socket
	#buffer = Buffer.alloc(0)
	#nextId = 1
	#pending = new Map()
	constructor(socket) {
		this.#socket = socket
		this.closed = new Promise((r) => socket.once('close', () => r()))
		socket.on('data', (chunk) => {
			this.#buffer = Buffer.concat([this.#buffer, chunk])
			while (this.#buffer.length >= 4) {
				const length = this.#buffer.readUInt32LE(0)
				if (this.#buffer.length < 4 + length) break
				const message = JSON.parse(this.#buffer.subarray(4, 4 + length).toString('utf-8'))
				this.#buffer = this.#buffer.subarray(4 + length)
				const pending = this.#pending.get(message.id)
				if (!pending) continue
				this.#pending.delete(message.id)
				if (message.error) pending.reject(new Error(`${message.error.code}: ${message.error.message}`))
				else pending.resolve(message.result)
			}
		})
		socket.on('close', () => {
			for (const { reject } of this.#pending.values()) reject(new Error('Connection closed'))
			this.#pending.clear()
		})
	}
	static connect(socketPath) {
		return new Promise((resolve, reject) => {
			const socket = createConnection(socketPath)
			socket.once('error', reject)
			socket.once('connect', () => {
				socket.off('error', reject)
				socket.on('error', () => {})
				resolve(new LocalApiClient(socket))
			})
		})
	}
	request(method, params) {
		const id = this.#nextId++
		const body = Buffer.from(JSON.stringify({ id, method, params }), 'utf-8')
		const header = Buffer.alloc(4)
		header.writeUInt32LE(body.length)
		return new Promise((resolve, reject) => {
			this.#pending.set(id, { resolve, reject })
			this.#socket.write(Buffer.concat([header, body]))
		})
	}
	destroy() {
		this.#socket.destroy()
	}
}

class Oracle {
	static async open() {
		const discovery = JSON.parse(readFileSync(DISCOVERY_FILE, 'utf-8'))
		const client = await LocalApiClient.connect(discovery.socketPath)
		const hello = await client.request('hello', {
			secret: discovery.secret,
			protocolVersions: [1],
			client: { name: 'desktop-smoke-oracle', version: '0.0.0' },
		})
		const oracle = new Oracle()
		oracle.client = client
		oracle.hello = hello
		oracle.discovery = discovery
		return oracle
	}
	async projects() {
		return (await this.client.request('projects.list')).projects
	}
	async ids(projectId) {
		const result = {}
		const { layers } = await this.client.request('layers.list', { projectId })
		for (const layer of layers) {
			const ids = []
			let cursor = null
			do {
				const params = { projectId, layerId: layer.layerId }
				if (cursor !== null) params.cursor = cursor
				const page = await this.client.request('layers.getFeatures', params)
				for (const f of page.features) ids.push(f.id)
				cursor = page.nextCursor
			} while (cursor !== null && cursor !== undefined)
			result[layer.layerId] = ids
		}
		return result
	}
	async counts(projectId) {
		const ids = await this.ids(projectId)
		return Object.fromEntries(Object.entries(ids).map(([k, v]) => [k, v.length]))
	}
	close() {
		this.client.destroy()
	}
}

// Desktop

const desktopLog = []
let openDesktop = null

async function launchDesktop() {
	const latestBuild = findLatestBuild(join(DESKTOP, 'out'))
	const appInfo = parseElectronApp(latestBuild)
	// Copies of this build already running (started by hand, say) are not this run's.
	for (const pid of processesOf(appInfo)) PREEXISTING.add(pid)
	if (appInfo.packageJson.productName !== 'CoMapeo Desktop Internal') {
		throw new Error('Only internal builds may be used')
	}
	const env = { ...process.env, HOME: FAKE_HOME, USER_DATA_PATH: USER_DATA, DEBUG: 'comapeo:*' }
	delete env.COMAPEO_LOCAL_API_DISCOVERY
	const app = await electron.launch({
		// The throwaway HOME has no login keychain for Chromium to use.
		args: ['--use-mock-keychain', appInfo.main],
		executablePath: appInfo.executable,
		env,
		timeout: 30_000,
	})
	const proc = app.process()
	for (const stream of [proc.stdout, proc.stderr]) {
		stream?.on('data', (chunk) => desktopLog.push(chunk.toString()))
	}
	const userData = await app.evaluate(({ app }) => app.getPath('userData'))
	if (userData !== USER_DATA) {
		await app.close().catch(() => {})
		throw new Error(`Desktop is not on the isolated user data folder: ${userData}`)
	}
	const appVersion = await app.evaluate(({ app }) => app.getVersion())
	openDesktop = app
	const page = await app.firstWindow()
	await app.evaluate(({ BrowserWindow }) => {
		const [window] = BrowserWindow.getAllWindows()
		window?.setBounds({ x: 40, y: 40, width: 1280, height: 860 })
	})
	return { app, page, appInfo, appVersion }
}

const QUIT_TIMEOUT_MS = 20_000

// Quits as a user would (app.quit()); a main process still alive after the timeout is killed and reported
async function quitDesktop(app) {
	const started = Date.now()
	const pid = app.process().pid
	const exited = new Promise((r) => app.process().once('exit', () => r(true)))
	const closed = await Promise.race([app.close().then(() => true, () => true), exited, sleep(QUIT_TIMEOUT_MS).then(() => false)])
	const seconds = (Date.now() - started) / 1000
	if (closed) return { exitedBySelf: true, seconds }
	try {
		process.kill(pid, 'SIGTERM')
	} catch {}
	await Promise.race([exited, sleep(10_000)])
	return { exitedBySelf: false, seconds, killedAfterSeconds: QUIT_TIMEOUT_MS / 1000 }
}

async function onboardAndCreateProject(page) {
	await page.getByRole('link', { name: 'Get Started' }).click()
	await page.getByRole('link', { name: 'Next', exact: true }).click()
	await page.getByRole('textbox', { name: 'Device Name', exact: true }).fill('Desktop (smoke)')
	await page.getByRole('button', { name: 'Add Name', exact: true }).click()
	await page.waitForURL((url) => /^#\/app$/.test(url.hash))
	await page.getByRole('link', { name: 'Start New Project', exact: true }).click()
	await page.getByRole('textbox', { name: 'Project Name', exact: true }).fill(PROJECT_NAME)
	await page.getByRole('button', { name: 'Create', exact: true }).click()
	await page.getByRole('button', { name: 'Switch Project', exact: true }).click()
	await page.getByRole('menu').getByRole('menuitem', { name: 'View All Projects', exact: true }).click()
}

function qgisCard(page) {
	return page
		.getByRole('main')
		.locator('section')
		.filter({ has: page.getByRole('heading', { name: 'QGIS', exact: true }) })
		.last()
}

async function switchOnQgis(page) {
	await page.getByRole('link', { name: 'CoMapeo Settings', exact: true }).click()
	const card = qgisCard(page)
	const checkbox = card.getByRole('checkbox', { name: 'Allow QGIS to Connect', exact: true })
	await card.scrollIntoViewIfNeeded()
	await card.screenshot({ path: join(OUT, 'desktop-settings-card-off.png') })
	const wasChecked = await checkbox.isChecked()
	await checkbox.click()
	await poll(() => checkbox.isChecked(), 'the checkbox to read as on', 15_000)
	await poll(() => existsSync(DISCOVERY_FILE), 'the discovery file', 15_000)
	await sleep(300)
	await card.screenshot({ path: join(OUT, 'desktop-settings-card.png') })
	await page.screenshot({ path: join(OUT, 'desktop-settings-window.png') })
	return { wasChecked, alert: await card.getByRole('alert').count() }
}

async function createTestData(page, projectId, { count, track }) {
	await page.evaluate((id) => {
		window.location.hash = `#/app/projects/${id}/test-data`
	}, projectId)
	const countField = page.getByRole('textbox', { name: 'Number of observations' })
	await countField.waitFor()
	await countField.fill(String(count))
	const trackBox = page.getByRole('checkbox', { name: 'Create track' })
	if ((await trackBox.isChecked()) !== track) await trackBox.click()
	await page.getByRole('button', { name: 'Create', exact: true }).click()
	const note = page.getByText(/Created \d+ observations?\./)
	await note.waitFor({ timeout: 60_000 })
	const text = await note.textContent()
	return text
}

// QGIS

function qgisExecutable() {
	const app = `/Applications/QGIS-final-${QGIS_VERSION.replaceAll('.', '_')}.app`
	const dir = join(app, 'Contents', 'MacOS')
	const found = readdirSync(dir).filter((n) => n.startsWith('QGIS')).sort()
	if (!found.length) throw new Error(`No QGIS executable in ${app}`)
	return join(dir, found[0])
}

function makeProfile() {
	const def = join(PROFILES, 'profiles', 'default')
	const plugins = join(def, 'python', 'plugins')
	mkdirSync(plugins, { recursive: true })
	symlinkSync(join(PLUGIN_SRC, 'comapeo_qgis'), join(plugins, 'comapeo_qgis'), 'dir')
	const settings = join(def, 'qgis.org')
	mkdirSync(settings, { recursive: true })
	// Without these QGIS may store a random master password in a keychain; the throwaway HOME has none.
	const ini = [
		'[PythonPlugins]',
		'comapeo_qgis=true',
		'',
		'[qgis]',
		'checkVersion=false',
		'',
		'[auth]',
		'use_password_helper=false',
		'',
		'[authentication]',
		'generate-random-password-for-keychain=false',
		'use-password-helper=false',
		'',
	].join('\n')
	for (const name of ['QGIS4.ini', 'QGIS3.ini']) writeFileSync(join(settings, name), ini)
}

function qgisEnv(mode, projectId) {
	const env = {}
	for (const [k, v] of Object.entries(process.env)) {
		if (/^(PYTHON|QT_|QGIS_|GDAL_|PROJ_)/.test(k)) continue
		if (k === 'COMAPEO_LOCAL_API_DISCOVERY') continue
		env[k] = v
	}
	Object.assign(env, {
		HOME: FAKE_HOME,
		COMAPEO_SMOKE_WORK: WORK,
		COMAPEO_SMOKE_OUT: OUT,
		COMAPEO_SMOKE_MODE: mode,
		COMAPEO_SMOKE_PREFIX: PREFIX,
		COMAPEO_SMOKE_PROJECT: projectId,
		COMAPEO_SMOKE_REAL_HOME: REAL_HOME,
		PYTHONDONTWRITEBYTECODE: '1',
		PYTHONFAULTHANDLER: '1',
	})
	return env
}

async function launchQgis(mode, projectId, extra, onTick) {
	const logPath = join(OUT, `${PREFIX}-${mode}-qgis.log`)
	const chunks = []
	const proc = spawn(
		qgisExecutable(),
		['--profiles-path', PROFILES, '--nologo', '--noversioncheck', ...extra, '--code', IN_QGIS],
		{ env: qgisEnv(mode, projectId), stdio: ['ignore', 'pipe', 'pipe'], detached: true },
	)
	proc.stdout.on('data', (c) => chunks.push(c.toString()))
	proc.stderr.on('data', (c) => chunks.push(c.toString()))
	let exitCode = null
	const exited = new Promise((r) => proc.once('exit', (code, signal) => r((exitCode = code ?? signal))))
	const deadline = Date.now() + Number(args.timeout) * 1000
	let done = false
	exited.then(() => (done = true))
	while (!done && Date.now() < deadline) {
		if (onTick) {
			try {
				await onTick()
			} catch (err) {
				console.log(`tick failed: ${err.stack}`)
				check(`${mode}: driver step`, false, String(err))
			}
		}
		await sleep(250)
	}
	const timedOut = !done
	if (timedOut) {
		for (const sig of ['SIGTERM', 'SIGKILL']) {
			try {
				process.kill(-proc.pid, sig)
			} catch {}
			const finished = await Promise.race([exited.then(() => true), sleep(10_000).then(() => false)])
			if (finished) break
		}
	}
	writeFileSync(logPath, scrub(chunks.join('')))
	check(`${mode}: QGIS exited by itself within ${args.timeout} s`, !timedOut, timedOut ? 'killed' : undefined)
	check(`${mode}: QGIS exit status 0`, exitCode === 0, exitCode)
	const reportPath = join(OUT, `${PREFIX}-${mode}.json`)
	if (!existsSync(reportPath)) {
		check(`${mode}: report written`, false)
		return null
	}
	return JSON.parse(readFileSync(reportPath, 'utf-8'))
}

function featuresShown(layers) {
	return Object.fromEntries((layers ?? []).map((l) => [l.layerId, l.featuresIterated]))
}

function same(a, b) {
	return JSON.stringify(Object.entries(a).sort()) === JSON.stringify(Object.entries(b).sort())
}

function commonChecks(mode, report) {
	check(`${mode}: plugin loaded by QGIS`, report.pluginLoaded)
	check(`${mode}: no failures in the QGIS script`, !report.failures?.length, report.failures?.length ? report.failures : undefined)
	check(`${mode}: no Python exceptions`, !report.exceptions?.length, report.exceptions?.length ? report.exceptions : undefined)
	const pyErrors = (report.messageLog ?? []).filter((m) => m.tag === 'Python error')
	check(`${mode}: no Python errors in the QGIS message log`, !pyErrors.length, pyErrors.length ? pyErrors : undefined)
	for (const layer of report.layers ?? []) {
		check(`${mode}: ${layer.layerId} layer valid`, layer.valid)
		check(`${mode}: ${layer.layerId} layer read-only`, layer.readOnly)
		check(
			`${mode}: ${layer.layerId} drawn with the CoMapeo categories`,
			layer.renderer === 'categorizedSymbol' && (layer.categoryCount ?? 0) > 1,
			`${layer.categoryCount} categories`,
		)
	}
	for (const [name, fraction] of Object.entries(report.canvasPaintedFraction ?? {})) {
		check(`${mode}: ${name} screenshot has something drawn`, fraction > 0.0002, `${(fraction * 100).toFixed(2)}% of pixels`)
	}
}

const PREEXISTING = new Set()

function processesOf(appInfo) {
	try {
		return execFileSync('pgrep', ['-f', appInfo.executable.split('/Contents/')[0]], { encoding: 'utf-8' })
			.trim()
			.split('\n')
			.filter((pid) => pid && !PREEXISTING.has(pid))
	} catch {
		return []
	}
}

async function main() {
	mkdirSync(OUT, { recursive: true })
	if (existsSync(WORK)) {
		if (readdirSync(WORK).length && !existsSync(join(WORK, WORK_MARKER))) {
			throw new Error(`--work ${WORK} is not empty and was not made by this script`)
		}
		rmSync(WORK, { recursive: true, force: true })
	}
	mkdirSync(FAKE_HOME, { recursive: true })
	writeFileSync(join(WORK, WORK_MARKER), '')
	const summary = { qgisVersion: QGIS_VERSION, started: new Date().toISOString() }

	console.log('Launching desktop…')
	let desktop = await launchDesktop()
	summary.desktopVersion = desktop.appVersion
	check('desktop: runs on the isolated user data folder under the throwaway HOME', true, USER_DATA)
	await onboardAndCreateProject(desktop.page)
	const setting = await switchOnQgis(desktop.page)
	check('desktop: Allow QGIS to Connect was off before, on after a click in Settings', !setting.wasChecked, setting)
	check('desktop: no error shown on the settings card', setting.alert === 0)
	const discoveryMode = statSync(DISCOVERY_FILE).mode & 0o777
	check('desktop: discovery file in the standard folder, mode 0600', discoveryMode === 0o600, DISCOVERY_FILE)

	let oracle = await Oracle.open()
	summary.hello = oracle.hello
	const [project] = (await oracle.projects()).filter((p) => p.name === PROJECT_NAME)
	if (!check('desktop: project listed by the local API', project, project)) throw new Error('no project')
	const projectId = project.projectId

	const firstBatch = await createTestData(desktop.page, projectId, { count: OBSERVATIONS, track: true })
	const secondBatch = await createTestData(desktop.page, projectId, { count: 12, track: true })
	await desktop.page.screenshot({ path: join(OUT, 'desktop-test-data.png') })
	summary.testData = [firstBatch, secondBatch]
	const expected = await oracle.counts(projectId)
	summary.desktopHolds = expected
	check('desktop: holds the generated observations and tracks', expected.observations === OBSERVATIONS + 12 && expected.tracks === 2, expected)
	await desktop.page.evaluate((id) => {
		window.location.hash = `#/app/projects/${id}`
	}, projectId)
	await sleep(2500)
	await desktop.page.screenshot({ path: join(OUT, 'desktop-project.png') })

	makeProfile()
	const state = {}
	const onTick = async () => {
		if (!state.created && existsSync(join(WORK, 'phase1.json'))) {
			state.created = 'pending'
			const before = await oracle.ids(projectId)
			const note = await createTestData(desktop.page, projectId, { count: 1, track: false })
			const after = await poll(async () => {
				const ids = await oracle.ids(projectId)
				return ids.observations.length > before.observations.length ? ids : null
			}, 'the new observation in the local API', 15_000)
			const docIds = after.observations.filter((id) => !before.observations.includes(id))
			state.created = { docId: docIds[0], note, docIds }
			state.after = Object.fromEntries(Object.entries(after).map(([k, v]) => [k, v.length]))
			state.createdAt = Date.now()
			writeJson(join(WORK, 'mutated.json'), { docId: docIds[0] })
			await desktop.page.screenshot({ path: join(OUT, 'desktop-new-observation.png') })
		}
		if (state.created && !state.quit && existsSync(join(WORK, 'phase2.json'))) {
			state.quit = 'pending'
			oracle.close()
			const quit = await quitDesktop(desktop.app)
			state.quit = {
				...quit,
				discoveryFileRemoved: !existsSync(DISCOVERY_FILE),
				processesLeft: processesOf(desktop.appInfo),
			}
			writeJson(join(WORK, 'desktop-quit.json'), state.quit)
		}
	}

	console.log(`Launching QGIS ${QGIS_VERSION}…`)
	const live = await launchQgis('live', projectId, [], onTick)
	if (state.quit === undefined || state.quit === 'pending') {
		await quitDesktop(desktop.app)
	}
	if (live) {
		commonChecks('live', live)
		const servers = live.status?.servers ?? []
		check(
			'live: plugin found desktop through the standard discovery folder (internal build)',
			servers.some((s) => s.build === 'internal' && s.state === 'connected'),
			servers,
		)
		check(
			'live: Browser items keep their CoMapeo classes',
			['RootItem', 'ProjectItem', 'LayerItem'].every((c) => (live.browserItemClasses ?? []).includes(c)),
			live.browserItemClasses,
		)
		check(
			'live: layers added with a mouse double-click in the Browser',
			live.addedBy && Object.values(live.addedBy).every((v) => v.includes('mouse')),
			live.addedBy,
		)
		const shown = featuresShown(live.layers)
		check('live: counts in QGIS match what desktop holds', same(shown, expected), { qgis: shown, desktop: expected })
		const status = live.status?.project ?? {}
		check('live: panel shows the project up to date', status.connected && status.describe?.startsWith('Up to date'), status.describe)
		const obs = (live.layers ?? []).find((l) => l.layerId === 'observations') ?? {}
		check('live: observation categories carry icons', (obs.categoriesWithIcon ?? 0) > 0, `${obs.categoriesWithIcon} of ${obs.categoryCount}`)
		if (state.created && typeof state.created === 'object') {
			const after = featuresShown(live.layersAfterUpdate)
			check(
				'live: an observation created in desktop reaches QGIS without a refresh',
				live.createdDocId === state.created.docId && same(after, state.after),
				{ seconds: live.liveUpdateSeconds, qgis: after, desktop: state.after },
			)
		} else {
			check('live: observation created in desktop', false, 'QGIS never reached phase 1')
		}
		if (state.quit && typeof state.quit === 'object') {
			check(`quit: desktop exited by itself within ${QUIT_TIMEOUT_MS / 1000} s of a quit`, state.quit.exitedBySelf, state.quit)
			check('quit: desktop removed its discovery file', state.quit.discoveryFileRemoved)
			check('quit: no desktop process left', state.quit.processesLeft.length === 0, state.quit.processesLeft)
			const afterQuit = live.statusAfterQuit ?? {}
			check(
				'quit: QGIS reads the project as not connected',
				afterQuit.project && !afterQuit.project.connected && afterQuit.project.describe?.startsWith('Not connected'),
				{ describe: afterQuit.project?.describe, seconds: live.staleAfterQuitSeconds },
			)
			const badges = afterQuit.layerTreeBadges ?? {}
			check('quit: every CoMapeo layer has a stale badge', Object.keys(badges).length >= 2 && Object.values(badges).every((b) => b.length), badges)
			check('quit: layers still show the data', same(featuresShown(live.layersAfterQuit), state.after), featuresShown(live.layersAfterQuit))
		} else {
			check('quit: desktop quit while QGIS was open', false, 'QGIS never reached phase 2')
		}
	}
	summary.live = { liveUpdateSeconds: live?.liveUpdateSeconds, staleAfterQuitSeconds: live?.staleAfterQuitSeconds, images: live?.images }

	const projectFile = join(WORK, 'smoke.qgz')
	if (check('offline: saved project exists', existsSync(projectFile))) {
		check('offline: desktop is not running', !existsSync(DISCOVERY_FILE) && processesOf(desktop.appInfo).length === 0)
		const offline = await launchQgis('offline', projectId, ['--project', projectFile])
		if (offline) {
			commonChecks('offline', offline)
			const shown = featuresShown(offline.layers)
			check('offline: cached features drawn', same(shown, state.after ?? expected), { qgis: shown, cachedFromDesktop: state.after ?? expected })
			const status = offline.status?.project ?? {}
			check('offline: project reads as not connected', !status.connected && status.describe?.startsWith('Not connected. Showing data cached on'), status.describe)
			const badges = offline.status?.layerTreeBadges ?? {}
			check('offline: every CoMapeo layer has a stale badge', Object.keys(badges).length >= 2 && Object.values(badges).every((b) => b.length), badges)
			summary.offline = { images: offline.images }
		}
	}

	writeFileSync(join(OUT, `${PREFIX}-desktop.log`), scrub(desktopLog.join('')))
	const failed = checks.filter((c) => !c.ok)
	summary.finished = new Date().toISOString()
	writeJson(join(OUT, `${PREFIX}-summary.json`), { ...summary, checks, failed: failed.length })
	console.log(`${checks.length - failed.length} passed, ${failed.length} failed`)
	return failed.length ? 1 : 0
}

main().then(
	(code) => process.exit(code),
	async (err) => {
		console.error(err)
		if (openDesktop) await quitDesktop(openDesktop)
		writeFileSync(join(OUT, `${PREFIX}-desktop.log`), scrub(desktopLog.join('')))
		writeJson(join(OUT, `${PREFIX}-summary.json`), { error: String(err.stack), checks })
		process.exit(2)
	},
)
