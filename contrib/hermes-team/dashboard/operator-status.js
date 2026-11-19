// Pure operator-facing summaries for the legacy Kanban UI.
// This file is injected into the dashboard IIFE; keep it dependency-free.

function compactTaskTitle(title) {
  var value = String(title == null ? "" : title).trim();
  // Request ids are useful in the details view, but add no meaning to every
  // board card. Strip only a leading, well-formed request token.
  value = value.replace(/^\s*\[req-[a-f0-9]{8,64}\]\s*/i, "");
  value = value.replace(/^\s*(?:—|–|-|:|·)+\s*/, "");
  return value.trim() || String(title == null ? "Без названия" : title).trim();
}

function operatorRoleLabel(assignee) {
  var value = String(assignee == null ? "" : assignee).trim();
  var key = value.toLowerCase();
  var labels = {
    "team-analyst": "Аналитик",
    "team-planner": "Планировщик",
    "team-skeptic": "Скептик",
    "team-judge": "Судья плана",
    "team-backend-developer": "Backend-разработчик",
    "team-backend": "Backend-разработчик",
    "team-frontend-developer": "Frontend-разработчик",
    "team-frontend": "Frontend-разработчик",
    "team-mobile-developer": "Мобильный разработчик",
    "team-android-developer": "Мобильный разработчик",
    "team-go-developer": "Go-разработчик",
    "team-reviewer": "Ревьюер",
    "team-qa-regression": "QA / регрессия",
    "team-qa": "QA / регрессия",
    "team-test-designer": "Проектировщик тестов",
    "team-controller": "Оркестратор",
  };
  if (labels[key]) return labels[key];
  if (/^team-/.test(value)) {
    return value.slice(5).split(/[-_]+/).filter(Boolean).map(function (part) {
      return part.charAt(0).toUpperCase() + part.slice(1);
    }).join(" ");
  }
  return value || "Не назначен";
}

function operatorStatus(task, context) {
  task = task || {};
  context = context || {};
  var team = context.teamTask || task._team || null;
  var status = String(task.status || "").toLowerCase();
  var result = {
    title: statusLabel(status),
    reason: "",
    action: "",
    tone: "neutral",
  };

  var isStopped = status === "blocked" || status === "waiting_tests";
  var workflowStop = isStopped ? teamWorkflowStop(team) : null;
  var owner = isStopped ? ownerInput(team, context) : null;
  if (owner) {
    result.title = "Нужен ваш ответ";
    result.reason = owner.reason;
    result.action = owner.action;
    result.tone = "warning";
    if (owner.href) result.href = owner.href;
    if (owner.linkLabel) result.linkLabel = owner.linkLabel;
    return result;
  }
  if (workflowStop) {
    var runs = Array.isArray(context.runs) ? context.runs : [];
    if (team.assignment_state === "pending" && !task.started_at && runs.length === 0) {
      workflowStop.title = "Ожидает возобновления команды";
      workflowStop.reason = "Командный процесс приостановлен. " + workflowStop.reason;
      workflowStop.action = "Ответ от вас сейчас не запрошен. Откройте запрос, чтобы увидеть состояние команды.";
    }
    return workflowStop;
  }

  var testWait = team && team.test_wait || task.test_wait || null;
  var testWaitStatus = String(team && team.status || "").toLowerCase();
  var latestRun = latestOperatorRun(context);
  // A new attempt or completed run supersedes a stale wait projection.
  var latestIsBlocked = !latestRun || String(latestRun.status || "").toLowerCase() === "blocked";
  if (isStopped && latestIsBlocked && (status === "waiting_tests" || testWaitStatus === "waiting_tests" || testWait || nativeTestWait(task, context))) {
    result.title = "Ожидает проверку";
    result.reason = "Проверка выполняется отдельным заданием. Бюджет агента и слот свободны.";
    result.action = "Действие не требуется. Команда продолжит по результату проверки.";
    result.tone = "info";
    return result;
  }

  if (status === "blocked") {
    var teamReason = currentQuotaReason(team, status);
    var quota = quotaKind(teamReason) || quotaKind(currentFailure(task, context));
    if (quota === "concurrency") {
      result.title = "Ожидает свободный слот";
      result.reason = "Ждёт свободного слота модели.";
      result.action = "От вас ничего не требуется. Диспетчер продолжит, когда слот освободится.";
      result.tone = "warning";
      return result;
    }
    if (quota === "limit") {
      result.title = "Ожидает ресурс модели";
      result.reason = "Достигнут лимит доступного ресурса модели.";
      result.action = "От вас ничего не требуется. Срок восстановления в данных не указан.";
      result.tone = "warning";
      return result;
    }
    var runs = Array.isArray(context.runs) ? context.runs : [];

    var deps = unresolvedParents(task, context);
    if (deps.length) {
      result.title = "Ожидает предыдущий этап";
      result.reason = "Ждёт завершения: " + deps.map(function (item) { return item.title; }).join(", ") + ".";
      result.action = deps.some(function (item) { return item.status === "archived"; })
        ? "Проверьте зависимость: архивная задача не выполнена; её нужно вернуть в работу или снять связь."
        : "От вас ничего не требуется. Задача продолжится после выполнения зависимости.";
      result.tone = "warning";
      return result;
    }

    var failure = currentFailure(task, context);
    if (failure) {
      result.title = "Остановлена из-за ошибки";
      result.reason = readableFailure(failure);
      result.action = "Нужна проверка последнего запуска и устранение этой ошибки.";
      result.tone = "error";
      return result;
    }

    if (team && team.assignment_state === "pending" && String(team.status || "").toLowerCase() === status && !task.started_at && runs.length === 0) {
      result.title = "Ожидает допуска к запуску";
      var stageLabel = shortOperatorText(team.stage_label || team.stage || "не указан", 100);
      result.reason = "Оркестратор ещё не запустил эту задачу. Текущий этап запроса: «" + stageLabel + "».";
      result.action = "Откройте запрос, чтобы увидеть текущую работу команды.";
      result.tone = "warning";
      var requestId = team.request_id || team.requestId;
      if (requestId) {
        result.href = "/engineering-team?request=" + encodeURIComponent(requestId);
        result.linkLabel = "Открыть запрос";
      }
      return result;
    }

    result.title = "Причина требует уточнения";
    result.reason = "Причина остановки не записана в доступных данных.";
    result.action = "Откройте историю событий и последний запуск задачи.";
    result.tone = "warning";
    return result;
  }

  if (status === "running") {
    result.reason = "Исполнитель сейчас работает над задачей.";
    result.action = "Ждите обновления результата.";
    result.tone = "info";
  } else if (status === "review") {
    result.reason = "Реализация ожидает проверки.";
    result.action = "Проверьте результат или дождитесь ревьюера.";
    result.tone = "warning";
  } else if (status === "done" || status === "complete" || status === "completed") {
    result.reason = "Задача завершена.";
    result.action = "Действие не требуется.";
    result.tone = "success";
  } else if (status === "ready" || status === "todo" || status === "triage" || status === "scheduled") {
    result.reason = status === "ready" ? "Задача готова к запуску." : "Задача ещё не выполняется.";
    result.action = status === "ready"
      ? (task.assignee ? "Диспетчер запустит её, когда появится ресурс." : "Назначьте роль исполнителя, чтобы задачу можно было запустить.")
      : "Проверьте требования и назначение, если запуск задерживается.";
    result.tone = "warning";
  } else {
    result.reason = "Текущий статус: " + (status || "не указан") + ".";
    result.action = "Откройте подробности задачи.";
    result.tone = "neutral";
  }
  return result;
}

function latestOperatorRun(context) {
  var runs = Array.isArray(context.runs) ? context.runs.slice() : [];
  runs.sort(function (a, b) {
    // Native run ids are monotonically increasing; dates can have equal seconds.
    var ai = Number(a.id == null ? a.run_id : a.id);
    var bi = Number(b.id == null ? b.run_id : b.id);
    if (Number.isSafeInteger(ai) && ai > 0 && Number.isSafeInteger(bi) && bi > 0) return bi - ai;
    return runTime(b) - runTime(a);
  });
  return runs[0] || null;
}

function nativeTestWait(task, context) {
  if (String(task.status || "").toLowerCase() !== "blocked") return false;
  var marker = /^Ожидает проверку (?:backend|checks):[0-9a-f]{64}$/;
  var latest = latestOperatorRun(context);
  if (latest && String(latest.status || "").toLowerCase() !== "blocked") return false;
  // Only structured native reasons are considered, never task text or comments.
  var reason = task.blocked_reason == null ? task.block_reason : task.blocked_reason;
  if (reason != null) return marker.test(String(reason));
  if (latest && latest.summary != null) return marker.test(String(latest.summary));
  var events = Array.isArray(context.events) ? context.events.slice() : [];
  events.sort(function (a, b) { return eventTime(b) - eventTime(a) || Number(b.id || 0) - Number(a.id || 0); });
  var event = events[0];
  if (!event || event.kind !== "blocked") return false;
  if (latest && event.run_id != null && String(event.run_id) !== String(latest.id == null ? latest.run_id : latest.id)) return false;
  if (latest && eventTime(event) < runTime(latest)) return false;
  var payload = event.payload;
  if (typeof payload === "string") {
    try { payload = JSON.parse(payload); } catch (_) { return false; }
  }
  return !!(payload && typeof payload === "object" && marker.test(String(payload.reason || "")));
}

function teamWorkflowStop(team) {
  if (!team || String(team.stage || "").toLowerCase() !== "blocked") return null;
  var blocker = String(team.blocker || team.blocked_reason || "").trim();
  if (!blocker) return null;
  var requestId = team.request_id || team.requestId;
  var result = {
    title: "Процесс команды остановлен",
    reason: "Контроллер приостановил выполнение запроса. Подробная причина доступна в запросе.",
    action: "Техническая причина остановки требует проверки. Ответ от вас сейчас не запрошен.",
    tone: "warning",
  };
  if (/^rework\s+limit\s+reached:\s*qa\s+check\b[\s\S]*?non-zero\s+exit_code\b/i.test(blocker)) {
    result.title = "Не принят отчёт проверки";
    result.reason = "Контроллер не принял результат QA. Автоматическое продолжение остановлено; требуется разбор отчёта.";
    result.action = "Проверьте отчёт и причину отказа в карточке запроса. Ответ от вас сейчас не запрошен.";
    result.tone = "error";
  }
  if (requestId) {
    result.href = "/engineering-team?request=" + encodeURIComponent(requestId);
    result.linkLabel = "Открыть запрос";
  }
  return result;
}

function statusLabel(status) {
  var labels = {
    blocked: "Остановлена",
    waiting_tests: "Ожидает проверку",
    running: "В работе",
    review: "На проверке",
    done: "Готово",
    complete: "Готово",
    completed: "Готово",
    ready: "Готова к запуску",
    todo: "В очереди",
    triage: "Нужна сортировка",
    scheduled: "Запланирована",
  };
  return labels[status] || "Статус задачи";
}

function quotaKind(value) {
  var text = String(value || "").toLowerCase();
  if (!text) return "";
  if (/max[_ -]?concurrent|reached\s+max\s+concurrent|concurrent\s+(?:limit|slot)|слот(?:а|ов)?\s+занят|максимум\s+одновременн/.test(text)) return "concurrency";
  if (/quota|headroom|weekly|limit|лимит|остат(?:ок|ка)\s+(?:квот|подписк)/.test(text)) return "limit";
  return "";
}

function currentQuotaReason(team, taskStatus) {
  if (!team || team.assignment_state !== "pending" || String(team.status || "").toLowerCase() !== taskStatus) return "";
  if (team.quota_reason) return String(team.quota_reason);
  var selection = String(team.selection_reason || "");
  var marker = selection.match(/Выбор ресурса:\s*([\s\S]*)$/i);
  return marker ? marker[1].trim() : "";
}

function ownerInput(team, context) {
  if (!team) return null;
  var approval = team.approval || {};
  var requestId = team.request_id || team.requestId;
  var blocker = String(team.blocker || team.blocked_reason || "").trim();
  var stage = String(team.stage || "").toLowerCase();
  var awaitingStages = ["awaiting_owner_input", "awaiting_requirements", "awaiting_plan", "awaiting_delivery"];
  var awaiting = awaitingStages.indexOf(stage) !== -1 || approval.status === "pending" || approval.status === "needed" || approval.required === true;
  if (!awaiting) return null;
  var reason = blocker ? shortOperatorText(blocker, 150) : "Команда ждёт вашего ответа или согласования.";
  return {
    reason: reason,
    action: "Откройте карточку запроса и ответьте команде.",
    href: requestId ? "/engineering-team?request=" + encodeURIComponent(requestId) : undefined,
    linkLabel: requestId ? "Открыть запрос" : undefined,
  };
}

function unresolvedParents(task, context) {
  var links = context.links || {};
  var parents = Array.isArray(links.parents) ? links.parents : [];
  var all = Array.isArray(context.allTasks) ? context.allTasks : [];
  return parents.map(function (parent) {
    var id = typeof parent === "string" ? parent : parent && (parent.id || parent.parent_id || parent.task_id);
    var found = typeof parent === "object" && parent ? parent : all.find(function (item) { return item && item.id === id; });
    if (!found || !id || id === task.id) return null;
    var parentStatus = String(found.status || "").toLowerCase();
    // Unknown parents are omitted: without a status we cannot explain the
    // block. Archived work does not satisfy a dependency in the native board.
    if (!parentStatus || parentStatus === "done" || parentStatus === "complete" || parentStatus === "completed") return null;
    return { id: id, title: compactTaskTitle(found.title || id), status: parentStatus };
  }).filter(Boolean);
}

function currentFailure(task, context) {
  var runs = Array.isArray(context.runs) ? context.runs.slice() : [];
  runs.sort(function (a, b) { return runTime(b) - runTime(a); });
  var latest = runs[0];
  var latestFailed = latest && /failed|error|gave[_ -]?up|timeout|timed[_ -]?out/i.test(String(latest.status || "") + " " + String(latest.outcome || ""));
  // A newer successful or active run supersedes an older failure.
  if (latest && !latestFailed) return "";
  if (latestFailed && (latest.error || latest.summary || latest.outcome)) return latest.error || latest.summary || latest.outcome;
  if (task.last_failure_error && (task.consecutive_failures == null || Number(task.consecutive_failures) > 0)) return task.last_failure_error;
  var diagnostics = Array.isArray(context.diagnostics) ? context.diagnostics : [];
  var diag = diagnostics.find(function (item) {
    return item && /error|failure|failed|blocked/i.test(String(item.severity || "") + " " + String(item.kind || "")) && (item.detail || item.title);
  });
  if (diag) return diag.detail || diag.title;
  var events = Array.isArray(context.events) ? context.events.slice() : [];
  events.sort(function (a, b) { return eventTime(b) - eventTime(a); });
  var event = events[0];
  if (!event || !/failed|error|gave_up|timeout/i.test(String(event.kind || ""))) return "";
  if (event) {
    var payload = event.payload;
    if (typeof payload === "string") {
      try { payload = JSON.parse(payload); } catch (_) { return payload; }
    }
    if (payload && typeof payload === "object") return payload.error || payload.message || payload.reason || payload.detail || event.kind;
    return event.kind;
  }
  return "";
}

function readableFailure(value) {
  var text = String(value || "").replace(/\s+/g, " ").trim();
  if (!text) return "Последний запуск завершился ошибкой.";
  var lower = text.toLowerCase();
  if (/max[_ -]?concurrent|reached max concurrent/.test(lower)) return "Ждёт свободного слота модели.";
  if (/iteration(?:s)?[ _-]?(?:budget|limit)|max(?:imum)?[ _-]?iterations|reached.{0,24}iterations|gave[_ -]?up.{0,24}\d+\s*\/\s*\d+/i.test(lower)) return "Исчерпан лимит попыток этого запуска.";
  if (/authentication|unauthorized|invalid[_ -]?api[_ -]?key|401/.test(lower)) return "Не прошла авторизация провайдера модели.";
  if (/rate.?limit|too many requests|\b429\b/.test(lower)) return "Провайдер временно ограничил частоту запросов.";
  if (/timed?\s*out|timeout|deadline exceeded/.test(lower)) return "Запуск превысил допустимое время.";
  if (/connection refused|connection reset|network is unreachable|dns|name or service not known/.test(lower)) return "Не удалось подключиться к нужному сервису.";
  if (/no space left|disk full/.test(lower)) return "На диске закончилось место.";
  if (/permission denied|operation not permitted/.test(lower)) return "Не хватает прав для выполнения операции.";
  if (/test.*fail|failed.*test|assertionerror/.test(lower)) return "Не прошли проверки или тесты.";
  // Avoid flooding the board with paths, traces and machine-specific data.
  text = text.replace(/(?:\/[^\s:]+){2,}/g, "путь к файлу").replace(/\b0x[0-9a-f]+\b/gi, "");
  return shortOperatorText(text, 180);
}

function shortOperatorText(value, maxLength) {
  var text = String(value || "").replace(/\s+/g, " ").trim();
  if (text.length > maxLength) text = text.slice(0, maxLength - 1).trimEnd() + "…";
  return text;
}

function runTime(run) {
  return timestamp(run && (run.ended_at || run.started_at || run.created_at));
}
function eventTime(event) {
  return timestamp(event && (event.created_at || event.timestamp));
}
function timestamp(value) {
  if (typeof value === "number") return value < 100000000000 ? value * 1000 : value;
  var parsed = Date.parse(value || "");
  return Number.isFinite(parsed) ? parsed : 0;
}
