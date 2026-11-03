import test from 'node:test';
import assert from 'node:assert/strict';
import { build } from 'esbuild';
const bundle = await build({ stdin: { contents: "export * from './src/agent-request'; export { ApiError } from './src/api';", resolveDir: process.cwd() }, bundle: true, write: false, format: 'esm', platform: 'node' });
const { newChatRequest, keepPending, savePending, readPending, appendResult, ApiError } = await import(
  'data:text/javascript;base64,' + Buffer.from(bundle.outputFiles[0].text).toString('base64'));

test('request snapshot remains stable when user changes selected versions', () => {
  const versions = ['a'.repeat(32)];
  const payload = newChatRequest('查质量', 'quality', versions);
  versions[0] = 'b'.repeat(32);
  assert.equal(payload.version_ids[0], 'a'.repeat(32));
  assert.equal(payload.request_id.length, 36);
  assert.equal(keepPending(new TypeError('network reset')), true);
  assert.equal(keepPending(new SyntaxError('incomplete response')), true);
});
test('page refresh restores same request only for the originating account', t => {
  const data = new Map();
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: {
    getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value), removeItem: key => data.delete(key)
  } });
  t.after(() => { if (previous) Object.defineProperty(globalThis, 'sessionStorage', previous); else delete globalThis.sessionStorage; });
  const payload = newChatRequest('找数据', 'search', []);
  savePending('owner', payload);
  assert.deepEqual(readPending('owner'), payload);
  assert.equal(readPending('reader'), undefined);
  savePending('owner'); assert.equal(readPending('owner'), undefined);
});
test('validation or identity failures clear pending, uncertain or busy requests retain it', () => {
  for (const code of [400, 401, 403, 404, 422]) assert.equal(keepPending(new ApiError('error', code)), false);
  for (const code of [409, 429, 500, 502, 503]) assert.equal(keepPending(new ApiError('error', code)), true);
});
test('replayed result does not append the same run twice', () => {
  const first = { run_id: 'r', answer: 'first' };
  const rows = appendResult([first], { run_id: 'r', answer: 'saved' });
  assert.equal(rows.length, 1); assert.equal(rows[0].answer, 'saved');
});
