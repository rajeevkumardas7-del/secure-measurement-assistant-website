const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// A pending PDF must never be saved into the next signed-in account.
async function checkSessionChange(rejectRequest) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {files: [], value: '', textContent: '',
      addEventListener() {}, replaceChildren() {}});
    return nodes.get(id);
  };
  node('pdf-file').files = [{name: 'test.pdf', size: 100}];
  node('reader-mode').value = 'custom_ai';
  let finish, fail, requests = 0, saved = 0;
  const pending = new Promise((resolve, reject) => {finish = resolve; fail = reject;});
  const context = vm.createContext({document: {getElementById: node},
    api: 'https://example.test', token: 'first-account', pdfReading: false,
    AbortSignal, window: {secureApp: {canUse: () => true, setProgress() {},
      persistEstimate() {saved++;}}}, fetch: () => {requests++; return pending;}});
  vm.runInContext(fs.readFileSync(__dirname + '/pdf.js', 'utf8'), context);
  const reading = node('read-pdf').onclick();
  await node('read-pdf').onclick();
  assert.equal(requests, 1, 'Repeated clicks must not send duplicate reads');
  context.token = 'second-account';
  node('pdf-status').textContent = 'Second account';
  if (rejectRequest) fail(new Error('Old request failed'));
  else finish({text: async () => JSON.stringify({items: [], notice: ''}), ok: true});
  await reading;
  assert.equal(saved, 0);
  assert.equal(node('pdf-status').textContent, 'Second account');
  assert.equal(context.pdfReading, false);
}
(async () => {await checkSessionChange(false); await checkSessionChange(true);
  console.log('PDF session isolation and duplicate-request checks passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
