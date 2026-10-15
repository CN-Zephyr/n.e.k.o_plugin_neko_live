// Run against the same UI kit, compiler and declarations used by the host.
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
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
  window.eval(readFileSync(resolve(host, 'frontend/plugin-manager/src/components/plugin/hosted/ui-kit/runtime.js'), 'utf8'))
  // happy-dom constructs text nodes via window.Text. The host kit exposes its
  // Text component there too; preserve DOM construction and bind kit locals.
  window.Text = NativeText
  const original = readFileSync(resolve(root, 'ui/panel_compat.tsx'), 'utf8')
  const names = original.match(/^import \{ (.*) \} from "@neko\/plugin-ui"/m)[1]
  const source = original.replace(/^import .* from "@neko\/plugin-ui"\n/m, '').replace('export default function ', 'function ')
  const compiled = transform(source, { transforms: ['typescript', 'jsx'], jsxPragma: 'h', jsxFragmentPragma: 'Fragment', production: true }).code
  window.eval('(function(){ const { h, Fragment, ' + names + ' } = window.NekoUiKit;\n' + compiled + '\nwindow.__ProfileTable = ViewerProfilesTable; window.__Panel = NekoLivePanel; })()')
  const kit = window.NekoUiKit
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
  const clickButton = (label, last = false) => {
    const buttons = [...window.document.querySelectorAll('button')].filter(button => button.textContent.trim() === label)
    assert.ok(buttons.length, `missing button ${label}`)
    buttons[last ? buttons.length - 1 : 0].click()
  }
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
  console.log('UI typecheck, six-source compilation, search/reset/delete interaction and new-action forwarding passed')
} finally {
  await window.happyDOM.close()
}
