"""Exercise the real Desktop entry with synthetic host boundaries; no audio or network."""
import json
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "desktop" / "plugin.js"


def run_desktop(script, *, browser_language="en-US", storage=None):
    source = SOURCE.read_text()
    source = "\n".join(line for line in source.splitlines() if not line.startswith("import "))
    source = source.replace("export default {", "const plugin = {", 1)
    harness = r'''
const assert = require('node:assert/strict');
Object.defineProperty(globalThis, 'navigator', {value: {language: BROWSER_LANGUAGE}, configurable: true});
const stored = new Map(Object.entries(STORAGE));
const writes = [];
const ctx = {
  storage: {get: key => stored.get(key), set: (key, value) => {writes.push([key, value]); stored.set(key, value);}},
  register: item => contributions.push(item),
  rest: async () => {throw new Error('No network allowed in this test');}
};
const contributions = [];
const jsx = (type, props) => ({type, props: props || {}}), jsxs = jsx;
let stateOverrides = [];
let deviceOverrides = null;
const useState = value => [stateOverrides.length ? stateOverrides.shift() : (deviceOverrides && value?.mics && value?.outs ? deviceOverrides : (typeof value === 'function' ? value() : value)), () => {}];
const useEffect = () => {}, useRef = value => ({current: value});
const sdk = {
  host: {state: {focusedSessionProfile: 'default', focusedSessionId: 'session'}, notifyError: () => {}},
  useValue: value => value,
  ...Object.fromEntries(['Popover','PopoverContent','PopoverTrigger','Select','SelectContent','SelectItem','SelectTrigger','SelectValue','Switch','Button'].map(name => [name,name]))
};
globalThis.window = {dispatchEvent: () => {}};
globalThis.CustomEvent = class {};
SOURCE
plugin.register(ctx);
const renderComposer = () => {
  const element = contributions.find(item => item.id === 'composer-live').render();
  return element.type(element.props);
};
const nodes = tree => {
  if (tree == null || typeof tree === 'boolean') return [];
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  if (typeof tree !== 'object') return [tree];
  if (typeof tree.type === 'function') return nodes(tree.type(tree.props));
  return [tree, ...nodes(tree.props.children)];
};
SCRIPT
'''.replace("BROWSER_LANGUAGE", json.dumps(browser_language)).replace("STORAGE", json.dumps(storage or {})).replace("SOURCE", source).replace("SCRIPT", script)
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("browser_language", ["es-CL", "fr-FR", "de-DE", "", "en-US"])
def test_desktop_defaults_to_english_independent_of_browser_locale(browser_language):
    run_desktop("assert.equal(tr('Español', 'English'), 'English');", browser_language=browser_language)


def test_explicit_spanish_preference_is_honored_without_changing_other_settings():
    run_desktop("""
assert.equal(tr('Español', 'English'), 'Español');
assert.equal(stored.get('engine.v1'), 'codex');
assert.equal(stored.get('voice.v3'), 'juniper');
assert.deepEqual(writes, []);
""", storage={"language.v1": "es", "engine.v1": "codex", "voice.v3": "juniper"})


def test_language_picker_saves_only_an_explicit_language_preference():
    run_desktop("""
const select = nodes(renderComposer()).find(node => node?.props?.['aria-label'] === 'Voice language');
assert.ok(select, 'Voice language selector is missing');
select.props.onValueChange('es');
assert.equal(stored.get('language.v1'), 'es');
assert.equal(tr('Español', 'English'), 'Español');
assert.equal(stored.get('engine.v1'), 'codex');
assert.equal(stored.get('voice.v3'), 'juniper');
bus.live = true;
select.props.onValueChange('en');
assert.equal(stored.get('language.v1'), 'es', 'An active call must retain its language');
assert.equal(tr('Español', 'English'), 'Español');
bus.live = false;
select.props.onValueChange('en');
assert.equal(stored.get('language.v1'), 'en');
assert.equal(tr('Español', 'English'), 'English');
assert.deepEqual(writes, [['language.v1','es'], ['language.v1','en']]);
""", storage={"engine.v1": "codex", "voice.v3": "juniper"})


def test_subscription_quota_error_does_not_switch_engine_or_voice():
    run_desktop("""
startLive = async () => {throw new Error('LIVE_SIN_QUOTA: Subscription usage limit reached.');};
const button = nodes(renderComposer()).find(node => node?.props?.title === 'Live Voice — click to talk');
assert.ok(button);
(async () => {
  await button.props.onClick();
  assert.equal(stored.get('engine.v1'), 'codex', 'Quota failure must not select another billing lane');
  assert.equal(stored.get('voice.v3'), 'juniper');
  assert.ok(!writes.some(([key]) => key === 'engine.v1'));
  assert.match(bus.err, /Subscription usage limit/);
})().catch(error => {console.error(error); process.exitCode = 1;});
""", storage={"engine.v1": "codex", "voice.v3": "juniper"})


def test_usage_ui_keeps_local_measurement_separate_from_provider_allowance():
    run_desktop("""
const usage = {ok: true, plan: 'plus', voice: {ok: true, rolling5h: {minutes: 10.5}, week: {sessions: 1, minutes: 10.5}}, windows: {'Provider window': {usedPercent: 25}}};
stateOverrides = [null, false, false, false, '', usage, false];
const text = nodes(CodexSection({ctx})).filter(node => typeof node === 'string').join(' ');
assert.ok(!text.includes('~15-30 min'), 'Do not invent a plan voice allowance');
assert.ok(!text.includes('plan allowance'), 'Local duration is not provider quota');
assert.ok(text.includes('10.5 min'), 'English duration must not use a Spanish decimal comma');
assert.ok(text.includes('25% used') && text.includes('75% left'), 'Keep actual provider-reported usage');
""")


def test_queued_correction_is_not_reported_as_already_applied():
    run_desktop("""
_delegBusy = true;
const messages = [];
const dc = {send: text => messages.push(JSON.parse(text))};
handleDelegation(ctx, {item: {id: 'correction', content: [{text: 'Please fix the running task to use English'}]}}, dc);
const instruction = messages.at(-1).content[0].text;
assert.ok(!instruction.includes('will be reflected'), 'Queued is not applied');
assert.ok(instruction.includes('not yet been applied'));
""")


def test_connection_progress_uses_the_selected_english_language():
    run_desktop("""
const logs = [];
navigator.mediaDevices = {getUserMedia: async () => {const error = new Error('Synthetic permission refusal'); error.name = 'NotAllowedError'; throw error;}};
(async () => {
  await assert.rejects(startLive(ctx, {engine: 'codex', log: message => logs.push(message)}), /MIC: NotAllowedError/);
  assert.deepEqual(logs, ['requesting session…', 'opening microphone…']);
})().catch(error => {console.error(error); process.exitCode = 1;});
""")


def test_unlabeled_output_device_has_an_english_fallback():
    run_desktop("""
deviceOverrides = {mics: [], outs: [{deviceId: 'speaker', label: ''}]};
const text = nodes(renderComposer()).filter(node => typeof node === 'string').join(' ');
assert.ok(text.includes('Output 1'), 'Unnamed speakers must use the selected UI language');
assert.ok(!text.includes('Salida 1'));
""")
