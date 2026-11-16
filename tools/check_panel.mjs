// Run against the same UI kit, compiler and declarations used by the host.
import assert from 'node:assert/strict'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = process.argv[3] ? resolve(process.argv[3]) : resolve(dirname(fileURLToPath(import.meta.url)), '..')
const host = resolve(process.argv[2] || process.env.NEKO_UI_HOST_ROOT || '../neko')
const requireHost = createRequire(resolve(host, 'frontend/plugin-manager/package.json'))
const { transform } = requireHost('sucrase')
const ts = requireHost('typescript')
const { Window } = await import(pathToFileURL(requireHost.resolve('happy-dom')).href)
const files = readdirSync(resolve(root, 'ui')).filter(name => /\.tsx?$/.test(name)).map(name => resolve(root, 'ui', name))
const options = {
  noEmit: true, target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext,
  moduleResolution: ts.ModuleResolutionKind.Bundler, jsx: ts.JsxEmit.Preserve,
  skipLibCheck: true, paths: { '@neko/plugin-ui': [resolve(host, 'plugin/sdk/hosted-ui/index.d.ts')] },
}
const program = ts.createProgram(files, options)
const diagnostics = ts.getPreEmitDiagnostics(program)
if (diagnostics.length) throw new Error(ts.formatDiagnosticsWithColorAndContext(diagnostics, {
  getCanonicalFileName: name => name, getCurrentDirectory: () => root, getNewLine: () => '\n',
}))
for (const file of files) transform(readFileSync(file, 'utf8'), { transforms: ['typescript', 'jsx'], production: true })

const window = new Window({ url: 'https://host.example' })
try {
  window.__NEKO_PAYLOAD = { locale: 'en', host: { origin: 'https://host.example' }, i18n: { locale: 'en', messages: {} } }
  const NativeText = window.Text
  const runtime = readFileSync(resolve(host, 'frontend/plugin-manager/src/components/plugin/hosted/ui-kit/runtime.js'), 'utf8')
  // The host prepends the shared display factory before evaluating its UI kit.
  const displayClient = resolve(host, 'static/plugin-ui-sdk/display.js')
  const displayFactory = existsSync(displayClient)
    ? `const createPluginDisplayClient = ${requireHost(displayClient).toString()};\n`
    : ''
  window.eval(displayFactory + runtime)
  // happy-dom constructs text nodes via window.Text. The host kit exposes its
  // Text component there too; preserve DOM construction and bind kit locals.
  window.Text = NativeText
  const original = readFileSync(resolve(root, 'ui/panel_compat.tsx'), 'utf8')
  const names = original.match(/^import \{ (.*) \} from "@neko\/plugin-ui"/m)[1]
  const source = original.replace(/^import .* from "@neko\/plugin-ui"\r?\n/m, '').replace('export default function ', 'function ')
  const compiled = transform(source, { transforms: ['typescript', 'jsx'], jsxPragma: 'h', jsxFragmentPragma: 'Fragment', production: true }).code
  window.eval('(function(){ const { h, Fragment, ' + names + ' } = window.NekoUiKit;\n' + compiled + '\nwindow.__ProfileTable = ViewerProfilesTable; window.__Panel = NekoLivePanel; window.__AvatarPreview = AvatarPreview; window.__MemoryForgetRecovery = MemoryForgetRecovery; })()')
  const kit = window.NekoUiKit
  const componentSource = readFileSync(resolve(root, 'ui/panel_components.tsx'), 'utf8')
    .replace(/^import\s+\{[\s\S]*?\}\s+from "@neko\/plugin-ui"\r?\n/m, '')
    .replace(/\bexport function /g, 'function ')
  const componentCode = transform(componentSource, { transforms: ['typescript', 'jsx'], jsxPragma: 'h', jsxFragmentPragma: 'Fragment', production: true }).code
  window.eval('(function(){ const { h, Fragment, ' + names + ' } = window.NekoUiKit;\n' + componentCode + '\nwindow.__ModularAvatarPreview = AvatarPreview; })()')
  const previewContainer = window.document.createElement('div')
  window.document.body.append(previewContainer)
  const inlineJpeg = 'data:image/jpeg;base64,/9j/2Q=='
  for (const component of [window.__AvatarPreview, window.__ModularAvatarPreview]) {
    for (const src of [
      'https://example.invalid/avatar', 'http://127.0.0.1:48911/private',
      '//example.invalid/avatar', 'file:///C:/private.png', 'blob:https://host.example/avatar',
      'data:image/svg+xml,<svg onload="alert(1)"/>',
      'data:image/jpeg;base64,' + 'A'.repeat(16385), null, {},
    ]) {
      kit.render(kit.h(component, { src, alt: 'avatar' }), previewContainer)
      assert.equal(previewContainer.querySelector('img'), null, 'unsafe or oversized avatar must not reach img.src')
    }
    kit.render(kit.h(component, { src: inlineJpeg, alt: 'avatar' }), previewContainer)
    assert.equal(previewContainer.querySelector('img').getAttribute('src'), inlineJpeg)
    kit.render(null, previewContainer)
  }
  previewContainer.remove()
  let calls = 0
  let answer = []
  let deleted = 0
  let reset = 0
  function Parent() {
    return kit.h(window.__ProfileTable, {
      t: key => key, profiles: [],
      onDeleteProfile: async () => { deleted += 1; answer = [] },
      onResetImpression: async () => { reset += 1; answer = [{ uid: '7', nickname: 'updated viewer' }] },
      onSearch: async () => { calls += 1; assert.ok(calls < 5, 'search request loop'); return answer },
    })
  }
  const container = window.document.createElement('main')
  window.document.body.append(container)
  kit.render(kit.h(Parent, {}), container)
  const input = container.querySelector('input')
  assert.ok(input)
  input.value = 'abc'
  input.dispatchEvent(new window.Event('input', { bubbles: true }))
  await new Promise(resolve => setTimeout(resolve, 100))
  assert.equal(calls, 1, 'one query should produce one search')
  kit.render(kit.h(Parent, {}), container)
  await new Promise(resolve => setTimeout(resolve, 50))
  assert.equal(calls, 1, 'parent rerender must not repeat search')
  answer = [{ uid: '7', nickname: 'viewer' }]
  const next = container.querySelector('input')
  next.value = 'abcd'
  next.dispatchEvent(new window.Event('input', { bubbles: true }))
  await new Promise(resolve => setTimeout(resolve, 100))
  assert.equal(calls, 2)
  assert.match(container.textContent, /viewer/)
  const findButton = (label, last = false) => {
    const buttons = [...window.document.querySelectorAll('button')].filter(button => button.textContent.trim() === label)
    assert.ok(buttons.length, `missing button ${label}`)
    return buttons[last ? buttons.length - 1 : 0]
  }
  const clickButton = (label, last = false) => findButton(label, last).click()
  clickButton('viewer')
  await new Promise(resolve => setTimeout(resolve, 20))
  clickButton('panel.actions.resetViewerImpression')
  await new Promise(resolve => setTimeout(resolve, 20))
  clickButton('panel.actions.resetViewerImpression', true)
  await new Promise(resolve => setTimeout(resolve, 100))
  assert.equal(reset, 1)
  assert.equal(calls, 3, 'reset should refresh the active search once')
  assert.match(container.textContent, /updated viewer/)
  clickButton('updated viewer')
  await new Promise(resolve => setTimeout(resolve, 20))
  clickButton('panel.actions.deleteViewerProfile')
  await new Promise(resolve => setTimeout(resolve, 20))
  clickButton('panel.actions.deleteViewerProfile', true)
  await new Promise(resolve => setTimeout(resolve, 100))
  assert.equal(deleted, 1)
  assert.equal(calls, 4, 'delete should refresh the active search once')
  assert.doesNotMatch(container.textContent, /updated viewer/)
  kit.render(kit.h(Parent, {}), container)
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.equal(calls, 4, 'refresh after deletion must not restart the search loop')
  kit.render(null, container)
  const forgotten = []
  let rejectForget = true
  const forgetProfile = { uid: '42', nickname: 'forget viewer' }
  const renderForget = (disabled, target = 'CatA') => kit.render(kit.h(window.__ProfileTable, {
    t: key => key, profiles: [forgetProfile],
    onResetImpression: async () => {}, onDeleteProfile: async () => {},
    hostMemoryScope: { platform: 'twitch', target }, hostMemoryDisabled: disabled,
    onForgetProfile: async (uid, scope) => {
      forgotten.push({ uid, ...scope })
      if (rejectForget) throw new Error('unconfirmed erasure')
    },
  }), container)
  renderForget(true)
  clickButton('forget viewer')
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.equal(findButton('panel.privacy.forgetHost').disabled, true, 'active stream disables host erasure')
  renderForget(false)
  await new Promise(resolve => setTimeout(resolve, 20))
  clickButton('panel.privacy.forgetHost')
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.match(window.document.body.textContent, /twitch \/ CatA \/ UID: 42/)
  forgetProfile.uid = '43'
  renderForget(false, 'CatB')
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.match(window.document.body.textContent, /twitch \/ CatA \/ UID: 42/, 'confirmation must retain its original scope')
  clickButton('panel.privacy.forgetHost', true)
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.deepEqual(forgotten[0], { uid: '42', platform: 'twitch', target: 'CatA' })
  assert.match(window.document.body.textContent, /panel\.privacy\.forgetConfirm/, 'uncertain deletion keeps retry confirmation open')
  rejectForget = false
  clickButton('panel.privacy.forgetHost', true)
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.deepEqual(forgotten[1], forgotten[0])
  assert.doesNotMatch(window.document.body.textContent, /panel\.privacy\.forgetConfirm/)
  kit.render(null, container)
  const recovered = []
  let recoveryFails = true
  const pendingRecord = { uid: '42', platform: 'twitch', target: 'CatA' }
  const renderRecovery = disabled => kit.render(kit.h(window.__MemoryForgetRecovery, {
    t: key => key, disabled, state: { available: true, pending: [pendingRecord] },
    onRetry: async (uid, scope) => {
      recovered.push({ uid, ...scope })
      if (recoveryFails) throw new Error('retry required')
    },
  }), container)
  renderRecovery(true)
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.equal(recovered.length, 0, 'restoring pending state must not erase automatically')
  assert.equal(findButton('panel.privacy.retryForget').disabled, true)
  renderRecovery(false)
  await new Promise(resolve => setTimeout(resolve, 20))
  clickButton('panel.privacy.retryForget')
  await new Promise(resolve => setTimeout(resolve, 20))
  pendingRecord.uid = '43'
  renderRecovery(false)
  await new Promise(resolve => setTimeout(resolve, 20))
  clickButton('panel.privacy.retryForget', true)
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.deepEqual(recovered[0], { uid: '42', platform: 'twitch', target: 'CatA' })
  assert.match(window.document.body.textContent, /panel\.privacy\.forgetConfirm/)
  recoveryFails = false
  clickButton('panel.privacy.retryForget', true)
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.deepEqual(recovered[1], recovered[0])
  assert.doesNotMatch(window.document.body.textContent, /panel\.privacy\.forgetConfirm/)
  kit.render(null, container)
  // A newly registered action is deliberately absent from the startup list.
  // api.call should still send it to the host for runtime authorization.
  const sent = []
  Object.defineProperty(window, 'parent', { configurable: true, value: { postMessage(message) {
    sent.push(message)
    window.dispatchEvent(new window.MessageEvent('message', { data: {
      type: 'neko-hosted-surface-response', requestId: message.requestId, ok: true, result: { accepted: true },
    } }))
  } } })
  assert.deepEqual(await kit.api.call('new_action_after_startup', {}), { accepted: true })
  assert.equal(sent[0].payload.actionId, 'new_action_after_startup')

  const douyinContainer = window.document.createElement('main')
  window.document.body.append(douyinContainer)
  const douyinActions = []
  let failLogout = true
  const renderDouyinPanel = (bridgeAvailable, listening = false) => kit.render(kit.h(window.__Panel, {
    state: {
      store_enabled: true,
      config: { live_platform: 'douyin', live_room_ref: '1234' },
      modules: [{ id: 'douyin_live_ingest', status: { bridge_available: bridgeAvailable } }],
      live_connection: listening ? { state: 'connected', connected: true } : {},
    },
    t: key => key,
    api: { refresh: async () => {}, call: async action => {
      douyinActions.push(action)
      if (action === 'douyin_cookie_status') return { logged_in: true }
      if (action === 'douyin_cookie_delete') {
        return failLogout ? { logged_out: false, logout_error: 'credential_removal_failed' } : { logged_out: true }
      }
      return {}
    } },
  }), douyinContainer)
  renderDouyinPanel(false)
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.match(window.document.body.textContent, /panel\.onboarding\.privacyBody/)
  assert.match(window.document.body.textContent, /panel\.onboarding\.privacyAction/)
  clickButton('panel.actions.cancel')
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.equal(findButton('panel.console.preparation.notReady').disabled, true)
  clickButton('panel.console.manage')
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.match(window.document.body.textContent, /panel\.platform\.douyinSourceOnly/)
  assert.match(window.document.body.textContent, /panel\.douyinAuth\.bridgeUnavailable/)
  assert.equal(findButton('panel.actions.douyinCookieImport').disabled, true)
  assert.equal(findButton('panel.actions.douyinCookieValidate').disabled, true)
  renderDouyinPanel(true, true)
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.equal(findButton('panel.actions.douyinCookieDelete').disabled, false, 'logout is available while listening')
  clickButton('panel.actions.douyinCookieDelete')
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.ok(douyinActions.includes('douyin_cookie_delete'), 'existing credentials remain removable')
  const accountDialog = window.document.querySelector('[role="dialog"]')
  assert.ok(accountDialog.textContent.includes('panel.auth.logoutFailed'))
  failLogout = false
  clickButton('panel.actions.douyinCookieDelete')
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.equal(douyinActions.filter(action => action === 'douyin_cookie_delete').length, 2)
  assert.doesNotMatch(accountDialog.textContent, /panel\.auth\.logoutFailed/)
  renderDouyinPanel(true)
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.match(window.document.body.textContent, /panel\.douyinAuth\.bridgeNetworkHint/)
  assert.doesNotMatch(window.document.body.textContent, /panel\.douyinAuth\.bridgeUnavailable/)
  assert.equal(findButton('panel.actions.douyinCookieImport').disabled, false)
  kit.render(null, douyinContainer)

  window.localStorage.setItem('neko-live:onboarding:v2', 'done')
  const panelContainer = window.document.createElement('main')
  window.document.body.append(panelContainer)
  const settingsCalls = []
  let rejectSettings = false
  const panelState = {
    store_enabled: true, config: { danmaku_filter_enabled: true, danmaku_filter_prefixes: '!\n！' },
    viewer_store: { writable: true, capacity_reasons: ['profile_count', 'file_size'] },
  }
  kit.render(kit.h(window.__Panel, {
    state: panelState, t: key => key,
    api: { refresh: async () => {}, call: async (action, payload) => {
      if (action === 'clear_recent_diagnostics') {
        settingsCalls.push(action)
        return { cleared: true }
      }
      assert.equal(action, 'update_config')
      if (rejectSettings) throw new Error('test save failure')
      settingsCalls.push(JSON.parse(JSON.stringify(payload)))
      return { accepted: true }
    } },
  }), panelContainer)
  await new Promise(resolve => setTimeout(resolve, 30))
  clickButton('panel.tabs.interaction')
  await new Promise(resolve => setTimeout(resolve, 30))
  const filterCard = [...panelContainer.querySelectorAll('section.neko-card')].find(card =>
    card.textContent.includes('panel.interaction.module.danmakuFilter.title'))
  assert.ok(filterCard, 'filter settings card must be reachable')
  filterCard.querySelector('[role="switch"]').click()
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.deepEqual(settingsCalls.pop(), { danmaku_filter_enabled: false })
  const openFilter = () => [...panelContainer.querySelectorAll('section.neko-card')].find(card =>
    card.textContent.includes('panel.interaction.module.danmakuFilter.title')).querySelectorAll('button')[1].click()
  openFilter()
  await new Promise(resolve => setTimeout(resolve, 30))
  const editPrefixes = value => {
    const textarea = window.document.querySelector('textarea')
    assert.ok(textarea)
    textarea.value = value
    textarea.dispatchEvent(new window.Event('input', { bubbles: true }))
  }
  assert.equal(window.document.querySelector('textarea').value, '!\n！')
  editPrefixes('#\n私聊:')
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.equal(settingsCalls.length, 0, 'typing prefixes must not save each keystroke')
  rejectSettings = true
  clickButton('panel.actions.save')
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.equal(window.document.querySelector('textarea').value, '#\n私聊:', 'failed save preserves the draft')
  rejectSettings = false
  clickButton('panel.actions.save')
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.deepEqual(settingsCalls.pop(), { danmaku_filter_prefixes: '#\n私聊:' })
  assert.equal(window.document.querySelector('textarea'), null, 'successful save closes the editor')
  openFilter()
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.equal(window.document.querySelector('textarea').value, '#\n私聊:', 'reopening shows saved prefixes')
  clickButton('panel.interaction.module.danmakuFilter.reset')
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.equal(window.document.querySelector('textarea').value, '!\n！')
  assert.equal(settingsCalls.length, 0, 'restoring defaults only changes the draft until saved')
  clickButton('panel.actions.save')
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.deepEqual(settingsCalls.pop(), { danmaku_filter_prefixes: '!\n！' })
  openFilter()
  await new Promise(resolve => setTimeout(resolve, 30))
  editPrefixes('private  chat:\nprivate\tchat:\nprivate\u3000chat:\nprivate\u2028chat:')
  await new Promise(resolve => setTimeout(resolve, 20))
  clickButton('panel.actions.save')
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.deepEqual(settingsCalls.pop(), { danmaku_filter_prefixes: 'private chat:' })
  openFilter()
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.equal(window.document.querySelector('textarea').value, 'private chat:', 'reopening shows normalized whitespace')
  clickButton('panel.actions.cancel', true)
  await new Promise(resolve => setTimeout(resolve, 20))
  const getRoastCard = () => [...panelContainer.querySelectorAll('section.neko-card')].find(card =>
    card.textContent.includes('panel.interaction.module.avatarRoast.title') &&
    card.textContent.includes('panel.interaction.module.entryGreet.title') && card.querySelector('[role="switch"]'))
  assert.ok(getRoastCard())
  getRoastCard().querySelector('[role="switch"]').click()
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.deepEqual(settingsCalls.pop(), { avatar_roast_enabled: false })
  const roastButtons = getRoastCard().querySelectorAll('button')
  roastButtons[roastButtons.length - 1].click()
  await new Promise(resolve => setTimeout(resolve, 30))
  const getAnalysisToggle = () => [...window.document.querySelectorAll('[role="switch"]')].find(toggle =>
    toggle.textContent.includes('panel.interaction.module.avatarRoast.avatarAnalysis'))
  assert.ok(getAnalysisToggle())
  assert.equal(getAnalysisToggle().disabled, false, 'avatar analysis is independent of roasting')
  getAnalysisToggle().click()
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.deepEqual(settingsCalls.pop(), { avatar_analysis_enabled: false })
  getAnalysisToggle().click()
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.deepEqual(settingsCalls.pop(), { avatar_analysis_enabled: true })
  clickButton('panel.actions.cancel', true)
  await new Promise(resolve => setTimeout(resolve, 20))
  clickButton('panel.tabs.settings')
  await new Promise(resolve => setTimeout(resolve, 20))
  clickButton('panel.settings.privacyTab')
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.match(panelContainer.textContent, /panel\.settings\.storageNeedsAttention/)
  assert.match(panelContainer.textContent, /panel\.storage\.profileLimit/)
  assert.match(panelContainer.textContent, /panel\.storage\.fileLimit/)
  assert.match(panelContainer.textContent, /panel\.privacy\.localOnly/)
  const contentCard = () => [...panelContainer.querySelectorAll('section.neko-card')].findLast(card =>
    card.querySelector('textarea') && card.textContent.includes('panel.contentFilter.title'))
  const editTerms = value => {
    const field = contentCard().querySelector('textarea')
    field.value = value
    field.dispatchEvent(new window.Event('input', { bubbles: true }))
  }
  editTerms('blocked text')
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.equal(settingsCalls.length, 0, 'editing a word list must not autosave')
  rejectSettings = true
  contentCard().querySelector('button').click()
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.equal(contentCard().querySelector('textarea').value, 'blocked text', 'failed save must preserve draft')
  assert.match(contentCard().textContent, /panel\.contentFilter\.invalid/)
  rejectSettings = false
  contentCard().querySelector('button').click()
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.deepEqual(settingsCalls.pop(), { content_filter_terms: 'blocked text' })
  editTerms('x'.repeat(65))
  await new Promise(resolve => setTimeout(resolve, 20))
  contentCard().querySelector('button').click()
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.equal(settingsCalls.length, 0, 'oversized terms must not reach configuration API')
  await new Promise(resolve => setTimeout(resolve, 20))
  clickButton('panel.actions.clearRecentDiagnostics')
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.equal(settingsCalls.length, 0, 'opening the dialog does not clear diagnostics')
  clickButton('panel.actions.clearRecentDiagnostics', true)
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.deepEqual(settingsCalls, ['clear_recent_diagnostics'])
  kit.render(null, panelContainer)

  const developerContainer = window.document.createElement('main')
  window.document.body.append(developerContainer)
  const lookupCalls = []
  let provideThumbnail = true
  kit.render(kit.h(window.__Panel, {
    state: { config: { developer_tools_enabled: true } }, t: key => key,
    api: { refresh: async () => {}, call: async (action, payload) => {
      assert.equal(action, 'submit_viewer_event')
      assert.equal(payload.lookup_only, true)
      assert.equal(payload.include_avatar_preview, true)
      lookupCalls.push(payload)
      return {
        identity: { uid: '42', nickname: 'viewer', avatar_url: 'https://example.invalid/avatar', avatar_preview_url: 'http://127.0.0.1:48911/private' },
        profile: { avatar_url: 'https://example.invalid/fallback' },
        avatar_preview_data_url: provideThumbnail ? inlineJpeg : '',
      }
    } },
  }), developerContainer)
  await new Promise(resolve => setTimeout(resolve, 20))
  clickButton('panel.tabs.dev')
  await new Promise(resolve => setTimeout(resolve, 20))
  clickButton('panel.actions.lookupSandbox')
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.equal(lookupCalls.length, 1)
  assert.equal(developerContainer.querySelector('img').getAttribute('src'), inlineJpeg)
  clickButton('panel.dev.emitter.title')
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.equal(developerContainer.querySelector('img').getAttribute('src'), inlineJpeg)
  assert.ok(!developerContainer.textContent.includes(inlineJpeg), 'image data must not enter sandbox JSON')
  const avatarOverride = [...developerContainer.querySelectorAll('label.neko-field')]
    .find(field => field.textContent.includes('panel.fields.overrideAvatarUrl')).querySelector('input')
  avatarOverride.value = 'http://127.0.0.1:48911/private'
  avatarOverride.dispatchEvent(new window.Event('input', { bubbles: true }))
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.equal(developerContainer.querySelector('img'), null, 'an unvalidated override must not reuse the previous thumbnail')
  clickButton('panel.dev.lookup.title')
  await new Promise(resolve => setTimeout(resolve, 20))
  provideThumbnail = false
  clickButton('panel.actions.lookupSandbox')
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.equal(lookupCalls[1].avatar_url, 'http://127.0.0.1:48911/private', 'overrides go through explicit backend lookup')
  assert.equal(developerContainer.querySelector('img'), null, 'missing thumbnail must not fall back to a remote identity/profile URL')
  kit.render(null, developerContainer)
  console.log('UI typecheck, compilation, onboarding, bridge availability, logout, profile actions, settings, action forwarding and avatar privacy passed')
} finally {
  await window.happyDOM.close()
}
