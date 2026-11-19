(function () {
  "use strict";

  const sdk = window.__HERMES_PLUGIN_SDK__;
  const registry = window.__HERMES_PLUGINS__;
  if (!sdk || !registry) return;
  const React = sdk.React;
  const h = React.createElement;
  const { useState, useEffect, useCallback, useRef } = sdk.hooks;
  const API = "/api/plugins/engineering-team";

  const copy = (value) => JSON.parse(JSON.stringify(value));
  const titleCase = (value) => String(value || "").replace(/[_-]+/g, " ");
  const effortLabel = (value) => ({ none: "Без рассуждений", minimal: "Минимальная", low: "Низкая", medium: "Средняя", high: "Высокая", xhigh: "Очень высокая", max: "Максимальная", ultra: "Предельная" })[value] || titleCase(value);
  const when = (value) => {
    if (!value) return "—";
    const date = new Date(typeof value === "number" && value < 1000000000000 ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
  };
  const errorText = (error) => String(error && error.message ? error.message : error).replace(/^\d+:\s*/, "");
  const get = (path, init) => sdk.fetchJSON(API + path, init);

  function Badge(props) {
    return h("span", { className: "et-badge et-badge--" + (props.tone || "muted") }, props.children);
  }
  function Field(props) {
    return h("label", { className: "et-field" },
      h("span", { className: "et-field__label" }, props.label),
      props.children,
      props.help ? h("small", { className: "et-help" }, props.help) : null);
  }
  function Input(props) {
    return h("input", Object.assign({ className: "et-input" }, props));
  }
  function Select(props) {
    const { options, ...attributes } = props;
    return h("select", Object.assign({ className: "et-input et-select" }, attributes),
      (props.options || []).map((item) => typeof item === "string"
        ? h("option", { key: item, value: item }, item)
        : h("option", { key: item.value, value: item.value }, item.label)));
  }
  function Empty(props) {
    return h("div", { className: "et-empty" }, h("strong", null, props.title), h("span", null, props.text || "Пока нет данных."));
  }
  function IntegrationCheckProgress(props) {
    const check = props.value;
    if (!check || !Number.isFinite(check.index) || !Number.isFinite(check.total)) return null;
    const seconds = Math.max(0, Math.floor(Number(check.elapsed_seconds) || 0));
    const duration = seconds >= 60 ? Math.floor(seconds / 60) + " мин " + (seconds % 60) + " с" : seconds + " с";
    const state = check.event === "completed"
      ? ({ pass: "пройдена", fail: "ошибка", timeout: "превышено время", error: "не удалось выполнить" })[check.result] || "завершена"
      : "выполняется";
    return h("p", { className: "et-note", role: "status" },
      "Регрессионная проверка " + check.index + " из " + check.total + ": " + state + " · " + duration);
  }

  function VerificationProgress(props) {
    const value = props.value;
    if (!value || !value.staging_sha) return null;
    const total = Number.isFinite(value.total_task_count) ? value.total_task_count : null;
    const completed = Number.isFinite(value.completed_task_count) ? value.completed_task_count : null;
    const taskCount = total != null && completed != null ? " · " + completed + " из " + total + " задач приняты" : "";
    const labels = {
      running: "Полная регрессия выполняется",
      failed: "Регрессия нашла ошибки — команда исправляет пакет",
      pending: "Ожидает полной регрессии",
      in_progress: "Последняя полная регрессия: " + (completed != null && total != null ? completed + " из " + total + " задач" : "результат сохранён") + " · текущие изменения ещё проверяются",
      passed: "Полная регрессия пройдена",
      unknown: "Результат полной регрессии ещё не записан"
    };
    return h("div", null,
      h("p", { className: value.regression_status === "failed" ? "et-warning-text" : "et-note", role: "status" },
        labels[value.regression_status] || labels.unknown,
        value.regression_status !== "in_progress" && value.regression_status !== "passed" ? taskCount : "",
        value.provisional_task_count ? " · задач с быстрыми проверками: " + value.provisional_task_count : "",
        value.batch_repair_active ? " · исправление пакета в работе" : ""),
      h(IntegrationCheckProgress, { value: value.check }));
  }

  function EfficiencyPanel(props) {
    const [report, setReport] = useState(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const duration = (seconds) => seconds == null ? "нет данных" : Math.round(seconds / 60) + " мин";
    const codeLabel = (code) => code && code.added != null ? "+" + code.added + " / −" + code.deleted + " строк · " + code.files + " файлов" : "нет данных";
    return h("details", { className: "et-context-diagnostics" },
      h("summary", null, "Время работы и объём кода"),
      h("button", { type: "button", className: "et-button et-button--quiet", disabled: busy, onClick: async () => {
        setBusy(true); setError("");
        try { setReport(await get("/requests/" + encodeURIComponent(props.requestId) + "/efficiency")); }
        catch (e) { setError(errorText(e)); }
        finally { setBusy(false); }
      } }, busy ? "Считаю…" : report ? "Обновить расчёт" : "Рассчитать"),
      error ? h("p", { className: "et-error-inline" }, error) : null,
      report ? h("div", null,
        h("p", null, "С начала задачи: " + duration(report.elapsed_seconds) + ". " +
          (report.workers && report.workers.coverage && report.workers.coverage.complete === false
            ? "По найденным карточкам: " : "Агенты работали: ") +
          duration(report.workers && report.workers.wall_seconds_union) + "; сумма запусков: " + duration(report.workers && report.workers.wall_seconds_sum) + "."),
        report.workers && report.workers.coverage && report.workers.coverage.complete === false
          ? h("p", { className: "et-help" }, report.workers.coverage.truncated
            ? "Охват ограничен первыми 200 карточками доски; время показано по найденным карточкам и может быть неполным."
            : "Охват частичный: учтены только найденные карточки и их запуски.") : null,
        h("p", null, ((report.code && report.code.accepted_or_integrated && report.code.accepted_or_integrated.status === "provisional") ? "Интегрировано, проверка ещё не подтверждена: " : "После полной регрессии: ") + codeLabel(report.code && report.code.accepted_or_integrated)),
        h("p", null, "Накоплено в рабочей ветке: " + codeLabel(report.code && report.code.staging)),
        h("p", { className: "et-help" }, "Строки включают тесты и документацию. Время запуска включает ожидание инструментов; оно не показывает расход подписки."),
        h("a", { href: API + "/requests/" + encodeURIComponent(props.requestId) + "/efficiency", target: "_blank", rel: "noreferrer" }, "Подробный отчёт JSON")) : null);
  }

  function ExecutionPolicyEditor(props) {
    const policy = props.value;
    const change = (fn) => { const next = copy(policy); fn(next); props.onChange(next); };
    return h("details", { className: "et-context-diagnostics" }, h("summary", null, "Быстрые проверки и пакетная регрессия"),
      h("label", { className: "et-toggle" }, h("input", { type: "checkbox", checked: !!policy, onChange: (e) => props.onChange(e.target.checked ? { version: 1, full_after_tasks: 2, path_rules: [{ name: "Основной код", prefixes: ["src"], quick_check_indexes: [0] }] } : null) }),
        h("span", null, "Запускать полную регрессию пакетом")),
      h("p", { className: "et-help" }, "Непокрытые правилами пути требуют всех проверок. Финальная приёмка доступна только после полной регрессии. Изменения применяются к новым запросам."),
      policy ? h("div", null,
        h(Field, { label: "Полная регрессия после задач", help: "Также запускается перед финальной приёмкой и при завершении доступной группы зависимостей." }, h(Input, { type: "number", min: 1, max: 20, value: policy.full_after_tasks, onChange: (e) => change((p) => { p.full_after_tasks = Number(e.target.value); }) })),
        (policy.path_rules || []).map((rule, index) => h("div", { className: "et-panel", key: index },
          h(Field, { label: "Имя правила" }, h(Input, { value: rule.name, onChange: (e) => change((p) => { p.path_rules[index].name = e.target.value; }) })),
          h(Field, { label: "Каталоги или файлы через запятую", help: "Например: backend/, db/. Пути относительно репозитория." }, h(Input, { value: rule.prefixes.join(", "), onChange: (e) => change((p) => { p.path_rules[index].prefixes = e.target.value.split(",").map((v) => v.trim()); }) })),
          h("p", { className: "et-help" }, "Быстрые проверки этого правила:"),
          (props.checks || []).map((argv, checkIndex) => h("label", { className: "et-toggle", key: checkIndex }, h("input", { type: "checkbox", checked: rule.quick_check_indexes.includes(checkIndex), onChange: (e) => change((p) => {
            p.path_rules[index].quick_check_indexes = e.target.checked ? rule.quick_check_indexes.concat(checkIndex) : rule.quick_check_indexes.filter((v) => v !== checkIndex);
          }) }), h("span", null, (checkIndex + 1) + ". " + argv.join(" ")))),
          h("button", { type: "button", className: "et-button et-button--quiet", onClick: () => change((p) => { p.path_rules.splice(index, 1); }) }, "Удалить правило"))),
        h("button", { type: "button", className: "et-button et-button--quiet", onClick: () => change((p) => { p.path_rules.push({ name: "Правило " + (p.path_rules.length + 1), prefixes: ["src"], quick_check_indexes: [0] }); }) }, "+ Правило")) : null);
  }

  function TeamPlugin() {
    const initialRequestId = initialRequestFromUrl();
    const [tab, setTab] = useState(() => initialTabFromUrl(initialRequestId));
    const [envelope, setEnvelope] = useState(null);
    const [draft, setDraft] = useState(null);
    const [status, setStatus] = useState(null);
    const [configLoading, setConfigLoading] = useState(true);
    const [statusLoading, setStatusLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [dirty, setDirty] = useState(false);
    const [error, setError] = useState("");
    const [notice, setNotice] = useState("");
    const [filter, setFilter] = useState("all");
    const [projectChecks, setProjectChecks] = useState({});
    const [projectContexts, setProjectContexts] = useState({});
    const [expandedRoles, setExpandedRoles] = useState({});
    const [requests, setRequests] = useState([]);
    const [requestsLoading, setRequestsLoading] = useState(false);
    const [requestError, setRequestError] = useState("");
    const [attachmentWarning, setAttachmentWarning] = useState("");
    const [selectedRequestId, setSelectedRequestId] = useState(initialRequestId);
    const [selectedRequest, setSelectedRequest] = useState(null);
    const [requestDraft, setRequestDraft] = useState({ project: "", title: "", brief: "" });
    const [requestAttachments, setRequestAttachments] = useState([]);
    const [existingAttachments, setExistingAttachments] = useState([]);
    const [attachmentBusy, setAttachmentBusy] = useState(false);
    const [requestSubmitting, setRequestSubmitting] = useState(false);
    const [requestComment, setRequestComment] = useState("");
    const [ownerQuestionDrafts, setOwnerQuestionDrafts] = useState({});
    const [ownerAnswerBusy, setOwnerAnswerBusy] = useState(false);
    const [requestDecisionBusy, setRequestDecisionBusy] = useState(false);
    const [requestCommentBusy, setRequestCommentBusy] = useState(false);
    const requestSubmitLock = useRef(false);
    const requestMutationLock = useRef(false);
    const pendingRequestIds = useRef({});
    const selectedRequestIdRef = useRef(initialRequestId);
    const requestDetailSequence = useRef(0);
    const attachmentMutationLock = useRef(false);
    const existingAttachmentOwnerRef = useRef(initialRequestId);

    const loadConfig = useCallback(async function () {
      setConfigLoading(true);
      try {
        const result = await get("/config");
        setEnvelope(result);
        setDraft(copy(result.config || {}));
        setDirty(false);
        setProjectChecks({});
        setProjectContexts({});
        setError("");
        setNotice("");
      } catch (e) { setError("Не удалось загрузить настройки: " + errorText(e)); }
      finally { setConfigLoading(false); }
    }, []);
    const reloadConfig = () => {
      if (dirty && !window.confirm("Перезагрузить настройки и отбросить несохранённые изменения?")) return;
      loadConfig();
    };

    const loadStatus = useCallback(async function (quiet) {
      if (!quiet) setStatusLoading(true);
      try {
        const result = await get("/status");
        setStatus(result);
        setError((old) => old.startsWith("Не удалось загрузить состояние команды:") ? "" : old);
      } catch (e) {
        setError((old) => old || ("Не удалось загрузить состояние команды: " + errorText(e)));
      } finally { if (!quiet) setStatusLoading(false); }
    }, []);

    useEffect(() => { loadConfig(); loadStatus(false); }, [loadConfig, loadStatus]);
    useEffect(() => {
      const timer = window.setInterval(() => loadStatus(true), 10000);
      return () => window.clearInterval(timer);
    }, [loadStatus]);

    const update = (mutate) => {
      setDraft((current) => {
        if (!current) return current;
        const next = copy(current);
        mutate(next);
        return next;
      });
      setDirty(true);
      setNotice("");
    };
    const roles = draft && draft.roles || {};
    const pools = draft && draft.quota && draft.quota.pools || {};
    const projects = draft && draft.projects || {};
    const catalog = envelope && envelope.catalog || {};
    const roleCatalog = catalog.roles || {};
    const providerOptions = (catalog.providers || []).map((p) => ({ value: p.id, label: p.label || p.id }));
    const effortOptions = (catalog.efforts || ["low", "medium", "high"]).map((x) => ({ value: x, label: effortLabel(x) }));

    const save = async () => {
      if (!draft || !envelope || saving) return;
      const configToSave = copy(draft);
      const invalidChecks = {};
      Object.entries(projectChecks).forEach(([key, value]) => {
        if (key.endsWith("!error")) return;
        if (!configToSave.projects[key] || typeof value !== "string") return;
        try {
          const parsed = JSON.parse(value);
          if (!Array.isArray(parsed) || parsed.some((command) => !Array.isArray(command) || command.some((arg) => typeof arg !== "string"))) throw new Error("Ожидается массив массивов строк.");
          configToSave.projects[key].checks = parsed;
        } catch (e) { invalidChecks[key + "!error"] = e.message || "Некорректный JSON"; }
      });
      if (Object.keys(invalidChecks).length) {
        setProjectChecks((current) => Object.assign({}, current, invalidChecks));
        setError("Сначала исправьте JSON команд регрессии: настройки не отправлены.");
        return;
      }
      const candidateProblem = Object.entries(configToSave.roles || {}).flatMap(([roleId, role]) => (role.candidates || []).map((candidate, index) => {
        const pool = configToSave.quota && configToSave.quota.pools && configToSave.quota.pools[candidate.pool];
        if (!candidate.model || !String(candidate.model).trim()) return roleId + ": заполните модель кандидата " + (index + 1) + ".";
        if (!pool) return roleId + ": выберите настроенный пул для кандидата " + (index + 1) + ".";
        if (candidate.provider && pool.provider !== candidate.provider) return roleId + ": провайдер и пул кандидата " + (index + 1) + " не совпадают.";
        return null;
      })).find(Boolean);
      if (candidateProblem) { setError("Проверьте список выбора моделей: " + candidateProblem); return; }
      Object.values(configToSave.roles || {}).forEach((role) => {
        if (role.pool === "") delete role.pool;
        if (Array.isArray(role.candidates)) {
          role.candidates.forEach((candidate) => {
            if (candidate.pool && configToSave.quota && configToSave.quota.pools && configToSave.quota.pools[candidate.pool]) candidate.provider = configToSave.quota.pools[candidate.pool].provider;
          });
          if (!role.candidates.length) delete role.candidates;
        }
      });
      setSaving(true); setError(""); setNotice("");
      try {
        const result = await get("/config", {
          method: "PUT",
          headers: { "Content-Type": "application/json", "X-Team-CSRF": envelope.csrf_token || "" },
          body: JSON.stringify({ revision: envelope.revision, config: configToSave }),
        });
        setEnvelope(result);
        setDraft(copy(result.config || draft));
        setDirty(false);
        setProjectChecks({});
        setProjectContexts({});
        setNotice(result.message || "Настройки сохранены. Новые назначения применятся к следующим задачам.");
      } catch (e) {
        const message = errorText(e);
        if (/409|conflict|revision/i.test(message)) setError("Настройки изменились на сервере. Ваши правки сохранены в форме; нажмите «Перезагрузить», чтобы увидеть актуальную версию.");
        else setError("Не удалось сохранить настройки: " + message);
      } finally { setSaving(false); }
    };

    const nav = ["Задачи", "Обзор", "Роли и модели", "Подписки", "Проекты"];
    const taskList = status && Array.isArray(status.tasks) ? status.tasks : [];
    const visibleTasks = taskList.filter((task) => filter === "all" || (filter === "active" ? ["in_progress", "running", "active"].includes(task.status) : ["blocked", "waiting", "waiting_tests", "waiting_quota", "waiting_for_quota", "todo", "ready", "planned"].includes(task.status)));
    const features = status && Array.isArray(status.features) ? status.features : [];
      const activeFeatures = features.filter((feature) => !["done", "completed", "cancelled", "canceled", "merged"].includes(String(feature.status || feature.stage || "").toLowerCase()));
    const quotaPools = status && status.quota && status.quota.pools || {};

    const loadRequests = useCallback(async function (quiet) {
      if (!quiet) setRequestsLoading(true);
      try {
        const result = await get("/requests");
        setRequests(Array.isArray(result.requests) ? result.requests : []);
        setRequestError("");
      } catch (e) {
        if (!quiet) setRequestError("Не удалось загрузить задачи: " + errorText(e));
      } finally { if (!quiet) setRequestsLoading(false); }
    }, []);
    useEffect(() => { loadRequests(false); }, [loadRequests]);
    useEffect(() => {
      const timer = window.setInterval(() => loadRequests(true), 10000);
      return () => window.clearInterval(timer);
    }, [loadRequests]);
    useEffect(() => {
      selectedRequestIdRef.current = selectedRequestId;
      if (!selectedRequestId) { requestDetailSequence.current += 1; setSelectedRequest(null); return; }
      let alive = true;
      const refresh = (quiet) => {
        const sequence = ++requestDetailSequence.current;
        get("/requests/" + encodeURIComponent(selectedRequestId)).then((result) => {
          if (alive && sequence === requestDetailSequence.current && selectedRequestIdRef.current === selectedRequestId) setSelectedRequest(result.request || result);
        }).catch((e) => { if (alive && !quiet && selectedRequestIdRef.current === selectedRequestId) setRequestError("Не удалось открыть задачу: " + errorText(e)); });
      };
      refresh(false);
      const timer = window.setInterval(() => refresh(true), 10000);
      return () => { alive = false; window.clearInterval(timer); };
    }, [selectedRequestId]);

    async function submitRequest(event) {
      if (event && event.preventDefault) event.preventDefault();
      if (requestSubmitLock.current || !envelope || !requestDraft.project || !requestDraft.title.trim() || !requestDraft.brief.trim()) return;
      if (requestAttachments.some((entry) => entry.status !== "uploaded" || !entry.id)) {
        setRequestError("Сначала дождитесь загрузки всех файлов или удалите файлы с ошибкой."); return;
      }
      requestSubmitLock.current = true; setRequestSubmitting(true); setRequestError("");
      const payload = { project: requestDraft.project, title: requestDraft.title.trim(), brief: requestDraft.brief.trim(), attachments: requestAttachments.map((entry) => entry.id) };
      const requestId = getPendingRequestId("create", payload);
      try {
        const result = await get("/requests", {
          method: "POST", headers: { "Content-Type": "application/json", "X-Team-CSRF": envelope.csrf_token || "" },
          body: JSON.stringify(Object.assign({}, payload, { request_id: requestId })),
        });
        clearPendingRequestId("create", requestId);
        const record = result.request || result;
        const id = record.id || record.request_id || requestId;
        setRequestDraft((current) => ({ project: current.project, title: "", brief: "" }));
        setRequestAttachments([]);
        setAttachmentWarning("");
        setRequestError(""); selectedRequestIdRef.current = id; requestDetailSequence.current += 1; updateRequestUrl(id);
        setSelectedRequestId(id); setSelectedRequest(record); setRequestComment("");
        await loadRequests(true);
      } catch (e) {
        const csrf = await refreshCsrfAfter403(e);
        if (csrf !== null) setRequestError(csrf ? "Сессия панели обновлена. Нажмите «Создать задачу» ещё раз; форма сохранена." : "Сессия истекла. Не удалось обновить защитный токен, попробуйте обновить панель.");
        else setRequestError("Не удалось отправить задачу: " + errorText(e));
      } finally { requestSubmitLock.current = false; setRequestSubmitting(false); }
    }
    function attachmentSet(target, updater) {
      const setter = target === "existing" ? setExistingAttachments : setRequestAttachments;
      setter((current) => target === "existing" && existingAttachmentOwnerRef.current !== selectedRequestIdRef.current
        ? current : typeof updater === "function" ? updater(current) : updater);
    }
    async function uploadAttachment(file, target, localId, featureId) {
      const update = (change) => attachmentSet(target, (items) => items.map((entry) => entry.localId === localId ? Object.assign({}, entry, change) : entry));
      if (target === "existing" && featureId !== selectedRequestIdRef.current) return;
      update({ status: "uploading", error: "" });
      try {
        const contentBase64 = await fileToBase64(file);
        const result = await get("/attachments", {
          method: "POST", headers: { "Content-Type": "application/json", "X-Team-CSRF": envelope && envelope.csrf_token || "" },
          body: JSON.stringify({ name: file.webkitRelativePath || file.name, content_base64: contentBase64, request_id: "attachment-" + localId }),
        });
        if (target !== "existing" || featureId === selectedRequestIdRef.current) update({ status: "uploaded", id: result.id, name: result.name || file.webkitRelativePath || file.name, size: result.size || file.size, text_status: result.text_status || "unknown", media_type: result.media_type || file.type });
      } catch (e) {
        const csrf = await refreshCsrfAfter403(e);
        if (target !== "existing" || featureId === selectedRequestIdRef.current) update({ status: "error", error: csrf ? "Сессия обновлена, нажмите «Повторить»." : errorText(e) });
      }
    }
    function addAttachmentFiles(fileList, target) {
      const allFiles = Array.from(fileList || []);
      const files = allFiles.filter((file) => !/(?:^|\/)(?:\.DS_Store|Thumbs\.db|desktop\.ini|\.Trashes|\.Spotlight-V100)$/.test(file.webkitRelativePath || file.name)
        && !/(?:^|\/)\._[^/]+$/.test(file.webkitRelativePath || file.name));
      if (files.length !== allFiles.length) setAttachmentWarning("Системные файлы macOS/Windows пропущены; остальные файлы проверяются как обычно.");
      if (!files.length || attachmentBusy || attachmentMutationLock.current) return;
      if (target === "existing" && requestMutationLock.current) { setRequestError("Сначала дождитесь завершения текущего действия."); return; }
      const current = target === "existing" ? existingAttachments : requestAttachments;
      const attached = target === "existing" && selectedRequest ? selectedRequest.attachments || [] : [];
      const currentBytes = current.concat(attached).reduce((sum, item) => sum + (item.file && item.file.size || item.size || 0), 0);
      if (current.length + attached.length + files.length > 50) { setRequestError("Можно прикрепить не более 50 файлов к одной задаче."); return; }
      if (currentBytes + files.reduce((sum, file) => sum + file.size, 0) > 30 * 1024 * 1024) { setRequestError("Общий размер вложений не должен превышать 30 МиБ."); return; }
      const allowed = /\.(md|txt|pdf|docx|html?|json|ya?ml|csv|png|jpe?g|webp)$/i;
      const valid = files.filter((file) => allowed.test(file.name) && file.size <= 5 * 1024 * 1024);
      const invalid = files.filter((file) => !allowed.test(file.name) || file.size > 5 * 1024 * 1024);
      const featureId = target === "existing" ? selectedRequestIdRef.current : "";
      if (invalid.length) setAttachmentWarning((files.length !== allFiles.length ? "Системные файлы macOS/Windows пропущены. " : "") + "Не добавлены файлы: " + invalid.map((file) => file.webkitRelativePath || file.name).join(", ") + ". Поддерживаются MD, TXT, PDF, DOCX, HTML, JSON, YAML, CSV, PNG, JPG и WEBP размером до 5 МиБ каждый.");
      if (!valid.length) return;
      if (!invalid.length && files.length === allFiles.length) setAttachmentWarning("");
      if (!invalid.length) setRequestError("");
      const entries = valid.map((file) => ({ localId: newAttachmentId(), name: file.webkitRelativePath || file.name, file, size: file.size, status: "queued", requestId: featureId }));
      if (target === "existing") existingAttachmentOwnerRef.current = featureId;
      attachmentSet(target, (items) => items.concat(entries));
      entries.reduce((chain, entry) => chain.then(() => uploadAttachment(entry.file, target, entry.localId, featureId)), Promise.resolve());
    }
    async function attachUploadedToRequest() {
      if (attachmentMutationLock.current || requestMutationLock.current || !envelope || !selectedRequestId || !existingAttachments.length || existingAttachments.some((entry) => entry.status !== "uploaded" || !entry.id)) return;
      attachmentMutationLock.current = true; setAttachmentBusy(true); setRequestError("");
      const targetId = selectedRequestId;
      const payload = { attachment_ids: existingAttachments.map((entry) => entry.id) };
      const scope = "attach:" + targetId;
      const requestId = getPendingRequestId(scope, payload);
      const sequence = ++requestDetailSequence.current;
      try {
        const result = await get("/requests/" + encodeURIComponent(targetId) + "/attachments", {
          method: "POST", headers: { "Content-Type": "application/json", "X-Team-CSRF": envelope.csrf_token || "" },
          body: JSON.stringify(Object.assign({}, payload, { request_id: requestId })),
        });
        clearPendingRequestId(scope, requestId);
        if (selectedRequestIdRef.current === targetId && sequence === requestDetailSequence.current) {
          setExistingAttachments([]);
          if (result.request || result.id) setSelectedRequest(result.request || result);
          else await refreshRequestDetail(targetId);
          await loadRequests(true);
        }
      } catch (e) {
        if (selectedRequestIdRef.current === targetId && sequence === requestDetailSequence.current) {
          if (await refreshCsrfAfter403(e) !== null) setRequestError("Сессия панели обновлена. Файлы загружены; нажмите «Добавить к задаче» ещё раз.");
          else setRequestError("Не удалось добавить файлы к задаче: " + errorText(e));
        }
      }
      finally { attachmentMutationLock.current = false; setAttachmentBusy(false); }
    }
    async function decideRequest(decision) {
      const approval = selectedRequest && selectedRequest.approval;
      if (!approval || requestMutationLock.current || !envelope || !selectedRequestId) return;
      if (existingAttachments.length || attachmentBusy) { setRequestError("Сначала добавьте загруженные файлы к задаче или уберите их из списка, затем принимайте решение."); return; }
      if (decision === "revise" && !requestComment.trim()) { setRequestError("Добавьте замечание, чтобы вернуть этап на доработку."); return; }
      requestMutationLock.current = true; setRequestDecisionBusy(true); setRequestError(""); requestDetailSequence.current += 1;
      const payload = { gate_id: approval.id, decision, comment: requestComment.trim() };
      const requestId = getPendingRequestId("decision:" + selectedRequestId, payload, approval.revision);
      try {
        const result = await get("/requests/" + encodeURIComponent(selectedRequestId) + "/decision", {
          method: "POST", headers: { "Content-Type": "application/json", "X-Team-CSRF": envelope.csrf_token || "" },
          body: JSON.stringify(Object.assign({}, payload, { request_id: requestId })),
        });
        clearPendingRequestId("decision:" + selectedRequestId, requestId);
        if (selectedRequestIdRef.current === selectedRequestId) {
          if (result.request || result.id) setSelectedRequest(result.request || result);
          else await refreshRequestDetail(selectedRequestId);
          setRequestComment("");
        }
        await loadRequests(true);
      } catch (e) {
        const stale = isStaleRequestError(e);
        if (selectedRequestIdRef.current === selectedRequestId && stale) {
          setRequestError("Этап уже обновился. Показываю актуальную версию; текст вашего замечания сохранён.");
          await refreshRequestDetail(selectedRequestId);
        } else if (await refreshCsrfAfter403(e) !== null) {
          if (selectedRequestIdRef.current === selectedRequestId) setRequestError("Сессия панели обновлена. Ваш текст сохранён; повторите действие, чтобы завершить решение.");
        } else if (selectedRequestIdRef.current === selectedRequestId) setRequestError("Не удалось сохранить решение: " + errorText(e));
      } finally { requestMutationLock.current = false; setRequestDecisionBusy(false); }
    }
    async function addRequestComment() {
      if (!requestComment.trim() || requestMutationLock.current || !envelope || !selectedRequestId) return;
      requestMutationLock.current = true; setRequestCommentBusy(true); setRequestError(""); requestDetailSequence.current += 1;
      const payload = { comment: requestComment.trim() };
      const scope = "comment:" + selectedRequestId;
      const requestId = getPendingRequestId(scope, payload, selectedRequest && selectedRequest.revision);
      try {
        const result = await get("/requests/" + encodeURIComponent(selectedRequestId) + "/comments", {
          method: "POST", headers: { "Content-Type": "application/json", "X-Team-CSRF": envelope.csrf_token || "" },
          body: JSON.stringify(Object.assign({}, payload, { request_id: requestId })),
        });
        clearPendingRequestId(scope, requestId);
        if (selectedRequestIdRef.current === selectedRequestId) {
          if (result.request || result.id) setSelectedRequest(result.request || result);
          else await refreshRequestDetail(selectedRequestId);
          setRequestComment("");
        }
      } catch (e) {
        if (selectedRequestIdRef.current === selectedRequestId && isStaleRequestError(e)) {
          setRequestError("История обновилась. Показываю свежую версию; ваш текст сохранён."); await refreshRequestDetail(selectedRequestId);
        } else if (await refreshCsrfAfter403(e) !== null) {
          if (selectedRequestIdRef.current === selectedRequestId) setRequestError("Сессия панели обновлена. Ваш комментарий сохранён; нажмите «Отправить» ещё раз.");
        } else if (selectedRequestIdRef.current === selectedRequestId) setRequestError("Не удалось добавить комментарий: " + errorText(e));
      } finally { requestMutationLock.current = false; setRequestCommentBusy(false); }
    }
    function ownerQuestionDraftKey(requestId, gateId, questionId) {
      return [requestId, gateId, questionId].map((part) => encodeURIComponent(String(part || ""))).join(":");
    }
    function updateOwnerQuestionDraft(requestId, gateId, questionId, value) {
      const key = ownerQuestionDraftKey(requestId, gateId, questionId);
      setOwnerQuestionDrafts((current) => Object.assign({}, current, { [key]: value }));
    }
    async function answerOwnerQuestion(questionId, answer) {
      const request = selectedRequest;
      const approval = request && request.approval;
      if (!request || !approval || !envelope || !selectedRequestId || requestMutationLock.current || ownerAnswerBusy) return;
      if (existingAttachments.length || attachmentBusy) { setRequestError("Сначала добавьте загруженные файлы к задаче или уберите их из списка."); return; }
      const question = (approval.questions || []).find((entry) => entry.id === questionId);
      if (!question || question.answer) return;
      const payload = Object.assign({ gate_id: approval.id, question_id: questionId }, answer);
      requestMutationLock.current = true; setOwnerAnswerBusy(true); setRequestError(""); requestDetailSequence.current += 1;
      const scope = "answer:" + selectedRequestId + ":" + questionId;
      const requestId = getPendingRequestId(scope, payload, approval.revision);
      try {
        const result = await get("/requests/" + encodeURIComponent(selectedRequestId) + "/answers", {
          method: "POST", headers: { "Content-Type": "application/json", "X-Team-CSRF": envelope.csrf_token || "" },
          body: JSON.stringify(Object.assign({}, payload, { request_id: requestId })),
        });
        clearPendingRequestId(scope, requestId);
        if (selectedRequestIdRef.current === selectedRequestId) {
          if (result.request || result.id) setSelectedRequest(result.request || result);
          else await refreshRequestDetail(selectedRequestId);
          const draftKey = ownerQuestionDraftKey(selectedRequestId, approval.id, questionId);
          setOwnerQuestionDrafts((current) => Object.assign({}, current, { [draftKey]: "" }));
        }
        await loadRequests(true);
      } catch (e) {
        if (selectedRequestIdRef.current === selectedRequestId && isStaleRequestError(e)) {
          setRequestError("Вопрос уже обновился. Показываю актуальную версию; проверьте, нужен ли ещё ваш ответ.");
          await refreshRequestDetail(selectedRequestId);
        } else if (await refreshCsrfAfter403(e) !== null) {
          if (selectedRequestIdRef.current === selectedRequestId) setRequestError("Сессия панели обновлена. Ваш ответ сохранён; нажмите ещё раз.");
        } else if (selectedRequestIdRef.current === selectedRequestId) setRequestError("Не удалось сохранить ответ: " + errorText(e));
      } finally { requestMutationLock.current = false; setOwnerAnswerBusy(false); }
    }
    async function refreshRequestDetail(id) {
      const sequence = ++requestDetailSequence.current;
      if (selectedRequestIdRef.current !== id) return;
      try {
        const result = await get("/requests/" + encodeURIComponent(id));
        if (sequence === requestDetailSequence.current && selectedRequestIdRef.current === id) setSelectedRequest(result.request || result);
      }
      catch (e) { if (sequence === requestDetailSequence.current && selectedRequestIdRef.current === id) setRequestError("Не удалось обновить задачу: " + errorText(e)); }
    }
    function getPendingRequestId(scope, payload, revision) {
      const fingerprint = JSON.stringify({ payload, revision: revision == null ? null : revision });
      const current = pendingRequestIds.current[scope];
      if (current && current.fingerprint === fingerprint) return current.id;
      const id = newRequestId();
      pendingRequestIds.current[scope] = { fingerprint, id };
      return id;
    }
    function clearPendingRequestId(scope, id) {
      if (pendingRequestIds.current[scope] && pendingRequestIds.current[scope].id === id) delete pendingRequestIds.current[scope];
    }
    async function refreshCsrfAfter403(error) {
      if (!hasHttpStatus(error, 403)) return null;
      try {
        const fresh = await get("/config");
        if (fresh && fresh.csrf_token) setEnvelope((current) => current ? Object.assign({}, current, { csrf_token: fresh.csrf_token }) : fresh);
        return !!(fresh && fresh.csrf_token);
      } catch (_) { return false; }
    }

    function RequestsTab() {
      const projectIds = Object.keys(projects);
      const awaiting = requests.filter((item) => item.approval);
      const active = requests.filter((item) => !item.approval && !isRequestFinished(item));
      return h("div", { className: "et-stack et-requests" },
        h("div", { className: "et-page-title" }, h("div", null, h("h1", null, "Задачи проекта"), h("p", null, "Создавайте запросы, обсуждайте требования и принимайте ключевые этапы. Статусы обновляются автоматически.")), selectedRequestId ? h("button", { className: "et-button", type: "button", onClick: () => { setRequestComment(""); setExistingAttachments([]); existingAttachmentOwnerRef.current = ""; setSelectedRequestId(""); setSelectedRequest(null); selectedRequestIdRef.current = ""; requestDetailSequence.current += 1; updateRequestUrl(""); } }, "К списку задач") : null),
        requestError ? h("div", { className: "et-alert et-alert--error", role: "alert" }, h("span", null, requestError), h("button", { type: "button", onClick: () => selectedRequestId ? refreshRequestDetail(selectedRequestId) : loadRequests(false) }, "Обновить")) : null,
        attachmentWarning ? h("div", { className: "et-alert et-alert--warning", role: "status" }, h("span", null, attachmentWarning), h("button", { type: "button", onClick: () => setAttachmentWarning("") }, "Понятно")) : null,
        !selectedRequestId ? h("div", { className: "et-grid et-grid--requests" },
          h("section", { className: "et-panel" },
            h("div", { className: "et-panel__head" }, h("div", null, h("h2", null, "Новая задача"), h("p", null,
              envelope && envelope.config && envelope.config.workflow.auto_start_after_judge_accept
                ? "Опишите желаемый результат. Вы согласуете требования, затем команда сама проверит план и начнёт разработку. В конце вы примете результат."
                : "Опишите желаемый результат обычным языком. Команда подготовит требования и план на согласование."))),
            h("form", { className: "et-request-form", onSubmit: submitRequest },
              h(Field, { label: "Проект" }, h(Select, { value: requestDraft.project, required: true, disabled: requestSubmitting || !projectIds.length, options: [{ value: "", label: "Выберите проект" }].concat(projectIds.map((id) => ({ value: id, label: id + (projects[id].repo ? " · " + projects[id].repo : "") }))), onChange: (e) => setRequestDraft((v) => Object.assign({}, v, { project: e.target.value })) })),
              h(Field, { label: "Короткое название" }, h(Input, { value: requestDraft.title, required: true, maxLength: 160, disabled: requestSubmitting, placeholder: "Например: Поиск клиентов по телефону", onChange: (e) => setRequestDraft((v) => Object.assign({}, v, { title: e.target.value })) })),
              h(Field, { label: "Описание задачи", help: "Напишите проблему, ожидаемое поведение и примеры. Неизвестные решения можно отметить как открытые вопросы." }, h("textarea", { className: "et-input et-textarea et-request-brief", value: requestDraft.brief, required: true, disabled: requestSubmitting, placeholder: "Зачем это нужно? Что должно происходить? По каким примерам поймём, что задача готова?", onChange: (e) => setRequestDraft((v) => Object.assign({}, v, { brief: e.target.value })) })),
              h(AttachmentPicker, { items: requestAttachments, disabled: requestSubmitting || !envelope, onFiles: (files) => addAttachmentFiles(files, "new"), onRemove: (id) => setRequestAttachments((items) => items.filter((entry) => entry.localId !== id)), onRetry: (entry) => uploadAttachment(entry.file, "new", entry.localId), label: "Документы к задаче", directory: true }),
              h("div", { className: "et-request-gates" }, ["Согласование требований",
                envelope && envelope.config && envelope.config.workflow.auto_start_after_judge_accept
                  ? "План и разработка — команда самостоятельно" : "Утверждение плана до разработки",
                "Приёмка результата"].map((text, index) => h("div", { className: "et-request-gate", key: text }, h("span", null, String(index + 1)), h("span", null, text)))),
              projectIds.length ? h("button", { className: "et-button et-button--primary", type: "submit", disabled: requestSubmitting || !envelope || requestAttachments.some((entry) => entry.status !== "uploaded") }, requestSubmitting ? "Отправляю…" : requestAttachments.some((entry) => entry.status === "uploading" || entry.status === "queued") ? "Загружаю документы…" : "Создать задачу") : h(Empty, { title: "Нет настроенных проектов", text: "Сначала добавьте проект в разделе «Проекты»." }))),
          h("section", { className: "et-panel" },
            h("div", { className: "et-panel__head et-panel__head--wrap" }, h("div", null, h("h2", null, "Ваше участие"), h("p", null, "Задачи, которые ждут вашего решения.")), h(Badge, { tone: awaiting.length ? "warning" : "success" }, awaiting.length ? awaiting.length + " требуют решения" : "Решений не ожидают")),
            awaiting.length ? h("div", { className: "et-request-list" }, awaiting.map((item) => RequestRow({ key: item.id, item, onOpen: openRequest }))) : h(Empty, { title: "Нет ожидающих согласований", text: "Когда команде понадобится ваше решение, задача появится здесь." }),
            h("div", { className: "et-request-section-title" }, "В работе"),
            active.length ? h("div", { className: "et-request-list" }, active.slice(0, 8).map((item) => RequestRow({ key: item.id, item, onOpen: openRequest }))) : h(Empty, { title: requestsLoading ? "Обновляю список" : "Активных задач нет", text: "Создайте задачу слева, чтобы начать." }),
            h("div", { className: "et-request-section-title" }, "Завершённые"),
            requests.filter(isRequestFinished).length ? h("div", { className: "et-request-list" }, requests.filter(isRequestFinished).slice(0, 5).map((item) => RequestRow({ key: item.id, item, onOpen: openRequest }))) : h("p", { className: "et-help" }, "Завершённые задачи появятся здесь.")))
        : RequestDetail({ request: selectedRequest, loading: !selectedRequest, comment: requestComment, onComment: setRequestComment, onSendComment: addRequestComment, commentBusy: requestCommentBusy, busy: requestDecisionBusy || requestMutationLock.current, onDecision: decideRequest, onOpen: openRequest, attachments: existingAttachments, attachmentBusy, onAttachmentFiles: (files) => addAttachmentFiles(files, "existing"), onRemoveAttachment: (id) => setExistingAttachments((items) => items.filter((entry) => entry.localId !== id)), onRetryAttachment: (entry) => uploadAttachment(entry.file, "existing", entry.localId), onAttachFiles: attachUploadedToRequest, questionDrafts: ownerQuestionDrafts, onQuestionDraft: updateOwnerQuestionDraft, onAnswerQuestion: answerOwnerQuestion, answerBusy: ownerAnswerBusy }));

      function openRequest(id) { setRequestError(""); setRequestComment(""); setExistingAttachments([]); existingAttachmentOwnerRef.current = id; setSelectedRequest(null); selectedRequestIdRef.current = id; requestDetailSequence.current += 1; setSelectedRequestId(id); updateRequestUrl(id); }
    }
    function startRequest(projectId) {
      setRequestDraft((current) => ({ project: projectId || current.project || Object.keys(projects)[0] || "", title: "", brief: "" }));
      setRequestError(""); setRequestComment(""); setExistingAttachments([]); existingAttachmentOwnerRef.current = ""; setSelectedRequestId(""); setSelectedRequest(null); selectedRequestIdRef.current = ""; requestDetailSequence.current += 1; updateRequestUrl(""); setTab("Задачи");
    }

    function RequestRow(props) {
      const item = props.item || {};
      const approval = item.approval;
      const tone = approval ? "warning" : isRequestFinished(item) ? "success" : "info";
      const waiting = item.waiting_for || item.waiting_for_human;
      const actor = item.actor_label || item.actor || item.assignee;
      return h("button", { key: props.key || item.id, className: "et-request-row", type: "button", onClick: () => props.onOpen(item.id),
        "aria-label": "Открыть " + (item.title || "задачу") },
        h("span", { className: "et-request-row__top" }, h("strong", null, item.title || item.id || "Задача"), h(Badge, { tone }, approval ? approvalLabel(approval.kind) : item.stage_label || stageLabel(item.stage || item.status))),
        h("span", { className: "et-request-row__meta" }, item.project || item.project_id || "Проект не указан", waiting ? "Ожидает: " + actorName(waiting, roleCatalog) : actor ? "Сейчас: " + actorName(actor, roleCatalog) : "", item.updated_at ? when(item.updated_at) : ""));
    }

    function RequestDetail(props) {
      const item = props.request || {};
      if (props.loading) return h("section", { className: "et-panel" }, h(Empty, { title: "Загружаю задачу", text: "Получаю описание, историю и этапы согласования." }));
      const approval = item.approval;
      const ownerQuestion = approval && approval.question;
      const ownerQuestions = approval && Array.isArray(approval.questions) ? approval.questions : [];
      const documents = approval && approval.documents || item.artifacts || [];
      const discussion = item.discussion || item.comments || [];
      const subtasks = Array.isArray(item.tasks) ? item.tasks : [];
      const plannedRoles = Array.isArray(item.role_plan) ? item.role_plan : [];
      const approvalHistory = Array.isArray(item.approval_history) ? item.approval_history : [];
      const sourceAttachments = Array.isArray(item.attachments) ? item.attachments : [];
      const canAttach = ["queued", "analysis", "awaiting_requirements", "awaiting_owner_input"].includes(String(item.stage || item.status || "").toLowerCase());
      const assignmentMeta = (entry, planned) => {
        const state = entry.assignment_state;
        const stateText = planned
          ? (state === "proposed" ? "Вариант из плана" : "Прогноз по настройкам")
          : state === "pending" ? "Назначение ожидает запуска" : state === "selected" ? "Назначение зафиксировано" : "Состояние назначения не зафиксировано";
        const provider = entry.provider ? providerName(entry.provider, catalog) : "Провайдер не зафиксирован";
        const model = entry.model || (planned ? "Модель пока не определена" : "Не зафиксировано");
        const reason = entry.selection_reason || entry.purpose;
        return h("article", { className: "et-role-assignment", key: (entry.id || entry.plan_task_id || entry.role || entry.title || "role") + (planned ? "-planned" : "-actual") },
          h("div", { className: "et-role-assignment__heading" },
            h("div", null,
              h("strong", null, roleName(entry.role || entry.assignee, roleCatalog)),
              h("span", { className: "et-role-assignment__title" }, entry.title || "Роль команды")),
            h(Badge, { tone: planned ? "muted" : toneForStatus(entry.status) }, planned ? "Прогноз" : statusLabel(entry.status || "unknown"))),
          h("div", { className: "et-role-assignment__model" },
            h("span", null, provider), h("span", null, model),
            entry.reasoning_effort ? h("span", null, "Эффорт: " + effortLabel(entry.reasoning_effort)) : h("span", null, "Эффорт не зафиксирован")),
          h("div", { className: "et-role-assignment__meta" },
            h("span", null, stateText),
            entry.quota_pool ? h("span", null, "Пул: " + entry.quota_pool) : null,
            entry.assignment_source ? h("span", null, "Источник: " + assignmentSourceLabel(entry.assignment_source)) : null),
          reason ? h("p", { className: "et-role-assignment__reason" }, reason) : null);
      };
      const related = [];
      if (item.parent_feature) related.push({ id: item.parent_feature, label: "Исходная задача" });
      (Array.isArray(item.followups) ? item.followups : []).forEach((id) => {
        if (id) related.push({ id, label: "Связанная доработка" });
      });
      const questionCards = ownerQuestions.length ? h("section", { className: "et-owner-questions", "aria-label": "Вопросы команды к владельцу" },
        h("div", { className: "et-owner-questions__intro" },
          h("h3", null, "Нужно уточнить у вас"),
          h("p", null, "Выберите подходящий вариант. Можно предложить свой ответ. После ответа на все вопросы команда продолжит работу.")),
        ownerQuestions.map((question, questionIndex) => {
          const options = Array.isArray(question.options) ? question.options : [];
          const answer = question.answer;
          const draftKey = ownerQuestionDraftKey(item.id || "", approval.id, question.id);
          const questionDraft = (props.questionDrafts || {})[draftKey] || "";
          const answeredOption = answer && options.find((option) => option.id === answer.option_id);
          const recommendedId = question.recommended_option_id;
          return h("article", { className: "et-owner-question", key: question.id || "question-" + questionIndex },
            h("div", { className: "et-owner-question__top" }, h("strong", null, "Вопрос " + (questionIndex + 1)),
              h(Badge, { tone: answer ? "success" : "warning" }, answer ? "Ответ отправлен" : "Нужен ответ")),
            h("h4", null, question.question || "Уточните, пожалуйста"),
            question.example ? h("p", { className: "et-owner-question__example" }, h("strong", null, "Пример: "), question.example) : null,
            answer ? h("div", { className: "et-owner-question__saved", role: "status" },
              h("strong", null, "Ваш ответ: "), answeredOption ? answeredOption.label : answer.text || "Ответ записан")
              : h(React.Fragment, null,
                h("div", { className: "et-owner-question__options" }, options.map((option) => {
                  const recommended = option.id === recommendedId;
                  return h("button", { className: "et-owner-choice" + (recommended ? " et-owner-choice--recommended" : ""), type: "button",
                    key: option.id, disabled: props.busy || props.answerBusy,
                    onClick: () => props.onAnswerQuestion(question.id, { option_id: option.id }) },
                    h("span", { className: "et-owner-choice__title" }, option.label,
                      recommended ? h("span", { className: "et-owner-choice__recommendation" }, "Рекомендация команды") : null),
                    option.description ? h("span", { className: "et-owner-choice__description" }, option.description) : null,
                    recommended && question.recommendation_reason ? h("span", { className: "et-owner-choice__why" }, "Почему: " + question.recommendation_reason) : null);
                })),
                h("details", { className: "et-owner-question__custom" },
                  h("summary", null, "Предложить свой вариант"),
                  h("textarea", { className: "et-input et-textarea", value: questionDraft, disabled: props.busy || props.answerBusy,
                    onChange: (event) => props.onQuestionDraft(item.id || "", approval.id, question.id, event.target.value), placeholder: "Напишите, как лучше поступить…" }),
                  h("button", { className: "et-button", type: "button", disabled: props.busy || props.answerBusy || !String(questionDraft).trim(),
                    onClick: () => props.onAnswerQuestion(question.id, { text: String(questionDraft).trim() }) }, props.answerBusy ? "Отправляю…" : "Отправить свой ответ"))));
        })) : null;
      return h("div", { className: "et-request-detail" },
        h("section", { className: "et-panel" },
          h("div", { className: "et-request-detail__head" }, h("div", null, h("div", { className: "et-request-row__meta" }, item.project || item.project_id || "Проект", item.id ? "Задача " + item.id : ""), h("h2", null, item.title || "Задача"), h("p", null, item.stage_label || stageLabel(item.stage || item.status), item.actor ? " · Сейчас: " + actorName(item.actor, roleCatalog) : item.actor_label ? " · Сейчас: " + item.actor_label : "")), h(Badge, { tone: approval ? "warning" : isRequestFinished(item) ? "success" : "info" }, approval ? approvalLabel(approval.kind) : item.status_label || stageLabel(item.stage || item.status))),
          h(IntegrationCheckProgress, { value: item.integration_check }),
          h(VerificationProgress, { value: item.verification_progress }),
          h(EfficiencyPanel, { key: item.id, requestId: item.id }),
          !ownerQuestions.length ? h("div", { className: "et-request-original" }, h("h3", null, "Исходное описание"), h("pre", null, item.brief || item.description || "Описание пока недоступно.")) : null),
        approval && ownerQuestions.length ? h("section", { className: "et-panel et-approval-card et-approval-card--priority", "aria-labelledby": "et-approval-title" },
          h("div", { className: "et-approval-card__heading" }, h("div", null, h("div", { className: "et-eyebrow" }, "Нужно ваше решение"), h("h2", { id: "et-approval-title" }, "Уточните условия задачи"), h("p", null, "Ваши ответы помогут команде завершить требования.")), h(Badge, { tone: "warning" }, "Ожидает вас")),
          questionCards,
          Object.keys(documents).length ? h("details", { className: "et-request-document-bundle" },
            h("summary", null, "Посмотреть материалы команды"),
            h(RequestDocuments, { documents: withoutInternalQuestionDocuments(documents) })) : null) : null,
        ownerQuestions.length ? h("section", { className: "et-panel et-request-original et-request-original--collapsed" },
          h("details", null, h("summary", null, "Прочитать исходное описание задачи"), h("pre", null, item.brief || item.description || "Описание пока недоступно."))) : null,
        h("section", { className: "et-panel et-source-attachments" },
          h("div", { className: "et-panel__head" }, h("div", null, h("h2", null, "Исходные документы"), h("p", null, "Файлы, которые приложил владелец задачи. Они отделены от документов, созданных командой."))),
          sourceAttachments.length ? h("ul", { className: "et-attachment-list" }, sourceAttachments.map((file, index) => h("li", { className: "et-attachment-row", key: file.id || file.name || index },
            h("div", null, h("strong", null, file.name || file.filename || "Документ"), h("small", null, formatBytes(file.size) + (file.text_status ? " · " + attachmentTextLabel(file.text_status) : ""))),
            h("a", { className: "et-button et-button--small", href: API + "/requests/" + encodeURIComponent(item.id) + "/attachments/" + encodeURIComponent(file.id), download: file.name || "" }, "Скачать")))) : h(Empty, { title: "Файлы не приложены", text: "Исходные документы появятся здесь." }),
          canAttach ? h("div", { className: "et-existing-attachment-compose" },
            h(AttachmentPicker, { items: props.attachments || [], disabled: props.attachmentBusy || props.busy, onFiles: props.onAttachmentFiles, onRemove: props.onRemoveAttachment, onRetry: props.onRetryAttachment, label: "Добавить документы", directory: true }),
            h("p", { className: "et-help" }, "После добавления файлы станут частью контекста задачи; команда должна сверить их с текущими требованиями."),
            h("button", { className: "et-button et-button--primary", type: "button", disabled: props.attachmentBusy || props.busy || !(props.attachments || []).length || (props.attachments || []).some((entry) => entry.status !== "uploaded"), onClick: props.onAttachFiles }, props.attachmentBusy ? "Добавляю…" : "Добавить к задаче"))
            : h("p", { className: "et-help" }, "Добавление файлов доступно на этапах принятия, анализа и согласования требований.")),
        approval && !ownerQuestions.length ? h("section", { className: "et-panel et-approval-card", "aria-labelledby": "et-approval-title" },
          h("div", { className: "et-approval-card__heading" }, h("div", null, h("div", { className: "et-eyebrow" }, "Нужно ваше решение"), h("h2", { id: "et-approval-title" }, approvalLabel(approval.kind)), h("p", null, "Проверьте материалы ниже. Команда продолжит после вашего решения.")), h(Badge, { tone: "warning" }, "Ожидает вас")),
          approval.summary ? h("pre", { className: "et-request-summary" }, approval.summary) : null,
          ownerQuestions.length ? h("section", { className: "et-owner-questions", "aria-label": "Вопросы команды к владельцу" },
            h("div", { className: "et-owner-questions__intro" },
              h("h3", null, "Нужно уточнить у вас"),
              h("p", null, "Выберите подходящий вариант. Можно предложить свой ответ. После ответа на все вопросы команда продолжит работу.")),
            ownerQuestions.map((question, questionIndex) => {
              const options = Array.isArray(question.options) ? question.options : [];
              const answer = question.answer;
              const draftKey = ownerQuestionDraftKey(item.id || "", approval.id, question.id);
              const questionDraft = (props.questionDrafts || {})[draftKey] || "";
              const answeredOption = answer && options.find((option) => option.id === answer.option_id);
              const recommendedId = question.recommended_option_id;
              return h("article", { className: "et-owner-question", key: question.id || "question-" + questionIndex },
                h("div", { className: "et-owner-question__top" }, h("strong", null, "Вопрос " + (questionIndex + 1)),
                  h(Badge, { tone: answer ? "success" : "warning" }, answer ? "Ответ отправлен" : "Нужен ответ")),
                h("h4", null, question.question || "Уточните, пожалуйста"),
                question.example ? h("p", { className: "et-owner-question__example" }, h("strong", null, "Пример: "), question.example) : null,
                answer ? h("div", { className: "et-owner-question__saved", role: "status" },
                  h("strong", null, "Ваш ответ: "), answeredOption ? answeredOption.label : answer.text || "Ответ записан")
                  : h(React.Fragment, null,
                    h("div", { className: "et-owner-question__options" }, options.map((option) => {
                      const recommended = option.id === recommendedId;
                      return h("button", { className: "et-owner-choice" + (recommended ? " et-owner-choice--recommended" : ""), type: "button",
                        key: option.id, disabled: props.busy || props.answerBusy,
                        onClick: () => props.onAnswerQuestion(question.id, { option_id: option.id }) },
                        h("span", { className: "et-owner-choice__title" }, option.label,
                          recommended ? h("span", { className: "et-owner-choice__recommendation" }, "Рекомендация команды") : null),
                        option.description ? h("span", { className: "et-owner-choice__description" }, option.description) : null,
                        recommended && question.recommendation_reason ? h("span", { className: "et-owner-choice__why" }, "Почему: " + question.recommendation_reason) : null);
                    })),
                    h("details", { className: "et-owner-question__custom" },
                      h("summary", null, "Предложить свой вариант"),
                      h("textarea", { className: "et-input et-textarea", value: questionDraft, disabled: props.busy || props.answerBusy,
                        onChange: (event) => props.onQuestionDraft(item.id || "", approval.id, question.id, event.target.value), placeholder: "Напишите, как лучше поступить…" }),
                      h("button", { className: "et-button", type: "button", disabled: props.busy || props.answerBusy || !String(questionDraft).trim(),
                        onClick: () => props.onAnswerQuestion(question.id, { text: String(questionDraft).trim() }) }, props.answerBusy ? "Отправляю…" : "Отправить свой ответ"))));
            })) : ownerQuestion ? h("div", { className: "et-request-summary", role: "status" },
            h("strong", null, "Вопрос команды к вам"), h("p", null, ownerQuestion)) : null,
          h(RequestDocuments, { documents }),
          !ownerQuestions.length ? h(Field, { label: ownerQuestion ? "Ваш ответ" : "Комментарий команде", help: ownerQuestion ? "Ответ отправится аналитику. После этого команда обновит требования и снова попросит их согласовать." : "При возврате на доработку комментарий обязателен. Можно также задать вопрос или уточнить требование." }, h("textarea", { className: "et-input et-textarea et-request-comment", value: props.comment, disabled: props.busy || props.commentBusy, onChange: (e) => props.onComment(e.target.value), placeholder: ownerQuestion ? "Напишите ответ на вопрос команды…" : "Ваше решение или замечания…" })) : null,
          !ownerQuestions.length ? h("div", { className: "et-request-actions" }, ownerQuestion
            ? h("button", { className: "et-button et-button--primary", type: "button", disabled: props.busy || props.commentBusy || !props.comment.trim(), onClick: props.onSendComment }, props.commentBusy ? "Отправляю…" : "Отправить ответ")
            : h(React.Fragment, null,
              h("button", { className: "et-button", type: "button", disabled: props.busy || props.commentBusy || !props.comment.trim(), onClick: props.onSendComment }, props.commentBusy ? "Отправляю…" : "Отправить комментарий"),
              h("button", { className: "et-button et-button--danger", type: "button", disabled: props.busy || props.commentBusy || !props.comment.trim(), onClick: () => props.onDecision("revise") }, props.busy ? "Сохраняю…" : "Вернуть с замечаниями"),
              h("button", { className: "et-button et-button--primary", type: "button", disabled: props.busy || props.commentBusy, onClick: () => props.onDecision("approve") }, approval.kind === "requirements" ? "Согласовать требования" : approval.kind === "plan" ? "Утвердить план и начать разработку" : approval.kind === "delivery" ? "Принять результат" : "Подтвердить и продолжить"))) : null)
        : null,
        item.blocker ? h("section", { className: "et-panel et-request-blocker" }, h("strong", null, "Задача остановлена"), h("p", null, String(item.blocker))) : null,
        related.length ? h("section", { className: "et-panel et-request-related" },
          h("div", { className: "et-panel__head" }, h("div", null, h("h2", null, "Связанные задачи"), h("p", null, "Переход к исходной задаче и доработкам."))),
          h("div", { className: "et-request-related__links" }, related.map((entry, index) => h("button", {
            className: "et-button", type: "button", key: entry.id + ":" + index,
            onClick: () => props.onOpen && props.onOpen(entry.id),
          }, entry.label + " · " + entry.id)))) : null,
        (subtasks.length || plannedRoles.length) ? h("section", { className: "et-panel et-role-assignment-panel" },
          h("div", { className: "et-panel__head" }, h("div", null, h("h2", null, "Роли и модели задачи"), h("p", null, "Фактические назначения отделены от прогноза по плану."))),
          subtasks.length ? h("div", { className: "et-role-assignment-group" },
            h("h3", null, "Назначения по задаче"),
            h("div", { className: "et-role-assignment-list" }, subtasks.map((task) => assignmentMeta(task, false)))) : null,
          plannedRoles.length ? h("div", { className: "et-role-assignment-group et-role-assignment-group--planned" },
            h("h3", null, "Планируемые роли · прогноз"),
            h("p", { className: "et-help" }, "Это ещё не запущенные исполнители. Модель или пул могут измениться при допуске к квоте; прогноз не меняет сведения о прошлых запусках."),
            h("div", { className: "et-role-assignment-list" }, plannedRoles.map((entry) => assignmentMeta(entry, true)))) : null,
          !plannedRoles.length && ["intake", "queued", "requirements", "awaiting_requirements", "analysis"].includes(String(item.stage || "").toLowerCase())
            ? h("p", { className: "et-help" }, "Специализации разработчиков появятся после подготовки плана и его согласования.") : null) : null,
        approvalHistory.length ? h("section", { className: "et-panel et-approval-history" },
          h("div", { className: "et-panel__head" }, h("div", null, h("h2", null, "История решений"), h("p", null, "Согласования и замечания владельца по этапам."))),
          h("div", { className: "et-approval-history__list" }, approvalHistory.map((entry, index) => h("article", {
            className: "et-approval-history__entry", key: entry.gate_id || (entry.gate_revision + ":" + index),
          },
          h("div", { className: "et-approval-history__top" },
            h("strong", null, approvalLabel(entry.kind)),
            h(Badge, { tone: entry.decision === "approve" ? "success" : "warning" }, decisionLabel(entry.decision))),
          h("div", { className: "et-request-row__meta" },
            "Версия " + (entry.gate_revision || "—"),
            entry.channel ? "Канал: " + channelLabel(entry.channel) : "Канал не указан",
            entry.actor ? "Кто: " + String(entry.actor) : "",
            entry.created_at ? when(entry.created_at) : ""),
          entry.comment ? h("pre", { className: "et-approval-history__comment" }, entry.comment) : null)))) : null,
        h("section", { className: "et-panel" }, h("div", { className: "et-panel__head" }, h("div", null, h("h2", null, "Обсуждение"), h("p", null, "Вопросы, ответы и решения по задаче."))),
          discussion.length ? h("div", { className: "et-discussion" }, discussion.map((entry, index) => h("article", { className: "et-discussion__entry", key: entry.id || entry.created_at || index }, h("div", { className: "et-discussion__meta" }, h("strong", null, entry.author_label || entry.author || (entry.actor ? actorName(entry.actor, roleCatalog) : "Команда")), entry.created_at ? h("time", null, when(entry.created_at)) : null, entry.channel ? h(Badge, { tone: "muted" }, titleCase(entry.channel)) : null), h("pre", null, entry.comment || entry.content || entry.message || "")))) : h(Empty, { title: "Обсуждение пока пустое", text: "Вопросы и ответы появятся здесь." }),
          !approval ? h("div", { className: "et-request-comment-compose" }, h(Field, { label: "Написать команде" }, h("textarea", { className: "et-input et-textarea et-request-comment", value: props.comment, disabled: props.commentBusy, onChange: (e) => props.onComment(e.target.value), placeholder: "Добавьте уточнение или ответ на вопрос…" })), h("button", { className: "et-button et-button--primary", type: "button", disabled: props.commentBusy || !props.comment.trim(), onClick: props.onSendComment }, props.commentBusy ? "Отправляю…" : "Отправить")) : null),
        !approval && Object.keys(documents).length ? h("section", { className: "et-panel" }, h("div", { className: "et-panel__head" }, h("div", null, h("h2", null, "Материалы задачи"))), h(RequestDocuments, { documents })) : null);
    }

    function Overview() {
      const taskCards = visibleTasks.map((task) => h("article", { className: "et-task", key: task.id },
        h("div", { className: "et-task__main" },
          h("div", { className: "et-task__title-line" }, h("strong", null, task.title || task.id), h(Badge, { tone: toneForStatus(task.status) }, statusLabel(task.status))),
          h("div", { className: "et-task__meta" },
            h("span", null, roleName(task.assignee, roleCatalog)),
            task.provider || task.model ? h("span", null, [task.provider, task.model].filter(Boolean).join(" · ")) : null,
          task.reasoning_effort ? h("span", null, "Глубина: " + effortLabel(task.reasoning_effort)) : null,
            task.project_context ? h("span", null, "Контекст: " + ((task.project_context.manifest && task.project_context.manifest.name) || task.project_context.project_id) + " · " + String(task.project_context.context_hash || "").slice(0, 10)) : null,
            task.updated_at ? h("span", null, when(task.updated_at)) : null)),
        task.quota_reason ? h("p", { className: "et-task__reason" }, "Ожидание квоты: " + task.quota_reason) : null));
      return h("div", { className: "et-stack" },
        h("section", { className: "et-hero" },
          h("div", null,
            h("div", { className: "et-eyebrow" }, "Инженерная команда Hermes"),
            h("h1", null, "Ход работы и состояние команды"),
          h("p", null, "Смотрите активные задачи, блокировки и доступность подписок. Статусы обновляются автоматически каждые 10 секунд.")),
      h("a", { className: "et-primary-link", href: "/kanban", onClick: goBoard }, "Открыть канбан →")),
        h("div", { className: "et-stats" },
          stat("Задач на доске", taskList.length, "Все роли и статусы"),
          stat("Активных фич", activeFeatures.length, "В работе сейчас"),
          stat("Контроллер", status && status.controller ? statusLabel(status.controller.state) : "—", status && status.controller && status.controller.heartbeat_at ? "Связь " + when(status.controller.heartbeat_at) : "Состояние процесса"),
          stat("Обновлено", status ? when(status.observed_at) : "—", statusLoading ? "Обновляем…" : "Состояние сервера")),
        h("section", { className: "et-panel" },
          h("div", { className: "et-panel__head et-panel__head--wrap" },
            h("div", null, h("h2", null, "Исполнители"), h("p", null, "Кто сейчас работает и какая роль ждёт назначения.")),
            h("div", { className: "et-filter" },
              [ ["all", "Все"], ["active", "Активные"], ["waiting", "Ожидают"] ].map(([id, label]) => h("button", { key: id, className: filter === id ? "et-filter__button is-selected" : "et-filter__button", onClick: () => setFilter(id) }, label)))),
          statusLoading && !status ? h(Empty, { title: "Загружаю состояние", text: "Читаю актуальные карточки и занятость ролей." }) : null,
          !statusLoading && !status ? h(Empty, { title: "Состояние пока недоступно", text: "Проверьте связь с контроллером и обновите страницу." }) : null,
          status && visibleTasks.length === 0 ? h(Empty, { title: filter === "all" ? "Карточек пока нет" : "Таких задач сейчас нет", text: "Подробная история и перемещение карточек доступны на канбан-доске." }) : null,
          status && visibleTasks.length ? h("div", { className: "et-task-list" }, taskCards) : null,
        h("div", { className: "et-grid et-grid--two" },
          h("section", { className: "et-panel" }, h("div", { className: "et-panel__head" }, h("div", null, h("h2", null, "Активные фичи"), h("p", null, "Крупные задачи и их этапы."))),
          activeFeatures.length ? h("div", { className: "et-feature-list" }, activeFeatures.map((feature) => h("article", { className: "et-feature", key: feature.id },
              h("div", { className: "et-feature__top" }, h("strong", null, feature.title || feature.id), h(Badge, { tone: feature.blocker ? "warning" : "info" }, feature.blocker ? "Блокировка" : stageLabel(feature.stage))),
              h("p", null, "Этап: " + stageLabel(feature.stage) + (Number.isFinite(feature.index) ? " · шаг " + (feature.index + 1) + (feature.total_tasks ? " из " + feature.total_tasks : "") : "")),
              h(IntegrationCheckProgress, { value: feature.integration_check }),
              h(VerificationProgress, { value: feature.verification_progress }),
              feature.blocker ? h("p", { className: "et-warning-text" }, feature.blocker) : null,
              feature.project_context ? h("p", { className: "et-context-summary" }, "Контекст проекта: ", (feature.project_context.manifest && feature.project_context.manifest.name) || feature.project_id, " · ревизия ", String(feature.project_context.revision || "—").slice(0, 10), " · ", String(feature.project_context.context_hash || "—").slice(0, 10)) : null,
              feature.waiting_for_quota && feature.waiting_for_quota.length ? h("p", { className: "et-warning-text" }, "Ждут квоту: " + feature.waiting_for_quota.join(", ")) : null))) : h(Empty, { title: "Нет активных фич", text: "Новые задачи появятся здесь после отправки ТЗ." })),
          h("section", { className: "et-panel" }, h("div", { className: "et-panel__head" }, h("div", null, h("h2", null, "Подписки сейчас"), h("p", null, "Квота из последних доступных снимков."))),
            Object.keys(quotaPools).length ? h("div", { className: "et-mini-pools" }, Object.entries(quotaPools).map(([id, pool]) => h("div", { className: "et-mini-pool", key: id },
              h("div", null, h("strong", null, id), h("span", null, pool.provider || (pools[id] && pools[id].provider) || "Провайдер")),
              h(Badge, { tone: pool.auth_ready ? "success" : "muted" }, pool.auth_ready ? "Вход подтверждён оператором" : "Вход не подтверждён"),
              pool.snapshot_age_seconds != null ? h("small", null, "Снимок " + formatAge(pool.snapshot_age_seconds) + " назад") : h("small", null, "Снимок неизвестен")))) : h(Empty, { title: "Нет снимков квоты", text: "Состояние появится после чтения источников подписок." })))));
    }

    function stat(label, value, note) {
      return h("article", { className: "et-stat", key: label }, h("span", null, label), h("strong", null, value), h("small", null, note));
    }

    function RolesTab() {
      const entries = Object.entries(roles);
      const setRole = (id, key, value) => update((config) => {
        if (key === "pool" && !value) delete config.roles[id].pool;
        else config.roles[id][key] = value;
      });
      return h("div", { className: "et-stack" },
        h("div", { className: "et-page-title" }, h("div", null, h("h1", null, "Роли и модели"), h("p", null, "Назначайте провайдера, модель и уровень рассуждений каждой роли.")), dirty ? h(Badge, { tone: "warning" }, "Есть несохранённые изменения") : null),
        h("div", { className: "et-note" }, h("strong", null, "Настройки применяются к новым задачам."), " Уже созданные карточки сохраняют выбранную модель и глубину рассуждений."),
        entries.length ? h("div", { className: "et-role-list" }, entries.map(([id, role]) => {
          const info = roleCatalog[id] || {};
          const isOpen = !!expandedRoles[id];
          const candidates = Array.isArray(role.candidates) ? role.candidates : [];
          return h("section", { className: "et-panel et-role", key: id },
            h("div", { className: "et-role__header" },
              h("div", null, h("div", { className: "et-role__name" }, h("h2", null, info.label || titleCase(id)), h(Badge, { tone: "muted" }, id)), h("p", null, info.description || "Настройки исполнения роли.")),
              h("div", { className: "et-role__status" }, activeRole(id, status) ? h(Badge, { tone: toneForStatus(activeRole(id, status).status) }, statusLabel(activeRole(id, status).status)) : h(Badge, { tone: "muted" }, "Свободна"), activeRole(id, status) ? h("small", null, activeRole(id, status).task_title || "Карточка " + activeRole(id, status).task_id) : null)),
            h("div", { className: "et-form-grid" },
              h(Field, { label: "Провайдер" }, h(Select, { value: role.provider || "", options: providerOptions, onChange: (e) => setRole(id, "provider", e.target.value) })),
              h(Field, { label: "Модель", help: "Укажите модель, доступную выбранному провайдеру." }, h(Input, { value: role.model || "", placeholder: "Например, kimi-k2.6", onChange: (e) => setRole(id, "model", e.target.value) })),
              h(Field, { label: "Глубина рассуждений" }, h(Select, { value: role.reasoning_effort || "medium", options: effortOptions, onChange: (e) => setRole(id, "reasoning_effort", e.target.value) })),
              h(Field, { label: "Бюджет запуска", help: "Ограничение времени одного запуска, в секундах." }, h(Input, { type: "number", min: 1, value: role.run_budget == null ? "" : role.run_budget, onChange: (e) => setRole(id, "run_budget", numberOrEmpty(e.target.value)) })),
              h(Field, { label: "Максимум ходов", help: "Ограничивает длину одного рабочего цикла." }, h(Input, { type: "number", min: 1, value: role.max_turns == null ? "" : role.max_turns, onChange: (e) => setRole(id, "max_turns", numberOrEmpty(e.target.value)) })),
          h(Field, { label: "Пул квоты" }, h(Select, { value: role.pool || inferredPool(role.provider, pools), options: [{ value: "", label: "Автоматический выбор" }].concat(Object.keys(pools).map((pool) => ({ value: pool, label: pool }))), onChange: (e) => setRole(id, "pool", e.target.value) }))),
            h("button", { type: "button", className: "et-disclosure", onClick: () => setExpandedRoles((old) => Object.assign({}, old, { [id]: !old[id] })) }, isOpen ? "Скрыть список моделей" : "Настроить список выбора моделей"),
            isOpen ? h("div", { className: "et-candidates" },
              h("p", { className: "et-help" }, candidates.length
                ? "Когда список не пуст, он полностью задаёт модели этой роли; primary-поля выше в маршрутизации не участвуют. При первом добавлении primary-модель автоматически включается как кандидат с приоритетом 0."
                : "Пока список пуст, используются primary-поля выше. Первый клик «Добавить кандидата» сохранит эту модель в списке с приоритетом 0 и добавит строку для следующей модели."),
              candidates.map((candidate, index) => h("div", { className: "et-candidate", key: index },
                h(Field, { label: "Провайдер" }, h(Select, { value: candidate.provider || (pools[candidate.pool] && pools[candidate.pool].provider) || "", options: providerOptions, onChange: (e) => update((c) => { c.roles[id].candidates[index].provider = e.target.value; }) })),
                h(Field, { label: "Пул" }, h(Select, { value: candidate.pool || "", options: Object.keys(pools).map((pool) => ({ value: pool, label: pool })), onChange: (e) => update((c) => { c.roles[id].candidates[index].pool = e.target.value; c.roles[id].candidates[index].provider = c.quota.pools[e.target.value] ? c.quota.pools[e.target.value].provider : ""; }) })),
                h(Field, { label: "Модель" }, h(Input, { value: candidate.model || "", onChange: (e) => update((c) => { c.roles[id].candidates[index].model = e.target.value; }) })),
                h(Field, { label: "Глубина рассуждений" }, h(Select, { value: candidate.reasoning_effort || role.reasoning_effort || "medium", options: effortOptions, onChange: (e) => update((c) => { c.roles[id].candidates[index].reasoning_effort = e.target.value; }) })),
                h(Field, { label: "Приоритет" }, h(Input, { type: "number", value: candidate.tier == null ? index + 1 : candidate.tier, onChange: (e) => update((c) => { c.roles[id].candidates[index].tier = numberOrEmpty(e.target.value); }) })),
                h("button", { className: "et-icon-button et-remove", type: "button", title: "Удалить кандидата", onClick: () => update((c) => {
                  c.roles[id].candidates.splice(index, 1);
                  if (!c.roles[id].candidates.length) delete c.roles[id].candidates;
                }) }, "×"))),
              h("button", { className: "et-button et-button--quiet", type: "button", onClick: () => update((c) => {
                const current = c.roles[id].candidates || [];
                const poolId = c.roles[id].pool || inferredPool(c.roles[id].provider, c.quota.pools) || "";
                const provider = c.roles[id].provider || (c.quota.pools[poolId] && c.quota.pools[poolId].provider) || "";
                if (!current.length) current.push({ pool: poolId, provider, model: c.roles[id].model || "", reasoning_effort: c.roles[id].reasoning_effort || "medium", tier: 0 });
                current.push({ pool: poolId, provider, model: "", reasoning_effort: c.roles[id].reasoning_effort || "medium", tier: Math.max(0, ...current.map((candidate) => Number(candidate.tier) || 0)) + 1 });
                c.roles[id].candidates = current;
              }) }, "+ Добавить модель в список")) : null);
        })) : h(Empty, { title: "Каталог ролей ещё не загружен", text: "Перезагрузите настройки." }),
        h("section", { className: "et-panel et-workflow" },
          h("div", { className: "et-panel__head" }, h("div", null, h("h2", null, "Границы рабочего процесса"), h("p", null, "Ограничения декомпозиции и повторных попыток."))),
          h("div", { className: "et-form-grid et-form-grid--four" },
            workflowField("Параллельных задач", "max_parallel_tasks", "1 или 2. Только независимые задачи в отдельных рабочих копиях. Дополнительно действуют лимиты подписки."),
            workflowField("Задач в плане максимум", "max_plan_tasks", "Максимальный размер декомпозиции."),
            workflowField("Возвратов кода на доработку", "max_rework_rounds"),
            workflowField("Поправок плана", "max_plan_corrections"),
            workflowField("Повторов проверки плана", "max_plan_report_retries"),
            workflowField("Повторов review / QA", "max_report_retries"),
            h("label", { className: "et-toggle et-workflow__auto-start" },
              h("input", {
                type: "checkbox",
                checked: !!(draft.workflow && draft.workflow.auto_start_after_judge_accept),
                onChange: (e) => update((c) => { c.workflow.auto_start_after_judge_accept = e.target.checked; })
              }),
              h("span", null,
                h("strong", null, "После принятия плана судьёй автоматически начать разработку"),
                h("small", null, "Сначала вы согласуете требования. Если включено, после решения judge «принять план» разработчики начнут работу без отдельного подтверждения плана. Приёмка результата всё равно останется за вами."))))));

      function workflowField(label, key, help) {
        return h(Field, { label, help }, h(Input, { type: "number", min: 0, value: draft.workflow && draft.workflow[key] != null ? draft.workflow[key] : "", onChange: (e) => update((c) => { c.workflow[key] = numberOrEmpty(e.target.value); }) }));
      }
    }

    function QuotasTab() {
      const entries = Object.entries(pools);
      const setPool = (id, key, value) => update((c) => {
        c.quota.pools[id][key] = value;
        if (key === "provider") Object.values(c.roles || {}).forEach((role) => (role.candidates || []).forEach((candidate) => { if (candidate.pool === id) candidate.provider = value; }));
      });
      const addPool = () => update((c) => {
        const base = "pool-" + (Object.keys(c.quota.pools).length + 1); let id = base; let n = 2; while (c.quota.pools[id]) id = base + "-" + n++;
        c.quota.pools[id] = { provider: providerOptions[0] ? providerOptions[0].value : "", enabled: false, auth_ready: false, max_concurrent: 1, reserve_percent: 20, max_snapshot_age: 300, unknown_policy: "wait" };
      });
      return h("div", { className: "et-stack" },
        h("div", { className: "et-page-title" }, h("div", null, h("h1", null, "Подписки и квоты"), h("p", null, "Управляйте доступными пулами и правилами ожидания.")), dirty ? h(Badge, { tone: "warning" }, "Есть несохранённые изменения") : null),
        h("div", { className: "et-note" }, h("strong", null, "Здесь не вводятся пароли или токены."), " Подключите учётную запись отдельно в Hermes. Контроллер не переключается на неразрешённый платный API."),
        h("div", { className: "et-quota-grid" }, entries.map(([id, pool]) => {
          const live = quotaPools[id] || {};
          const windows = live.snapshot_windows || live.windows || [];
          return h("section", { className: "et-panel et-pool-card", key: id },
            h("div", { className: "et-pool-card__head" }, h("div", null, h("h2", null, id), h("p", null, pool.provider || "Провайдер не выбран")), h("div", { className: "et-pool-badges" },
              h(Badge, { tone: pool.enabled ? "success" : "muted" }, pool.enabled ? "Пул включён" : "Пул выключен"),
              h(Badge, { tone: pool.auth_ready ? "success" : "muted" }, pool.auth_ready ? "Авторизация настроена" : "Авторизация не подтверждена"),
              h(Badge, { tone: live.snapshot_state === "known" ? "info" : "warning" }, live.snapshot_state === "known" ? "Квота известна" : "Квота неизвестна"))),
            h("div", { className: "et-form-grid" },
              h(Field, { label: "Провайдер" }, h(Select, { value: pool.provider || "", options: providerOptions, onChange: (e) => setPool(id, "provider", e.target.value) })),
              h(Field, { label: "Параллельные запуски" }, h(Input, { type: "number", min: 1, value: pool.max_concurrent == null ? 1 : pool.max_concurrent, onChange: (e) => setPool(id, "max_concurrent", numberOrEmpty(e.target.value)) })),
              h(Field, { label: "Резерв проверки, %", help: "Лимит, оставляемый для reviewer и QA." }, h(Input, { type: "number", min: 0, max: 100, value: pool.reserve_percent == null ? 20 : pool.reserve_percent, onChange: (e) => setPool(id, "reserve_percent", numberOrEmpty(e.target.value)) })),
              h(Field, { label: "Максимальный возраст снимка, сек." }, h(Input, { type: "number", min: 1, value: pool.max_snapshot_age == null ? 300 : pool.max_snapshot_age, onChange: (e) => setPool(id, "max_snapshot_age", numberOrEmpty(e.target.value)) })),
              h(Field, { label: "Неизвестная квота", help: "Разрешённый запуск может упереться в исчерпанный лимит: точный остаток и расход заранее неизвестны." }, h(Select, { value: pool.unknown_policy || "wait", options: [{ value: "wait", label: "Ждать свежего снимка" }, { value: "allow_limited", label: "Один запуск при неизвестной квоте" }], onChange: (e) => setPool(id, "unknown_policy", e.target.value) })),
              h("label", { className: "et-toggle" }, h("input", { type: "checkbox", checked: !!pool.enabled, onChange: (e) => setPool(id, "enabled", e.target.checked) }), h("span", null, h("strong", null, "Пул включён"), h("small", null, "Только включённые пулы участвуют в выборе."))),
              h("label", { className: "et-toggle" }, h("input", { type: "checkbox", checked: !!pool.auth_ready, onChange: (e) => setPool(id, "auth_ready", e.target.checked) }), h("span", null, h("strong", null, "Авторизация настроена"), h("small", null, "Отметьте после входа в Hermes под этим провайдером.")))),
            h("div", { className: "et-window-heading" }, h("strong", null, "Последние снимки лимитов"), h("span", null, live.snapshot_observed_at ? "Получен " + when(live.snapshot_observed_at) : "Снимок недоступен")),
            live.remaining_percent != null ? h("div", { className: "et-quota-summary" },
              h("strong", null, clamp(live.remaining_percent) + "% остаток ограничивающего окна"),
              live.reset_at ? h("span", null, "Следующее окно квоты: " + when(live.reset_at)) : null,
              live.active_leases != null ? h("span", null, "Активных запусков: " + live.active_leases + (live.max_concurrent != null ? " из " + live.max_concurrent : "")) : null) : null,
            windows.length ? h("div", { className: "et-windows" }, windows.map((window, index) => h("div", { className: "et-window", key: window.id || index },
              h("div", null, h("strong", null, quotaWindowName(window.name || window.label || window.window || "Окно " + (index + 1))), h("small", null, window.reset_at ? "Сброс " + when(window.reset_at) : window.resets_at ? "Сброс " + when(window.resets_at) : "Сброс неизвестен")),
              h("div", { className: "et-meter" }, h("span", { style: { width: clamp(window.remaining_percent != null ? window.remaining_percent : 100 - (window.used_percent || 0)) + "%" } })),
              h("b", null, window.remaining_percent != null ? window.remaining_percent + "% осталось" : window.used_percent != null ? clamp(100 - window.used_percent) + "% осталось" : "Неизвестно")))) : h(Empty, { title: "Снимков лимитов пока нет", text: live.reason || "Источник квоты не сообщил доступные окна." }),
            h("div", { className: "et-pool-status" },
              h("span", null, "Источник: " + (live.snapshot_source || live.source || pool.provider || "неизвестен")),
              h("span", null, live.snapshot_age_seconds != null ? "Возраст снимка: " + formatAge(live.snapshot_age_seconds) : "Возраст снимка неизвестен"),
              live.unknown_limited_leases ? h("span", null, "Запусков с неизвестной квотой: " + live.unknown_limited_leases) : null,
              live.reason ? h("span", { className: "et-warning-text" }, live.reason) : null),
            h("button", { className: "et-button et-button--danger", type: "button", onClick: () => update((c) => {
              delete c.quota.pools[id];
              Object.values(c.roles || {}).forEach((role) => {
                if (role.pool === id) role.pool = "";
                if (Array.isArray(role.candidates)) {
                  role.candidates = role.candidates.filter((candidate) => candidate.pool !== id);
                  if (!role.candidates.length) delete role.candidates;
                }
              });
            }) }, "Удалить пул"));
        })),
        h("button", { className: "et-button et-button--quiet", type: "button", onClick: addPool }, "+ Добавить пул (выключен до настройки)"));
    }

    function ProjectsTab() {
      const entries = Object.entries(projects);
      return h("div", { className: "et-stack" },
        h("div", { className: "et-page-title" }, h("div", null, h("h1", null, "Проекты"), h("p", null, "Репозитории и команды регрессионной проверки для каждой задачи.")), h("div", { className: "et-inline-actions" }, dirty ? h(Badge, { tone: "warning" }, "Есть несохранённые изменения") : null, entries.length ? h("button", { className: "et-button et-button--primary", type: "button", onClick: () => startRequest("") }, "Новая задача") : null)),
        h("div", { className: "et-note" }, h("strong", null, "Пути задаются на сервере Hermes."), " Проверки хранятся как массив команд argv; они выполняются контроллером в рабочей копии проекта."),
        entries.length ? h("div", { className: "et-project-list" }, entries.map(([id, project]) => {
          const existing = !!(envelope && envelope.config && envelope.config.projects && envelope.config.projects[id]);
          const context = projectContexts[id];
          return h("section", { className: "et-panel et-project", key: id },
          h("div", { className: "et-project__head" }, h("div", null, h("h2", null, id), h("p", null, project.repo || "Путь к репозиторию не указан")), h("div", { className: "et-inline-actions" }, existing ? h("button", { className: "et-button et-button--primary", type: "button", onClick: () => startRequest(id) }, "Создать задачу") : null, !existing ? h("button", { className: "et-button et-button--danger", type: "button", onClick: () => {
            update((c) => { delete c.projects[id]; });
            setProjectChecks((current) => { const next = Object.assign({}, current); delete next[id]; delete next[id + "!error"]; return next; });
          } }, "Удалить черновик") : h(Badge, { tone: "muted" }, "Существующий проект"))),
          h("div", { className: "et-form-grid" },
            h(Field, { label: "Путь к репозиторию", help: existing ? "Путь существующего проекта защищён от изменения." : "Укажите абсолютный путь на сервере Hermes." }, h(Input, { value: project.repo || "", placeholder: "/home/alex/workspaces/project", disabled: existing, onChange: (e) => setProject(id, "repo", e.target.value) })),
            h(Field, { label: "Базовая ветка" }, h(Input, { value: project.base_ref || "main", onChange: (e) => setProject(id, "base_ref", e.target.value) })),
            h(Field, { label: "Файл паспорта проекта", help: "Путь относительно корня репозитория. По умолчанию .agents/project.json." }, h(Input, { value: project.context_file || ".agents/project.json", onChange: (e) => setProject(id, "context_file", e.target.value) })),
            h("label", { className: "et-toggle" }, h("input", { type: "checkbox", checked: !!project.context_required, onChange: (e) => setProject(id, "context_required", e.target.checked) }), h("span", null, h("strong", null, "Паспорт обязателен"), h("small", null, "Новые задачи не запускаются без корректного паспорта."))),
            h(Field, { label: "Таймаут проверок, сек." }, h(Input, { type: "number", min: 1, value: project.check_timeout == null ? 600 : project.check_timeout, onChange: (e) => setProject(id, "check_timeout", numberOrEmpty(e.target.value)) }))),
          h("div", { className: "et-inline-actions" }, h("button", { className: "et-button et-button--quiet", type: "button", disabled: dirty || !existing || !!(context && context.loading), title: dirty ? "Сначала сохраните настройки проекта" : "Проверяет паспорт на настроенной базовой ревизии; команды проекта не запускаются", onClick: () => inspectContext(id) }, context && context.loading ? "Проверяю…" : "Проверить контекст"), h("span", { className: "et-help" }, dirty ? "Сохраните настройки перед проверкой." : "Проверка читает Git и документы; тесты и сборка не запускаются.")),
          context && context.result ? projectContextView(context.result) : null,
          context && context.error ? h("p", { className: "et-error-inline" }, context.error) : null,
          h(Field, { label: "Регрессионные команды (JSON массив argv)", help: "Например: [[\"go\",\"test\",\"./...\"],[\"go\",\"vet\",\"./...\"]]. Команда должна быть массивом аргументов, не shell-строкой." },
            h("textarea", { className: "et-input et-textarea", spellCheck: false, value: projectChecks[id] == null ? JSON.stringify(project.checks || [], null, 2) : projectChecks[id], onChange: (e) => {
              setProjectChecks((current) => { const next = Object.assign({}, current, { [id]: e.target.value }); delete next[id + "!error"]; return next; });
              setDirty(true); setError("");
            } })),
          projectChecks[id] != null ? h("div", { className: "et-inline-actions" }, h("button", { className: "et-button et-button--quiet", type: "button", onClick: () => commitChecks(id) }, "Применить JSON"), h("span", { className: "et-help" }, "Ошибка разбора будет показана рядом.")) : null,
          projectChecks[id + "!error"] ? h("p", { className: "et-error-inline" }, projectChecks[id + "!error"]) : null,
          h(ExecutionPolicyEditor, { value: project.execution_policy, checks: project.checks, onChange: (value) => update((c) => {
            if (value) c.projects[id].execution_policy = value;
            else delete c.projects[id].execution_policy;
          }) }));
        })) : h(Empty, { title: "Проекты не добавлены", text: "Добавьте репозиторий, чтобы команда могла планировать работу." }),
        h("button", { className: "et-button et-button--quiet", type: "button", onClick: () => update((c) => {
          const base = "project"; let id = base; let n = 2; while (c.projects[id]) id = base + "-" + n++;
          c.projects[id] = { repo: "", base_ref: "main", checks: [], check_timeout: 600, context_file: ".agents/project.json", context_required: false };
        }) }, "+ Добавить проект"));

      function setProject(id, key, value) {
        update((c) => { c.projects[id][key] = value; });
        if (["base_ref", "context_file", "context_required"].includes(key)) {
          setProjectContexts((current) => { const next = Object.assign({}, current); delete next[id]; return next; });
        }
      }
      async function inspectContext(id) {
        setProjectContexts((old) => Object.assign({}, old, { [id]: { loading: true } }));
        try {
          const result = await get("/projects/" + encodeURIComponent(id) + "/context");
          setProjectContexts((old) => Object.assign({}, old, { [id]: { result } }));
        } catch (e) {
          setProjectContexts((old) => Object.assign({}, old, { [id]: { error: "Не удалось проверить контекст: " + errorText(e) } }));
        }
      }
      function projectContextView(context) {
        const manifest = context.manifest || {};
        const hasManifest = !!manifest.name;
        const warnings = context.warnings || [];
        const errors = context.errors || [];
        const translateContextMessage = (message) => {
          const value = String(message || "");
          if (value.startsWith("Project context manifest is missing; using tracked documentation only:")) return "Паспорт не найден; используются только отслеживаемые Git инструкции и документы.";
          if (value.startsWith("Required project context manifest is missing:")) return "Не найден обязательный паспорт проекта: " + value.split(":").slice(1).join(":").trim();
          return value;
        };
        return h("div", { className: "et-context-result" },
          h("div", { className: "et-context-result__head" }, h("strong", null, manifest.name || context.project_id || "Контекст проекта"), h(Badge, { tone: context.ready ? (hasManifest ? "success" : "info") : "warning" }, context.ready ? (hasManifest ? "Паспорт проверен" : "Только базовые документы") : "Есть ошибки")),
          manifest.summary ? h("p", null, manifest.summary) : null,
          manifest.stack && manifest.stack.length ? h("p", null, "Стек: " + manifest.stack.join(", ")) : null,
          manifest.components && manifest.components.length ? h("p", null, "Компоненты: " + manifest.components.map((component) => component.id).join(", ")) : null,
          h("small", null, "Ревизия " + String(context.revision || "—").slice(0, 12) + " · отпечаток " + String(context.context_hash || "—").slice(0, 12)),
          context.sources && context.sources.length ? h("details", { className: "et-context-sources" }, h("summary", null, "Проверенные источники (" + context.sources.length + ")"), h("ul", null, context.sources.map((source, index) => h("li", { key: source.path || index }, h("code", null, source.path || "Источник"), " · ", source.scope || "область не указана", source.sha256 ? " · SHA " + source.sha256.slice(0, 10) : "")))) : null,
          warnings.concat(errors).map((message, index) => h("p", { className: errors.includes(message) ? "et-error-inline" : "et-warning-text", key: index }, translateContextMessage(message))),
          warnings.length || errors.length ? h("details", { className: "et-context-diagnostics" }, h("summary", null, "Исходные сообщения проверки"), h("ul", null, warnings.concat(errors).map((message, index) => h("li", { key: index }, h("code", null, message))))) : null);
      }
      function commitChecks(id) {
        const textValue = projectChecks[id];
        if (textValue == null) return;
        try {
          const parsed = JSON.parse(textValue);
          if (!Array.isArray(parsed) || parsed.some((command) => !Array.isArray(command) || command.some((arg) => typeof arg !== "string"))) throw new Error("Ожидается массив массивов строк.");
          update((c) => { c.projects[id].checks = parsed; delete c.projects[id]._checksError; });
          setProjectChecks((current) => { const next = Object.assign({}, current); delete next[id]; delete next[id + "!error"]; return next; });
        } catch (e) { setProjectChecks((current) => Object.assign({}, current, { [id + "!error"]: e.message || "Некорректный JSON" })); }
      }
    }

    function setProject(id, key, value) { update((c) => { c.projects[id][key] = value; }); }
    return h("main", { className: "et-app" },
      h("header", { className: "et-topbar" },
        h("div", { className: "et-brand" }, h("span", { className: "et-brand__mark" }, "✳"), h("span", null, "Команда агентов")),
        h("div", { className: "et-topbar__actions" },
          status && status.controller ? h(Badge, { tone: status.controller.state === "running" ? "success" : "warning" }, "Контроллер: " + statusLabel(status.controller.state)) : h(Badge, { tone: "muted" }, "Контроллер неизвестен"),
          h("a", { className: "et-board-link", href: "/kanban", onClick: goBoard }, "Канбан ↗"))),
      h("nav", { className: "et-tabs", "aria-label": "Разделы команды" }, nav.map((item) => h("button", { key: item, className: tab === item ? "et-tab is-active" : "et-tab", onClick: () => setTab(item) }, item))),
      error ? h("div", { className: "et-alert et-alert--error", role: "alert" }, h("span", null, error), h("button", { type: "button", onClick: () => !draft && error.startsWith("Не удалось загрузить настройки:") ? loadConfig() : loadStatus(false) }, !draft && error.startsWith("Не удалось загрузить настройки:") ? "Повторить загрузку" : "Обновить состояние")) : null,
      notice ? h("div", { className: "et-alert et-alert--success", role: "status" }, notice) : null,
      configLoading && !draft ? h(Empty, { title: "Загружаю настройки команды", text: "Читаю роли, модели, квотные пулы и проекты." }) : null,
      draft ? (tab === "Задачи" ? RequestsTab() : tab === "Обзор" ? Overview() : tab === "Роли и модели" ? RolesTab() : tab === "Подписки" ? QuotasTab() : ProjectsTab()) : null,
      h("footer", { className: "et-footer" }, h("span", null, "Hermes · команда агентов"), h("span", null, "Состояние " + (status && status.observed_at ? when(status.observed_at) : "не получено"))),
      h("div", { className: "et-savebar" }, h("span", { className: dirty ? "et-savebar__dirty" : "et-savebar__clean" }, dirty ? "Есть несохранённые изменения" : "Все настройки сохранены"),
        h("button", { type: "button", className: "et-button et-button--quiet", disabled: configLoading || saving, onClick: reloadConfig }, "Перезагрузить"),
        h("button", { type: "button", className: "et-button et-button--primary", disabled: !dirty || saving || configLoading, onClick: save }, saving ? "Сохраняю…" : "Сохранить настройки")));
  }

  function roleName(id, catalog) { return (catalog[id] && catalog[id].label) || titleCase(id || "Не назначен"); }
  function providerName(id, catalog) {
    const providers = catalog && Array.isArray(catalog.providers) ? catalog.providers : [];
    const provider = providers.find((entry) => entry && entry.id === id);
    return provider ? provider.label || provider.id : titleCase(id || "Провайдер не зафиксирован");
  }
  function assignmentSourceLabel(source) {
    return ({ card: "Карточка исполнения", quota: "Выбор по квоте", snapshot: "Снимок назначения", workflow: "Роль процесса", plan: "План разработки", unknown: "Неизвестно" })[String(source || "").toLowerCase()] || titleCase(source || "Не указан");
  }
  function initialRequestFromUrl() {
    try { return new URL(window.location.href).searchParams.get("request") || ""; }
    catch (_) { return ""; }
  }
  function initialTabFromUrl(requestId) {
    if (requestId) return "Задачи";
    try {
      const tab = new URL(window.location.href).searchParams.get("tab");
      return ({ tasks: "Задачи", overview: "Обзор", roles: "Роли и модели", quota: "Подписки", projects: "Проекты" })[String(tab || "").toLowerCase()] || "Задачи";
    } catch (_) { return "Задачи"; }
  }
  function updateRequestUrl(requestId) {
    try {
      const url = new URL(window.location.href);
      if (requestId) url.searchParams.set("request", String(requestId));
      else url.searchParams.delete("request");
      window.history.replaceState(window.history.state, "", url.toString());
    } catch (_) { /* Keep in-panel navigation working if history is restricted. */ }
  }
  function hasHttpStatus(error, expected) {
    if (error && (Number(error.status) === expected || Number(error.statusCode) === expected)) return true;
    const raw = String(error && error.message ? error.message : error);
    return new RegExp("(?:^|\\D)" + expected + "(?:\\D|$)").test(raw);
  }
  function isStaleRequestError(error) {
    const raw = String(error && error.message ? error.message : error);
    return hasHttpStatus(error, 409) || /conflict|stale|revision/i.test(raw);
  }
  function actorName(value, catalog) {
    if (value && typeof value === "object") return value.label || value.name || roleName(value.role || value.id, catalog);
    return roleName(value, catalog);
  }
  function newRequestId() {
    try { if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID(); } catch (_) { /* Fallback below. */ }
    return "req-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12);
  }
  function isRequestFinished(item) {
    const value = String(item && (item.status || item.stage) || "").toLowerCase();
    return ["done", "completed", "accepted", "cancelled", "canceled", "archived"].includes(value);
  }
  function approvalLabel(kind) {
    return ({ requirements: "Согласование требований", plan: "Утверждение плана", delivery: "Приёмка результата", input: "Нужен ваш ответ" })[kind] || "Требуется решение";
  }
  function decisionLabel(decision) {
    return ({ approve: "Согласовано", revise: "Возвращено с замечаниями", superseded_by_comment: "Обновлено комментарием", questions_answered: "Ответы получены" })[decision] || titleCase(decision || "Решение");
  }
  function channelLabel(channel) {
    return ({ dashboard: "Дашборд", telegram: "Telegram", api: "API", web: "Веб-интерфейс" })[String(channel || "").toLowerCase()] || titleCase(channel || "Не указан");
  }
  function RequestDocuments(props) {
    const source = props.documents || [];
    const entries = Array.isArray(source) ? source.map((doc, index) => [doc.name || doc.filename || "Материал " + (index + 1), doc.content || doc.text || ""]) : Object.entries(source);
    if (!entries.length) return h(Empty, { title: "Материалы ещё не подготовлены", text: "Команда добавит документы на следующем этапе." });
    return h("div", { className: "et-request-documents" }, entries.map(([name, content], index) => h("details", { className: "et-request-document", key: name + index, open: index === 0 }, h("summary", null, readableDocumentName(name)), h("pre", null, String(content == null ? "" : content)))));
  }
  function withoutInternalQuestionDocuments(documents) {
    const isInternal = (name) => ["questions", "attachments"].includes(String(name || "").split("/").pop().replace(/\.[^.]+$/, "").toLowerCase());
    if (Array.isArray(documents)) return documents.filter((doc) => !isInternal(doc && (doc.name || doc.filename)));
    if (!documents || typeof documents !== "object") return {};
    return Object.fromEntries(Object.entries(documents).filter(([name]) => !isInternal(name)));
  }
  function AttachmentPicker(props) {
    const items = props.items || [];
    const [dragging, setDragging] = useState(false);
    const accept = ".md,.txt,.pdf,.docx,.html,.htm,.json,.yaml,.yml,.csv,.png,.jpg,.jpeg,.webp";
    return h("div", { className: "et-attachment-picker" },
      h("div", { className: "et-attachment-picker__controls" },
        h("label", { className: "et-button et-button--small et-attachment-picker__button" }, "Выбрать файлы", h("input", { type: "file", multiple: true, accept, disabled: props.disabled, onChange: (event) => { props.onFiles(event.target.files); event.target.value = ""; } })),
        props.directory ? h("label", { className: "et-button et-button--small et-attachment-picker__button" }, "Выбрать папку", h("input", { type: "file", multiple: true, accept, disabled: props.disabled, webkitdirectory: "", directory: "", onChange: (event) => { props.onFiles(event.target.files); event.target.value = ""; } })) : null),
      h("div", { className: "et-attachment-dropzone" + (dragging ? " is-dragging" : ""), onDragOver: (event) => { event.preventDefault(); if (!props.disabled) setDragging(true); }, onDragLeave: (event) => { if (!event.currentTarget.contains(event.relatedTarget)) setDragging(false); }, onDrop: (event) => { event.preventDefault(); setDragging(false); if (!props.disabled) props.onFiles(event.dataTransfer.files); }, role: "note" }, "Перетащите сюда документы. Можно выбрать целую папку; структура каталогов сохранится."),
      h("p", { className: "et-help" }, "MD, TXT, PDF, DOCX, HTML, JSON, YAML, CSV, PNG, JPG, WEBP · до 5 МиБ на файл · до 50 файлов и 30 МиБ суммарно."),
      items.length ? h("ul", { className: "et-attachment-list" }, items.map((entry) => h("li", { className: "et-attachment-row", key: entry.localId },
        h("div", { className: "et-attachment-row__main" }, h("strong", { title: entry.name }, entry.name), h("small", null, formatBytes(entry.size) + " · " + attachmentStatusLabel(entry.status) + (entry.error ? " · " + entry.error : ""))),
        h("div", { className: "et-attachment-row__actions" }, entry.status === "error" && entry.file ? h("button", { className: "et-button et-button--small", type: "button", disabled: props.disabled, onClick: () => props.onRetry(entry) }, "Повторить") : null,
          h("button", { className: "et-button et-button--small et-button--danger", type: "button", disabled: props.disabled, onClick: () => props.onRemove(entry.localId), "aria-label": "Удалить " + entry.name }, "Убрать"))))) : h("p", { className: "et-help et-attachment-empty" }, props.label ? "Документы не выбраны." : "Список пуст."));
  }
  function newAttachmentId() {
    return window.crypto && typeof window.crypto.randomUUID === "function" ? window.crypto.randomUUID() : "attachment-" + Date.now() + "-" + Math.random().toString(16).slice(2);
  }
  async function fileToBase64(file) {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    const chunkSize = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += chunkSize) binary += String.fromCharCode.apply(null, bytes.subarray(offset, offset + chunkSize));
    return btoa(binary);
  }
  function formatBytes(value) {
    const bytes = Number(value) || 0;
    return bytes < 1024 * 1024 ? Math.max(1, Math.round(bytes / 1024)) + " КиБ" : (bytes / (1024 * 1024)).toFixed(1).replace(".", ",") + " МиБ";
  }
  function attachmentStatusLabel(status) {
    return ({ queued: "в очереди", uploading: "загружается", uploaded: "загружен", error: "ошибка загрузки" })[status] || "ожидает";
  }
  function attachmentTextLabel(status) {
    return ({ extracted: "текст доступен", available: "текст доступен", pending: "текст ещё обрабатывается", truncated: "текст извлечён частично; оригинал сохранён", no_text: "текст не извлечён; оригинал сохранён", unsupported: "текст недоступен", failed: "не удалось прочитать текст" })[status] || "текст: " + String(status);
  }
  function readableDocumentName(name) {
    const base = String(name || "Документ").split("/").pop().replace(/\.[^.]+$/, "").replace(/[-_]+/g, " ").trim();
    return ({ SPEC: "Требования", DESIGN: "Архитектура", PLAN: "План работ", DELIVERY: "Результат", "PLAN DECISION": "Решение по плану" })[base.toUpperCase()] || base || "Документ";
  }
  function statusLabel(status) {
    return ({ ready: "Готова", todo: "К выполнению", queued: "В очереди", planned: "Запланировано", in_progress: "В работе", running: "Работает", blocked: "Заблокирована", waiting: "Ожидает", waiting_tests: "Ожидает проверку", waiting_human: "Ждёт вашего решения", awaiting_owner: "Ждёт вашего решения", waiting_approval: "Ждёт вашего решения", waiting_quota: "Ждёт квоту", waiting_for_quota: "Ждёт квоту", done: "Готово", completed: "Завершена", accepted: "Принята", archived: "В архиве", cancelled: "Отменена", canceled: "Отменена", review: "На ревью", failed: "Ошибка", idle: "Свободна", stopped: "Остановлен", unknown: "Неизвестно" })[status] || titleCase(status || "Неизвестно");
  }
  function stageLabel(stage) {
    return ({ intake: "Принята", queued: "В очереди", requirements: "Согласование требований", awaiting_requirements: "Согласование требований", analysis: "Анализ задачи", planning: "Подготовка плана", plan_challenge: "Проверка плана", plan_judgment: "Решение по плану", awaiting_plan: "Утверждение плана", implementation: "Разработка", verification: "Проверка и тесты", integration_review: "Интеграционная проверка", batch_regression: "Полная регрессия пакета", batch_repair: "Исправление ошибок пакета", delivery: "Приёмка результата", awaiting_delivery: "Приёмка результата", awaiting_owner_input: "Ждёт вашего ответа", finalizing: "Завершение", blocked: "Заблокирована", waiting_human: "Ждёт вашего решения", awaiting_owner: "Ждёт вашего решения", waiting_approval: "Ждёт вашего решения", done: "Готово", completed: "Завершена", cancelled: "Отменена" })[stage] || titleCase(stage || "Неизвестно");
  }
  function quotaWindowName(name) {
    const value = String(name || "");
    if (/^(usage|primary|main)$/i.test(value)) return "Основное окно";
    if (/^(limit[_-]?0|short|short_window)$/i.test(value)) return "Короткое окно";
    return value;
  }
  function inferredPool(provider, pools) {
    const match = Object.entries(pools || {}).find(([, pool]) => pool.provider === provider && pool.enabled);
    return match ? match[0] : "";
  }
  function goBoard(event) {
    try { window.localStorage.setItem("hermes.kanban.selectedBoard", "engineering-team"); }
    catch (_) { /* Kanban can still open using its last selected board. */ }
  }
  function activeRole(id, status) { return status && Array.isArray(status.roles) ? status.roles.find((role) => role.id === id && role.task_id) : null; }
  function toneForStatus(status) {
    if (["done", "completed"].includes(status)) return "success";
    if (["blocked", "waiting", "waiting_quota", "waiting_for_quota"].includes(status)) return "warning";
    if (status === "waiting_tests") return "info";
    if (["running", "in_progress", "active"].includes(status)) return "info";
    return "muted";
  }
  function numberOrEmpty(value) { return value === "" ? "" : Number.isFinite(Number(value)) ? Number(value) : ""; }
  function clamp(value) { const n = Number(value); return Math.max(0, Math.min(100, Number.isFinite(n) ? n : 0)); }
  function formatAge(value) { const seconds = Math.max(0, Math.floor(Number(value) || 0)); return seconds < 60 ? seconds + " сек." : Math.floor(seconds / 60) + " мин."; }

  registry.register("engineering-team", TeamPlugin);
})();
