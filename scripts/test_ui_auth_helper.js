// Exercise the dynamically injected browser fetch/token helper without a real browser.
const vm = require('node:vm');
let html = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { html += chunk; });
process.stdin.on('end', async () => {
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(match => match[1]);
  const source = scripts.find(script => script.includes('m5_api_token') && script.includes('window.fetch=async'));
  if (!source) throw new Error('Injected token helper script not found.');

  const storage = new Map();
  const calls = [];
  let prompts = 0;
  const fakeWindow = {
    prompt(message) {
      prompts++;
      if (!message.includes('Studio API token')) throw new Error('Unexpected prompt text.');
      return 'unit-test-token-not-a-real-credential';
    },
    async fetch(input, init = {}) {
      const headers = new Headers(input instanceof Request ? input.headers : undefined);
      new Headers(init.headers || {}).forEach((value, key) => headers.set(key, value));
      calls.push(headers.get('Authorization'));
      return new Response('', { status: headers.get('Authorization') ? 200 : 401 });
    },
  };
  const context = {
    window: fakeWindow,
    location: { origin: 'http://127.0.0.1:3000', href: 'http://127.0.0.1:3000/' },
    sessionStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value) },
    Headers, Request, Response, URL,
  };
  vm.runInNewContext(source, context);

  let response = await fakeWindow.fetch('/api/providers');
  if (response.status !== 200 || prompts !== 1 || storage.get('m5_api_token') !== 'unit-test-token-not-a-real-credential') {
    throw new Error('Same-origin 401 did not prompt, cache for session, and retry successfully.');
  }
  response = await fakeWindow.fetch('/api/providers');
  if (response.status !== 200 || prompts !== 1) throw new Error('Session token was not reused.');
  response = await fakeWindow.fetch('https://other.example/api/providers');
  if (response.status !== 401 || prompts !== 1) throw new Error('Cross-origin 401 unexpectedly triggered a token prompt.');
  if (calls[0] !== null || calls[1] !== 'Bearer unit-test-token-not-a-real-credential' || calls[2] !== 'Bearer unit-test-token-not-a-real-credential' || calls[3] !== null) {
    throw new Error(`Authorization header behavior unexpected: ${JSON.stringify(calls)}`);
  }
  console.log('Injected UI auth helper passed: same-origin prompt/retry, session reuse, and no cross-origin prompt.');
});
