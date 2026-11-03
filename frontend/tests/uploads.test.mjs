// Node 自带测试运行器；esbuild 使用已安装的前端依赖，不额外引入测试框架。
import test from 'node:test';
import assert from 'node:assert/strict';
import { build } from 'esbuild';

const bundle = await build({ entryPoints: ['src/uploads.ts'], bundle: true, write: false, format: 'esm', platform: 'node' });
const { fileProblem, uploadFile, uploadRequest, waitForRetry } = await import(
  'data:text/javascript;base64,' + Buffer.from(bundle.outputFiles[0].text).toString('base64'));

const config = { max_file_bytes: 1024, max_upload_bytes: 64, supported_formats: ['txt'] };
test('dataset formats restrict globally supported images while preserving image labels',()=>{
  const policy={supported_formats:['png','txt','json'],modality_formats:{text:['txt','json'],image:['png','txt','json']}};
  assert.match(fileProblem({name:'photo.png',size:1},{...policy,dataset_modality:'text'}),/类型不符/);
  assert.equal(fileProblem({name:'labels.json',size:1},{...policy,dataset_modality:'image'}),'');
});
function fixture(t, fault = () => undefined) {
  const storage = new Map(), sessions = new Map(), requests = [], notices = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    const method = init.method || 'GET';
    const path = url.slice('/api/v1'.length);
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : init.body;
    requests.push({ path, method, body });
    const failed = fault({ path, method, body, requests, sessions });
    if (failed) return failed;
    const json = data => new Response(JSON.stringify(data), { status: 200 });
    if (path === '/versions/v/uploads') {
      let session = [...sessions.values()].find(value => value.idempotency_key === body.idempotency_key);
      if (!session) {
        session = { ...body, id: 'upload-' + sessions.size, status: 'uploading', part_size: 2,
          parts: {}, expires_at: '2099-01-01T00:00:00', asset_id: null };
        sessions.set(session.id, session);
      }
      return json(session);
    }
    const session = sessions.get(path.split('/')[2]);
    assert.ok(session, path);
    if (method === 'GET') return json(session);
    if (method === 'DELETE') { session.status = 'aborted'; return json({ message: 'aborted' }); }
    if (method === 'PUT') {
      session.parts[path.split('/').pop()] = { etag: 'etag' };
      return json({ etag: 'etag' });
    }
    if (path.endsWith('/complete')) {
      session.status = 'completed'; session.asset_id = 'asset-' + session.id;
      return json({ asset_id: session.asset_id });
    }
    assert.fail('Unexpected request: ' + path);
  });
  const previous = globalThis.localStorage;
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) };
  t.after(() => { globalThis.localStorage = previous; });
  const options = { userId: 'owner', versionId: 'v', previewAllowed: false, config,
    signal: new AbortController().signal, onStatus: message => notices.push(message), onProgress: () => {} };
  return { storage, sessions, requests, notices, options };
}

test('preflight uses scan capacity and supported formats', () => {
  assert.match(fileProblem({ name: 'image.txt', size: 65 }, config), /上限/);
  assert.match(fileProblem({ name: 'image.zip', size: 4 }, config), /格式/);
  assert.match(fileProblem({ name: 'empty.txt', size: 0 }, config), /空文件/);
  assert.equal(fileProblem({ name: 'UPPER.TXT', size: 64 }, config), '');
});

test('reselecting a completed file skips uploading even after session expiry', async t => {
  const f = fixture(t);
  const file = new File(['abcd'], 'file.txt');
  assert.equal((await uploadFile(file, f.options)).reused, false);
  assert.equal(f.storage.size, 1);
  [...f.sessions.values()][0].expires_at = '2000-01-01T00:00:00';
  const before = f.requests.length;
  assert.equal((await uploadFile(file, f.options)).reused, true);
  assert.deepEqual(f.requests.slice(before).map(r => r.method), ['GET']);
  assert.equal(f.sessions.size, 1);
});

test('lost create response retries the same persisted idempotency key', async t => {
  let lost = false;
  const f = fixture(t, ({ path, body, sessions }) => {
    if (path === '/versions/v/uploads' && !lost) {
      lost = true;
      sessions.set('upload-0', { ...body, id: 'upload-0', status: 'uploading', parts: {}, part_size: 2,
        expires_at: '2099-01-01T00:00:00', asset_id: null });
      throw new TypeError('Network response lost');
    }
  });
  await uploadFile(new File(['abcd'], 'file.txt'), f.options);
  const creates = f.requests.filter(r => r.path === '/versions/v/uploads');
  assert.equal(creates.length, 2);
  assert.equal(creates[0].body.idempotency_key, creates[1].body.idempotency_key);
  assert.equal(f.sessions.size, 1);
});

test('resume skips accepted parts after an interrupted upload', async t => {
  let fail = true;
  const f = fixture(t, ({ path }) => {
    if (path.endsWith('/parts/2') && fail) return new Response('{}', { status: 400 });
  });
  const file = new File(['abcd'], 'file.txt');
  await assert.rejects(uploadFile(file, f.options));
  fail = false;
  const before = f.requests.length;
  await uploadFile(file, f.options);
  assert.equal(f.requests.slice(before).filter(r => r.method === 'PUT').length, 1);
  assert.ok(f.requests.slice(before).some(r => r.path.endsWith('/parts/2')));
  assert.equal(f.sessions.size, 1);
});

test('expired unfinished upload is aborted before a new session reserves quota', async t => {
  let fail = true;
  const f = fixture(t, ({ path }) => {
    if (path.endsWith('/parts/2') && fail) return new Response('{}', { status: 400 });
  });
  const file = new File(['abcd'], 'file.txt');
  await assert.rejects(uploadFile(file, f.options));
  f.sessions.get('upload-0').expires_at = '2000-01-01T00:00:00';
  fail = false;
  const before = f.requests.length;
  await uploadFile(file, f.options);
  const operations = f.requests.slice(before);
  assert.deepEqual(operations.slice(0, 3).map(r => r.method), ['GET', 'DELETE', 'POST']);
  assert.equal(f.sessions.get('upload-0').status, 'aborted');
  assert.equal(f.sessions.get('upload-1').status, 'completed');
});

test('rate limits wait and retry; authentication loss does not create a new session', async t => {
  let limited = true, revoked = false;
  const f = fixture(t, () => {
    if (revoked) return new Response('{"message":"login expired"}', { status: 401 });
    if (limited) { limited = false; return new Response('{}', { status: 429, headers: { 'Retry-After': '1' } }); }
  });
  const file = new File(['abcd'], 'file.txt');
  await uploadFile(file, f.options);
  assert.ok(f.notices.some(message => message.includes('自动重试')));
  revoked = true;
  const before = f.requests.length;
  await assert.rejects(uploadFile(file, f.options), /login expired/);
  assert.deepEqual(f.requests.slice(before).map(r => r.method), ['GET']);
});

test('pause interrupts retry waiting and never retries a permission error', async t => {
  const controller = new AbortController();
  const pending = waitForRetry(60000, controller.signal);
  controller.abort();
  await assert.rejects(pending, { name: 'AbortError' });
  const f = fixture(t, () => new Response('{}', { status: 403 }));
  await assert.rejects(uploadRequest('/versions/v/uploads', { method: 'POST', body: '{}' },
    new AbortController().signal, () => {}));
  assert.equal(f.requests.length, 1);
});

test('retry waiting publishes countdown without issuing more requests and can be paused',async()=>{
  const controller=new AbortController(),ticks=[];
  const pending=waitForRetry(60000,controller.signal,seconds=>ticks.push(seconds));
  assert.equal(ticks[0],60);controller.abort();
  await assert.rejects(pending,{name:'AbortError'});
});

for (const status of ['finalizing', 'aborting']) {
  test(`resume cleans ${status} before allocating another upload`, async t => {
    let interrupted = true;
    const f = fixture(t, ({path}) => path.endsWith('/complete') && interrupted
      ? new Response('{"message":"interrupted"}', {status: 403}) : undefined);
    const file = new File(['abcd'], 'file.txt');
    await assert.rejects(uploadFile(file, f.options));
    const previous = f.sessions.get('upload-0');
    previous.status = status;
    if (status === 'finalizing') previous.expires_at = '2000-01-01T00:00:00';
    interrupted = false;
    const before = f.requests.length;
    await uploadFile(file, f.options);
    assert.deepEqual(f.requests.slice(before, before + 3).map(row => row.method), ['GET', 'DELETE', 'POST']);
    assert.equal(previous.status, 'aborted');
    assert.equal(f.sessions.get('upload-1').status, 'completed');
  });
}
