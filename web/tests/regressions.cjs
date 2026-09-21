// Offline harness: transpile the real TS modules, mock only browser/network boundaries.
const ts = require('typescript');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const assert = require('node:assert/strict');
const test = require('node:test');
const root = path.resolve(__dirname, '..');
const resolve = Module._resolveFilename;
Module._resolveFilename = function(id, ...args) {
  return resolve.call(this, id.startsWith('@/') ? path.join(root, id.slice(2)) : id, ...args);
};
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (mod, filename) => {
  mod._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  }).outputText, filename);
};
process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID = 'fixture-client';
const tick = () => new Promise(resolve => setImmediate(resolve));
const auth = require('../lib/google-auth.ts');
const gmail = require('../lib/gmail.ts');
const filter = require('../lib/filter.ts');
const llm = require('../lib/llm.ts');
const cache = require('../lib/cache.ts');
let configs = [], requests = 0, syncError = false;
global.window = { google: { accounts: { oauth2: {
  initTokenClient(config) { configs.push(config); return { requestAccessToken() { requests++; if (syncError) throw Error('blocked'); } }; },
  revoke() {},
} } } };
global.fetch = async () => { throw Error('Unexpected network request'); };

// Minimal asynchronous IndexedDB boundary supporting the production cache API.
const records = new Map([['results', [{ id:'legacy-secret' }]]]);
global.indexedDB = { open() {
  const req = {};
  queueMicrotask(() => {
    req.result = { close() {}, transaction() {
      const tx = { objectStore() { return {
        get(key) { const r = {}; queueMicrotask(() => { r.result = records.get(key); r.onsuccess(); }); return r; },
        put(value,key) { records.set(key, structuredClone(value)); queueMicrotask(() => tx.oncomplete()); },
        delete(key) { records.delete(key); queueMicrotask(() => tx.oncomplete()); },
      }; } }; return tx;
    } }; req.onsuccess();
  }); return req;
} };
const email = id => ({ id, subject:'Internship', from:'careers@acme.com', date:'today', body:'Software Intern Summer 2027 Montreal Acme' });

test('OAuth workers share requests, settle errors, retry, and ignore callbacks after sign-out', async () => {
  const a = auth.getAccessToken(), b = auth.getAccessToken();
  assert.equal(a,b);
  await tick(); assert.equal(requests,1);
  configs.at(-1).callback({ access_token:'a', expires_in:3600 });
  assert.deepEqual(await Promise.all([a,b]), ['a','a']);
  auth.signOut();
  const failed = [auth.getAccessToken(), auth.getAccessToken()];
  const failures = Promise.allSettled(failed);
  await tick(); configs.at(-1).error_callback({ message:'popup closed' });
  assert.ok((await failures).every(x => x.status === 'rejected'));
  syncError = true;
  await assert.rejects(auth.getAccessToken(), /blocked/);
  syncError = false;
  const old = auth.getAccessToken(); const oldSettled = assert.rejects(old,/Signed out/);
  await tick(); const stale = configs.at(-1);
  auth.signOut(); await oldSettled;
  stale.callback({ access_token:'stale' }); assert.equal(auth.hasValidToken(),false);
  const retry = auth.getAccessToken(); await tick();
  stale.callback({ access_token:'stale' }); assert.equal(auth.hasValidToken(),false);
  configs.at(-1).callback({ access_token:'b', expires_in:3600 });
  assert.equal(await retry,'b');
});

test('analysis failures remain distinct from valid empty and mixed successes', async () => {
  const original = global.fetch;
  try {
    for (const bad of ['broken','{}','{"results":[{}]}','{"results":[42]}', new Error('timeout')]) {
      const values = [bad,bad,'{"results":[]}','{"results":[]}'];
      global.fetch = async url => {
        if (url.endsWith('/api/tags')) return { ok:true, json:async()=>({models:[{name:'qwen3.5:9b'}]}) };
        const value = values.shift(); if (value instanceof Error) throw value;
        return { ok:true, json:async()=>({response:value}) };
      };
      const outcome = await llm.analyze([email('a'),email('b')], {backend:'ollama'});
      assert.deepEqual(outcome, {results:[],successfulIds:['b'],failedIds:['a']});
    }
    global.fetch = async url => url.endsWith('/api/tags')
      ? {ok:true,json:async()=>({models:[{name:'qwen3.5:9b'}]})}
      : Promise.reject(Error('timeout'));
    assert.deepEqual((await llm.analyze([email('a')],{backend:'ollama'})).failedIds,['a']);
  } finally { global.fetch = original; }
});

test('Glassdoor retains the only qualifying first listing and does not mix following jobs', () => {
  const body = 'Job alert: Software Engineer Your job listings for September 21, 2026 Software Engineer Montreal, QC Software Intern Summer 2027 Acme Montreal, QC 4.2 ★ Finance Manager Bank Toronto, ON 3.8 ★ See more jobs';
  const fixture = {...email('glass'), from:'alerts@glassdoor.com', subject:'Acme is hiring', body};
  assert.equal(filter.ruleBasedAnalyze([fixture]).length,1);
  fixture.body = body.replace('Software Intern Summer 2027 Acme Montreal, QC','Software Engineer Acme Toronto, ON').replace('Finance Manager Bank Toronto, ON','Finance Intern Summer 2027 Bank Montreal, QC');
  assert.equal(filter.ruleBasedAnalyze([fixture]).length,0);
});

test('keyword query grouping applies cutoff and unread constraints to every OR branch', () => {
  const q = gmail.buildQueries('Acme OR Beta',7,true)[0];
  assert.match(q,/^is:unread \(Acme OR Beta\) \(intern OR/);
  const d = new Date(Date.now()-7*86400000);
  assert.ok(q.endsWith(`after:${d.getFullYear()}/${String(d.getMonth()+1).padStart(2,'0')}/${String(d.getDate()).padStart(2,'0')}`));
  assert.ok(!gmail.buildQueries('Acme',7,false)[0].includes('is:unread'));
});

test('account caches ignore legacy data and preserve failed rescan decisions while allowing retry', async () => {
  assert.deepEqual(await cache.loadCache('a@example.com'),{scan:null,results:null});
  await cache.saveResults('a@example.com',[{id:'private-a'}]);
  await cache.addSeen('a@example.com',['private-a']);
  await cache.removeSeen('a@example.com',['private-a']);
  assert.deepEqual(await cache.loadSeen('a@example.com'),[]);
  assert.deepEqual(await cache.loadResults('a@example.com'),[{id:'private-a'}]);
  assert.deepEqual(await cache.loadCache('b@example.com'),{scan:null,results:null});
});

// Render the actual dashboard using a tiny hook host; user event handlers and
// async scan/cache orchestration remain production code.
test('dashboard A → sign-out → B ignores late scan completion and clears displayed mailbox data', async () => {
  const reactPath = require.resolve('react');
  const realReact = require('react');
  const originalReact = require.cache[reactPath];
  const slots = []; let cursor=0;
  const fakeReact = { ...realReact,
    useState(initial) { const i=cursor++; if (!(i in slots)) slots[i]=initial; return [slots[i],v=>{slots[i]=typeof v==='function'?v(slots[i]):v;}]; },
    useRef(initial) { const i=cursor++; if (!(i in slots)) slots[i]={current:initial}; return slots[i]; },
    useCallback(fn) { return fn; }, useMemo(fn) { return fn(); }, useEffect() {},
  };
  require.cache[reactPath] = {id:reactPath,filename:reactPath,loaded:true,exports:fakeReact};
  const saved = { signIn:auth.signIn, signOut:auth.signOut, profile:gmail.getProfileEmail, search:gmail.runGmailSearch, analyze:llm.analyze };
  let account='a@example.com', finish;
  auth.signIn = async()=> 'token'; auth.signOut=()=>{};
  gmail.getProfileEmail=async()=>account;
  gmail.runGmailSearch=async()=>[email('late-a')];
  llm.analyze=()=>new Promise(resolve=>{finish=resolve;});
  const Dashboard = require('../app/page.tsx').default;
  const render = () => {cursor=0; return Dashboard();};
  function nodes(node) {
    if (!node || typeof node!=='object') return [];
    const children = [node.props?.children, node.props?.headerRight].flat(Infinity);
    return [node,...children.flatMap(nodes)];
  }
  function button(label) {
    return nodes(render()).find(n=>n.type==='button' && JSON.stringify(n.props.children).includes(label));
  }
  try {
    await button('Sign in').props.onClick();
    assert.ok(JSON.stringify(render()).includes('private-a'));
    // A failed rescan retains its old result and removes its completed marker.
    await cache.addSeen(account,['private-a']);
    nodes(render()).find(n=>n.props?.label==='Re-analyze all (ignore cache)').props.onChange(true);
    gmail.runGmailSearch=async()=>[email('private-a')];
    llm.analyze=async()=>({results:[],successfulIds:[],failedIds:['private-a']});
    await button('Run scan').props.onClick();
    assert.deepEqual(await cache.loadResults(account),[{id:'private-a'}]);
    assert.deepEqual(await cache.loadSeen(account),[]);
    // The following successful negative replaces the old keep result.
    llm.analyze=async()=>({results:[],successfulIds:['private-a'],failedIds:[]});
    await button('Run scan').props.onClick();
    assert.deepEqual(await cache.loadResults(account),[]);
    assert.deepEqual(await cache.loadSeen(account),['private-a']);
    await cache.saveResults(account,[{id:'private-a'}]);
    gmail.runGmailSearch=async()=>[email('late-a')];
    llm.analyze=()=>new Promise(resolve=>{finish=resolve;});
    const scanning=button('Run scan').props.onClick(); await tick();
    assert.equal(typeof finish,'function');
    button('Sign out').props.onClick();
    assert.ok(!JSON.stringify(render()).includes('private-a'));
    account='b@example.com'; await button('Sign in').props.onClick();
    finish({results:[{id:'late-a',summary:'secret A'}],successfulIds:['late-a'],failedIds:[]});
    await scanning;
    assert.ok(!JSON.stringify(render()).includes('secret A'));
    assert.deepEqual(await cache.loadCache('b@example.com'),{scan:null,results:null});
    assert.deepEqual(await cache.loadResults('a@example.com'),[{id:'private-a'}]);
  } finally {
    Object.assign(auth,{signIn:saved.signIn,signOut:saved.signOut});
    Object.assign(gmail,{getProfileEmail:saved.profile,runGmailSearch:saved.search});
    llm.analyze=saved.analyze;
    require.cache[reactPath]=originalReact;
  }
});
