const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const projectConfig = {
  crm: { repo: "/srv/crm" },
  billing: { repo: "/srv/billing" },
};
const configEnvelope = { csrf_token: "test-csrf", revision: 1, config: { projects: projectConfig, roles: {}, quota: { pools: {} }, workflow: { auto_start_after_judge_accept: false } }, catalog: { providers: [{ id: "anthropic", label: "Anthropic" }], roles: {} } };
let RegisteredPlugin;
let stateIndex = 0;
let selectedRequestForRender = null;
let ownerDraftState = {};
const apiCalls = [];
let forceAnswerConflict = false;

const React = {
  createElement(type, props, ...children) {
    return { type, props: Object.assign({}, props || {}, { children }) };
  },
};
const hooks = {
  useState(initial) {
    const index = stateIndex++;
    const value = index === 26 ? ownerDraftState : index === 1 ? configEnvelope : index === 2 ? configEnvelope.config
      : index === 19 && selectedRequestForRender ? selectedRequestForRender
        : typeof initial === "function" ? initial() : initial;
    return [value, (next) => { if (index === 26) ownerDraftState = typeof next === "function" ? next(ownerDraftState) : next; }];
  },
  useEffect() {},
  useCallback(fn) { return fn; },
  useRef(current) { return { current }; },
};
const context = {
  window: {
    __HERMES_PLUGIN_SDK__: { React, hooks, fetchJSON: async (path, init) => {
      apiCalls.push({ path, init });
      if (forceAnswerConflict && /\/answers$/.test(path)) throw Object.assign(new Error("409 Conflict"), { status: 409 });
      return selectedRequestForRender || {};
    } },
    __HERMES_PLUGINS__: { register(name, component) { assert.equal(name, "engineering-team"); RegisteredPlugin = component; } },
    location: { href: "http://localhost/engineering-team" },
    history: { state: null, lastUrl: "", replaceState(_state, _title, url) { this.lastUrl = url; } },
    setInterval,
    clearInterval,
    localStorage: { setItem() {} },
    confirm: () => true,
    crypto: { randomUUID: () => "test-request-id" },
  },
  URL,
  Date,
  Math,
  JSON,
  Number,
  String,
  Object,
  Array,
  RegExp,
  Promise,
  Error,
  console,
};

vm.runInNewContext(fs.readFileSync(require.resolve("../dashboard/index.js"), "utf8"), context);
assert.equal(typeof RegisteredPlugin, "function", "plugin registers a renderable component");
const tree = RegisteredPlugin();

function find(node, predicate, found = []) {
  if (!node || typeof node !== "object") return found;
  if (Array.isArray(node)) { node.forEach((child) => find(child, predicate, found)); return found; }
  if (predicate(node)) found.push(node);
  const children = node.props && node.props.children;
  if (Array.isArray(children)) children.forEach((child) => find(child, predicate, found));
  else find(children, predicate, found);
  return found;
}

const projectSelect = find(tree, (node) => node.type && node.type.name === "Select")[0];
assert.ok(projectSelect, "new task form renders the project selector");
assert.deepEqual(Array.from(projectSelect.props.options, (option) => option.value), ["", "crm", "billing"]);
assert.equal(projectSelect.props.value, "", "empty project selection remains required until selected");
assert.ok(find(tree, (node) => node.type === "button" && node.props.children.includes("Создать задачу")).length,
  "new task submit action is present");
const newTaskPicker = find(tree, (node) => node.type && node.type.name === "AttachmentPicker")[0];
assert.ok(newTaskPicker, "new task form renders a document picker");
assert.equal(newTaskPicker.props.directory, true, "document picker supports selecting a whole folder");
assert.equal(newTaskPicker.props.label, "Документы к задаче");
assert.ok(find(tree, (node) => node.type === "button" && node.props.children.includes("Задачи")).length,
  "tasks are the default entry point");

selectedRequestForRender = {
  id: "child-1", title: "Доработка фичи", brief: "Поправить проверку", stage: "awaiting_owner_input",
  approval: { id: "gate-1", kind: "requirements", revision: 3, documents: {
    "SPEC.md": "SPEC_SENTINEL_LONG_TEXT", "QUESTIONS.json": "QUESTIONS_SENTINEL_RAW_JSON", "ATTACHMENTS.json": "ATTACHMENTS_SENTINEL_RAW_JSON",
  }, questions: [
    { id: "manager", question: "Кому передать новый лид?", example: "Например, лид из Астаны поступил вечером. Кто должен первым увидеть его утром?",
      options: [
        { id: "any", label: "Любому свободному менеджеру", description: "Система назначит первого доступного." },
        { id: "region", label: "Менеджеру своего региона", description: "Региональный менеджер будет отвечать за контакт." },
      ], recommended_option_id: "region", recommendation_reason: "Так проще сохранить ответственность за клиента.", answer: null },
    { id: "contact", question: "Когда создавать задачу на первый звонок?", example: "Клиент оставил заявку после рабочего дня.",
      options: [{ id: "next-day", label: "На следующий рабочий день", description: "Менеджер видит понятный срок." }], answer: { option_id: "next-day", actor: "owner", channel: "dashboard", created_at: 1761500000 } },
  ] },
  tasks: [
    { id: "task-analyst", role: "analyst", title: "Уточнить требования", status: "completed", model: "claude-opus-5-5", provider: "anthropic", reasoning_effort: "xhigh", quota_pool: "anthropic-main", assignment_state: "selected", assignment_source: "card", selection_reason: "Сложная бизнес-логика" },
    { id: "task-review", role: "reviewer", title: "Проверить изменения", status: "queued", assignment_state: "unknown" },
  ],
  role_plan: [
    { role: "backend", title: "Разработать API", purpose: "Реализовать серверную часть", selection_reason: "В плане есть изменения API", selection_source: "plan", model: "claude-opus-5-5", provider: "anthropic", reasoning_effort: "high", quota_pool: "anthropic-main", assignment_state: "proposed" },
    { role: "qa", title: "Проверить сценарии", selection_reason: "Покрыть критерии приёмки", assignment_state: "planned" },
  ],
  parent_feature: "parent-1", followups: [], brief: "LONG_BRIEF_SENTINEL: описание задачи на несколько экранов…",
  verification_progress: { staging_sha: "abc123", regression_status: "in_progress",
    total_task_count: 3, completed_task_count: 1, provisional_task_count: 0 },
  approval_history: [{ gate_revision: 2, kind: "delivery", decision: "revise", channel: "telegram",
    actor: "owner", comment: "Добавить проверку пустого значения", created_at: 1761500000 }],
  attachments: [{ id: "att-1", name: "specs/REQUIREMENTS.md", size: 3072, text_status: "extracted" }],
};
context.window.location.href = "http://localhost/engineering-team?request=child-1";
stateIndex = 0;
const detailTree = RegisteredPlugin();
const textOf = (node) => {
  if (node == null || typeof node === "boolean") return "";
  if (Array.isArray(node)) return node.map(textOf).join(" ");
  if (typeof node !== "object") return String(node);
  return textOf(node.props && node.props.children);
};
const detailText = textOf(detailTree);
const verificationNode = find(detailTree, (node) => node.type && node.type.name === "VerificationProgress")[0];
assert.ok(verificationNode, "request detail renders the verification-progress component");
const verificationText = textOf(verificationNode.type(verificationNode.props));
assert.match(detailText, /История решений/, "request detail renders approval history");
assert.match(detailText, /Возвращено с замечаниями/, "history decisions use Russian labels");
assert.match(detailText, /Версия 2/, "history includes gate revision");
assert.match(detailText, /Канал: Telegram/, "history includes translated channel");
assert.match(detailText, /Добавить проверку пустого значения/, "history includes owner comment");
assert.match(detailText, /Роли и модели задачи/, "detail has a dedicated role and model panel");
assert.match(detailText, /Назначения по задаче/, "actual assignments are shown separately");
assert.match(detailText, /Anthropic/, "provider name is shown from provider catalog");
assert.match(detailText, /claude-opus-5-5/, "actual model is shown");
assert.match(detailText, /Очень высокая/, "actual reasoning effort is shown");
assert.match(detailText, /Сложная бизнес-логика/, "actual selection reason is shown");
assert.match(detailText, /Не зафиксировано/, "unknown actual model stays explicit and is not inferred from settings");
assert.match(detailText, /Планируемые роли · прогноз/, "planned roles have a separate forecast section");
assert.match(detailText, /Это ещё не запущенные исполнители/, "forecast is clearly marked as not actual execution");
assert.match(detailText, /Модель или пул могут измениться при допуске к квоте/, "forecast warns quota admission can change assignment");
assert.match(detailText, /Разработать API/, "planned developer role is visible before approval");
assert.match(detailText, /Исходные документы/, "source documents are shown separately in request detail");
assert.match(detailText, /specs\/REQUIREMENTS\.md/, "source attachment path is displayed as text");
assert.match(detailText, /текст доступен/, "attachment text availability is presented");
const sourceDownload = find(detailTree, (node) => node.type === "a" && node.props.href === "/api/plugins/engineering-team/requests/child-1/attachments/att-1")[0];
assert.ok(sourceDownload, "source attachment has an authenticated same-origin download link");
const existingPicker = find(detailTree, (node) => node.type && node.type.name === "AttachmentPicker")[0];
assert.ok(existingPicker, "eligible request detail lets the owner add source files");
assert.equal(existingPicker.props.directory, true, "existing request supports folder selection too");
assert.match(detailText, /Ждёт вашего ответа/, "awaiting owner input has a Russian stage label");
assert.match(verificationText, /Последняя полная регрессия: 1 из 3 задач · текущие изменения ещё проверяются/,
  "a previous green batch is scoped to accepted tasks and does not imply the whole feature is complete");
assert.doesNotMatch(verificationText, /Полная регрессия пройдена/,
  "the dashboard does not announce feature-level completion while tasks remain active");
assert.match(detailText, /Кому передать новый лид\?/, "structured owner question is shown in plain language");
assert.match(detailText, /лид из Астаны поступил вечером/, "question includes an everyday business example");
assert.match(detailText, /Рекомендация команды/, "recommended option is explicitly labelled");
assert.match(detailText, /Так проще сохранить ответственность за клиента/, "recommendation includes a reason");
assert.match(detailText, /Ответ отправлен/, "previous answers are visibly acknowledged");
assert.match(detailText, /Предложить свой вариант/, "owner can provide a custom answer");
assert.doesNotMatch(detailText, /QUESTIONS_SENTINEL_RAW_JSON|ATTACHMENTS_SENTINEL_RAW_JSON/, "internal question and attachment manifests are hidden from the owner materials view");
const requestDetail = find(detailTree, (node) => node.type === "div" && node.props.className === "et-request-detail")[0];
const directChildren = Array.isArray(requestDetail.props.children) && requestDetail.props.children.length === 1 && Array.isArray(requestDetail.props.children[0]) ? requestDetail.props.children[0] : requestDetail.props.children;
const priorityCardIndex = directChildren.findIndex((node) => node && node.props && node.props.className === "et-panel et-approval-card et-approval-card--priority");
const briefIndex = directChildren.findIndex((node) => node && node.props && node.props.className === "et-panel et-request-original et-request-original--collapsed");
const sourceFilesIndex = directChildren.findIndex((node) => node && node.props && node.props.className === "et-panel et-source-attachments");
assert.ok(priorityCardIndex >= 0 && priorityCardIndex < briefIndex && priorityCardIndex < sourceFilesIndex,
  "owner questions appear before the long brief and source attachment list");
const briefDisclosure = find(directChildren[briefIndex], (node) => node.type === "details")[0];
assert.ok(briefDisclosure && !briefDisclosure.props.open, "long original brief is collapsed while questions are pending");
const documentDisclosure = find(directChildren[priorityCardIndex], (node) => node.type === "details" && node.props.className === "et-request-document-bundle")[0];
assert.ok(documentDisclosure && !documentDisclosure.props.open, "raw team documents are collapsed while questions are pending");
assert.equal(find(detailTree, (node) => node.type === "button" && textOf(node).includes("Согласовать требования")).length, 0,
  "answering questions does not silently offer or trigger requirement approval");
const regionChoice = find(detailTree, (node) => node.type === "button" && textOf(node).includes("Менеджеру своего региона"))[0];
assert.ok(regionChoice, "each answer choice is a real button");
assert.equal(typeof regionChoice.props.onClick, "function", "choice is connected to the answer handler");
const customAnswer = find(detailTree, (node) => node.type === "textarea" && node.props.placeholder === "Напишите, как лучше поступить…")[0];
assert.ok(customAnswer, "custom-answer field is rendered for the unanswered question");
customAnswer.props.onChange({ target: { value: "Назначать по региону" } });
assert.equal(ownerDraftState["child-1:gate-1:manager"], "Назначать по региону", "draft key includes request, gate and question ids");
const originalRequest = selectedRequestForRender;
selectedRequestForRender = Object.assign({}, originalRequest, { id: "another-task", approval: Object.assign({}, originalRequest.approval, { id: "gate-2" }) });
stateIndex = 0;
const otherRequestTree = RegisteredPlugin();
const otherRequestAnswer = find(otherRequestTree, (node) => node.type === "textarea" && node.props.placeholder === "Напишите, как лучше поступить…")[0];
assert.equal(otherRequestAnswer.props.value, "", "a draft from another request and gate is never shown for this question");
selectedRequestForRender = originalRequest;
const parentLink = find(detailTree, (node) => node.type === "button"
  && textOf(node).includes("Исходная задача · parent-1"))[0];
assert.ok(parentLink, "linked parent task is available in request detail");

(async () => {
  await regionChoice.props.onClick();
  const answerCall = apiCalls.find((call) => /\/requests\/child-1\/answers$/.test(call.path));
  assert.ok(answerCall, "answer button sends to the scoped request answer route");
  assert.equal(answerCall.init.method, "POST", "answer is submitted as a POST");
  assert.equal(answerCall.init.headers["X-Team-CSRF"], "test-csrf", "answer request includes CSRF protection");
  const answerPayload = JSON.parse(answerCall.init.body);
  assert.equal(answerPayload.gate_id, "gate-1", "answer is bound to the displayed approval gate");
  assert.equal(answerPayload.question_id, "manager", "answer is bound to the selected question");
  assert.equal(answerPayload.option_id, "region", "button submits its option id");
  assert.equal(answerPayload.request_id, "test-request-id", "answer mutation carries an idempotency key");

  const callsBeforeConflict = apiCalls.length;
  forceAnswerConflict = true;
  await regionChoice.props.onClick();
  assert.ok(apiCalls.slice(callsBeforeConflict).some((call) => /\/requests\/child-1$/.test(call.path)), "a stale 409 refreshes the request before retrying");
  parentLink.props.onClick();
  assert.match(context.window.history.lastUrl, /request=parent-1/, "related task navigation uses selected request URL");
  console.log("Dashboard workflow UI smoke test passed.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
