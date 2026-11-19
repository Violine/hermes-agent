const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(require.resolve("../dashboard/index.js"), "utf8");
const configEnvelope = { csrf_token: "csrf", config: { projects: { crm: { repo: "/srv/crm" } }, roles: {}, quota: { pools: {} }, workflow: { auto_start_after_judge_accept: false } }, catalog: { providers: [], roles: {} } };

function boot({ url = "http://localhost/engineering-team", seed = {}, fetchJSON } = {}) {
  let RegisteredPlugin;
  const state = new Map();
  seed = Object.assign({ 1: configEnvelope, 2: configEnvelope.config }, seed);
  const refs = new Map();
  let stateIndex = 0;
  let refIndex = 0;
  const React = { createElement(type, props, ...children) { return { type, props: Object.assign({}, props || {}, { children }) }; }, Fragment: Symbol("Fragment") };
  const hooks = {
    useState(initial) {
      const index = stateIndex++;
      if (!state.has(index)) state.set(index, Object.prototype.hasOwnProperty.call(seed, index) ? seed[index] : typeof initial === "function" ? initial() : initial);
      return [state.get(index), (value) => state.set(index, typeof value === "function" ? value(state.get(index)) : value)];
    },
    useEffect() {}, useCallback(fn) { return fn; },
    useRef(current) { const index = refIndex++; if (!refs.has(index)) refs.set(index, { current }); return refs.get(index); },
  };
  const window = {
    __HERMES_PLUGIN_SDK__: { React, hooks, fetchJSON: fetchJSON || (async () => ({})) },
    __HERMES_PLUGINS__: { register(name, component) { assert.equal(name, "engineering-team"); RegisteredPlugin = component; } },
    location: { href: url }, history: { state: null, replaceState() {} }, setInterval, clearInterval,
    localStorage: { setItem() {} }, confirm: () => true, crypto: { randomUUID: (() => { let id = 0; return () => "uuid-" + (++id); })() },
  };
  vm.runInNewContext(source, { window, URL, Date, Math, JSON, Number, String, Object, Array, RegExp, Promise, Error, console, btoa: (value) => Buffer.from(value, "binary").toString("base64") });
  function render() { stateIndex = 0; refIndex = 0; return RegisteredPlugin(); }
  return { render, state, window };
}
function find(node, predicate, found = []) {
  if (!node || typeof node !== "object") return found;
  if (Array.isArray(node)) { node.forEach((child) => find(child, predicate, found)); return found; }
  if (predicate(node)) found.push(node);
  const children = node.props && node.props.children;
  if (Array.isArray(children)) children.forEach((child) => find(child, predicate, found)); else find(children, predicate, found);
  return found;
}
function textOf(node) {
  if (node == null || typeof node === "boolean") return "";
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (typeof node !== "object") return String(node);
  return textOf(node.props && node.props.children);
}
function file(name, bytes = "doc", relative = "") {
  return { name, size: Buffer.byteLength(bytes), type: "text/plain", webkitRelativePath: relative, async arrayBuffer() { return Buffer.from(bytes); } };
}
const tick = async () => { await Promise.resolve(); await Promise.resolve(); await new Promise((resolve) => setImmediate(resolve)); };

(async () => {
  const pendingUploads = [];
  const calls = [];
  const app = boot({ fetchJSON(path, init) {
    calls.push({ path, init });
    if (path.endsWith("/attachments") && init && init.method === "POST") return new Promise((resolve, reject) => pendingUploads.push({ resolve, reject }));
    if (path === "/api/plugins/engineering-team/requests" && init && init.method === "POST") return Promise.resolve({ request: { id: "created-1", title: "Task", stage: "analysis" } });
    return Promise.resolve({});
  } });
  let tree = app.render();
  const project = find(tree, (node) => node.type && node.type.name === "Select")[0];
  project.props.onChange({ target: { value: "crm" } });
  tree = app.render();
  find(tree, (node) => node.type && node.type.name === "Input" && node.props.placeholder && node.props.placeholder.includes("Поиск клиентов"))[0].props.onChange({ target: { value: "Avito intake" } });
  tree = app.render();
  find(tree, (node) => node.type === "textarea" && node.props.className.includes("et-request-brief"))[0].props.onChange({ target: { value: "Analyze source docs" } });
  tree = app.render();
  const picker = find(tree, (node) => node.type && node.type.name === "AttachmentPicker")[0];
  picker.props.onFiles([file("good.md", "hello", "docs/good.md"), file("bad.exe", "no", "docs/bad.exe")]);
  await tick();
  tree = app.render();
  assert.match(String(find(tree, (node) => node.props && node.props.role === "status")[0].props.children[0].props.children), /docs\/bad\.exe/,
    "mixed directory selection keeps a visible rejection naming the skipped file");
  assert.equal(pendingUploads.length, 1, "only supported file is uploaded from the mixed selection");
  const form = find(tree, (node) => node.type === "form")[0];
  await form.props.onSubmit({ preventDefault() {} });
  assert.equal(calls.filter((call) => call.path === "/api/plugins/engineering-team/requests" && call.init.method === "POST").length, 0,
    "create does not submit while document upload is pending");
  pendingUploads[0].resolve({ id: "attachment-1", name: "docs/good.md", size: 5, text_status: "extracted" });
  await tick();
  tree = app.render();
  assert.match(String(find(tree, (node) => node.props && node.props.role === "status")[0].props.children[0].props.children), /docs\/bad\.exe/,
    "upload completion does not erase skipped-file warning");
  const createForm = find(tree, (node) => node.type === "form")[0];
  await createForm.props.onSubmit({ preventDefault() {} });
  const createCall = calls.find((call) => call.path === "/api/plugins/engineering-team/requests" && call.init.method === "POST");
  assert.ok(createCall, "request submits after all accepted files finish uploading");
  assert.deepEqual(JSON.parse(createCall.init.body).attachments, ["attachment-1"], "new request references uploaded attachment IDs");

  const late = [];
  const second = boot({ url: "http://localhost/engineering-team?request=feature-a", seed: {
    14: [{ id: "feature-a", title: "A", stage: "analysis" }, { id: "feature-b", title: "B", stage: "analysis" }],
    19: { id: "feature-a", title: "A", stage: "analysis", attachments: [] },
  }, fetchJSON(path, init) {
    if (path.endsWith("/attachments") && init && init.method === "POST") return new Promise((resolve) => late.push(resolve));
    return Promise.resolve({});
  } });
  tree = second.render();
  let existingPicker = find(tree, (node) => node.type && node.type.name === "AttachmentPicker")[0];
  existingPicker.props.onFiles([file("late.txt")]);
  await tick();
  tree = second.render();
  find(tree, (node) => node.type === "button" && textOf(node) === "К списку задач")[0].props.onClick();
  tree = second.render();
  const rowB = find(tree, (node) => node.type === "button" && node.props.className === "et-request-row" && node.props["aria-label"] === "Открыть B")[0];
  rowB.props.onClick();
  second.state.set(19, { id: "feature-b", title: "B", stage: "analysis", attachments: [] }); // Simulate the detail fetch completing for B.
  tree = second.render();
  late[0]({ id: "late-attachment", name: "late.txt", size: 3 });
  await tick();
  tree = second.render();
  existingPicker = find(tree, (node) => node.type && node.type.name === "AttachmentPicker")[0];
  assert.equal(existingPicker.props.items.length, 0, "a late upload from feature A cannot appear in feature B after navigation");

  const retryCalls = [];
  let attachmentAttempt = 0;
  const retryApp = boot({ url: "http://localhost/engineering-team?request=feature-r", seed: {
    19: { id: "feature-r", title: "Retry", stage: "analysis", attachments: [] },
  }, fetchJSON(path, init) {
    retryCalls.push({ path, init });
    if (path === "/api/plugins/engineering-team/attachments" && init && init.method === "POST") return Promise.resolve({ id: "retry-att", name: "retry.txt", size: 3 });
    if (path.endsWith("/requests/feature-r/attachments") && init && init.method === "POST") {
      attachmentAttempt++;
      return attachmentAttempt === 1 ? Promise.reject(new Error("offline")) : Promise.resolve({ request: { id: "feature-r", stage: "analysis", attachments: [{ id: "retry-att", name: "retry.txt", size: 3 }] } });
    }
    return Promise.resolve({});
  } });
  tree = retryApp.render();
  existingPicker = find(tree, (node) => node.type && node.type.name === "AttachmentPicker")[0];
  existingPicker.props.onFiles([file("retry.txt")]);
  await tick();
  tree = retryApp.render();
  let addButton = find(tree, (node) => node.type === "button" && textOf(node) === "Добавить к задаче")[0];
  await addButton.props.onClick();
  tree = retryApp.render();
  addButton = find(tree, (node) => node.type === "button" && textOf(node) === "Добавить к задаче")[0];
  await addButton.props.onClick();
  const attachRequests = retryCalls.filter((call) => call.path.endsWith("/requests/feature-r/attachments"));
  assert.equal(attachRequests.length, 2, "failed attach can be retried");
  assert.equal(JSON.parse(attachRequests[0].init.body).request_id, JSON.parse(attachRequests[1].init.body).request_id,
    "retry reuses the idempotency key for the same attachment set");
  console.log("Dashboard attachment workflow behavioral tests passed.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
