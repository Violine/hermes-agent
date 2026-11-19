/**
 * Hermes Kanban — Dashboard Plugin
 *
 * Board view for the multi-agent collaboration board backed by
 * ~/.hermes/kanban.db. Calls the plugin's backend at /api/plugins/kanban/
 * and tails task_events over a WebSocket for live updates.
 *
 * Plain IIFE, no build step. Uses window.__HERMES_PLUGIN_SDK__ for React +
 * shadcn primitives; HTML5 drag-and-drop for card movement on desktop and
 * a pointer-based fallback for touch.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const { React } = SDK;
  const h = React.createElement;
  const {
    Card, CardContent,
    Badge, Button, Input, Label, Select, SelectOption,
  } = SDK.components;
  const { useState, useEffect, useCallback, useMemo, useRef } = SDK.hooks;
  const { cn, timeAgo } = SDK.utils;

  // Newer host dashboards expose a DS-styled Checkbox on the plugin SDK.
  // Fall back to a native <input type="checkbox"> shim so older hosts that
  // predate the design-system rollout still render. The shim normalises
  // Radix's onCheckedChange(checked) signature to native onChange(event).
  const Checkbox = SDK.components.Checkbox || function (props) {
    const { checked, onCheckedChange, className, onClick, ...rest } = props;
    return h("input", Object.assign({
      type: "checkbox",
      checked: !!checked,
      className: className,
      onClick: onClick,
      onChange: function (e) {
        if (onCheckedChange) onCheckedChange(e.target.checked);
      },
    }, rest));
  };

  // useI18n is a hook each component calls locally. Older host dashboards
  // may not expose it yet; fall back to a shim so the bundle still renders
  // English against an older host SDK. English fallback strings live
  // alongside each call site (passed as the third arg of tx()).
  const hostUseI18n = SDK.useI18n || function () { return { t: { kanban: null }, locale: "en" }; };
  // The dashboard's plugin SDK exposes the active app locale. Keep the
  // fallback strings below tied to that locale so newer Kanban copy remains
  // readable even when the host's bundled dictionary predates this plugin.
  const useI18n = function () {
    const state = hostUseI18n();
    const locale = state.locale || "en";
    const localized = React.useMemo(() => ({
      t: Object.assign({}, state.t, { __kanbanLocale: locale }),
      uiText: (en, ru) => /^ru(?:[-_]|$)/i.test(locale) ? ru : en,
      localizedTimeAgo: (value) => formatTimeAgo(value, locale),
    }), [state.t, locale]);
    return Object.assign({}, state, localized);
  };

  const RU_KANBAN = {
    "blockedWaitingHelp": "Ожидает ответа, квоты или зависимостей. Причина — в карточке.",
    "columnLabels.review": "Проверка",
    "columnLabels.blocked": "Ожидание и остановки",
    "columnHelp.blocked": "Причина указана в карточке. Откройте её, чтобы увидеть следующее действие.",
    "columnHelp.review": "Реализация завершена — ожидает проверки.",
    "confirmDoneMany": "Отметить {n} задач как выполненные? Захваты исполнителей будут освобождены, а зависимые дочерние задачи станут готовы.",
    "confirmArchiveMany": "Архивировать {n} задач? Они исчезнут из стандартного вида доски.",
    "confirmBlockedMany": "Заблокировать {n} задач? Захваты исполнителей будут освобождены.",
    "selectedTasks": "{n} выбранных задач",
    "thisTask": "эту задачу",
    "confirmStatusTitle.triage": "Переместить задачу в сортировку?",
    "confirmStatusTitle.todo": "Переместить задачу в список дел?",
    "confirmStatusTitle.ready": "Переместить задачу в готовые?",
    "confirmStatusTitle.blocked": "Заблокировать задачу?",
    "confirmStatusTitle.done": "Завершить задачу?",
    "confirmStatusTitle.archived": "Архивировать задачу?",
    "confirmStatusLabel.triage": "Переместить",
    "confirmStatusLabel.todo": "Переместить",
    "confirmStatusLabel.ready": "Переместить",
    "confirmStatusLabel.blocked": "Заблокировать",
    "confirmStatusLabel.done": "Завершить",
    "confirmStatusLabel.archived": "Архивировать",
    "bulkConfirmTitle": "Применить массовое изменение",
    "trash.label": "Корзина",
    "trash.title": "Перетащите карточку сюда, чтобы удалить её безвозвратно",
    "trash.confirmTitle": "Удалить задачу?",
    "trash.confirm": "Удалить задачу безвозвратно? Это действие нельзя отменить.",
    "trash.dropHint": "Перетащите сюда для удаления",
    "trash.confirmManyTitle": "Удалить {n} задач?",
    "trash.confirmMany": "Удалить выбранные задачи безвозвратно? Это действие нельзя отменить.",
    "projectDirectory": "Каталог проекта",
    "projectDirectoryHint": "(рекомендуется)",
    "projectDirectoryPlaceholder": "например, ~/projects/my-app",
    "projectDirectoryHelp": "Каталог проекта, в котором будут создаваться рабочие пространства задач.",
    "projectDirectoryExplanation": "Можно задать позже в настройках доски.",
    "projectDirectoryOverrideHint": "Путь можно переопределить для каждой задачи.",
    "clearFilters": "Сбросить фильтры",
    "needsAssignee": "Нужно назначить исполнителя",
    "needsAssigneeHint": "Зависимости выполнены, но диспетчер пропустит задачу, пока вы не назначите профиль.",
    "setPriority": "Задать приоритет",
    "newTaskTitle": "Новая задача — {column}",
    "taskTitleLabel": "Название",
    "assigneeLabel": "Исполнитель",
    "assigneeLabelHint": "(оставьте пустым — диспетчер выберет сам)",
    "skillsLabel": "Навыки",
    "skillsLabelHint": "(необязательно, через запятую)",
    "workspaceScratch": "Временное — удаляется после завершения",
    "workspaceScratchWarning": "Это рабочее пространство и оставшиеся в нём файлы будут удалены после завершения задачи.",
    "workspaceWorktree": "Git worktree — сохраняется",
    "workspaceDir": "Каталог — сохраняется",
    "parentLabel": "Родительская задача",
    "parentLabelHint": "(дочерняя останется заблокированной, пока родительская задача не будет завершена)",
    "goalMode": "Режим цели",
    "goalMaxTurns": "лимит ходов (по умолчанию 20)",
    "confirmTitle": "Подтвердите изменение",
    "common.confirm": "Подтвердить",
    "common.delete": "Удалить",
    "commentHintTitle": "Комментарии — это канал связи с исполнителем задачи",
    "commentHint": "Комментарий появится в обсуждении задачи. Исполнитель увидит его в следующем ходе; останавливать задачу не нужно.",
    "attachments": "Вложения",
    "uploading": "Загрузка…",
    "uploadFile": "Загрузить файл",
    "noAttachments": "— вложений пока нет —",
    "removeAttachment": "Удалить вложение",
    "confirmRemoveAttachment": "Удалить это вложение?",
    "finalResult": "Итоговый результат (сводка запуска)",
    "doneParentNote": "Это карточка-оркестратор или родительская задача. Подробности работы смотрите в результатах дочерних задач.",
    "doneNoResult": "Итоговый результат не сохранён. Посмотрите историю запусков, журналы или дочерние задачи.",
    "childResults": "Результаты дочерних задач",
    "noChildResult": "Результат пока не записан.",
    "model": "Модель",
    "modelProfileDefault": "модель профиля по умолчанию",
    "clickToEditModel": "Нажмите, чтобы задать модель для следующего запуска этой задачи",
    "modelFreeTextPlaceholder": "название модели (пусто — модель профиля по умолчанию)",
    "modelLoading": "Загрузка моделей…",
    "modelProfileDefaultOption": "(модель профиля по умолчанию)",
    "loadSettingsFailed": "Не удалось загрузить настройки: ",
    "settingsSaved": "Настройки сохранены.",
    "saveFailed": "Не удалось сохранить: ",
    "descriptionSaved": "Описание профиля {name} сохранено.",
    "autoGenerateFailed": "Не удалось создать описание автоматически: ",
    "reviewing": "Проверка",
  };

  function pluralRu(n, forms) {
    const value = Math.abs(Number(n) || 0);
    const mod10 = value % 10;
    const mod100 = value % 100;
    const index = mod10 === 1 && mod100 !== 11 ? 0
      : mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14) ? 1 : 2;
    return `${n} ${forms[index]}`;
  }

  function formatTimeAgo(value, locale) {
    if (!value) return "";
    if (!/^ru(?:[-_]|$)/i.test(locale)) return timeAgo ? timeAgo(value) : "";
    // Kanban API timestamps, like SDK.utils.timeAgo, are Unix seconds.
    const timestamp = typeof value === "number" ? value * 1000 : new Date(value).getTime();
    if (!Number.isFinite(timestamp)) return "";
    const seconds = Math.round((timestamp - Date.now()) / 1000);
    const units = [[60, "second"], [60, "minute"], [24, "hour"], [7, "day"], [4.345, "week"], [12, "month"], [Infinity, "year"]];
    let amount = seconds;
    let unit = "second";
    for (const [limit, candidate] of units) {
      if (Math.abs(amount) < limit) { unit = candidate; break; }
      amount /= limit;
    }
    const rtf = new Intl.RelativeTimeFormat("ru", { numeric: "auto" });
    const unitMap = { second: "second", minute: "minute", hour: "hour", day: "day", week: "week", month: "month", year: "year" };
    return rtf.format(Math.round(amount), unitMap[unit]);
  }

  // Resolve a translation by dotted path under the kanban namespace
  // (e.g. "columnLabels.triage"); fall back to the English string passed in.
  function tx(t, path, fallback, vars) {
    let node = t && t.kanban;
    if (node) {
      const parts = path.split(".");
      for (let i = 0; i < parts.length; i++) {
        if (node && typeof node === "object" && parts[i] in node) {
          node = node[parts[i]];
        } else { node = null; break; }
      }
    }
    let str = (typeof node === "string") ? node : fallback;
    // Some upstream locale catalogs contain English placeholders for keys
    // added after that catalog was authored. Replace those with the local
    // Russian fallback while preserving the host's English/default behavior.
    if (t && /^ru(?:[-_]|$)/i.test(t.__kanbanLocale) && (typeof node !== "string" || node === fallback)) {
      str = path === "selectedTasks" && vars
        ? pluralRu(vars.n, ["выбранная задача", "выбранные задачи", "выбранных задач"])
        : (RU_KANBAN[path] || str);
    }
    if (vars) {
      for (const k in vars) {
        str = str.replace(new RegExp("\\{" + k + "\\}", "g"), vars[k]);
      }
    }
    return str;
  }

  // ``fetchJSON`` throws ``Error("<status>: <raw body>")`` on non-2xx, and
  // FastAPI bodies look like ``{"detail":"<message>"}``.  Pull the
  // human-readable message out so banners/toasts don't have to leak HTTP
  // plumbing at the user (e.g. ``409: {"detail":"…"}``).  See #26744.
  function parseApiErrorMessage(err) {
    const raw = (err && err.message) ? String(err.message) : String(err || "");
    const m = raw.match(/^(\d{3}):\s*(.*)$/s);
    const body = m ? m[2] : raw;
    try {
      const parsed = JSON.parse(body);
      if (parsed && typeof parsed.detail === "string") return parsed.detail;
      if (parsed && parsed.detail && typeof parsed.detail.message === "string") {
        return parsed.detail.message;
      }
    } catch (_e) { /* not JSON — fall through to raw body */ }
    return body || raw;
  }

  // Board column display order; any backend status not listed here renders after these.
  const COLUMN_ORDER = ["triage", "todo", "ready", "running", "blocked", "review", "done"];
  // English fallback dictionaries — used when the i18n catalog is missing
  // a key, and as defaults for the get*() helpers below so callers running
  // outside any React component (where there's no `t`) still get sane text.
  const FALLBACK_COLUMN_LABEL = {
    triage: "Triage",
    todo: "Todo",
    ready: "Ready",
    running: "In Progress",
    blocked: "Blocked",
    review: "Review",
    done: "Done",
    archived: "Archived",
  };
  const FALLBACK_COLUMN_HELP = {
    triage: "Raw ideas — a specifier will flesh out the spec",
    todo: "Waiting on dependencies or unassigned",
    ready: "Dependencies satisfied; assign a profile to dispatch",
    running: "Claimed by a worker — in-flight",
    blocked: "Worker asked for human input",
    review: "Implementation complete — awaiting review",
    done: "Completed",
    archived: "Archived",
  };
  const FALLBACK_DESTRUCTIVE = {
    done: "Mark this task as done? The worker's claim is released and dependent children become ready.",
    archived: "Archive this task? It disappears from the default board view.",
    blocked: "Mark this task as blocked? The worker's claim is released.",
  };
  // Pluralized variants used by getDestructiveConfirm() when count > 1.
  // Each entry may use {n} as a placeholder for the count.
  const FALLBACK_DESTRUCTIVE_MANY = {
    done: "Mark {n} tasks as done? The workers' claims are released and dependent children become ready.",
    archived: "Archive {n} tasks? They disappear from the default board view.",
    blocked: "Mark {n} tasks as blocked? The workers' claims are released.",
  };
  const FALLBACK_DIAGNOSTIC_EVENT_LABELS = {
    completion_blocked_hallucination: "⚠ Completion blocked — phantom card ids",
    suspected_hallucinated_references: "⚠ Prose referenced phantom card ids",
  };
  const FALLBACK_TRASH = {
    label: "Trash",
    title: "Drag a card here to permanently delete it",
    confirm: "Permanently delete this task? This cannot be undone.",
    dropHint: "Drop to delete",
  };
  const DIAGNOSTIC_EVENT_KIND_KEYS = {
    completion_blocked_hallucination: "completionBlockedHallucination",
    suspected_hallucinated_references: "suspectedHallucinatedReferences",
  };
  const DESTRUCTIVE_KEYS = {
    done: "confirmDone",
    archived: "confirmArchive",
    blocked: "confirmBlocked",
  };

  function getColumnLabel(t, status) {
    if (status === "blocked" && t && /^ru(?:[-_]|$)/i.test(t.__kanbanLocale)) return "Ожидание и остановки";
    return tx(t, "columnLabels." + status, FALLBACK_COLUMN_LABEL[status] || status);
  }
  function getColumnHelp(t, status) {
    if (status === "blocked") {
      if (t && /^ru(?:[-_]|$)/i.test(t.__kanbanLocale)) return "Причина и следующее действие — в начале карточки.";
      return tx(t, "blockedWaitingHelp", "Waiting for input, quota, or dependencies. Open the task for details.");
    }
    return tx(t, "columnHelp." + status, FALLBACK_COLUMN_HELP[status] || "");
  }
  function getDestructiveConfirm(t, status, count) {
    const key = DESTRUCTIVE_KEYS[status];
    if (!key) return null;
    // For bulk operations, use the *Many variant of the i18n key so the
    // copy pluralizes correctly ("Mark 3 tasks as done?" instead of
    // "Mark this task as done?"). Falls back to the singular English
    // string if a translation for the *Many key isn't shipped.
    if (count && count > 1) {
      const manyKey = key + "Many";
      const manyFallback = FALLBACK_DESTRUCTIVE_MANY[status] || FALLBACK_DESTRUCTIVE[status];
      return tx(t, manyKey, manyFallback, { n: count });
    }
    return tx(t, key, FALLBACK_DESTRUCTIVE[status]);
  }
  function getDiagnosticEventLabel(t, kind) {
    const key = DIAGNOSTIC_EVENT_KIND_KEYS[kind];
    if (!key) return null;
    return tx(t, key, FALLBACK_DIAGNOSTIC_EVENT_LABELS[kind]);
  }

  const COLUMN_DOT = {
    triage: "hermes-kanban-dot-triage",
    todo: "hermes-kanban-dot-todo",
    ready: "hermes-kanban-dot-ready",
    running: "hermes-kanban-dot-running",
    blocked: "hermes-kanban-dot-blocked",
    review: "hermes-kanban-dot-review",
    done: "hermes-kanban-dot-done",
    archived: "hermes-kanban-dot-archived",
  };

  function isDiagnosticEvent(kind) {
    return Object.prototype.hasOwnProperty.call(FALLBACK_DIAGNOSTIC_EVENT_LABELS, kind);
  }

  function phantomIdsFromEvent(ev) {
    if (!ev || !ev.payload) return [];
    const p = ev.payload;
    return p.phantom_cards || p.phantom_refs || [];
  }

  // Helpers for the dialog state machine used by `useKanbanDialogs` below.
  // The dialog API is Promise-based so call sites can preserve their
  // synchronous-ish flow: ``await kanbanDialogs.request(...)`` and then
  // continue with the optimistic UI + PATCH. See #50547.
  function dialogLabelForCount(count, t) {
    return count && count > 1 ? tx(t, "selectedTasks", "{n} selected tasks", { n: count }) : tx(t, "thisTask", "this task");
  }

  /**
   * Hook owning the kanban plugin's modal dialog state. Returns
   *   - `request(req)` — imperative API. Resolves to
   *     `{ confirmed: false }` if the user cancels, or
   *     `{ confirmed: true, summary?: string }` if they confirm.
   *   - `dialogState` — current dialog descriptor for rendering, or null.
   *   - `dialogProps` — onConfirm/onCancel handlers bound to the current
   *     request.
   *
   * `req` shapes:
   *   { kind: "confirm", title, description, confirmLabel, destructive }
   *
   * The "completion" kind (textarea prompt) is deferred: the host's
   * ConfirmDialog hardcodes onClick → unmount, preventing validation-
   * state retention. See KanbanDialogs doc comment.
   */
  function useKanbanDialogs(t) {
    const [dialogState, setDialogState] = React.useState(null);
    const resolverRef = React.useRef(null);

    const request = React.useCallback(function (req) {
      return new Promise(function (resolve) {
        resolverRef.current = resolve;
        setDialogState(req);
      });
    }, []);

    const close = React.useCallback(function (confirmed, extras) {
      const resolve = resolverRef.current;
      resolverRef.current = null;
      setDialogState(null);
      if (resolve) {
        resolve(Object.assign({ confirmed: confirmed }, extras || {}));
      }
    }, []);

    const onConfirm = React.useCallback(function (maybeSummary) {
      close(true, maybeSummary ? { summary: maybeSummary } : null);
    }, [close]);
    const onCancel = React.useCallback(function () { close(false, null); }, [close]);

    // Wrap the ConfirmDialog props so call sites can hand them straight
    // to <ConfirmDialog {...props} />. Title/description/confirmLabel are
    // sourced from the current dialog state. For "completion" the dialog
    // body (textarea + dual-validation) is rendered separately.
    const dialogProps = React.useMemo(function () {
      if (!dialogState) return null;
      return {
        open: true,
        title: dialogState.title || "",
        description: dialogState.description,
        confirmLabel: dialogState.confirmLabel || (dialogState.kind === "completion"
          ? tx(t, "confirm", "Confirm")
          : tx(t, "ok", "OK")),
        destructive: !!dialogState.destructive,
        onConfirm: function () { onConfirm(); },
        onCancel: onCancel,
      };
    }, [dialogState, t, onConfirm, onCancel]);

    return { dialogState: dialogState, dialogProps: dialogProps, request: request };
  }

  const API = "/api/plugins/kanban";
  const MIME_TASK = "text/x-hermes-task";

  // Docs link — surfaced as a `?` icon next to the board switcher and as
  // `title=` hints on unlabelled controls. Kept in one place so rebrands or
  // path changes are a single edit.
  const DOCS_URL = "https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban";
  const DOCS_TUTORIAL_URL = "https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban-tutorial";

  // localStorage key for the user's selected board. Independent of the
  // CLI's on-disk ``<root>/kanban/current`` pointer so browser users
  // can inspect any board without shifting the CLI's active board out
  // from under a terminal they left open.
  const LS_BOARD_KEY = "hermes.kanban.selectedBoard";

  function readSelectedBoard() {
    try {
      const v = window.localStorage.getItem(LS_BOARD_KEY);
      return (v || "").trim() || null;
    } catch (_e) { return null; }
  }

  function writeSelectedBoard(slug) {
    try {
      // Persist the user's dashboard-side board pin even for "default".
      // Previously this stripped "default" to keep localStorage empty,
      // but the fetch layer read that absence as "no opinion" and fell
      // through to the server-side ``current`` file — which the board
      // switcher also writes. Result: selecting the default tab after
      // creating a new board with "switch" checked showed the new
      // board's (wrong) data because the URL omitted ``?board=`` and
      // the backend happily returned whichever board was "current".
      // Persisting every selection keeps the dashboard's board opinion
      // independent of the CLI's active board, which was the original
      // design intent. Regression: #20879.
      if (slug) window.localStorage.setItem(LS_BOARD_KEY, slug);
      else window.localStorage.removeItem(LS_BOARD_KEY);
    } catch (_e) { /* ignore quota / private mode */ }
  }

  function withBoard(url, board) {
    // Always append ?board=<slug> when we have one picked — including
    // "default". Omitting the param would fall through to the backend's
    // resolution chain (env var → ``current`` file → default), which
    // means the dashboard's tab selection gets silently overridden by
    // whatever board the CLI or "switch" checkbox last activated.
    // Regression: #20879.
    if (!board) return url;
    const sep = url.indexOf("?") >= 0 ? "&" : "?";
    return `${url}${sep}board=${encodeURIComponent(board)}`;
  }

  // The SDK's Select component fires ``onValueChange(value)`` directly
  // (it's a shadcn-style popup, not a native <select>). Older plugin
  // code calls ``onChange({target: {value}})`` which silently never
  // fires. This helper wires both signatures so a setter works with
  // either API — use it as:
  //
  //   h(Select, {..., ...selectChangeHandler(setState), ...})
  function selectChangeHandler(setter) {
    return {
      onValueChange: function (v) { setter(v == null ? "" : v); },
      onChange: function (e) {
        const v = e && e.target ? e.target.value : e;
        setter(v == null ? "" : v);
      },
    };
  }

  // -------------------------------------------------------------------------
  // Minimal safe markdown renderer.
  //
  // Recognises a small subset (headings, bold, italic, inline code, fenced
  // code, links, bullet lists, paragraphs). HTML escaping first, then
  // inline replacements against the escaped string — no raw HTML from the
  // user is ever executed.
  // -------------------------------------------------------------------------

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
  function renderInline(esc) {
    // Fenced code has already been extracted before this runs; process
    // inline replacements on the escaped string.
    return esc
      // inline code
      .replace(/`([^`\n]+)`/g, (_m, c) => `<code>${c}</code>`)
      // bold
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      // italic
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      // safe links — only http(s) and mailto
      .replace(
        /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g,
        (_m, text, href) =>
          `<a href="${href}" target="_blank" rel="noopener noreferrer">${text}</a>`,
      );
  }
  function renderMarkdown(src) {
    if (!src) return "";
    // Split out fenced code blocks first so their contents aren't mangled.
    const blocks = [];
    let working = String(src).replace(/```([\s\S]*?)```/g, (_m, code) => {
      blocks.push(code);
      return `\u0000CODE${blocks.length - 1}\u0000`;
    });
    const escaped = escapeHtml(working);
    const lines = escaped.split(/\r?\n/);
    const out = [];
    let inList = false;
    for (const raw of lines) {
      const line = raw;
      const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
      const heading = /^(#{1,4})\s+(.*)$/.exec(line);
      if (bullet) {
        if (!inList) { out.push("<ul>"); inList = true; }
        out.push(`<li>${renderInline(bullet[1])}</li>`);
        continue;
      }
      if (inList) { out.push("</ul>"); inList = false; }
      if (heading) {
        const level = heading[1].length;
        out.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      } else if (line.trim() === "") {
        out.push("");
      } else {
        out.push(`<p>${renderInline(line)}</p>`);
      }
    }
    if (inList) out.push("</ul>");
    let html = out.join("\n");
    // Re-insert fenced code blocks.
    html = html.replace(/\u0000CODE(\d+)\u0000/g, (_m, i) =>
      `<pre class="hermes-kanban-md-code"><code>${escapeHtml(blocks[Number(i)])}</code></pre>`,
    );
    return html;
  }
  const MARKDOWN_ALLOWED_TAGS = new Set([
    "a",
    "code",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "li",
    "p",
    "pre",
    "strong",
    "ul",
  ]);
  function escapeAttribute(value) {
    return escapeHtml(value).replace(/`/g, "&#96;");
  }
  function sanitizeMarkdownAttrs(tag, attrs) {
    if (tag === "a") {
      const hrefMatch =
        /\shref=(["'])(.*?)\1/i.exec(attrs) ||
        /\shref=([^\s>]+)/i.exec(attrs);
      const href = hrefMatch ? (hrefMatch[2] || hrefMatch[1] || "").trim() : "";
      if (!/^(https?:\/\/|mailto:)/i.test(href)) return "";
      return ` href="${escapeAttribute(href)}" target="_blank" rel="noopener noreferrer"`;
    }
    if (tag === "pre" && /\sclass=(["'])hermes-kanban-md-code\1/i.test(attrs)) {
      return ' class="hermes-kanban-md-code"';
    }
    return "";
  }
  function sanitizeMarkdownHtml(html) {
    return String(html || "").replace(
      /<\/?([a-zA-Z][A-Za-z0-9-]*)([^>]*)>/g,
      (match, rawTag, attrs) => {
        const tag = rawTag.toLowerCase();
        if (!MARKDOWN_ALLOWED_TAGS.has(tag)) return "";
        if (/^<\s*\//.test(match)) return `</${tag}>`;
        return `<${tag}${sanitizeMarkdownAttrs(tag, attrs || "")}>`;
      },
    );
  }

  function MarkdownBlock(props) {
    const enabled = props.enabled !== false;
    if (!enabled) {
      return h("pre", { className: "hermes-kanban-pre" }, props.source || "");
    }
    return h("div", {
      className: "hermes-kanban-md",
      dangerouslySetInnerHTML: { __html: sanitizeMarkdownHtml(renderMarkdown(props.source || "")) },
    });
  }

  // -------------------------------------------------------------------------
  // Touch drag-drop helper.
  //
  // HTML5 DnD is desktop-only. On touch devices we attach a pointerdown
  // handler that simulates a drag proxy and fires a custom event on the
  // column under the finger when released. Columns listen for both the
  // standard `drop` event and our `hermes-kanban:drop` event.
  // -------------------------------------------------------------------------

  function attachTouchDrag(el, taskId) {
    if (!el) return;
    function onDown(e) {
      if (e.pointerType !== "touch") return;
      e.preventDefault();
      const proxy = el.cloneNode(true);
      proxy.classList.add("hermes-kanban-touch-proxy");
      document.body.appendChild(proxy);
      let lastTarget = null;

      function move(ev) {
        proxy.style.left = `${ev.clientX - proxy.offsetWidth / 2}px`;
        proxy.style.top = `${ev.clientY - 24}px`;
        proxy.style.display = "none";
        const under = document.elementFromPoint(ev.clientX, ev.clientY);
        proxy.style.display = "";
        const col = under && under.closest && under.closest("[data-kanban-column]");
        const trash = under && under.closest && under.closest("[data-kanban-trash]");
        const target = col || trash;
        if (target !== lastTarget) {
          if (lastTarget) lastTarget.classList.remove("hermes-kanban-column--drop");
          if (target) target.classList.add("hermes-kanban-column--drop");
          lastTarget = target;
        }
      }
      function up() {
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", up);
        document.removeEventListener("pointercancel", up);
        if (lastTarget) {
          lastTarget.classList.remove("hermes-kanban-column--drop");
          const status = lastTarget.getAttribute("data-kanban-column");
          const isTrash = lastTarget.hasAttribute("data-kanban-trash");
          if (isTrash) {
            lastTarget.dispatchEvent(new CustomEvent("hermes-kanban:delete", {
              detail: { taskId },
              bubbles: true,
            }));
          } else if (status) {
            lastTarget.dispatchEvent(new CustomEvent("hermes-kanban:drop", {
              detail: { taskId, status },
              bubbles: true,
            }));
          }
        }
        proxy.remove();
      }
      // Kick off proxy at the pointer origin.
      proxy.style.position = "fixed";
      proxy.style.pointerEvents = "none";
      proxy.style.opacity = "0.85";
      proxy.style.zIndex = "9999";
      proxy.style.width = `${el.offsetWidth}px`;
      proxy.style.left = `${e.clientX - el.offsetWidth / 2}px`;
      proxy.style.top = `${e.clientY - 24}px`;
      document.addEventListener("pointermove", move);
      document.addEventListener("pointerup", up);
      document.addEventListener("pointercancel", up);
    }
    el.addEventListener("pointerdown", onDown);
    return function () { el.removeEventListener("pointerdown", onDown); };
  }

  // -------------------------------------------------------------------------
  // Error boundary
  // -------------------------------------------------------------------------

  // Wrap the boundary's fallback in a tiny function component so we can
  // call useI18n() — class components can't use hooks directly.
  function ErrorBoundaryFallback(props) {
    const { t } = useI18n();
    return h(Card, null,
      h(CardContent, { className: "p-6 text-sm" },
        h("div", { className: "text-destructive font-semibold mb-1" },
          tx(t, "renderingError", "Kanban tab hit a rendering error")),
        h("div", { className: "text-muted-foreground text-xs mb-3" },
          props.message),
        h(Button, {
          onClick: props.onReset,
          size: "sm",
        }, tx(t, "reloadView", "Reload view")),
      ),
    );
  }

  class ErrorBoundary extends React.Component {
    constructor(props) { super(props); this.state = { error: null }; }
    static getDerivedStateFromError(error) { return { error }; }
    componentDidCatch(error, info) {
      // eslint-disable-next-line no-console
      console.error("Kanban plugin crashed:", error, info);
    }
    render() {
      if (this.state.error) {
        return h(ErrorBoundaryFallback, {
          message: String(this.state.error && this.state.error.message || this.state.error),
          onReset: () => this.setState({ error: null }),
        });
      }
      return this.props.children;
    }
  }

  // -------------------------------------------------------------------------
  // Dialog renderer
  // -------------------------------------------------------------------------

  /**
   * Single component that owns the kanban plugin's modal dialog UI. Renders
   * whichever dialog `useKanbanDialogs` is currently requesting, or nothing
   * if no dialog is open.
   *
   * Currently supports one dialog kind:
   *   - "confirm"  → standard ConfirmDialog (title + description + buttons)
   *
   * The "completion" kind (Mark Done → textarea prompt) is not yet wired
   * because the host's ConfirmDialog hardcodes `onClick → unmount`, which
   * prevents keeping the dialog open across a validation failure. See
   * issue #50547 followups. Completion summaries triggered from the
   * side-drawer use a documented carve-out (`withCompletionSummary` in
   * TaskDetail) until that lands.
   */
  function KanbanDialogs(props) {
    const { dialogProps, dialogState } = props;
    if (!dialogState || !dialogProps) return null;
    const ConfirmDialog = SDK.components.ConfirmDialog;
    if (!ConfirmDialog) return null;
    return h(ConfirmDialog, dialogProps);
  }

  // -------------------------------------------------------------------------
  // Root page
  // -------------------------------------------------------------------------

  // BEGIN operator-status.js
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
  // END operator-status.js

  function mapTeamTasks(envelope, nativeBoard) {
    const tasks = {};
    for (const request of (envelope && envelope.requests) || []) {
      // Controller cards are intentionally absent from public role assignments.
      // Bind them via their controller-owned idempotency key, never prompt text.
      for (const col of (nativeBoard && nativeBoard.columns) || []) {
        for (const root of col.tasks || []) {
          if (root.created_by === "hermes-team-controller" && root.assignee === "team-controller" &&
              String(root.idempotency_key || "").startsWith(request.id + ":root:")) {
            tasks[root.id] = { request_id: request.id, status: root.status,
              stage: request.stage, stage_label: request.stage_label, blocker: request.blocker,
              approval: request.approval, prerequisites: request.prerequisites || [] };
          }
        }
      }
      for (const task of request.tasks || []) {
        tasks[task.id] = Object.assign({}, task, { request_id: request.id,
          stage: request.stage, stage_label: request.stage_label, blocker: request.blocker,
          approval: request.approval, prerequisites: request.prerequisites || [] });
      }
    }
    return tasks;
  }

  function operatorColumns(columns, options) {
    const visible = columns.filter(function (col) {
      if (options.showAllColumns) return true;
      if ((col.name === "done" || col.name === "archived") && !options.showDone && !options.forceHistory) return false;
      return col.tasks.length > 0;
    });
    if (visible.length) return visible;
    // An empty board keeps an obvious place to create the first task.
    return columns.filter(function (col) { return col.name === "triage"; });
  }

  async function loadBlockedContexts(data, board, cache) {
    const blocked = data.columns.reduce(function (list, col) {
      return list.concat(col.tasks.filter(function (task) { return task.status === "blocked"; }));
    }, []);
    const contexts = {};
    const pending = [];
    for (const task of blocked) {
      const key = (board || "default") + ":" + task.id;
      const revision = JSON.stringify([task.status, task.comment_count, task.current_run_id,
        task.last_failure_error, task.consecutive_failures, task.block_kind, task.link_counts]);
      const old = cache.get(key);
      if (old && old.revision === revision && Date.now() - old.at < 60000) contexts[task.id] = old.context;
      else pending.push({ task, key, revision });
    }
    for (let offset = 0; offset < pending.length; offset += 4) {
      await Promise.all(pending.slice(offset, offset + 4).map(async function (item) {
        try {
          const detail = await SDK.fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(item.task.id)}`, board));
          // A status change in flight must not attach an old failure to a new run.
          if (!detail.task || detail.task.status !== item.task.status) return;
          const context = { links: detail.links, runs: detail.runs, events: detail.events,
            diagnostics: detail.task.diagnostics || [] };
          contexts[item.task.id] = context;
          cache.set(item.key, { revision: item.revision, at: Date.now(), context });
        } catch (_) { /* Missing diagnostics are explicit in operatorStatus. */ }
      }));
    }
    return contexts;
  }

  function KanbanPage() {
    const { t } = useI18n();
    const kanbanDialogs = useKanbanDialogs(t);
    const [board, setBoard] = useState(() => readSelectedBoard() || null);
    const [boardList, setBoardList] = useState([]);      // [{slug, name, counts, ...}]
    const [showNewBoard, setShowNewBoard] = useState(false);
    const [showBoardSettings, setShowBoardSettings] = useState(false);

    const [kanbanBoard, setKanbanBoard] = useState(null);  // the grid data
    // Alias so the rest of the function can keep using `board` semantically
    // for the grid data (card columns + tenants + assignees) without
    // colliding with the selected-board slug above. History: the old
    // component had `const [board, setBoard]` for the grid data. We
    // renamed the grid data to `kanbanBoard` so the more useful name
    // (`board`) belongs to the selected slug.
    const boardData = kanbanBoard;
    const setBoardData = setKanbanBoard;
    const [config, setConfig] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    const [tenantFilter, setTenantFilter] = useState("");
    const [assigneeFilter, setAssigneeFilter] = useState("");
    const [includeArchived, setIncludeArchived] = useState(false);
    const [search, setSearch] = useState("");
    const [laneByProfile, setLaneByProfile] = useState(false);
    const [showAllColumns, setShowAllColumns] = useState(false);
    const [showDone, setShowDone] = useState(false);
    const [showCreateTask, setShowCreateTask] = useState(false);
    const [teamTasks, setTeamTasks] = useState({});
    const [taskContexts, setTaskContexts] = useState({});
    const contextCacheRef = useRef(new Map());
    const loadVersionRef = useRef(0);
    const [configApplied, setConfigApplied] = useState(false);

    const [selectedTaskId, setSelectedTaskId] = useState(null);
    const [selectedIds, setSelectedIds] = useState(() => new Set());
    const [lastSelectedId, setLastSelectedId] = useState(null);
    const [failedIds, setFailedIds] = useState(() => new Set());
    const [draggingTaskId, setDraggingTaskId] = useState(null);
    const handleDragStart = useCallback(function (taskId) { setDraggingTaskId(taskId); }, []);
    const handleDragEnd = useCallback(function () { setDraggingTaskId(null); }, []);
    // Per-task event counter incremented whenever the WS stream reports
    // a new event for that task id. TaskDrawer useEffect-depends on its
    // own task's counter so it reloads itself on live events instead of
    // showing stale data.
    const [taskEventTick, setTaskEventTick] = useState({});

    const cursorRef = useRef(0);
    const reloadTimerRef = useRef(null);
    const wsRef = useRef(null);
    const wsBackoffRef = useRef(1000);
    const wsClosedRef = useRef(false);

    // --- load config once ---------------------------------------------------
    useEffect(function () {
      SDK.fetchJSON(withBoard(`${API}/config`, board))
        .then(function (c) {
          setConfig(c);
          if (!configApplied) {
            if (c.default_tenant) setTenantFilter(c.default_tenant);
            // Compact view is the default; profile grouping remains available in view options.
            if (typeof c.include_archived_by_default === "boolean") setIncludeArchived(c.include_archived_by_default);
            setConfigApplied(true);
          }
        })
        .catch(function () { setConfig({ render_markdown: true }); });
    }, []);  // eslint-disable-line react-hooks/exhaustive-deps

    // Board data and one team snapshot share a refresh. Details are fetched only
    // for waiting cards, bounded to four in flight and cached by native revision.
    const loadBoard = useCallback(() => {
      const version = ++loadVersionRef.current;
      const qs = new URLSearchParams();
      if (tenantFilter) qs.set("tenant", tenantFilter);
      if (includeArchived) qs.set("include_archived", "true");
      const url = qs.toString() ? `${API}/board?${qs}` : `${API}/board`;
      return Promise.all([
        SDK.fetchJSON(withBoard(url, board)),
        SDK.fetchJSON("/api/plugins/engineering-team/requests").catch(function () { return null; }),
      ]).then(function (results) {
        if (loadVersionRef.current !== version) return;
        const data = results[0];
        setBoardData(data);
        setTeamTasks(mapTeamTasks(results[1], data));
        cursorRef.current = data.latest_event_id || 0;
        setError(null);
        loadBlockedContexts(data, board, contextCacheRef.current).then(function (contexts) {
          if (loadVersionRef.current === version) setTaskContexts(contexts);
        });
      }).catch(function (err) {
        if (loadVersionRef.current === version) setError(String(err && err.message ? err.message : err));
      }).finally(function () {
        if (loadVersionRef.current === version) setLoading(false);
      });
    }, [tenantFilter, includeArchived, board]);

    useEffect(function () {
      const timer = window.setInterval(function () {
        if (!document.hidden) loadBoard();
      }, 30000);
      return function () { window.clearInterval(timer); };
    }, [loadBoard]);

    // --- load list of boards for the switcher ------------------------------
    const loadBoardList = useCallback(function () {
      return SDK.fetchJSON(withBoard(`${API}/boards`, board))
        .then(function (data) {
          const boards = (data && data.boards) || [];
          const storedBoard = readSelectedBoard();
          setBoardList(boards);
          if (!storedBoard && !board && data && data.current) {
            setBoard(data.current);
            return;
          }
          // If the stored slug isn't in the list any longer (board was
          // deleted in the CLI while dashboard was open), fall back to
          // default so the UI doesn't hang on a 404.
          if (board && board !== "default" && !boards.find(function (b) { return b.slug === board; })) {
            setBoard("default");
            writeSelectedBoard("default");
          }
        })
        .catch(function () { /* non-fatal */ });
    }, [board]);

    useEffect(function () { loadBoardList(); }, [loadBoardList]);

    const scheduleReload = useCallback(function () {
      if (reloadTimerRef.current) return;
      reloadTimerRef.current = setTimeout(function () {
        reloadTimerRef.current = null;
        loadBoard();
      }, 250);
    }, [loadBoard]);

    useEffect(function () {
      loadBoard();
      return function () {
        if (reloadTimerRef.current) {
          clearTimeout(reloadTimerRef.current);
          reloadTimerRef.current = null;
        }
      };
    }, [loadBoard]);

    // --- WebSocket ---------------------------------------------------------
    useEffect(function () {
      if (!boardData) return undefined;
      wsClosedRef.current = false;
      function openWs() {
        if (wsClosedRef.current) return;
        // Build the WS URL via the host SDK so the correct auth param is used
        // in BOTH modes: single-use ?ticket= in gated OAuth mode, ?token= in
        // loopback. Reading window.__HERMES_SESSION_TOKEN__ directly (the old
        // path) sends an empty token and is rejected in gated mode. buildWsUrl
        // also applies the dashboard base-path prefix for reverse-proxied
        // deployments, which the old inline URL did not. It's async (gated
        // mode mints a fresh ticket per connect), so resolve then open.
        const wsParams = { since: String(cursorRef.current || 0) };
        // Pin the WS stream to the currently-selected board so events
        // from other boards don't bleed in. Includes "default" so the
        // dashboard's own board pin always wins over the server-side
        // ``current`` file — same rationale as ``withBoard()`` above.
        // Regression: #20879.
        if (board) wsParams.board = board;
        SDK.buildWsUrl(`${API}/events`, wsParams).then(function (url) {
          if (wsClosedRef.current) return;
          let ws;
          try { ws = new WebSocket(url); } catch (_e) { return; }
          wsRef.current = ws;
          ws.onopen = function () { wsBackoffRef.current = 1000; };
          ws.onmessage = function (ev) {
            try {
              const msg = JSON.parse(ev.data);
              if (msg && Array.isArray(msg.events) && msg.events.length > 0) {
                cursorRef.current = msg.cursor || cursorRef.current;
                // Stamp per-task signal so the TaskDrawer can reload itself.
                setTaskEventTick(function (prev) {
                  const next = Object.assign({}, prev);
                  for (const e of msg.events) {
                    if (e && e.task_id) {
                      next[e.task_id] = (next[e.task_id] || 0) + 1;
                      contextCacheRef.current.delete((board || "default") + ":" + e.task_id);
                    }
                  }
                  return next;
                });
                scheduleReload();
              }
            } catch (_e) { /* ignore */ }
          };
          ws.onclose = function (ev) {
            if (wsClosedRef.current) return;
            if (ev && ev.code === 1008) {
              setError(tx(t, "wsAuthFailed",
                "WebSocket auth failed — reload the page to refresh the session token."));
              return;
            }
            const delay = Math.min(wsBackoffRef.current, 30000);
            wsBackoffRef.current = Math.min(wsBackoffRef.current * 2, 30000);
            setTimeout(openWs, delay);
          };
        }).catch(function () {
          // Ticket mint / URL build failed (e.g. session expired). Back off
          // and retry; a hard auth failure surfaces via the 1008 close path.
          if (wsClosedRef.current) return;
          const delay = Math.min(wsBackoffRef.current, 30000);
          wsBackoffRef.current = Math.min(wsBackoffRef.current * 2, 30000);
          setTimeout(openWs, delay);
        });
      }
      openWs();
      return function () {
        wsClosedRef.current = true;
        try { wsRef.current && wsRef.current.close(); } catch (_e) { /* noop */ }
      };
    }, [!!boardData, board, scheduleReload]);

    // Keep operator context separate from persisted task data.
    const enrichedBoard = useMemo(function () {
      if (!boardData) return null;
      return Object.assign({}, boardData, { columns: boardData.columns.map(function (col) {
        return Object.assign({}, col, { tasks: col.tasks.map(function (task) {
          return Object.assign({}, task, { _team: teamTasks[task.id] || null, _operatorContext: taskContexts[task.id] || {} });
        }) });
      }) });
    }, [boardData, teamTasks, taskContexts]);

    // --- filtering ----------------------------------------------------------
    const filteredBoard = useMemo(function () {
      if (!enrichedBoard) return null;
      const q = search.trim().toLowerCase();
      const filterTask = function (t) {
        if (tenantFilter && t.tenant !== tenantFilter) return false;
        if (assigneeFilter && t.assignee !== assigneeFilter) return false;
        if (q) {
          const hay = `${t.id} ${t.title || ""} ${t.body || ""} ${t.result || ""} ${t.latest_summary || ""} ${t.assignee || ""} ${t.tenant || ""}`.toLowerCase();
          if (hay.indexOf(q) === -1) return false;
        }
        return true;
      };
      return Object.assign({}, enrichedBoard, {
        columns: enrichedBoard.columns.map(function (col) {
          return Object.assign({}, col, { tasks: col.tasks.filter(filterTask) });
        }),
      });
    }, [enrichedBoard, tenantFilter, assigneeFilter, search]);

    // --- actions ------------------------------------------------------------
    // Performs the actual move (optimistic UI + PATCH) once any required
    // confirmation and/or completion summary has been collected by the
    // caller. Extracted so moveTask / moveSelected / applyBulk can all
    // share the same dispatch path regardless of how confirmation was
    // collected (synchronous window.confirm in the original code, async
    // dialog via useKanbanDialogs now).
    //   taskId  — required when count <= 1 (single-task PATCH endpoint)
    //           — ignored when count >  1 (bulk endpoint uses selectedIds)
    //   summary — completion summary string, or null/undefined to skip
    const performMoveTask = useCallback(function (taskId, newStatus, count, summary) {
      const patch = { status: newStatus };
      const finalPatch = summary
        ? Object.assign({}, patch, { result: summary, summary: summary })
        : patch;
      if (count > 1) {
        // Bulk path: optimistic UI prepends all moved tasks to dest column.
        setBoardData(function (b) {
          if (!b) return b;
          const moved = [];
          const columns = b.columns.map(function (col) {
            const kept = [];
            for (const tk of col.tasks) {
              if (selectedIds.has(tk.id)) moved.push(Object.assign({}, tk, { status: newStatus }));
              else kept.push(tk);
            }
            return Object.assign({}, col, { tasks: kept });
          });
          const dest = columns.find(function (c) { return c.name === newStatus; });
          if (dest) dest.tasks = moved.concat(dest.tasks);
          return Object.assign({}, b, { columns });
        });
        const ids = Array.from(selectedIds);
        SDK.fetchJSON(withBoard(`${API}/tasks/bulk`, board), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(Object.assign({ ids: ids }, finalPatch)),
        }).then(function (res) {
          const failed = (res.results || []).filter(function (r) { return !r.ok; });
          if (failed.length > 0) {
            setError(`Bulk move: ${failed.length} of ${res.results.length} failed`);
            setFailedIds(new Set(failed.map(function (f) { return f.id; })));
          } else {
            setFailedIds(new Set());
          }
          setSelectedIds(new Set());
          setLastSelectedId(null);
          loadBoard();
        }).catch(function (err) {
          setError(`Move failed: ${err.message || err}`);
          setFailedIds(new Set(selectedIds));
          loadBoard();
        });
        return;
      }
      // Single-task path.
      setBoardData(function (b) {
        if (!b) return b;
        let moved = null;
        const columns = b.columns.map(function (col) {
          const next = col.tasks.filter(function (tk) {
            if (tk.id === taskId) { moved = Object.assign({}, tk, { status: newStatus }); return false; }
            return true;
          });
          return Object.assign({}, col, { tasks: next });
        });
        if (moved) {
          const dest = columns.find(function (c) { return c.name === newStatus; });
          if (dest) dest.tasks = [moved].concat(dest.tasks);
        }
        return Object.assign({}, b, { columns });
      });
      SDK.fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(taskId)}`, board), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(finalPatch),
      }).catch(function (err) {
        setError(tx(t, "moveFailed", "Move failed: ") + parseApiErrorMessage(err));
        loadBoard();
      });
    }, [loadBoard, board, t, selectedIds]);

    // Pre-dispatch dialog step for both moveTask and moveSelected. Drives
    // the new in-app ConfirmDialog instead of window.confirm. The flow:
    //   1. If newStatus is destructive (done/archived/blocked), open
    //      a confirm dialog.
    //   2. If newStatus is "done", additionally open a completion-summary
    //      dialog (chained via Promise).
    //   3. On confirm of all steps, call performMoveTask.
    //   4. On cancel anywhere, do nothing.
    const requestMoveConfirm = useCallback(function (newStatus, count) {
      const confirmMsg = getDestructiveConfirm(t, newStatus, count);
      if (!confirmMsg) return Promise.resolve({ confirmed: true });
      return kanbanDialogs.request({
        kind: "confirm",
        title: tx(t, "confirmStatusTitle." + newStatus, "Confirm status change"),
        description: confirmMsg,
        confirmLabel: tx(t, "confirmStatusLabel." + newStatus, "Confirm"),
        destructive: true,
      });
    }, [kanbanDialogs, t]);

    const requestCompletionSummary = useCallback(function (count) {
      const label = dialogLabelForCount(count, t);
      // Uses window.prompt as a documented carve-out — the host's
      // ConfirmDialog hardcodes onClick → unmount (confirmedRef + Radix
      // AlertDialogAction), making it impossible to keep a dialog open
      // across a validation failure. Once ConfirmDialog grows a
      // disabled prop upstream, this switches to a Dialog-based
      // completion body (see KanbanDialogs doc comment).
      var summary = window.prompt(
        tx(t, "completionSummary",
          "Completion summary for {label}. This is stored as the task result.",
          { label: label }),
        "",
      );
      if (summary === null) return Promise.resolve({ confirmed: false });
      summary = summary.trim();
      if (!summary) {
        window.alert(tx(t, "completionSummaryRequired",
          "Completion summary is required before marking a task done."));
        return Promise.resolve({ confirmed: false });
      }
      return Promise.resolve({ confirmed: true, summary: summary });
    }, [t]);

    // Single-task card move. Drives confirmation + completion summary
    // dialogs via the hook, then dispatches via performMoveTask.
    const moveTask = useCallback(function (taskId, newStatus) {
      requestMoveConfirm(newStatus, 1)
        .then(function (r1) {
          if (!r1.confirmed) return null;
          if (newStatus !== "done") {
            performMoveTask(taskId, newStatus, 1, null);
            return null;
          }
          return requestCompletionSummary(1).then(function (r2) {
            if (!r2.confirmed) return null;
            performMoveTask(taskId, newStatus, 1, r2.summary || null);
          });
        })
        .catch(function () { /* dialog cancelled */ });
    }, [requestMoveConfirm, requestCompletionSummary, performMoveTask]);

    const clearSelected = useCallback(function () {
      setSelectedIds(new Set());
      setLastSelectedId(null);
      setFailedIds(new Set());
    }, []);
    const moveSelected = useCallback(function (newStatus) {
      if (selectedIds.size === 0) return;
      const count = selectedIds.size;
      const taskId = Array.from(selectedIds)[0]; // representative id for performMoveTask's single-task branch
      requestMoveConfirm(newStatus, count)
        .then(function (r1) {
          if (!r1.confirmed) return null;
          if (newStatus !== "done") {
            performMoveTask(taskId, newStatus, count, null);
            return null;
          }
          return requestCompletionSummary(count).then(function (r2) {
            if (!r2.confirmed) return null;
            performMoveTask(taskId, newStatus, count, r2.summary || null);
          });
        })
        .catch(function () { /* dialog cancelled */ });
    }, [selectedIds, requestMoveConfirm, requestCompletionSummary, performMoveTask]);

    const createTask = useCallback(function (body) {
      return SDK.fetchJSON(withBoard(`${API}/tasks`, board), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then(function (res) {
        // Surface dispatcher-presence warnings (e.g. "no gateway is
        // running") via the existing error banner channel. Not fatal —
        // the task was created successfully — but the user should know
        // their ready task will sit idle until the gateway is up.
        if (res && res.warning) {
          setError(tx(t, "taskCreatedWarning", "Task created, but: ") + res.warning);
        }
        loadBoard();
        loadBoardList();  // refresh counts in the switcher
        return res;
      });
    }, [loadBoard, loadBoardList, board, t]);

    const toggleSelected = useCallback(function (id, additive) {
      setSelectedIds(function (prev) {
        const next = new Set(additive ? prev : []);
        if (prev.has(id)) next.delete(id);
        else next.add(id);
        return next;
      });
      setLastSelectedId(id);
      setFailedIds(function (prev) {
        if (prev.has(id)) {
          const next = new Set(prev);
          next.delete(id);
          return next;
        }
        return prev;
      });
    }, []);

    const toggleRange = useCallback(function (toId) {
      // Build flat visible task order from filteredBoard columns.
      setSelectedIds(function (prev) {
        const next = new Set(prev);
        if (!filteredBoard || !filteredBoard.columns) return next;
        const order = [];
        for (const col of filteredBoard.columns) {
          for (const t of col.tasks || []) order.push(t.id);
        }
        const anchor = lastSelectedId;
        if (!anchor || anchor === toId) {
          next.add(toId);
          return next;
        }
        const aIdx = order.indexOf(anchor);
        const bIdx = order.indexOf(toId);
        if (aIdx === -1 || bIdx === -1) {
          next.add(toId);
          return next;
        }
        const lo = Math.min(aIdx, bIdx);
        const hi = Math.max(aIdx, bIdx);
        for (let i = lo; i <= hi; i++) next.add(order[i]);
        return next;
      });
      setLastSelectedId(toId);
    }, [filteredBoard, lastSelectedId]);

    const selectAllVisible = useCallback(function () {
      if (!filteredBoard || !filteredBoard.columns) return;
      const next = new Set();
      for (const col of filteredBoard.columns) {
        for (const t of col.tasks || []) next.add(t.id);
      }
      setSelectedIds(next);
      if (next.size > 0) {
        const first = Array.from(next)[0];
        setLastSelectedId(first);
      }
    }, [filteredBoard]);

    const selectAllInColumn = useCallback(function (columnName) {
      if (!filteredBoard || !filteredBoard.columns) return;
      const col = filteredBoard.columns.find(function (c) { return c.name === columnName; });
      if (!col) return;
      const allSelected = col.tasks && col.tasks.length > 0 && col.tasks.every(function (t) { return selectedIds.has(t.id); });
      const next = new Set(selectedIds);
      if (allSelected) {
        for (const t of col.tasks || []) next.delete(t.id);
      } else {
        for (const t of col.tasks || []) next.add(t.id);
      }
      setSelectedIds(next);
      if (col.tasks && col.tasks.length > 0) setLastSelectedId(col.tasks[0].id);
    }, [filteredBoard, selectedIds]);

    const applyBulk = useCallback(function (patch, confirmMsg) {
      if (selectedIds.size === 0) return;
      const count = selectedIds.size;
      const run = function () {
        const finalPatch = patch;
        const body = Object.assign({ ids: Array.from(selectedIds) }, finalPatch);
        // Optimistic UI for status moves (same pattern as moveSelected).
        if (finalPatch.status) {
          setBoardData(function (b) {
            if (!b) return b;
            const moved = [];
            const columns = b.columns.map(function (col) {
              const kept = [];
              for (const t of col.tasks) {
                if (selectedIds.has(t.id)) moved.push(Object.assign({}, t, { status: finalPatch.status }));
                else kept.push(t);
              }
              return Object.assign({}, col, { tasks: kept });
            });
            const dest = columns.find(function (c) { return c.name === finalPatch.status; });
            if (dest) dest.tasks = moved.concat(dest.tasks);
            return Object.assign({}, b, { columns });
          });
        }
        SDK.fetchJSON(withBoard(`${API}/tasks/bulk`, board), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        })
          .then(function (res) {
            const failed = (res.results || []).filter(function (r) { return !r.ok; });
            if (failed.length > 0) {
              setError(tx(t, "bulkFailed", "Bulk: ") +
                `${failed.length} of ${res.results.length} failed: ` +
                failed.slice(0, 3).map(function (f) { return `${f.id} (${f.error})`; }).join("; "));
              setFailedIds(new Set(failed.map(function (f) { return f.id; })));
            } else {
              setFailedIds(new Set());
            }
            setSelectedIds(new Set());
            setLastSelectedId(null);
            loadBoard();
          })
          .catch(function (e) {
            setError(String(e.message || e));
            setFailedIds(new Set(selectedIds));
            loadBoard();
          });
      };
      if (!confirmMsg) {
        run();
        return;
      }
      kanbanDialogs.request({
        kind: "confirm",
        title: tx(t, "bulkConfirmTitle", "Apply bulk change"),
        description: confirmMsg,
        confirmLabel: tx(t, "apply", "Apply"),
        destructive: false,
      }).then(function (r) {
        if (r.confirmed) run();
      }).catch(function () { /* cancelled */ });
    }, [selectedIds, loadBoard, board, t, kanbanDialogs]);

    // --- board switching ----------------------------------------------------
    const switchBoard = useCallback(function (nextSlug) {
      if (!nextSlug || nextSlug === board) return;
      // Optimistic UI: clear the current grid + show loading, reset the
      // event cursor so the WS reopens aligned to the new board's
      // latest_event_id on the next loadBoard.
      ++loadVersionRef.current;
      setBoardData(null);
      setTeamTasks({});
      setTaskContexts({});
      cursorRef.current = 0;
      setLoading(true);
      setBoard(nextSlug);
      writeSelectedBoard(nextSlug);
      // Reset filters so stale search/tenant/assignee don't persist across boards.
      setSearch("");
      setTenantFilter("");
      setAssigneeFilter("");
      setIncludeArchived(false);
      clearSelected();
    }, [board, clearSelected]);

    const createNewBoard = useCallback(function (payload) {
      return SDK.fetchJSON(`${API}/boards`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).then(function (res) {
        loadBoardList();
        const slug = res && res.board && res.board.slug;
        if (slug && payload.switch) switchBoard(slug);
        return res;
      });
    }, [loadBoardList, switchBoard, board]);

    // PATCH board metadata (name / description / default project directory).
    // Refreshes the board list so InlineCreate's workspace defaults pick up
    // the new default_workdir immediately.
    const updateBoard = useCallback(function (slug, payload) {
      return SDK.fetchJSON(`${API}/boards/${encodeURIComponent(slug)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).then(function (res) {
        loadBoardList();
        return res;
      });
    }, [loadBoardList]);

    const deleteBoard = useCallback(function (slug) {
      if (!slug || slug === "default") return Promise.resolve();
      return SDK.fetchJSON(`${API}/boards/${encodeURIComponent(slug)}`, {
        method: "DELETE",
      }).then(function () {
        loadBoardList();
        if (board === slug) switchBoard("default");
      });
    }, [board, loadBoardList, switchBoard]);

   const deleteTask = useCallback(function (taskId) {
     return kanbanDialogs.request({
       kind: "confirm",
       title: tx(t, "trash.confirmTitle", "Delete task?"),
       description: tx(t, "trash.confirm", FALLBACK_TRASH.confirm),
       confirmLabel: tx(t, "common.delete", "Delete"),
       destructive: true,
     }).then(function (r) {
       if (!r.confirmed) return null;
       return SDK.fetchJSON(`${API}/tasks/${encodeURIComponent(taskId)}`, {
         method: "DELETE",
       }).then(function () {
         loadBoard();
         setSelectedIds(function (prev) {
           const next = new Set(prev);
           next.delete(taskId);
           return next;
         });
       }).catch(function (e) { setError(String(e.message || e)); });
     }).catch(function () { /* cancelled */ });
   }, [board, loadBoard, t, kanbanDialogs]);

    const deleteSelected = useCallback(function (count) {
      if (selectedIds.size === 0) return Promise.resolve();
      kanbanDialogs.request({
        kind: "confirm",
        title: tx(t, "trash.confirmManyTitle", "Delete {n} tasks?", { n: count }),
        description: tx(t, "trash.confirmMany", "Permanently delete {n} selected tasks? This cannot be undone.", { n: count }),
        confirmLabel: tx(t, "common.delete", "Delete"),
        destructive: true,
      }).then(function (r) {
        if (!r.confirmed) return null;
        const ids = Array.from(selectedIds);
        setSelectedIds(new Set());
        return Promise.all(ids.map(function (id) {
          return SDK.fetchJSON(`${API}/tasks/${encodeURIComponent(id)}`, { method: "DELETE" });
        })).then(function () {
          loadBoard();
        }).catch(function (e) { setError(String(e.message || e)); });
      }).catch(function () { /* cancelled */ });
    }, [selectedIds, board, loadBoard, t, kanbanDialogs]);

    // --- render -------------------------------------------------------------
    if (loading && !boardData) {
      return h("div", { className: "p-8 text-sm text-muted-foreground" },
        tx(t, "loading", "Loading Kanban board…"));
    }
    if (error && !boardData) {
      return h(Card, null,
        h(CardContent, { className: "p-6" },
          h("div", { className: "text-sm text-destructive" },
            tx(t, "loadFailed", "Failed to load Kanban board: "), error),
          h("div", { className: "text-xs text-muted-foreground mt-2" },
            tx(t, "loadFailedHint",
              "The backend auto-creates kanban.db on first read. If this persists, check the dashboard logs.")),
        ),
      );
    }
    if (!filteredBoard) return null;

    const renderMd = !config || config.render_markdown !== false;
    const allTasks = enrichedBoard.columns.reduce(function (acc, c) { return acc.concat(c.tasks); }, []);
    const displayBoard = Object.assign({}, filteredBoard, { columns: operatorColumns(filteredBoard.columns, {
      showAllColumns, showDone, forceHistory: !!(search.trim() || tenantFilter || assigneeFilter || includeArchived),
    }) });
    const visibleCount = displayBoard.columns.reduce(function (n, c) { return n + c.tasks.length; }, 0);
    const filteredCount = filteredBoard.columns.reduce(function (n, c) { return n + c.tasks.length; }, 0);
    const doneCount = filteredBoard.columns.filter(function (c) { return c.name === "done" || c.name === "archived"; })
      .reduce(function (n, c) { return n + c.tasks.length; }, 0);

    return h(ErrorBoundary, null,
      h("div", { className: "hermes-kanban flex flex-col gap-4" },
        h(BoardSwitcher, {
          board: board,
          boardList: boardList,
          totalTasks: allTasks.length,
          onSwitch: switchBoard,
          onNewClick: function () { setShowNewBoard(true); },
          onSettingsClick: function () { setShowBoardSettings(true); },
          onDeleteBoard: deleteBoard,
          requestDialog: function (req) { return kanbanDialogs.request(req); },
        }),
        showNewBoard ? h(NewBoardDialog, {
          onCancel: function () { setShowNewBoard(false); },
          onCreate: function (payload) {
            return createNewBoard(payload).then(function () { setShowNewBoard(false); });
          },
        }) : null,
        showBoardSettings ? h(BoardSettingsDialog, {
          board: boardList.find(function (item) { return item.slug === board; })
            || { slug: board },
          onCancel: function () { setShowBoardSettings(false); },
          onSave: function (payload) {
            return updateBoard(board, payload).then(function () { setShowBoardSettings(false); });
          },
        }) : null,
        h(OrchestrationPanel, null),
        h(AttentionStrip, {
          boardData,
          onOpen: setSelectedTaskId,
        }),
        h(BoardToolbar, {
          board: boardData,
          tenantFilter, setTenantFilter,
          assigneeFilter, setAssigneeFilter,
          includeArchived, setIncludeArchived,
          laneByProfile, setLaneByProfile,
          showAllColumns, setShowAllColumns, showDone, setShowDone, doneCount,
          onNewTask: function () { setShowCreateTask(true); },
          search, setSearch,
          onNudgeDispatch: function () {
            SDK.fetchJSON(withBoard(`${API}/dispatch?max=8`, board), { method: "POST" })
              .then(loadBoard)
              .catch(function (e) { setError(String(e.message || e)); });
          },
          onRefresh: function () { contextCacheRef.current.clear(); loadBoard(); },
        }),
        h("p", { className: "hermes-kanban-view-hint", role: "status" },
          `Показано ${visibleCount} из ${filteredCount} задач. `,
          visibleCount < filteredCount ? "Завершённые — в истории. " : "",
          "Нажмите на карточку — причина ожидания и следующее действие будут в начале."),
        showCreateTask ? h(InlineCreate, {
          columnName: "triage", allTasks,
          defaultWorkspaceKind: (boardList.find(function (item) { return item.slug === board; }) || {}).default_workspace_kind || "scratch",
          defaultWorkspacePath: (boardList.find(function (item) { return item.slug === board; }) || {}).default_workdir || "",
          onSubmit: function (body) { return createTask(body).then(function () { setShowCreateTask(false); }); },
          onCancel: function () { setShowCreateTask(false); },
        }) : null,
       selectedIds.size > 0 ? h(BulkActionBar, {
         count: selectedIds.size,
         assignees: (boardData && boardData.assignees) || [],
         onApply: applyBulk,
         onClear: clearSelected,
         onSelectAllVisible: selectAllVisible,
         onDelete: deleteSelected,
       }) : null,
        error ? h("div", { className: "text-xs text-destructive px-2" }, error) : null,
        h(KanbanDialogs, {
          dialogProps: kanbanDialogs.dialogProps,
          dialogState: kanbanDialogs.dialogState,
        }),
        h(BoardColumns, {
          board: displayBoard,
          boardMeta: boardList.find(function (item) { return item.slug === board; }) || null,
          laneByProfile,
          selectedIds,
          failedIds,
          draggingTaskId,
          onDragStart: handleDragStart,
          onDragEnd: handleDragEnd,
          toggleSelected,
          toggleRange,
          selectAllInColumn,
          onMove: moveTask,
          onMoveSelected: moveSelected,
          onDelete: deleteTask,
          onDeleteSelected: deleteSelected,
          onOpen: setSelectedTaskId,
          onCreate: createTask,
          allTasks: allTasks,
        }),
        selectedTaskId ? h(TaskDrawer, {
          taskId: selectedTaskId,
          boardSlug: board,
          onClose: function () { setSelectedTaskId(null); },
          onOpenTask: setSelectedTaskId,
          onRefresh: loadBoard,
          renderMarkdown: renderMd,
          allTasks: allTasks,
          assignees: (boardData && boardData.assignees) || [],
          eventTick: taskEventTick[selectedTaskId] || 0,
          teamTask: teamTasks[selectedTaskId] || null,
          // Hook for the side-drawer's doPatch to use the same in-app
          // dialog machinery as the column-card flow. TaskDetail also
          // owns its own kanbanDialogs so the dialog portal mounts in
          // its tree; we expose requestDialog as the imperative API.
          requestDialog: function (req) { return kanbanDialogs.request(req); },
        }) : null,
      ),
    );
  }

  // -------------------------------------------------------------------------
  // Attention strip — surfaces every task with active diagnostics,
  // severity-marked (warning/error/critical). Collapsed by default; click
  // Show to expand into per-task rows with Open buttons. Dismissible
  // per session via state flag.
  // -------------------------------------------------------------------------

  function collectDiagTasks(boardData) {
    if (!boardData || !boardData.columns) return [];
    const out = [];
    for (const col of boardData.columns) {
      for (const t of col.tasks || []) {
        if (t.diagnostics && t.diagnostics.length > 0) out.push(t);
        else if (t.warnings && t.warnings.count > 0) out.push(t);
      }
    }
    // Sort: highest severity first (critical > error > warning), then by
    // most recent latest_at.
    const sevIdx = function (s) {
      if (s === "critical") return 3;
      if (s === "error") return 2;
      if (s === "warning") return 1;
      return 0;
    };
    out.sort(function (a, b) {
      const aSev = sevIdx((a.warnings && a.warnings.highest_severity) || "warning");
      const bSev = sevIdx((b.warnings && b.warnings.highest_severity) || "warning");
      if (aSev !== bSev) return bSev - aSev;
      const aLa = (a.warnings && a.warnings.latest_at) || 0;
      const bLa = (b.warnings && b.warnings.latest_at) || 0;
      return bLa - aLa;
    });
    return out;
  }

  function AttentionStrip(props) {
    const { t, uiText } = useI18n();
    const [expanded, setExpanded] = useState(false);
    const [dismissed, setDismissed] = useState(false);
    const diagTasks = useMemo(
      function () { return collectDiagTasks(props.boardData); },
      [props.boardData]
    );
    if (dismissed || diagTasks.length === 0) return null;
    // Pick the highest severity present so we can colour the strip.
    let topSev = "warning";
    for (const td of diagTasks) {
      const s = (td.warnings && td.warnings.highest_severity) || "warning";
      if (s === "critical") { topSev = "critical"; break; }
      if (s === "error" && topSev !== "critical") topSev = "error";
    }
    return h("div", {
      className: cn(
        "hermes-kanban-attention",
        "hermes-kanban-attention--" + topSev,
      ),
    },
      h("div", { className: "hermes-kanban-attention-bar" },
        h("span", { className: "hermes-kanban-attention-icon" },
          topSev === "critical" ? "!!!" : topSev === "error" ? "!!" : "⚠"),
        h("span", { className: "hermes-kanban-attention-text" },
          diagTasks.length === 1
            ? tx(t, "taskNeedsAttention", "1 task needs attention")
            : tx(t, "tasksNeedAttention", "{n} tasks need attention",
                { n: diagTasks.length }),
        ),
        h("button", {
          className: "hermes-kanban-attention-toggle",
          onClick: function () { setExpanded(function (x) { return !x; }); },
          type: "button",
        }, expanded ? tx(t, "hide", "Hide") : tx(t, "show", "Show")),
        h("button", {
          className: "hermes-kanban-attention-dismiss",
          onClick: function () { setDismissed(true); },
          title: uiText("Hide until next page reload", "Скрыть до следующей перезагрузки страницы"),
          type: "button",
        }, "\u2715"),
      ),
      expanded
        ? h("div", { className: "hermes-kanban-attention-list" },
            diagTasks.map(function (task) {
              const sev = (task.warnings && task.warnings.highest_severity) || "warning";
              const kinds = task.warnings && task.warnings.kinds ? Object.keys(task.warnings.kinds) : [];
              return h("div", {
                key: task.id,
                className: cn(
                  "hermes-kanban-attention-row",
                  "hermes-kanban-attention-row--" + sev,
                ),
              },
                h("span", { className: "hermes-kanban-attention-row-sev" },
                  sev === "critical" ? "!!!" : sev === "error" ? "!!" : "⚠"),
                h("span", { className: "hermes-kanban-attention-row-id" }, task.id),
                h("span", { className: "hermes-kanban-attention-row-title" },
                  task.title || tx(t, "untitled", "(untitled)")),
                h("span", { className: "hermes-kanban-attention-row-meta" },
                  task.assignee ? "@" + task.assignee : tx(t, "unassigned", "unassigned"),
                  " \u00b7 ",
                  kinds.length > 0 ? kinds.join(", ") : tx(t, "diagnostic", "diagnostic"),
                ),
                h("button", {
                  className: "hermes-kanban-attention-row-btn",
                  onClick: function () { props.onOpen(task.id); },
                  type: "button",
                }, tx(t, "open", "Open")),
              );
            }),
          )
        : null,
    );
  }

  // -------------------------------------------------------------------------
  // Diagnostics section — generic renderer for a task's active distress
  // signals. Each diagnostic carries its own title, detail, data payload,
  // and a list of structured actions; the section renders them uniformly
  // regardless of kind. Replaces the hallucination-specific
  // ``RecoveryPopover`` from the previous iteration.
  //
  // Action kinds supported today:
  //   reclaim   → POST /tasks/:id/reclaim
  //   reassign  → POST /tasks/:id/reassign (with profile picker)
  //   unblock   → PATCH /tasks/:id  body: {status: "ready"}
  //   comment   → scroll to the comment input at the bottom of the drawer
  //   cli_hint  → copy payload.command to clipboard
  //   open_docs → open payload.url in a new tab
  // Unknown kinds are rendered as a disabled informational row so the
  // server can add new action kinds without breaking the UI.
  // -------------------------------------------------------------------------

  function DiagnosticActionButton(props) {
    const { t } = useI18n();
    const { action, onExec, busy, extra } = props;
    const label = (action.suggested ? "\u2606 " : "") + action.label;
    const cls = cn(
      "hermes-kanban-diag-action-btn",
      action.suggested ? "hermes-kanban-diag-action-btn--suggested" : "",
    );
    if (action.kind === "reclaim" || action.kind === "reassign" ||
        action.kind === "unblock") {
      return h("button", {
        className: cls,
        disabled: busy || (extra && extra.disabled),
        onClick: function () { onExec(action); },
        type: "button",
      }, label);
    }
    if (action.kind === "cli_hint") {
      return h("button", {
        className: cls,
        disabled: busy,
        onClick: function () { onExec(action); },
        type: "button",
        title: tx(t, "copyCommand", "Copy command to clipboard"),
      }, (extra && extra.copied) ? tx(t, "copied", "Copied") : label);
    }
    if (action.kind === "comment") {
      return h("button", {
        className: cls,
        onClick: function () { onExec(action); },
        type: "button",
      }, label);
    }
    if (action.kind === "open_docs") {
      return h("a", {
        className: cls,
        href: (action.payload && action.payload.url) || "#",
        target: "_blank",
        rel: "noreferrer",
      }, label);
    }
    // Unknown kind — render informational, non-interactive.
    return h("span", { className: cls + " hermes-kanban-diag-action-btn--unknown" },
      label);
  }

  function DiagnosticCard(props) {
    const { t } = useI18n();
    const { diag, task, boardSlug, assignees, onRefresh } = props;
    const [busy, setBusy] = useState(false);
    const [msg, setMsg] = useState(null);
    const [copiedKey, setCopiedKey] = useState(null);
    const [reassignProfile, setReassignProfile] = useState(task.assignee || "");

    const execAction = function (action) {
      if (busy) return;
      if (action.kind === "cli_hint") {
        const cmd = (action.payload && action.payload.command) || action.label;
        const fallback = function () {
          // The clipboard API is unavailable in this context. The native
          // window.prompt is acceptable here because:
          //   (a) The success path doesn't open a dialog at all (just
          //       sets `copiedKey` for 2 seconds), and
          //   (b) the fallback only fires when the browser blocks
          //       navigator.clipboard, which is rare.
          // Documented carve-out — see issue #50547 followups for the
          // dedicated copyFallback dialog body that will replace this
          // once ConfirmDialog grows a `disabled` prop upstream.
          window.prompt("Copy this command:", cmd);
        };
        try {
          const p = navigator.clipboard && navigator.clipboard.writeText(cmd);
          if (p && p.then) {
            p.then(function () {
              setCopiedKey(action.label);
              setTimeout(function () { setCopiedKey(null); }, 2000);
            }).catch(fallback);
          } else {
            fallback();
          }
        } catch (_) {
          fallback();
        }
        return;
      }
      if (action.kind === "comment") {
        // Scroll the comment input into view; the drawer already has one
        // at the bottom. Focus it so the operator can start typing.
        const ta = document.querySelector(".hermes-kanban-drawer-comment-row input, .hermes-kanban-drawer-comment-row textarea");
        if (ta) {
          ta.scrollIntoView({ behavior: "smooth", block: "nearest" });
          ta.focus();
        }
        return;
      }
      if (action.kind === "unblock") {
        setBusy(true); setMsg(null);
        const url = withBoard(`${API}/tasks/${encodeURIComponent(task.id)}`, boardSlug);
        SDK.fetchJSON(url, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: "ready" }),
        }).then(function () {
          setMsg({ ok: true, text: tx(t, "unblockedMessage",
            "Unblocked {id}. Task is ready for the next tick.", { id: task.id }) });
          if (onRefresh) onRefresh();
        }).catch(function (err) {
          setMsg({ ok: false, text: tx(t, "unblockFailed", "Unblock failed: ") + (err.message || err) });
        }).then(function () { setBusy(false); });
        return;
      }
      if (action.kind === "reclaim") {
        setBusy(true); setMsg(null);
        const url = withBoard(`${API}/tasks/${encodeURIComponent(task.id)}/reclaim`, boardSlug);
        SDK.fetchJSON(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason: `recovery action for ${diag.kind}` }),
        }).then(function () {
          setMsg({ ok: true, text: tx(t, "reclaimedMessage",
            "Reclaimed {id}. Task is back to ready.", { id: task.id }) });
          if (onRefresh) onRefresh();
        }).catch(function (err) {
          setMsg({ ok: false, text: tx(t, "reclaimFailed", "Reclaim failed: ") + (err.message || err) });
        }).then(function () { setBusy(false); });
        return;
      }
      if (action.kind === "reassign") {
        if (!reassignProfile) {
          setMsg({ ok: false, text: tx(t, "pickProfileFirst", "Pick a profile first.") });
          return;
        }
        setBusy(true); setMsg(null);
        const url = withBoard(`${API}/tasks/${encodeURIComponent(task.id)}/reassign`, boardSlug);
        const body = {
          profile: reassignProfile || null,
          reclaim_first: !!(action.payload && action.payload.reclaim_first),
          reason: `recovery action for ${diag.kind}`,
        };
        SDK.fetchJSON(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }).then(function () {
          setMsg({
            ok: true,
            text: tx(t, "reassignedMessage", "Reassigned {id} to {profile}.",
              { id: task.id, profile: reassignProfile }),
          });
          if (onRefresh) onRefresh();
        }).catch(function (err) {
          setMsg({ ok: false, text: tx(t, "reassignFailed", "Reassign failed: ") + (err.message || err) });
        }).then(function () { setBusy(false); });
        return;
      }
    };

    // Pull out the reassign action so we can render its picker inline.
    const reassignAction = (diag.actions || []).find(function (a) {
      return a.kind === "reassign";
    });

    const sevClass = "hermes-kanban-diag--" + (diag.severity || "warning");
    return h("div", { className: cn("hermes-kanban-diag", sevClass) },
      h("div", { className: "hermes-kanban-diag-header" },
        h("span", { className: "hermes-kanban-diag-sev" },
          diag.severity === "critical" ? "!!!" :
          diag.severity === "error" ? "!!" : "\u26a0"),
        h("span", { className: "hermes-kanban-diag-title" },
          diag.title),
      ),
      h("div", { className: "hermes-kanban-diag-detail" },
        diag.detail),
      diag.data && Object.keys(diag.data).length > 0
        ? h("div", { className: "hermes-kanban-diag-data" },
            Object.keys(diag.data).map(function (k) {
              const v = diag.data[k];
              if (Array.isArray(v) && v.length > 0 && typeof v[0] === "string" &&
                  v[0].indexOf("t_") === 0) {
                // Task-id list — render as chips.
                return h("div", { key: k, className: "hermes-kanban-diag-data-row" },
                  h("span", { className: "hermes-kanban-diag-data-key" }, k + ":"),
                  v.map(function (x) {
                    return h("code", {
                      key: x, className: "hermes-kanban-event-phantom-chip",
                    }, x);
                  }),
                );
              }
              return h("div", { key: k, className: "hermes-kanban-diag-data-row" },
                h("span", { className: "hermes-kanban-diag-data-key" }, k + ":"),
                h("span", { className: "hermes-kanban-diag-data-val" },
                  Array.isArray(v) ? v.join(", ") : String(v)),
              );
            }),
          )
        : null,
      // Inline reassign picker — only shown when the diagnostic offers
      // a reassign action. Profile list comes from the board payload.
      reassignAction
        ? h("div", { className: "hermes-kanban-diag-reassign-row" },
            h("span", { className: "hermes-kanban-diag-reassign-label" },
              tx(t, "reassignTo", "Reassign to:")),
            h("select", {
              className: "hermes-kanban-recovery-select",
              value: reassignProfile,
              onChange: function (e) { setReassignProfile(e.target.value); },
            },
              h("option", { value: "" }, "(unassigned)"),
              (assignees || []).map(function (a) {
                return h("option", { key: a, value: a }, a);
              }),
            ),
          )
        : null,
      h("div", { className: "hermes-kanban-diag-actions" },
        (diag.actions || []).map(function (a, i) {
          return h(DiagnosticActionButton, {
            key: a.kind + i,
            action: a,
            onExec: execAction,
            busy: busy,
            extra: {
              copied: copiedKey === a.label,
              disabled: (a.kind === "reassign" && !reassignProfile),
            },
          });
        }),
      ),
      msg
        ? h("div", {
            className: cn(
              "hermes-kanban-diag-msg",
              msg.ok ? "hermes-kanban-diag-msg--ok" : "hermes-kanban-diag-msg--err",
            ),
          }, msg.text)
        : null,
    );
  }

  function DiagnosticsSection(props) {
    const { t } = useI18n();
    const diags = props.diagnostics || [];
    const hasOpenDiags = diags.length > 0;
    const [open, setOpen] = useState(hasOpenDiags);
    useEffect(function () {
      if (hasOpenDiags) setOpen(true);
    }, [hasOpenDiags]);
    if (!hasOpenDiags && !props.alwaysVisible) {
      // Nothing active. Collapse the section entirely rather than showing
      // an empty "Recovery" header — keeps clean tasks visually clean.
      return null;
    }
    return h("div", { className: "hermes-kanban-section" },
      h("div", { className: "hermes-kanban-section-head-row" },
        h("span", { className: "hermes-kanban-section-head" },
          hasOpenDiags
            ? h("span", { className: "hermes-kanban-section-head-warning" },
                `\u26a0 ${tx(t, "diagnostics", "Diagnostics")} (${diags.length})`)
            : tx(t, "diagnostics", "Diagnostics"),
        ),
        h("button", {
          className: "hermes-kanban-section-toggle",
          onClick: function () { setOpen(function (x) { return !x; }); },
          type: "button",
        }, open ? tx(t, "hide", "Hide") : tx(t, "show", "Show")),
      ),
      open
        ? h("div", { className: "hermes-kanban-diag-list" },
            diags.map(function (d, i) {
              return h(DiagnosticCard, {
                key: props.task.id + ":" + d.kind + i,
                diag: d,
                task: props.task,
                boardSlug: props.boardSlug,
                assignees: props.assignees,
                onRefresh: props.onRefresh,
              });
            }),
          )
        : null,
    );
  }

    // -------------------------------------------------------------------------
  // Board switcher (multi-project)
  // -------------------------------------------------------------------------

  // Small `?` affordance next to the board controls. Opens the kanban docs
  // page in a new tab so users can look up what any of the widgets mean
  // without losing the current board view.
  function DocsLink() {
    const { uiText } = useI18n();
    return h("a", {
      href: DOCS_URL,
      target: "_blank",
      rel: "noopener noreferrer",
      className: "hermes-kanban-docs-link",
      title: uiText("Open Hermes Kanban docs in a new tab", "Открыть документацию Hermes Kanban в новой вкладке"),
      "aria-label": uiText("Hermes Kanban documentation", "Документация Hermes Kanban"),
    }, "?");
  }

  // ---------------------------------------------------------------------
  // OrchestrationPanel — collapsible settings panel for the kanban
  // orchestrator (orchestrator profile picker, default assignee picker,
  // auto-decompose toggle, plus per-profile description editing with
  // auto-generate). Backed by /orchestration + /profiles endpoints.
  // ---------------------------------------------------------------------

  function OrchestrationPanel() {
    const { t, uiText } = useI18n();
    const [expanded, setExpanded] = useState(false);
    const [settings, setSettings] = useState(null);
    const [profiles, setProfiles] = useState([]);
    const [busy, setBusy] = useState({});
    const [msg, setMsg] = useState(null);

    const loadAll = useCallback(function () {
      Promise.all([
        SDK.fetchJSON(`${API}/orchestration`),
        SDK.fetchJSON(`${API}/profiles`),
      ]).then(function (results) {
        setSettings(results[0] || null);
        setProfiles((results[1] && results[1].profiles) || []);
        setMsg(null);
      }).catch(function (err) {
        setMsg({ ok: false, text: tx(t, "loadSettingsFailed", "Failed to load: ") + (err.message || String(err)) });
      });
    }, []);

    useEffect(function () {
      // Load on mount so the collapsed pill shows the real mode without
      // requiring the user to expand the panel first.
      if (settings === null) loadAll();
    }, [settings, loadAll]);

    const saveSettings = function (patch) {
      setMsg(null);
      return SDK.fetchJSON(`${API}/orchestration`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      }).then(function (res) {
        setSettings(res);
        setMsg({ ok: true, text: tx(t, "settingsSaved", "Settings saved.") });
        return res;
      }).catch(function (err) {
        setMsg({ ok: false, text: tx(t, "saveFailed", "Save failed: ") + (err.message || String(err)) });
      });
    };

    const saveProfileDescription = function (name, description) {
      setBusy(function (b) { return Object.assign({}, b, { [name]: "save" }); });
      return SDK.fetchJSON(`${API}/profiles/${encodeURIComponent(name)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ description: description }),
      }).then(function () {
        loadAll();
        setMsg({ ok: true, text: tx(t, "descriptionSaved", "Description saved for {name}.", { name: name }) });
      }).catch(function (err) {
        setMsg({ ok: false, text: tx(t, "saveFailed", "Save failed: ") + (err.message || String(err)) });
      }).then(function () {
        setBusy(function (b) {
          const next = Object.assign({}, b); delete next[name]; return next;
        });
      });
    };

    const autoGenerateDescription = function (name, overwrite) {
      setBusy(function (b) { return Object.assign({}, b, { [name]: "auto" }); });
      return SDK.fetchJSON(`${API}/profiles/${encodeURIComponent(name)}/describe-auto`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ overwrite: !!overwrite }),
      }).then(function (res) {
        if (res && res.ok) {
          loadAll();
          setMsg({ ok: true, text: `Auto-generated description for ${name}.` });
        } else {
          setMsg({
            ok: false,
            text: tx(t, "autoGenerateFailed", "Auto-generate failed: ") + ((res && res.reason) || "unknown error"),
          });
        }
      }).catch(function (err) {
        setMsg({ ok: false, text: tx(t, "autoGenerateFailed", "Auto-generate failed: ") + (err.message || String(err)) });
      }).then(function () {
        setBusy(function (b) {
          const next = Object.assign({}, b); delete next[name]; return next;
        });
      });
    };

    const headerLabel = expanded
      ? uiText("▾ Orchestration settings", "▾ Настройки оркестрации")
      : uiText("▸ Orchestration settings", "▸ Настройки оркестрации");

    // Mode pill — always visible (collapsed or expanded). One click flips
    // between Auto and Manual. Auto = dispatcher decomposes new triage tasks
    // every tick. Manual = pre-PR behavior, the user clicks ⚗ Decompose on
    // each triage card (or runs `hermes kanban decompose <id>`) and tasks
    // stay in triage until then.
    const autoOn = !!(settings && settings.auto_decompose);
    const modePillTitle = settings === null
      ? uiText("Loading mode…", "Загрузка режима…")
      : (autoOn
          ? uiText("Orchestration: Auto — the dispatcher decomposes new triage tasks automatically every tick. Click to switch to Manual (pre-PR behavior).", "Оркестрация: автоматически — диспетчер разбирает новые задачи на сортировке при каждом цикле. Нажмите, чтобы включить ручной режим.")
          : uiText("Orchestration: Manual — triage tasks stay in triage until you click ⚗ Decompose on each card. Click to switch to Auto.", "Оркестрация: вручную — задачи остаются в сортировке, пока вы не нажмёте ⚗ Разложить на карточке. Нажмите, чтобы включить автоматический режим."));
    const modePill = h("button", {
      type: "button",
      onClick: function () {
        if (settings === null) return;  // not loaded yet
        saveSettings({ auto_decompose: !autoOn });
      },
      disabled: settings === null,
      title: modePillTitle,
      className: "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 "
                 + "text-xs font-medium "
                 + (autoOn
                    ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                    : "border-muted-foreground/30 bg-muted/30 text-muted-foreground"),
    },
      uiText("Orchestration: ", "Оркестрация: "),
      h("span", { className: "ml-1 font-semibold" },
        settings === null ? "…" : (autoOn ? uiText("Auto", "Автоматически") : uiText("Manual", "Вручную")))
    );

    if (!expanded) {
      return h("div", { className: "flex items-center gap-3 text-xs" },
        modePill,
        h("button", {
          type: "button",
          onClick: function () { setExpanded(true); },
          className: "underline text-muted-foreground hover:text-foreground",
          title: uiText("Configure the kanban orchestrator (profile picker, default assignee, auto-decompose, profile descriptions)", "Настроить оркестрацию канбана: выбрать профиль, исполнителя по умолчанию, автоматическую декомпозицию и описания профилей"),
        }, headerLabel),
      );
    }

    const profileOptions = profiles.map(function (p) {
      const tag = p.is_default ? " (default)" : "";
      return h(SelectOption, { key: p.name, value: p.name }, p.name + tag);
    });

    return h(Card, { className: "p-3" },
      h(CardContent, { className: "p-2 flex flex-col gap-3" },
        h("div", { className: "flex items-center justify-between" },
          h("button", {
            type: "button",
            onClick: function () { setExpanded(false); },
            className: "text-sm font-medium underline-offset-2 hover:underline",
          }, headerLabel),
          modePill,
          h(Button, { onClick: loadAll, size: "sm" }, tx(t, "refresh", "Reload")),
        ),
        msg ? h("div", {
          className: msg.ok ? "hermes-kanban-msg-ok" : "hermes-kanban-msg-err",
        }, msg.text) : null,

        settings ? h("div", { className: "grid gap-3 sm:grid-cols-3" },
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs text-muted-foreground" },
              uiText("Orchestrator profile", "Профиль оркестратора")),
            h(Select, Object.assign({
              value: settings.orchestrator_profile || "",
              className: "h-8",
            }, selectChangeHandler(function (v) {
              saveSettings({ orchestrator_profile: v });
            })),
              h(SelectOption, { value: "" },
                uiText("(default: ", "(по умолчанию: ") + (settings.active_profile || "default") + ")"),
              profileOptions,
            ),
            h("div", { className: "text-[10px] text-muted-foreground" },
              uiText("Resolved: ", "Фактически используется: ") + (settings.resolved_orchestrator_profile || "default")),
            h("div", { className: "text-[10px] text-muted-foreground" },
              uiText("Owns the root task after fan-out (wakes back up to judge completion). Does not drive how tasks split — configure the decomposer model under auxiliary.kanban_decomposer.", "Ведёт корневую задачу после декомпозиции и возобновляется, чтобы оценить результат. Не управляет разбиением задач — модель декомпозитора настраивается в auxiliary.kanban_decomposer.")),
          ),
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs text-muted-foreground" },
              uiText("Default assignee", "Исполнитель по умолчанию")),
            h(Select, Object.assign({
              value: settings.default_assignee || "",
              className: "h-8",
            }, selectChangeHandler(function (v) {
              saveSettings({ default_assignee: v });
            })),
              h(SelectOption, { value: "" },
                uiText("(default: ", "(по умолчанию: ") + (settings.active_profile || "default") + ")"),
              profileOptions,
            ),
            h("div", { className: "text-[10px] text-muted-foreground" },
              uiText("Resolved: ", "Фактически используется: ") + (settings.resolved_default_assignee || "default")),
          ),
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs text-muted-foreground" },
              uiText("Orchestration mode", "Режим оркестрации")),
            h("label", { className: "flex items-center gap-2 text-xs h-8" },
              h(Checkbox, {
                checked: !!settings.auto_decompose,
                onCheckedChange: function (checked) {
                  saveSettings({ auto_decompose: checked === true });
                },
              }),
              uiText("Auto-decompose triage tasks", "Автоматически разбирать задачи на сортировке"),
            ),
            h("div", { className: "text-[10px] text-muted-foreground" },
              settings.auto_decompose
                ? uiText("The dispatcher decomposes new triage tasks automatically.", "Диспетчер автоматически разбирает новые задачи на сортировке.")
                : uiText("Triage tasks stay in triage until you click ⚗ Decompose.", "Задачи остаются на сортировке, пока вы не нажмёте ⚗ Разложить.")),
          ),
        ) : h("div", { className: "text-xs text-muted-foreground" },
          "Loading…"),

        h("div", { className: "border-t pt-3" },
          h(Label, { className: "text-xs text-muted-foreground" },
            uiText("Profile descriptions", "Описания профилей")),
          h("div", { className: "text-[10px] text-muted-foreground pb-2" },
            uiText("Descriptions guide the decomposer's routing. Click ⚗ to auto-generate, or edit and save.", "Описания помогают декомпозитору выбирать исполнителей. Нажмите ⚗ для создания описания или отредактируйте его и сохраните.")),
          profiles.length === 0
            ? h("div", { className: "text-xs text-muted-foreground" }, uiText("No profiles installed.", "Профили не установлены."))
            : h("div", { className: "flex flex-col gap-2" },
                profiles.map(function (p) {
                  return h(ProfileDescriptionRow, {
                    key: p.name,
                    profile: p,
                    busy: busy[p.name] || null,
                    onSave: saveProfileDescription,
                    onAuto: autoGenerateDescription,
                  });
                }),
              ),
        ),
      ),
    );
  }

  function ProfileDescriptionRow(props) {
    const { t, uiText } = useI18n();
    const p = props.profile;
    const [draft, setDraft] = useState(p.description || "");
    const busy = props.busy;
    // Re-sync the local draft if the server-side description changes (e.g.
    // after auto-generate). Cheap because re-runs only happen on prop change.
    useEffect(function () {
      setDraft(p.description || "");
    }, [p.description]);

    const tag = p.description_auto && p.description ? uiText(" [auto, review]", " [автоматически, проверить]") : "";
    return h("div", { className: "flex flex-col gap-1 border-l-2 pl-2",
      style: { borderColor: p.description ? "#888" : "#cc6" } },
      h("div", { className: "flex items-center gap-2 text-xs" },
        h("span", { className: "font-medium" }, p.name),
        p.is_default ? h("span", { className: "text-[10px] text-muted-foreground" }, uiText("(default)", "(по умолчанию)")) : null,
        p.description_auto && p.description
          ? h("span", { className: "text-[10px] text-yellow-600" }, uiText("auto — review", "автоматически — проверить"))
          : null,
        !p.description
          ? h("span", { className: "text-[10px] text-yellow-600" }, uiText("⚠ no description", "⚠ нет описания"))
          : null,
      ),
      h("div", { className: "flex items-center gap-2" },
        h(Input, {
          value: draft,
          onChange: function (e) { setDraft(e.target.value); },
          placeholder: uiText("What is this profile good at?", "В чём силён этот профиль?"),
          className: "h-7 text-xs flex-1",
        }),
        h(Button, {
          onClick: function () { props.onSave(p.name, draft); },
          size: "sm",
          disabled: !!busy || draft === (p.description || ""),
          title: uiText("Save the description above as user-authored", "Сохранить описание как написанное вручную"),
        }, busy === "save" ? uiText("Saving…", "Сохранение…") : tx(t, "save", "Save")),
        h(Button, {
          onClick: function () { props.onAuto(p.name, true); },
          size: "sm",
          disabled: !!busy,
          title: uiText("Auto-generate a description from this profile's skills and model", "Создать описание по навыкам и модели этого профиля"),
        }, busy === "auto" ? uiText("Generating…", "Создание…") : "⚗ " + uiText("Auto", "Авто")),
      ),
    );
  }

  function BoardSwitcher(props) {
    const { t, uiText, locale } = useI18n();
    const list = props.boardList || [];
    const current = list.find(function (b) { return b.slug === props.board; });
    const currentName = current && current.name ? current.name : props.board;
    const currentTotal = props.totalTasks != null ? props.totalTasks : (current ? current.total : 0);
    const hasMultipleBoards = list.length > 1;

    // Hide entirely when only the default board exists AND it's empty —
    // single-project users never see boards UI unless they ask for it.
    // We show the [+ New board] affordance as soon as any board has a
    // task (so the user can discover multi-project before they need it)
    // OR when any non-default board exists.
    const totalAcrossAllBoards = list.reduce(function (n, b) { return n + (b.total || 0); }, 0);
    const shouldShow = hasMultipleBoards || totalAcrossAllBoards > 0;
    if (!shouldShow) {
      return h("div", {
        className: "hermes-kanban-boardswitcher-compact",
        title: tx(t, "boardSwitcherHint", "Boards let you separate unrelated streams of work"),
      },
        h(Button, {
          onClick: props.onNewClick,
          size: "sm",
          className: "h-7 text-xs",
        }, tx(t, "newBoard", "+ New board")),
        h(Button, {
          onClick: props.onSettingsClick,
          size: "sm",
          className: "h-7 text-xs",
          title: tx(t, "boardSettingsTitle",
            "Board settings — name, description, and the default project directory new tasks inherit"),
        }, tx(t, "boardSettings", "Settings")),
        h(DocsLink, null),
      );
    }

    return h("div", { className: "hermes-kanban-boardswitcher" },
      h("div", { className: "hermes-kanban-boardswitcher-inner" },
        h("div", { className: "flex flex-col gap-0.5" },
          h("div", { className: "text-[11px] tracking-wider text-muted-foreground" },
            tx(t, "board", "Board")),
          h("div", { className: "flex items-center gap-2" },
            h(Select, Object.assign({
              value: props.board,
              className: "h-8 min-w-[220px]",
              "aria-label": uiText("Switch kanban board", "Переключить доску Kanban"),
              title: uiText("Boards are independent work streams. Each board has its own tasks, tenants, and assignees.", "Доски — независимые потоки работы. У каждой свои задачи, тенанты и исполнители."),
            }, selectChangeHandler(function (v) { if (v) props.onSwitch(v); })),
              list.map(function (b) {
                const label = b.total > 0
                  ? `${b.name || b.slug} · ${b.total}`
                  : (b.name || b.slug);
                return h(SelectOption, { key: b.slug, value: b.slug }, label);
              }),
            ),
              h("span", { className: "text-xs text-muted-foreground" },
                /^ru(?:[-_]|$)/i.test(locale)
                  ? pluralRu(currentTotal || 0, ["задача", "задачи", "задач"])
                  : `${currentTotal || 0} task${currentTotal === 1 ? "" : "s"}`),
          ),
        ),
        h("div", { className: "flex-1" }),
        h(DocsLink, null),
        h(Button, {
          onClick: props.onSettingsClick,
          size: "sm",
          className: "h-8",
          title: tx(t, "boardSettingsTitle",
            "Board settings — name, description, and the default project directory new tasks inherit"),
        }, tx(t, "boardSettings", "Settings")),
        h(Button, {
          onClick: props.onNewClick,
          size: "sm",
          className: "h-8",
          title: uiText("Create a new board. Useful when you want an unrelated work stream (different project, different team, isolated scratch area).", "Создать отдельную доску для другого проекта, команды или изолированного временного потока работы."),
        }, tx(t, "newBoard", "+ New board")),
        props.board !== "default"
          ? h(Button, {
            onClick: function () {
              const msg = tx(t, "archiveBoardConfirm",
                "Archive board '{name}'? It will be moved to boards/_archived/ so you can recover it later. Tasks on this board will no longer appear anywhere in the UI.",
                { name: currentName });
              // Prefer the in-app dialog flow if the host wired one.
              if (props.requestDialog) {
                props.requestDialog({
                  kind: "confirm",
                  title: tx(t, "archiveBoardTitle", "Archive this board"),
                  description: msg,
                  confirmLabel: tx(t, "archive", "Archive"),
                  destructive: true,
                }).then(function (r) {
                  if (r.confirmed) props.onDeleteBoard(props.board);
                }).catch(function () { /* cancelled */ });
              } else if (window.confirm(msg)) {
                props.onDeleteBoard(props.board);
              }
            },
            size: "sm",
            className: "h-8",
            title: tx(t, "archiveBoardTitle", "Archive this board"),
          }, tx(t, "archive", "Archive"))
          : null,
      ),
    );
  }

  // Browse the Hermes server, not the browser computer. Reuse the managed
  // Files API so authentication, sensitive-path filtering and locked roots
  // have exactly the same policy as the dashboard Files page.
  function ProjectDirectoryField(props) {
    const { t, uiText } = useI18n();
    const [open, setOpen] = useState(false);
    const [listing, setListing] = useState(null);
    const [pathInput, setPathInput] = useState("");
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const request = useRef(0);
    const browseButton = useRef(null);

    useEffect(function () {
      return function () { request.current += 1; };
    }, []);

    async function load(path) {
      const current = ++request.current;
      setLoading(true);
      setError(null);
      setListing(null);
      try {
        const result = await SDK.api.listFiles(path || undefined);
        if (request.current !== current) return;
        setListing(result);
        setPathInput(result.path);
      } catch (e) {
        if (request.current === current) setError(parseApiErrorMessage(e));
      } finally {
        if (request.current === current) setLoading(false);
      }
    }

    function close() {
      request.current += 1;
      setOpen(false);
      if (browseButton.current) browseButton.current.focus();
    }

    const directories = listing ? listing.entries.filter(function (entry) {
      return entry.is_directory;
    }) : [];

    return h("fieldset", {
      disabled: props.disabled,
      className: "flex flex-col gap-2",
      style: { border: 0, padding: 0, margin: 0, minWidth: 0 },
    },
      h("div", { style: { display: "flex", gap: "0.5rem", alignItems: "center" } },
        h(Input, {
          value: props.value,
          onChange: function (e) { props.onChange(e.target.value); },
          "aria-label": tx(t, "projectDirectory", "Project directory"),
          placeholder: tx(t, "projectDirectoryPlaceholder", "~/projects/my-app"),
          title: tx(t, "projectDirectoryHelp", "Project directory on the Hermes server"),
          className: "h-8",
          style: { minWidth: 0, flex: 1 },
          disabled: props.disabled,
          autoCapitalize: "none", autoCorrect: "off", spellCheck: false,
        }),
        h(Button, {
          ref: browseButton, type: "button", size: "sm", variant: "outline",
          disabled: props.disabled, "aria-expanded": open,
          onClick: function () {
            if (open) { close(); return; }
            const start = props.value.trim();
            setPathInput(start);
            setOpen(true);
            void load(start);
          },
        }, uiText("Browse…", "Обзор…")),
      ),
      open ? h("section", {
        role: "region", "aria-label": uiText("Choose a server folder", "Выбор папки на сервере"),
        className: "flex flex-col gap-2",
        style: { padding: "0.75rem", border: "1px solid var(--color-border)", borderRadius: "0.5rem" },
        onKeyDown: function (e) {
          if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
        },
      },
        h("div", { className: "text-xs text-muted-foreground" },
          uiText("Folders available to Hermes on the server. Your computer's folders are not shown.",
            "Папки, доступные Hermes на сервере. Папки вашего компьютера здесь не отображаются.")),
        h("div", { style: { display: "flex", gap: "0.5rem" } },
          h(Input, {
            value: pathInput, onChange: function (e) { setPathInput(e.target.value); },
            "aria-label": uiText("Server folder path", "Путь к папке на сервере"),
            autoFocus: true, autoCapitalize: "none", autoCorrect: "off", spellCheck: false,
            className: "h-8", style: { minWidth: 0, flex: 1 },
            disabled: loading || !!(listing && !listing.can_change_path),
            onKeyDown: function (e) {
              if (e.key === "Enter") {
                e.preventDefault(); e.stopPropagation();
                if (!loading) void load(pathInput.trim());
              }
            },
          }),
          h(Button, {
            type: "button", size: "sm", variant: "outline", disabled: loading,
            onClick: function () { void load(pathInput.trim()); },
          }, uiText("Go", "Перейти")),
        ),
        h("div", { style: { display: "flex", flexWrap: "wrap", gap: "0.5rem" } },
          h(Button, {
            type: "button", size: "sm", variant: "outline",
            disabled: loading || !listing || !listing.parent,
            onClick: function () { void load(listing.parent); },
          }, uiText("Up one level", "На уровень выше")),
          h(Button, {
            type: "button", size: "sm", variant: "outline", disabled: loading,
            onClick: function () { void load(); },
          }, uiText("Start folder", "Начальная папка")),
        ),
        loading ? h("div", { role: "status", className: "text-xs text-muted-foreground" },
          uiText("Loading folders…", "Загрузка папок…")) : null,
        error ? h("div", { role: "alert", className: "text-xs text-destructive" },
          uiText("Could not open folder: ", "Не удалось открыть папку: ") + error) : null,
        listing ? h("div", {
          "aria-label": uiText("Subfolders", "Вложенные папки"),
          style: { maxHeight: "12rem", overflowY: "auto", display: "flex", flexDirection: "column", gap: "0.25rem" },
        }, directories.length ? directories.map(function (entry) {
          return h(Button, {
            key: entry.path, type: "button", size: "sm", variant: "ghost", title: entry.path,
            style: { justifyContent: "flex-start", minHeight: "2rem", flexShrink: 0, overflow: "hidden" },
            onClick: function () { void load(entry.path); },
          }, h("span", { "aria-hidden": true }, "📁 "),
          h("span", { style: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, entry.name));
        }) : h("div", { className: "text-xs text-muted-foreground" },
          uiText("No subfolders. You can select this folder.", "Вложенных папок нет. Можно выбрать текущую папку."))) : null,
        listing && listing.locked_root ? h("div", { className: "text-xs text-muted-foreground" },
          uiText("Available root: ", "Доступный корень: ") + listing.locked_root) : null,
        h("div", { style: { display: "flex", justifyContent: "flex-end", flexWrap: "wrap", gap: "0.5rem" } },
          h(Button, { type: "button", size: "sm", variant: "outline", onClick: close },
            uiText("Cancel selection", "Отменить выбор")),
          h(Button, {
            type: "button", size: "sm",
            disabled: loading || !listing || pathInput.trim() !== listing.path,
            onClick: function () { props.onChange(listing.path); close(); },
          }, uiText("Select this folder", "Выбрать эту папку")),
        ),
      ) : null,
    );
  }

  function NewBoardDialog(props) {
    const { t, uiText } = useI18n();
    const [slug, setSlug] = useState("");
    const [name, setName] = useState("");
    const [description, setDescription] = useState("");
    const [icon, setIcon] = useState("");
    const [projectDirectory, setProjectDirectory] = useState("");
    const [switchTo, setSwitchTo] = useState(true);
    const [submitting, setSubmitting] = useState(false);
    const [err, setErr] = useState(null);

    // Auto-derive a name from the slug if the user hasn't typed one.
    const autoName = useMemo(function () {
      if (!slug) return "";
      return slug.replace(/[-_]+/g, " ")
        .split(" ")
        .filter(Boolean)
        .map(function (w) { return w[0].toUpperCase() + w.slice(1); })
        .join(" ");
    }, [slug]);

    function onSubmit(ev) {
      if (ev) ev.preventDefault();
      if (!slug.trim()) { setErr("slug is required"); return; }
      setSubmitting(true);
      setErr(null);
      props.onCreate({
        slug: slug.trim(),
        name: name.trim() || autoName || undefined,
        description: description.trim() || undefined,
        icon: icon.trim() || undefined,
        default_workdir: projectDirectory.trim() || undefined,
        switch: switchTo,
      }).catch(function (e) {
        setErr(String(e && e.message ? e.message : e));
        setSubmitting(false);
      });
    }

    return h("div", {
      className: "hermes-kanban-dialog-backdrop",
      onClick: function (e) { if (e.target === e.currentTarget) props.onCancel(); },
    },
      h("form", {
        className: "hermes-kanban-dialog",
        onSubmit: onSubmit,
      },
        h("div", { className: "hermes-kanban-dialog-title" },
          tx(t, "newBoardTitle", "New board")),
        h("div", { className: "text-xs text-muted-foreground mb-2" },
          tx(t, "newBoardDescription",
            "Boards let you separate unrelated streams of work — one per project, repo, or domain. Workers on one board never see another board's tasks.")),
        h("div", { className: "flex flex-col gap-3" },
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs" }, tx(t, "slug", "Slug"), " ",
              h("span", { className: "text-muted-foreground" },
                tx(t, "slugHint", "— lowercase, hyphens, e.g. atm10-server"))),
            h(Input, {
              value: slug,
              onChange: function (e) { setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9\-_]/g, "-")); },
              placeholder: "atm10-server",
              autoFocus: true,
              className: "h-8",
            }),
          ),
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs" }, tx(t, "displayName", "Display name"), " ",
              h("span", { className: "text-muted-foreground" },
                tx(t, "displayNameHint", "(optional)"))),
            h(Input, {
              value: name,
              onChange: function (e) { setName(e.target.value); },
              placeholder: autoName || tx(t, "displayName", "Display name"),
              className: "h-8",
            }),
          ),
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs" }, tx(t, "description", "Description"), " ",
              h("span", { className: "text-muted-foreground" },
                tx(t, "descriptionHint", "(optional)"))),
            h(Input, {
              value: description,
              onChange: function (e) { setDescription(e.target.value); },
              placeholder: uiText("What goes on this board?", "Для чего предназначена эта доска?"),
              className: "h-8",
            }),
          ),
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs" },
              tx(t, "projectDirectory", "Project directory"), " ",
              h("span", { className: "text-muted-foreground" },
                tx(t, "projectDirectoryHint", "(recommended)"))),
            h(ProjectDirectoryField, {
              value: projectDirectory,
              onChange: setProjectDirectory,
              disabled: submitting,
            }),
            h("div", { className: "text-xs text-muted-foreground" },
              tx(t, "projectDirectoryExplanation",
              uiText("Sets the default location for task files so project output is preserved.", "Задаёт каталог по умолчанию для файлов задач, чтобы сохранять результаты проекта."))),
          ),
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs" }, tx(t, "icon", "Icon"), " ",
              h("span", { className: "text-muted-foreground" },
                tx(t, "iconHint", "(single character or emoji)"))),
            h(Input, {
              value: icon,
              onChange: function (e) { setIcon(e.target.value.slice(0, 4)); },
              placeholder: "📦",
              className: "h-8 w-24",
            }),
          ),
          h("label", { className: "flex items-center gap-2 text-xs" },
            h(Checkbox, {
              checked: switchTo,
              onCheckedChange: function (checked) { setSwitchTo(checked === true); },
            }),
            tx(t, "switchAfterCreate", "Switch to this board after creating it"),
          ),
        ),
        err ? h("div", { className: "text-xs text-destructive mt-2" }, err) : null,
        h("div", { className: "hermes-kanban-dialog-actions" },
          h(Button, {
            type: "button",
            onClick: props.onCancel,
            size: "sm",
            disabled: submitting,
          }, tx(t, "cancel", "Cancel")),
          h(Button, {
            type: "submit",
            size: "sm",
            disabled: submitting || !slug.trim(),
          }, submitting ? tx(t, "creating", "Creating…") : tx(t, "createBoard", "Create board")),
        ),
      ),
    );
  }

  // Board settings dialog — edit display name, description, and the
  // board-level default project directory (default_workdir). The workdir
  // is the board-level setting every new task's workspace kind/path is
  // seeded from; task-level values in the create dialog override it.
  function BoardSettingsDialog(props) {
    const { t, uiText } = useI18n();
    const b = props.board || {};
    const [name, setName] = useState(b.name || "");
    const [description, setDescription] = useState(b.description || "");
    const [projectDirectory, setProjectDirectory] = useState(b.default_workdir || "");
    const [submitting, setSubmitting] = useState(false);
    const [err, setErr] = useState(null);

    function onSubmit(ev) {
      if (ev) ev.preventDefault();
      setSubmitting(true);
      setErr(null);
      // Send default_workdir unconditionally: "" clears it on the server,
      // a path sets it (validated server-side: absolute + existing dir).
      props.onSave({
        name: name.trim() || undefined,
        description: description.trim() || undefined,
        default_workdir: projectDirectory.trim(),
      }).catch(function (e) {
        setErr(parseApiErrorMessage(e));
        setSubmitting(false);
      });
    }

    return h("div", {
      className: "hermes-kanban-dialog-backdrop",
      onClick: function (e) { if (e.target === e.currentTarget) props.onCancel(); },
      onKeyDown: function (e) { if (e.key === "Escape") props.onCancel(); },
    },
      h("form", {
        className: "hermes-kanban-dialog",
        onSubmit: onSubmit,
      },
        h("div", { className: "hermes-kanban-dialog-title" },
          tx(t, "boardSettingsTitleFor", "Board settings — {name}",
            { name: b.name || b.slug || "default" })),
        h("div", { className: "flex flex-col gap-3" },
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs" }, tx(t, "displayName", "Display name")),
            h(Input, {
              value: name,
              onChange: function (e) { setName(e.target.value); },
              className: "h-8",
            }),
          ),
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs" }, tx(t, "description", "Description")),
            h(Input, {
              value: description,
              onChange: function (e) { setDescription(e.target.value); },
              className: "h-8",
            }),
          ),
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs" },
              tx(t, "projectDirectory", "Project directory")),
            h(ProjectDirectoryField, {
              value: projectDirectory,
              onChange: setProjectDirectory,
              disabled: submitting,
            }),
            h("div", { className: "text-xs text-muted-foreground" },
              tx(t, "projectDirectoryOverrideHint",
                uiText("New tasks inherit this as their workspace default; each task can still override it in the create dialog.", "Новые задачи наследуют этот каталог. Для каждой задачи его можно изменить при создании."))),
          ),
        ),
        err ? h("div", { className: "text-xs text-destructive mt-2" }, err) : null,
        h("div", { className: "hermes-kanban-dialog-actions" },
          h(Button, {
            type: "button",
            onClick: props.onCancel,
            size: "sm",
            disabled: submitting,
          }, tx(t, "cancel", "Cancel")),
          h(Button, {
            type: "submit",
            size: "sm",
            disabled: submitting,
          }, submitting ? tx(t, "saving", "Saving…") : tx(t, "save", "Save")),
        ),
      ),
    );
  }

  // -------------------------------------------------------------------------
  // Toolbar
  // -------------------------------------------------------------------------

  function BoardToolbar(props) {
    const { t } = useI18n();
    const tenants = (props.board && props.board.tenants) || [];
    const assignees = (props.board && props.board.assignees) || [];
    function toggle(label, checked, setter) {
      return h("label", { className: "hermes-kanban-view-toggle" },
        h(Checkbox, { checked: checked, onCheckedChange: function (value) { setter(value === true); } }), label);
    }
    return h("div", { className: "hermes-kanban-toolbar" },
      h("div", { className: "hermes-kanban-toolbar-main" },
        h(Input, { value: props.search, placeholder: "Найти задачу…", "aria-label": "Поиск задач",
          onChange: function (e) { props.setSearch(e.target.value); } }),
        h(Select, Object.assign({ value: props.assigneeFilter, "aria-label": "Исполнитель" }, selectChangeHandler(props.setAssigneeFilter)),
          h(SelectOption, { value: "" }, "Все исполнители"),
          assignees.map(function (a) { return h(SelectOption, { key: a, value: a }, operatorRoleLabel(a)); })),
        h(Button, { onClick: props.onRefresh, size: "sm" }, "Обновить"),
        h(Button, { onClick: props.onNewTask, size: "sm" }, "+ Задача")),
      h("div", { className: "hermes-kanban-toolbar-secondary" },
        toggle(`История · ${props.doneCount || 0}`, props.showDone, props.setShowDone),
        h("details", { className: "hermes-kanban-view-options" },
          h("summary", null, "Вид и фильтры"),
          h("div", { className: "hermes-kanban-view-options-body" },
            toggle("Все этапы, включая пустые", props.showAllColumns, props.setShowAllColumns),
            toggle("Группировать по исполнителю", props.laneByProfile, props.setLaneByProfile),
            toggle("Показывать архив", props.includeArchived, props.setIncludeArchived),
            h(Label, null, "Метка задачи"),
            h(Select, Object.assign({ value: props.tenantFilter, "aria-label": "Метка задачи" }, selectChangeHandler(props.setTenantFilter)),
              h(SelectOption, { value: "" }, "Все метки"),
              tenants.map(function (tn) { return h(SelectOption, { key: tn, value: tn }, tn); })),
            h(Button, { onClick: function () {
              props.setSearch(""); props.setTenantFilter(""); props.setAssigneeFilter(""); props.setIncludeArchived(false);
            }, size: "sm" }, "Сбросить фильтры"),
            h(Button, { onClick: props.onNudgeDispatch, size: "sm",
              title: "Проверить готовые задачи сейчас, сохраняя ограничения квот и зависимостей" }, "Проверить очередь сейчас")))),
    );
  }

  // -------------------------------------------------------------------------
  // Bulk action bar (appears when >= 1 card is selected)
  // -------------------------------------------------------------------------

  function BulkActionBar(props) {
    const { t, uiText } = useI18n();
    const [assignee, setAssignee] = useState("");
    const [reclaimFirst, setReclaimFirst] = useState(false);
    const [priority, setPriority] = useState("");
    return h("div", { className: "hermes-kanban-bulk" },
      h("span", { className: "hermes-kanban-bulk-count" },
        `${props.count} ${tx(t, "selected", "selected")}`),
      h(Button, {
        onClick: function () { props.onApply({ status: "todo" }); },
        size: "sm",
        title: uiText("Move selected tasks to Todo.", "Переместить выбранные задачи в «К выполнению»."),
      }, "→ " + uiText("todo", "к выполнению")),
      h(Button, {
        onClick: function () { props.onApply({ status: "ready" }); },
        size: "sm",
        title: uiText("Move selected tasks to Ready. Ready tasks are picked up by the dispatcher on the next tick.", "Переместить выбранные задачи в «Готово к работе». Диспетчер возьмёт их в следующем цикле."),
      }, "→ " + uiText("ready", "готово")),
      h(Button, {
        onClick: function () { props.onApply({ status: "blocked" },
          uiText(`Block ${props.count} task(s)?`, `Заблокировать ${pluralRu(props.count, ["задачу", "задачи", "задач"])}?`)); },
        size: "sm",
        title: uiText("Block selected tasks. Releases any active claims.", "Заблокировать выбранные задачи и освободить активные захваты."),
      }, tx(t, "block", "Block")),
      h(Button, {
        onClick: function () { props.onApply({ status: "ready" },
          uiText(`Unblock ${props.count} task(s)?`, `Снять блокировку с ${pluralRu(props.count, ["задачи", "задач", "задач"])}?`)); },
        size: "sm",
        title: uiText("Unblock selected tasks (promote to Ready).", "Снять блокировку и переместить выбранные задачи в «Готово к работе»."),
      }, tx(t, "unblock", "Unblock")),
      h(Button, {
        onClick: function () {
          props.onApply({ status: "done" },
            tx(t, "markDone", "Mark {n} task(s) as done?", { n: props.count }));
        },
        size: "sm",
        title: uiText("Mark selected tasks as done. Releases any claims and unblocks dependent children. You'll be asked for a completion summary.", "Завершить выбранные задачи, освободить захваты и разблокировать дочерние. Потребуется добавить итоговую сводку."),
      }, tx(t, "complete", "Complete")),
      h(Button, {
        onClick: function () {
          props.onApply({ archive: true },
            tx(t, "markArchived", "Archive {n} task(s)?", { n: props.count }));
        },
        size: "sm",
        title: uiText("Archive selected tasks. They disappear from the default board view but remain in the database.", "Переместить задачи в архив. Они исчезнут с доски, но останутся в базе данных."),
      }, tx(t, "archive", "Archive")),
      h(Button, {
        onClick: function () {
          props.onDelete(props.count);
        },
        size: "sm",
        variant: "destructive",
        title: uiText("Permanently delete selected tasks. This cannot be undone.", "Удалить выбранные задачи без возможности восстановления."),
      }, tx(t, "delete", "Delete")),
      h("div", { className: "hermes-kanban-bulk-priority",
                 title: uiText("Set priority on selected tasks. Higher = claimed first.", "Задать приоритет выбранным задачам. Задачи с высоким приоритетом берутся первыми.") },
        h(Input, {
          type: "number",
          value: priority,
          onChange: function (e) { setPriority(e.target.value); },
          placeholder: tx(t, "priority", "pri"),
          className: "h-7 text-xs w-16",
        }),
        h(Button, {
          onClick: function () {
            if (priority === "") return;
            props.onApply({ priority: Number(priority) });
            setPriority("");
          },
          disabled: priority === "",
          size: "sm",
        }, tx(t, "setPriority", "Set priority")),
      ),
      h("div", { className: "hermes-kanban-bulk-reassign",
                 title: uiText("Reassign selected tasks to a different Hermes profile. Pick a profile (or unassign) and click Apply.", "Назначить выбранные задачи другому профилю Hermes. Выберите профиль или снимите назначение и нажмите «Применить».") },
        h(Select, Object.assign({
          value: assignee,
          className: "h-7 text-xs",
        }, selectChangeHandler(setAssignee)),
          h(SelectOption, { value: "" }, uiText("— reassign —", "— переназначить —")),
          h(SelectOption, { value: "__none__" }, uiText("(unassign)", "(снять назначение)")),
          props.assignees.map(function (a) {
            return h(SelectOption, { key: a, value: a }, a);
          }),
        ),
        h(Button, {
          onClick: function () {
            if (!assignee) return;
            props.onApply({ assignee: assignee === "__none__" ? "" : assignee, reclaim_first: reclaimFirst });
            setAssignee("");
          },
          disabled: !assignee,
          size: "sm",
          title: uiText("Apply the selected assignee to all selected tasks.", "Назначить выбранный профиль всем выбранным задачам."),
        }, tx(t, "apply", "Apply")),
      ),
      h("label", { className: "hermes-kanban-bulk-reclaim-first", title: uiText("Reclaim any active claims before reassigning", "Сначала вернуть активные задачи в очередь, затем переназначить") },
        h(Checkbox, {
          checked: reclaimFirst,
          onCheckedChange: function (checked) { setReclaimFirst(checked === true); },
        }),
        uiText("Reclaim first", "Сначала вернуть"),
      ),
      h("div", { className: "flex-1" }),
      h(Button, {
        onClick: props.onSelectAllVisible,
        size: "sm",
        title: uiText("Select all visible cards across columns.", "Выбрать все видимые карточки во всех колонках."),
      }, uiText("Select all visible", "Выбрать все видимые")),
      h(Button, {
        onClick: props.onClear,
        size: "sm",
        title: uiText("Deselect all tasks and hide this bar.", "Снять выделение со всех задач и скрыть панель."),
      }, tx(t, "clear", "Clear")),
    );
  }

  // -------------------------------------------------------------------------
  // Trash Drop Zone
  // -------------------------------------------------------------------------

  function TrashDropZone(props) {
    const { t } = useI18n();
    const [dragOver, setDragOver] = useState(false);
    const zoneRef = useRef(null);

    useEffect(function () {
      if (!zoneRef.current) return undefined;
      const el = zoneRef.current;
      function onTouchDelete(e) {
        const taskId = e.detail && e.detail.taskId;
        if (taskId && props.onDelete) props.onDelete(taskId);
      }
      el.addEventListener("hermes-kanban:delete", onTouchDelete);
      return function () { el.removeEventListener("hermes-kanban:delete", onTouchDelete); };
    }, [props.onDelete]);

    const handleDragOver = function (e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      if (!dragOver) setDragOver(true);
    };
    const handleDragLeave = function () { setDragOver(false); };
    const handleDrop = function (e) {
      e.preventDefault();
      setDragOver(false);
      const taskId = e.dataTransfer.getData(MIME_TASK);
      if (!taskId) return;
      if (props.selectedIds && props.selectedIds.has(taskId) && props.selectedIds.size > 1) {
        // Delegate to the bulk-delete path on the parent so we use a
        // single in-app confirmation modal. Falling back to the per-id
        // onDelete path (which would prompt N times) is preserved for
        // hosts that haven't wired onDeleteSelected.
        if (props.onDeleteSelected) {
          props.onDeleteSelected(props.selectedIds.size);
        } else {
          Promise.all(
            Array.from(props.selectedIds).map(function (id) { return props.onDelete(id); })
          ).catch(function () {});
        }
      } else {
        props.onDelete(taskId);
      }
    };

    return h("div", {
      ref: zoneRef,
      "data-kanban-trash": "true",
      className: cn(
        "hermes-kanban-trash",
        dragOver ? "hermes-kanban-trash--drop" : "",
        props.draggingTaskId ? "hermes-kanban-trash--active" : "",
      ),
      onDragOver: handleDragOver,
      onDragLeave: handleDragLeave,
      onDrop: handleDrop,
    },
      h("span", { className: "hermes-kanban-trash-icon" }, "🗑️"),
      h("span", { className: "hermes-kanban-trash-label" },
        tx(t, "trash.dropHint", FALLBACK_TRASH.dropHint)),
    );
  }

  // -------------------------------------------------------------------------
  // Columns
  // -------------------------------------------------------------------------

  function BoardColumns(props) {
    const columnsRef = useRef(null);
    const panRef = useRef({ isPanning: false, startX: 0, scrollLeft: 0 });
    const [isPanning, setIsPanning] = useState(false);
    const [isScrollable, setIsScrollable] = useState(false);

    const checkScrollable = useCallback(function () {
      const el = columnsRef.current;
      setIsScrollable(!!el && el.scrollWidth > el.clientWidth + 1);
    }, []);

    useEffect(function () {
      checkScrollable();
      const el = columnsRef.current;
      if (!el) return undefined;
      if (typeof ResizeObserver !== "undefined") {
        const observer = new ResizeObserver(checkScrollable);
        observer.observe(el);
        return function () { observer.disconnect(); };
      }
      window.addEventListener("resize", checkScrollable);
      return function () { window.removeEventListener("resize", checkScrollable); };
    }, [checkScrollable, props.board]);

    const isPanBlockedTarget = useCallback(function (target) {
      if (!target) return true;
      if (target.closest && target.closest(".hermes-kanban-card")) return true;
      if (target.closest && target.closest(".hermes-kanban-column-add")) return true;
      if (target.closest && target.closest(".hermes-kanban-col-check")) return true;
      if (target.closest && target.closest("button,input,textarea,select,a,[role='button']")) return true;
      return false;
    }, []);

    const stopPan = useCallback(function () {
      const el = columnsRef.current;
      if (!panRef.current.isPanning) return;
      panRef.current.isPanning = false;
      setIsPanning(false);
      if (el) {
        // Keep cursor feedback instant even before React flushes the state update.
        el.classList.remove("hermes-kanban-columns--panning");
        el.style.userSelect = "";
      }
      if (panRef.current.cleanup) panRef.current.cleanup();
      panRef.current.cleanup = null;
    }, []);

    useEffect(function () {
      return function () { stopPan(); };
    }, [stopPan]);

    const handleMouseDown = useCallback(function (e) {
      if (e.button !== 0) return;
      if (isPanBlockedTarget(e.target)) return;
      const el = columnsRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      // Preserve the native horizontal scrollbar as a fallback; grab-pan starts above it.
      if (e.clientY >= rect.bottom - 20) return;
      if (el.scrollWidth <= el.clientWidth) return;

      panRef.current.isPanning = true;
      panRef.current.startX = e.clientX;
      panRef.current.scrollLeft = el.scrollLeft;
      setIsPanning(true);
      el.classList.add("hermes-kanban-columns--panning");
      el.style.userSelect = "none";

      function onMouseMove(ev) {
        if (!panRef.current.isPanning) return;
        const dx = ev.clientX - panRef.current.startX;
        el.scrollLeft = panRef.current.scrollLeft - dx;
        ev.preventDefault();
      }
      function onMouseUp() { stopPan(); }

      if (panRef.current.cleanup) panRef.current.cleanup();
      window.addEventListener("mousemove", onMouseMove);
      window.addEventListener("mouseup", onMouseUp, { once: true });
      window.addEventListener("blur", onMouseUp, { once: true });
      panRef.current.cleanup = function () {
        window.removeEventListener("mousemove", onMouseMove);
        window.removeEventListener("mouseup", onMouseUp);
        window.removeEventListener("blur", onMouseUp);
      };
      e.preventDefault();
    }, [isPanBlockedTarget, stopPan]);

    const handleDragStart = useCallback(function (e) {
      const card = e.target.closest && e.target.closest(".hermes-kanban-card");
      if (!card) return;
      const taskId = card.getAttribute("data-task-id");
      if (taskId && props.onDragStart) props.onDragStart(taskId);
    }, [props.onDragStart]);
    const handleDragEnd = useCallback(function () {
      if (props.onDragEnd) props.onDragEnd();
    }, [props.onDragEnd]);
    return h("div", {
      ref: columnsRef,
      className: cn(
        "hermes-kanban-columns",
        isScrollable ? "hermes-kanban-columns--scrollable" : "",
        isPanning ? "hermes-kanban-columns--panning" : "",
      ),
      onDragStart: handleDragStart,
      onDragEnd: handleDragEnd,
      onMouseDown: handleMouseDown,
    },
      props.board.columns.map(function (col) {
        return h(Column, {
          key: col.name,
          column: col,
          boardMeta: props.boardMeta,
          laneByProfile: props.laneByProfile,
          selectedIds: props.selectedIds,
          failedIds: props.failedIds,
          draggingTaskId: props.draggingTaskId,
          toggleSelected: props.toggleSelected,
          toggleRange: props.toggleRange,
          selectAllInColumn: props.selectAllInColumn,
          onMove: props.onMove,
          onMoveSelected: props.onMoveSelected,
          onOpen: props.onOpen,
          onCreate: props.onCreate,
          allTasks: props.allTasks,
        });
      }),
      h(TrashDropZone, {
        draggingTaskId: props.draggingTaskId,
        selectedIds: props.selectedIds,
        onDelete: props.onDelete,
        onDeleteSelected: props.onDeleteSelected,
      }),
    );
  }

  function Column(props) {
    const { t, uiText, locale } = useI18n();
    const [dragOver, setDragOver] = useState(false);
    const [showCreate, setShowCreate] = useState(false);
    const colRef = useRef(null);

    // Listen for our synthetic touch-drop events from attachTouchDrag().
    useEffect(function () {
      if (!colRef.current) return undefined;
      const el = colRef.current;
      function onTouchDrop(e) {
        if (e.detail && e.detail.status === props.column.name) {
          const taskId = e.detail.taskId;
          if (props.selectedIds && props.selectedIds.has(taskId) && props.selectedIds.size > 1 && props.onMoveSelected) {
            props.onMoveSelected(props.column.name);
          } else {
            props.onMove(taskId, props.column.name);
          }
        }
      }
      el.addEventListener("hermes-kanban:drop", onTouchDrop);
      return function () { el.removeEventListener("hermes-kanban:drop", onTouchDrop); };
    }, [props.column.name, props.onMove, props.selectedIds, props.onMoveSelected]);

    const handleDragOver = function (e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      if (!dragOver) setDragOver(true);
    };
    const handleDragLeave = function () { setDragOver(false); };
    const handleDrop = function (e) {
      e.preventDefault();
      setDragOver(false);
      const taskId = e.dataTransfer.getData(MIME_TASK);
      if (!taskId) return;
      if (props.selectedIds && props.selectedIds.has(taskId) && props.selectedIds.size > 1) {
        if (props.onMoveSelected) props.onMoveSelected(props.column.name);
      } else {
        props.onMove(taskId, props.column.name);
      }
    };

    const lanes = useMemo(function () {
      if (!props.laneByProfile || props.column.name !== "running") return null;
      const byProfile = {};
      for (const tk of props.column.tasks) {
        const key = tk.assignee || "(unassigned)";
        (byProfile[key] = byProfile[key] || []).push(tk);
      }
      return Object.keys(byProfile).sort().map(function (k) {
        return { assignee: k, tasks: byProfile[k] };
      });
    }, [props.column, props.laneByProfile]);

    const colHelp = getColumnHelp(t, props.column.name);
    const colLabel = getColumnLabel(t, props.column.name);

    return h("div", {
      ref: colRef,
      "data-kanban-column": props.column.name,
      className: cn(
        "hermes-kanban-column",
        dragOver ? "hermes-kanban-column--drop" : "",
      ),
      onDragOver: handleDragOver,
      onDragLeave: handleDragLeave,
      onDrop: handleDrop,
    },
      h("div", { className: "hermes-kanban-column-header",
                 title: colHelp || "" },
        h(Checkbox, {
          className: "hermes-kanban-col-check",
          title: uiText("Select all tasks in this column", "Выбрать все задачи в этой колонке"),
          "aria-label": uiText(`Select all tasks in ${colLabel || props.column.name}`, `Выбрать все задачи в колонке «${colLabel || props.column.name}»`),
          checked: props.column.tasks.length > 0 && props.column.tasks.every(function (t) { return props.selectedIds.has(t.id); }),
          onCheckedChange: function () {
            if (props.selectAllInColumn) props.selectAllInColumn(props.column.name);
          },
          onClick: function (e) { e.stopPropagation(); },
        }),
        h("span", { className: cn("hermes-kanban-dot", COLUMN_DOT[props.column.name]) }),
        h("span", { className: "hermes-kanban-column-label" },
          colLabel || props.column.name),
        h("span", { className: "hermes-kanban-column-count",
                    title: /^ru(?:[-_]|$)/i.test(locale)
                      ? `${pluralRu(props.column.tasks.length, ["задача", "задачи", "задач"])} в колонке`
                      : `${props.column.tasks.length} task${props.column.tasks.length === 1 ? "" : "s"} in this column` },
          props.column.tasks.length),
        h("button", {
          type: "button",
          className: "hermes-kanban-column-add",
          title: tx(t, "createTask", "Create task in this column"),
          onClick: function () { setShowCreate(function (v) { return !v; }); },
        }, showCreate ? "×" : "+"),
      ),
      h("div", { className: "hermes-kanban-column-sub" },
        colHelp || ""),
      showCreate ? h(InlineCreate, {
        columnName: props.column.name,
        allTasks: props.allTasks,
        defaultWorkspaceKind: (props.boardMeta && props.boardMeta.default_workspace_kind) || "scratch",
        defaultWorkspacePath: (props.boardMeta && props.boardMeta.default_workdir) || "",
        onSubmit: function (body) {
          props.onCreate(body).then(function () { setShowCreate(false); });
        },
        onCancel: function () { setShowCreate(false); },
      }) : null,
      h("div", { className: "hermes-kanban-column-body" },
        props.column.tasks.length === 0
          ? h("div", { className: "hermes-kanban-empty" }, tx(t, "noTasks", "— no tasks —"))
          : lanes
            ? lanes.map(function (lane) {
                return h("div", { key: lane.assignee, className: "hermes-kanban-lane" },
                  h("div", { className: "hermes-kanban-lane-head" },
                    h("span", { className: "hermes-kanban-lane-name" }, operatorRoleLabel(lane.assignee)),
                    h("span", { className: "hermes-kanban-lane-count" }, lane.tasks.length),
                  ),
                  lane.tasks.map(function (tk) {
                    return h(TaskCard, {
                      key: tk.id, task: tk,
                      allTasks: props.allTasks,
                      selected: props.selectedIds.has(tk.id),
                      failed: props.failedIds && props.failedIds.has(tk.id),
                      draggingTaskId: props.draggingTaskId,
                      draggingSource: props.draggingTaskId && props.selectedIds.has(props.draggingTaskId) && props.selectedIds.size > 1 && props.selectedIds.has(tk.id),
                      toggleSelected: props.toggleSelected,
                      toggleRange: props.toggleRange,
                      onOpen: props.onOpen,
                    });
                  }),
                );
              })
            : props.column.tasks.map(function (tk) {
                return h(TaskCard, {
                  key: tk.id, task: tk,
                      allTasks: props.allTasks,
                  selected: props.selectedIds.has(tk.id),
                  failed: props.failedIds && props.failedIds.has(tk.id),
                  draggingTaskId: props.draggingTaskId,
                  draggingSource: props.draggingTaskId && props.selectedIds.has(props.draggingTaskId) && props.selectedIds.size > 1 && props.selectedIds.has(tk.id),
                  toggleSelected: props.toggleSelected,
                  toggleRange: props.toggleRange,
                  onOpen: props.onOpen,
                });
              }),
      ),
    );
  }

  // -------------------------------------------------------------------------
  // Card
  // -------------------------------------------------------------------------

  // Staleness tiers — amber after a grace window, red when clearly stuck.
  // Values below are seconds.
  const STALENESS = {
    ready:   { amber: 1 * 60 * 60,   red: 24 * 60 * 60 },
    running: { amber: 10 * 60,       red: 60 * 60 },
    blocked: { amber: 1 * 60 * 60,   red: 24 * 60 * 60 },
    todo:    { amber: 7 * 24 * 60 * 60, red: 30 * 24 * 60 * 60 },
  };

  function stalenessClass(task) {
    if (!task || !task.age) return "";
    const age = task.status === "running"
      ? task.age.started_age_seconds
      : task.age.created_age_seconds;
    const tier = STALENESS[task.status];
    if (!tier || age == null) return "";
    if (age >= tier.red)   return "hermes-kanban-card--stale-red";
    if (age >= tier.amber) return "hermes-kanban-card--stale-amber";
    return "";
  }

  function TaskCard(props) {
    const { t: i18n, uiText, localizedTimeAgo, locale } = useI18n();
    const t = props.task;
    const cardRef = useRef(null);

    useEffect(function () {
      return attachTouchDrag(cardRef.current, t.id);
    }, [t.id]);

    const handleDragStart = function (e) {
      e.dataTransfer.setData(MIME_TASK, t.id);
      e.dataTransfer.effectAllowed = "move";
      const selectedCards = document.querySelectorAll(".hermes-kanban-card--selected");
      if (selectedCards.length > 1 && props.selected) {
        const ghost = document.createElement("div");
        ghost.className = "hermes-kanban-drag-ghost";
        ghost.textContent = selectedCards.length + " cards";
        document.body.appendChild(ghost);
        e.dataTransfer.setDragImage(ghost, 0, 0);
        requestAnimationFrame(function () {
          if (ghost.parentNode) document.body.removeChild(ghost);
        });
      }
    };
    const handleClick = function (e) {
      if (e.shiftKey) {
        e.preventDefault();
        e.stopPropagation();
        if (props.toggleRange) props.toggleRange(t.id);
        return;
      }
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        e.stopPropagation();
        props.toggleSelected(t.id, true);
        return;
      }
      props.onOpen(t.id);
    };
    const handleKeyDown = function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        props.onOpen(t.id);
      }
      if (e.key === "Escape") {
        if (props.toggleSelected) props.toggleSelected(t.id, false);
      }
    };
    const handleCheckedChange = function () {
      props.toggleSelected(t.id, true);
    };

    const progress = t.progress;
    const needsAssignee = t.status === "ready" && !t.assignee;
    const state = operatorStatus(t, Object.assign({}, t._operatorContext || {}, { allTasks: props.allTasks || [], teamTask: t._team }));

    return h("div", {
      ref: cardRef,
      "data-task-id": t.id,
      className: cn(
        "hermes-kanban-card",
        props.selected ? "hermes-kanban-card--selected" : "",
        props.failed ? "hermes-kanban-card--failed" : "",
        props.draggingSource ? "hermes-kanban-card--dragging-source" : "",
        stalenessClass(t),
      ),
      draggable: true,
      tabIndex: 0,
      role: "button",
      "aria-label": `${t.title || "untitled"} — ${t.id} — ${t.status}`,
      onDragStart: handleDragStart,
      onClick: handleClick,
      onKeyDown: handleKeyDown,
    },
      h(Card, null,
        h(CardContent, { className: "hermes-kanban-card-content" },
          h("div", { className: "hermes-kanban-card-row" },
            h("label", {
              className: "hermes-kanban-card-check-wrap",
              title: tx(i18n, "selectForBulk", "Select for bulk actions"),
              onClick: function (e) { e.stopPropagation(); },
            },
              h(Checkbox, {
                className: "hermes-kanban-card-check",
                checked: props.selected,
                onCheckedChange: handleCheckedChange,
                onClick: function (e) { e.stopPropagation(); },
                "aria-label": uiText(`Select task ${t.id}`, `Выбрать задачу ${t.id}`),
              }),
            ),
            h("span", { className: "hermes-kanban-card-id",
                        title: uiText(`Task id: ${t.id}. Use this id with kanban_show, /kanban show, or hermes kanban show.`, `ID задачи: ${t.id}. Используйте его с kanban_show, /kanban show или hermes kanban show.`) }, t.id),
            t.warnings && t.warnings.count > 0
              ? h("span", {
                  className: cn(
                    "hermes-kanban-warning-badge",
                    "hermes-kanban-warning-badge--" + (t.warnings.highest_severity || "warning"),
                  ),
                  title: (
                    `${t.warnings.count} active diagnostic` +
                    (t.warnings.count === 1 ? "" : "s") +
                    ` (severity: ${t.warnings.highest_severity || "warning"}). ` +
                    uiText(`Click to open for details.`, `Нажмите, чтобы открыть подробности.`)
                  ),
                }, t.warnings.highest_severity === "critical" ? "!!!" :
                   t.warnings.highest_severity === "error" ? "!!" : "⚠")
              : null,
            t.priority > 0
              ? h(Badge, { className: "hermes-kanban-priority",
                           title: uiText(`Priority ${t.priority}. Higher-priority tasks are claimed first by the dispatcher.`, `Приоритет ${t.priority}. Диспетчер сначала берёт задачи с более высоким приоритетом.`) }, `P${t.priority}`)
              : null,
            t.tenant
              ? h(Badge, { variant: "outline", className: "hermes-kanban-tag",
                           title: uiText(`Tenant: ${t.tenant}. Free-form tag for grouping tasks (customer, project, team).`, `Тенант: ${t.tenant}. Произвольная метка для группировки задач по клиенту, проекту или команде.`) }, t.tenant)
              : null,
            progress
              ? h("span", {
                  className: cn(
                    "hermes-kanban-progress",
                    progress.done === progress.total ? "hermes-kanban-progress--full" : "",
                  ),
                  title: `${progress.done} of ${progress.total} child tasks done`,
                }, `${progress.done}/${progress.total}`)
              : null,
            needsAssignee
              ? h(Badge, {
                  variant: "outline",
                  className: "hermes-kanban-needs-assignee",
                  title: tx(i18n, "needsAssigneeHint", "Dependencies are satisfied, but the dispatcher skips this task until you assign a profile."),
                }, tx(i18n, "needsAssignee", "Needs assignee"))
              : null,
          ),
          h("div", { className: "hermes-kanban-card-title", title: t.title || "" },
            compactTaskTitle(t.title || tx(i18n, "untitled", "(untitled)"))),
          t.status === "blocked" || t.status === "ready" || t.status === "running"
            ? h("div", { className: "hermes-kanban-card-state hermes-kanban-card-state--" + state.tone,
                title: state.reason + " " + state.action },
                h("strong", null, state.title),
                t.status === "blocked" ? h("span", null, state.reason) : null)
            : null,
          h("div", { className: "hermes-kanban-card-row hermes-kanban-card-meta" },
            t.assignee
              ? h("span", { className: "hermes-kanban-assignee",
                            title: uiText(`Assigned to Hermes profile @${t.assignee}`, `Назначено профилю Hermes @${t.assignee}`) }, operatorRoleLabel(t.assignee))
              : h("span", { className: "hermes-kanban-unassigned",
                            title: needsAssignee
                              ? tx(i18n, "needsAssigneeHint", "Dependencies are satisfied, but the dispatcher skips this task until you assign a profile.")
                              : uiText("No profile assigned.", "Профиль не назначен.") },
                  tx(i18n, "unassigned", "unassigned")),
            t.comment_count > 0
              ? h("span", { className: "hermes-kanban-count",
                            title: /^ru(?:[-_]|$)/i.test(locale)
                              ? `${pluralRu(t.comment_count, ["комментарий", "комментария", "комментариев"])} к задаче`
                              : `${t.comment_count} comment${t.comment_count === 1 ? "" : "s"} on this task` }, "💬 ", t.comment_count)
              : null,
            t.link_counts && (t.link_counts.parents + t.link_counts.children) > 0
              ? h("span", { className: "hermes-kanban-count",
                            title: /^ru(?:[-_]|$)/i.test(locale)
                              ? `${pluralRu(t.link_counts.parents, ["родительская задача", "родительские задачи", "родительских задач"])}, ${pluralRu(t.link_counts.children, ["дочерняя задача", "дочерние задачи", "дочерних задач"])}. Дочерние задачи остаются заблокированными до завершения родительской.`
                              : `${t.link_counts.parents} parent${t.link_counts.parents === 1 ? "" : "s"}, ${t.link_counts.children} child${t.link_counts.children === 1 ? "" : "ren"}. Children stay blocked until their parent is done.` },
                  "↔ ", t.link_counts.parents + t.link_counts.children)
              : null,
            h("span", { className: "hermes-kanban-ago",
                        title: t.created_at ? uiText(`Created ${t.created_at}`, `Создано ${t.created_at}`) : "" },
              localizedTimeAgo(t.created_at)),
          ),
        ),
      ),
    );
  }

  // -------------------------------------------------------------------------
  // Create-task dialog (modal, with parent selector)
  //
  // Launched from a column's [+] button. Was an inline form squeezed into
  // the ~280px column (8 fields, unlabeled, no room to breathe); now a
  // centered modal reusing the hermes-kanban-dialog chrome so the form is
  // resizable-window friendly and every field has a visible label.
  // -------------------------------------------------------------------------

  function InlineCreate(props) {
    const { t, uiText } = useI18n();
    const [title, setTitle] = useState("");
    const [assignee, setAssignee] = useState("");
    const [priority, setPriority] = useState(0);
    const [parent, setParent] = useState("");
    const [skills, setSkills] = useState("");
    // A board with a configured workdir defaults to a persistent workspace:
    // worktree for git repositories, dir for ordinary directories. Boards
    // without one keep scratch for disposable research and ops tasks.
    const defaultWorkspaceKind = props.defaultWorkspaceKind || "scratch";
    const defaultWorkspacePath = props.defaultWorkspacePath || "";
    const [workspaceKind, setWorkspaceKind] = useState(defaultWorkspaceKind);
    const [workspacePath, setWorkspacePath] = useState(defaultWorkspacePath);
    // Goal-mode: when on, the dispatched worker runs the Ralph-style /goal
    // loop — a judge re-checks the card after each turn and the worker keeps
    // going in the same session until done, or the turn budget runs out
    // (which blocks the card for review). goalMaxTurns is optional; blank
    // = backend default.
    const [goalMode, setGoalMode] = useState(false);
    const [goalMaxTurns, setGoalMaxTurns] = useState("");

    const submit = function () {
      const trimmed = title.trim();
      if (!trimmed) return;
      const body = {
        title: trimmed,
        assignee: assignee.trim() || null,
        priority: Number(priority) || 0,
        triage: props.columnName === "triage",
      };
      if (parent) body.parents = [parent];
      // Parse comma-separated skills into a clean list. Blank = no
      // extras (omit key so backend leaves it null). The dispatcher
      // always auto-loads kanban-worker; these are extras on top.
      const skillList = skills
        .split(",")
        .map(function (s) { return s.trim(); })
        .filter(function (s) { return s.length > 0; });
      if (skillList.length > 0) body.skills = skillList;
      // Only send workspace_kind when it's non-default. Keeps the request
      // shape small and interoperable with older dispatcher versions.
      if (workspaceKind && workspaceKind !== "scratch") {
        body.workspace_kind = workspaceKind;
      }
      const wpTrim = workspacePath.trim();
      if (wpTrim) body.workspace_path = wpTrim;
      // Goal-mode toggle. Only send the keys when enabled so the request
      // shape stays small and old dispatchers ignore it cleanly.
      if (goalMode) {
        body.goal_mode = true;
        const gmt = parseInt(goalMaxTurns, 10);
        if (Number.isFinite(gmt) && gmt > 0) body.goal_max_turns = gmt;
      }
      props.onSubmit(body);
      setTitle(""); setAssignee(""); setPriority(0); setParent(""); setSkills("");
      setWorkspaceKind(defaultWorkspaceKind); setWorkspacePath(defaultWorkspacePath);
      setGoalMode(false); setGoalMaxTurns("");
    };

    const showPathInput = workspaceKind !== "scratch";
    const pathPlaceholder = workspaceKind === "dir"
      ? tx(t, "workspacePathDir", "workspace path (required without a board workdir)")
      : tx(t, "workspacePathOptional",
          "repository path (optional when the board has a workdir)");

    const fieldLabel = function (text, hint) {
      return h(Label, { className: "text-xs" }, text,
        hint ? h("span", { className: "text-muted-foreground" }, " ", hint) : null);
    };

    return h("div", {
      className: "hermes-kanban-dialog-backdrop",
      onClick: function (e) { if (e.target === e.currentTarget) props.onCancel(); },
      onKeyDown: function (e) { if (e.key === "Escape") props.onCancel(); },
    },
      h("form", {
        className: "hermes-kanban-dialog hermes-kanban-create-dialog",
        onSubmit: function (e) { e.preventDefault(); submit(); },
      },
        h("div", { className: "hermes-kanban-dialog-title" },
          tx(t, "newTaskTitle", "New task — {column}",
            { column: getColumnLabel(t, props.columnName) || props.columnName })),
        h("div", { className: "flex flex-col gap-3" },
          h("div", { className: "flex flex-col gap-1" },
            fieldLabel(tx(t, "taskTitleLabel", "Title")),
            h("textarea", {
              value: title,
              onChange: function (e) { setTitle(e.target.value); },
              onKeyDown: function (e) {
                if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
              },
              placeholder: props.columnName === "triage"
                ? tx(t, "triagePlaceholder", "Rough idea — AI will spec it…")
                : tx(t, "taskTitlePlaceholder", "New task title…"),
              autoFocus: true,
              className: "text-sm min-h-[3rem] max-h-48 resize-y w-full border border-input bg-transparent px-2 py-1 rounded-md focus:outline-none focus:ring-2 focus:ring-ring",
              rows: 3,
            }),
          ),
          h("div", { className: "flex gap-2" },
            h("div", { className: "flex flex-col gap-1 flex-1" },
              fieldLabel(props.columnName === "triage"
                ? tx(t, "specifier", "specifier")
                : tx(t, "assigneeLabel", "Assignee"),
                tx(t, "assigneeLabelHint", "(blank = dispatcher picks)")),
              h(Input, {
                value: assignee,
                onChange: function (e) { setAssignee(e.target.value); },
                placeholder: props.columnName === "triage"
                  ? tx(t, "specifier", "specifier")
                  : tx(t, "assigneePlaceholder", "assignee"),
                className: "h-8 text-sm",
                title: props.columnName === "triage"
                  ? uiText("Hermes profile that will spec this task (default: the dispatcher's configured specifier). Leave blank to let the dispatcher pick.", "Профиль Hermes, который подготовит спецификацию. Оставьте поле пустым, чтобы профиль выбрал диспетчер.")
                  : uiText("Hermes profile to assign. Leave blank and the dispatcher will pick from available profiles when the task is Ready.", "Профиль Hermes для задачи. Оставьте поле пустым, чтобы диспетчер выбрал его, когда задача будет готова."),
                style: { textTransform: "none" },
                autoCapitalize: "none",
                autoCorrect: "off",
                spellCheck: false,
              }),
            ),
            h("div", { className: "flex flex-col gap-1 w-20" },
              fieldLabel(tx(t, "priority", "Priority")),
              h(Input, {
                type: "number",
                value: priority,
                onChange: function (e) { setPriority(e.target.value); },
                placeholder: "pri",
                className: "h-8 text-sm",
                title: uiText("Priority. Higher-priority tasks are claimed first by the dispatcher. 0 = default.", "Приоритет задачи. Диспетчер сначала берёт задачи с более высоким приоритетом. 0 — обычный."),
              }),
            ),
          ),
          h("div", { className: "flex flex-col gap-1" },
            fieldLabel(tx(t, "skillsLabel", "Skills"),
              tx(t, "skillsLabelHint", "(optional, comma-separated)")),
            h(Input, {
              value: skills,
              onChange: function (e) { setSkills(e.target.value); },
              placeholder: tx(t, "skillsPlaceholder",
                "skills (optional, comma-separated): translation, github-code-review"),
              title: uiText("Force-load these skills into the worker (in addition to the built-in kanban-worker).", "Дополнительно загрузить эти навыки исполнителю вместе со встроенным kanban-worker."),
              className: "h-8 text-sm",
            }),
          ),
          h("div", { className: "flex flex-col gap-1" },
            fieldLabel(tx(t, "workspace", "Workspace")),
            h("div", { className: "flex gap-2" },
              h(Select, Object.assign({
                value: workspaceKind,
                title: uiText("Choose whether task files are temporary or preserved after completion.", "Выберите, удалять или сохранять файлы задачи после завершения."),
                className: "h-8 text-sm flex-1",
              }, selectChangeHandler(setWorkspaceKind)),
                h(SelectOption, { value: "scratch" },
                  tx(t, "workspaceScratch", "Temporary — deleted on completion")),
                h(SelectOption, { value: "worktree" },
                  tx(t, "workspaceWorktree", "Git worktree — preserved")),
                h(SelectOption, { value: "dir" },
                  tx(t, "workspaceDir", "Directory — preserved")),
              ),
              showPathInput ? h(Input, {
                value: workspacePath,
                onChange: function (e) { setWorkspacePath(e.target.value); },
                placeholder: pathPlaceholder,
                className: "h-8 text-sm flex-1",
              }) : null,
            ),
            workspaceKind === "scratch" ? h("div", {
              className: "text-xs text-destructive",
              role: "alert",
            }, tx(t, "workspaceScratchWarning",
              uiText("This workspace and any files left in it are deleted when the task completes.", "Рабочее пространство и оставшиеся файлы будут удалены после завершения задачи."))) : null,
          ),
          h("div", { className: "flex flex-col gap-1" },
            fieldLabel(tx(t, "parentLabel", "Parent task"),
              tx(t, "parentLabelHint", "(child stays blocked until the parent is done)")),
            h(Select, Object.assign({
              value: parent,
              className: "h-8 text-sm",
              title: uiText("Optional parent task. A child stays blocked in its current column until the parent is marked done.", "Необязательная родительская задача. Дочерняя останется заблокированной, пока родительская не будет завершена."),
            }, selectChangeHandler(setParent)),
              h(SelectOption, { value: "" }, tx(t, "noParent", "— no parent —")),
              (props.allTasks || []).map(function (task) {
                return h(SelectOption, { key: task.id, value: task.id },
                  `${task.id} — ${(task.title || "").slice(0, 50)}`);
              }),
            ),
          ),
          h("div", { className: "flex gap-2 items-center" },
            h("label", {
              className: "flex items-center gap-1.5 text-xs cursor-pointer select-none",
              title: uiText("Goal mode: the worker keeps going in the same session until a judge agrees the card is done (or the turn budget runs out, which blocks it for review). Best for open-ended cards one shot rarely finishes.", "Режим цели: исполнитель работает в одной сессии, пока судья не подтвердит завершение. При исчерпании лимита ходов задача блокируется для проверки. Подходит для больших задач, которые не решить за один запуск."),
            },
              h("input", {
                type: "checkbox",
                checked: goalMode,
                onChange: function (e) { setGoalMode(!!e.target.checked); },
                className: "h-3.5 w-3.5 accent-current",
              }),
              tx(t, "goalMode", "goal mode"),
            ),
            goalMode ? h(Input, {
              type: "number",
              value: goalMaxTurns,
              onChange: function (e) { setGoalMaxTurns(e.target.value); },
              placeholder: tx(t, "goalMaxTurns", "max turns (default 20)"),
              className: "h-8 text-sm w-44",
              title: uiText("Turn budget for the goal loop. Blank = backend default (20).", "Лимит ходов в режиме цели. Пустое значение — значение по умолчанию (20)."),
              min: 1,
            }) : null,
          ),
        ),
        h("div", { className: "hermes-kanban-dialog-actions" },
          h(Button, {
            type: "button",
            onClick: props.onCancel,
            size: "sm",
          }, tx(t, "cancel", "Cancel")),
          h(Button, {
            type: "submit",
            size: "sm",
            disabled: !title.trim(),
          }, tx(t, "create", "Create")),
        ),
      ),
    );
  }

  // -------------------------------------------------------------------------
  // Task drawer
  // -------------------------------------------------------------------------

  function TaskDrawer(props) {
    const { t } = useI18n();
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState(null);
    // Surface PATCH failures (e.g. 409 "parent not done") right next to
    // the drawer's action row — without it, the drawer's only error
    // surface (``err``) is hidden behind the loaded ``data`` and the
    // Ready/Block/Complete buttons feel like no-ops.  See #26744.
    const [patchErr, setPatchErr] = useState(null);
    const [newComment, setNewComment] = useState("");
    const [uploadBusy, setUploadBusy] = useState(false);
    const [uploadErr, setUploadErr] = useState(null);
    const [editing, setEditing] = useState(false);
    // Home-channel notification toggles. homeChannels is the list of platforms
    // the user has a /sethome on; each entry has a `subscribed` bool telling
    // us whether this task is currently subscribed via that platform's home.
    const [homeChannels, setHomeChannels] = useState([]);
    const [homeBusy, setHomeBusy] = useState({});
    const boardSlug = props.boardSlug;

    const load = useCallback(function () {
      return SDK.fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}`, boardSlug))
        .then(function (d) { setData(d); setErr(null); setPatchErr(null); })
        .catch(function (e) { setErr(String(e.message || e)); })
        .finally(function () { setLoading(false); });
    }, [props.taskId, boardSlug]);

    const loadHomeChannels = useCallback(function () {
      const qs = new URLSearchParams({ task_id: props.taskId });
      const url = withBoard(`${API}/home-channels?${qs}`, boardSlug);
      return SDK.fetchJSON(url)
        .then(function (d) { setHomeChannels(d.home_channels || []); })
        .catch(function () { /* silent — endpoint optional on older gateways */ });
    }, [props.taskId, boardSlug]);

    // Reload when the WS stream reports new events for this task id
    // (completion, block, crash, etc. — anything that'd make the drawer
    // show stale data if we only loaded on mount).
    useEffect(function () { load(); }, [load, props.eventTick]);
    useEffect(function () { loadHomeChannels(); }, [loadHomeChannels]);
    useEffect(function () {
      function onKey(e) { if (e.key === "Escape" && !editing) props.onClose(); }
      window.addEventListener("keydown", onKey);
      return function () { window.removeEventListener("keydown", onKey); };
    }, [props.onClose, editing]);

    const handleComment = function () {
      const body = newComment.trim();
      if (!body) return;
      SDK.fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/comments`, boardSlug), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ body }),
      }).then(function () {
        setNewComment("");
        load();
        props.onRefresh();
      }).catch(function (e) { setErr(String(e.message || e)); });
    };

    // File upload uses raw fetch (not SDK.fetchJSON, which JSON-encodes)
    // so the browser sets the multipart boundary. Auth rides the session
    // cookie + bearer token, matching the rest of the dashboard.
    const handleUpload = function (fileList) {
      const files = Array.prototype.slice.call(fileList || []);
      if (!files.length) return;
      setUploadBusy(true);
      setUploadErr(null);
      const url = withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/attachments`, boardSlug);
      // Upload sequentially so a partial failure leaves a clear state.
      let chain = Promise.resolve();
      files.forEach(function (f) {
        chain = chain.then(function () {
          const fd = new FormData();
          fd.append("file", f, f.name);
          // SDK.authedFetch handles auth in BOTH modes (loopback token header /
          // gated cookie) and applies the dashboard base-path prefix. The old
          // hand-rolled Authorization:Bearer + credentials:'same-origin' sent
          // an empty token and 401'd in gated mode.
          return SDK.authedFetch(url, { method: "POST", body: fd })
            .then(function (resp) {
              if (!resp.ok) {
                return resp.text().then(function (txt) {
                  throw new Error(parseApiErrorMessage(new Error(resp.status + ": " + txt)));
                });
              }
            });
        });
      });
      chain.then(function () {
        load();
        props.onRefresh();
      }).catch(function (e) {
        setUploadErr(String(e.message || e));
      }).finally(function () {
        setUploadBusy(false);
      });
    };

    const handleDeleteAttachment = function (attachmentId) {
      return SDK.fetchJSON(withBoard(`${API}/attachments/${attachmentId}`, boardSlug), { method: "DELETE" })
        .then(function () { load(); props.onRefresh(); })
        .catch(function (e) { setUploadErr(String(e.message || e)); });
    };

    // doPatch is invoked by the side-drawer's StatusActions (block / unblock
    // / complete / archive), PriorityEditor, AssigneeEditor, etc. Two
    // requirements differ from the column-card drag path:
    //
    // 1. Confirmation: this happens via the in-app dialog flow exposed
    //    on `props` by the parent (KanbanPage passes a `requestDialog`
    //    function down). Falls back to a native window.confirm if the
    //    parent didn't wire one up.
    //
    // 2. Completion summary for status=done: until ConfirmDialog grows a
    //    `disabled` prop upstream (see #50547 followups), we keep the
    //    prompt + alert as a documented carve-out for this single call
    //    site. The prompt body, validation copy, and requirement are
    //    unchanged from the pre-migration implementation.
    const doPatch = function (patch, opts) {
      if (opts && opts.confirm && props.requestDialog) {
        return props.requestDialog({
          kind: "confirm",
              title: opts.confirmTitle || tx(t, "confirmTitle", "Confirm change"),
          description: opts.confirm,
          confirmLabel: opts.confirmLabel || tx(t, "common.confirm", "Confirm"),
          destructive: !!opts.destructive,
        }).then(function (r) {
          if (!r.confirmed) return null;
          return applyPatch(patch);
        });
      }
      if (opts && opts.confirm && !window.confirm(opts.confirm)) {
        return Promise.resolve();
      }
      return applyPatch(patch);

      function applyPatch(patch) {
        const finalPatch = withCompletionSummary(patch);
        if (!finalPatch) return Promise.resolve();
        setPatchErr(null);
        return SDK.fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}`, boardSlug), {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(finalPatch),
        }).then(function () { load(); props.onRefresh(); })
          .catch(function (e) { setPatchErr(parseApiErrorMessage(e)); });
      }
    };

    // Local completion-summary prompt used only by doPatch above.
    // Documented carve-out — see the doPatch comment.
    function withCompletionSummary(patch) {
      if (!patch || patch.status !== "done") return patch;
      const value = window.prompt(
        tx(t, "completionSummary",
          "Completion summary for this task. This is stored as the task result."),
        "",
      );
      if (value === null) return null;
      const summary = value.trim();
      if (!summary) {
        window.alert(tx(t, "completionSummaryRequired",
          "Completion summary is required before marking a task done."));
        return null;
      }
      return Object.assign({}, patch, { result: summary, summary: summary });
    }

    // Triage specifier — calls the auxiliary LLM to flesh out a rough
    // idea in the Triage column into a concrete spec (title + body with
    // goal, approach, acceptance criteria) and promotes it to todo.
    // Not a PATCH: runs through a dedicated POST endpoint because the
    // LLM call can take tens of seconds, and its outcome is richer than
    // a status flip (may update title AND body AND emit an audit
    // comment — or fail with a human-readable reason that the UI
    // surfaces inline without treating it as an HTTP error).
    const doSpecify = function () {
      return SDK.fetchJSON(
        withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/specify`, boardSlug),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        }
      ).then(function (res) {
        load();
        props.onRefresh();
        return res;
      });
    };

    // POST /tasks/:id/decompose — fan a triage task out into a graph
    // of child tasks routed to specialist profiles by description.
    // Refreshes both the drawer (so the user sees the root flip to
    // todo) and the board (so the new children appear in the columns).
    const doDecompose = function () {
      return SDK.fetchJSON(
        withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/decompose`, boardSlug),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        }
      ).then(function (res) {
        load();
        props.onRefresh();
        return res;
      });
    };

    const addLink = function (parentId) {
      return SDK.fetchJSON(withBoard(`${API}/links`, boardSlug), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ parent_id: parentId, child_id: props.taskId }),
      }).then(function () { load(); props.onRefresh(); })
        .catch(function (e) { setErr(String(e.message || e)); });
    };
    const removeLink = function (parentId) {
      const qs = new URLSearchParams({ parent_id: parentId, child_id: props.taskId });
      return SDK.fetchJSON(withBoard(`${API}/links?${qs}`, boardSlug), { method: "DELETE" })
        .then(function () { load(); props.onRefresh(); })
        .catch(function (e) { setErr(String(e.message || e)); });
    };
    const addChild = function (childId) {
      return SDK.fetchJSON(withBoard(`${API}/links`, boardSlug), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ parent_id: props.taskId, child_id: childId }),
      }).then(function () { load(); props.onRefresh(); })
        .catch(function (e) { setErr(String(e.message || e)); });
    };
    const removeChild = function (childId) {
      const qs = new URLSearchParams({ parent_id: props.taskId, child_id: childId });
      return SDK.fetchJSON(withBoard(`${API}/links?${qs}`, boardSlug), { method: "DELETE" })
        .then(function () { load(); props.onRefresh(); })
        .catch(function (e) { setErr(String(e.message || e)); });
    };

    const toggleHomeSubscription = function (platform, currentlySubscribed) {
      // Optimistic flip + busy flag to keep double-clicks idempotent.
      setHomeBusy(function (b) { return Object.assign({}, b, { [platform]: true }); });
      setHomeChannels(function (list) {
        return list.map(function (h) {
          return h.platform === platform
            ? Object.assign({}, h, { subscribed: !currentlySubscribed })
            : h;
        });
      });
      const method = currentlySubscribed ? "DELETE" : "POST";
      const url = withBoard(
        `${API}/tasks/${encodeURIComponent(props.taskId)}/home-subscribe/${encodeURIComponent(platform)}`,
        boardSlug,
      );
      return SDK.fetchJSON(url, { method: method })
        .then(function () { return loadHomeChannels(); })
        .catch(function (e) {
          // Revert optimistic flip on failure.
          setHomeChannels(function (list) {
            return list.map(function (h) {
              return h.platform === platform
                ? Object.assign({}, h, { subscribed: currentlySubscribed })
                : h;
            });
          });
          setErr(String(e.message || e));
        })
        .finally(function () {
          setHomeBusy(function (b) {
            const next = Object.assign({}, b);
            delete next[platform];
            return next;
          });
        });
    };

    return h("div", { className: "hermes-kanban-drawer-shade", onClick: props.onClose },
      h("div", {
        className: "hermes-kanban-drawer",
        onClick: function (e) { e.stopPropagation(); },
      },
        h("div", { className: "hermes-kanban-drawer-head" },
          h("button", {
            type: "button",
            onClick: props.onClose,
            className: "hermes-kanban-drawer-close",
            title: tx(t, "close", "Close (Esc)"),
          }, "×"),
        ),
        loading ? h("div", { className: "p-4 text-sm text-muted-foreground" },
          tx(t, "loadingDetail", "Loading…")) :
        err ? h("div", { className: "p-4 text-sm text-destructive" }, err) :
        data ? h(TaskDetail, {
          data, editing, setEditing,
          teamTask: props.teamTask,
          renderMarkdown: props.renderMarkdown,
          allTasks: props.allTasks,
          assignees: props.assignees || [],
          boardSlug: boardSlug,
          onPatch: doPatch,
          onSpecify: doSpecify,
          onDecompose: doDecompose,
          onAddParent: addLink,
          onRemoveParent: removeLink,
          onAddChild: addChild,
          onRemoveChild: removeChild,
          homeChannels: homeChannels,
          homeBusy: homeBusy,
          onToggleHomeSub: toggleHomeSubscription,
          onRefresh: props.onRefresh,
          onUpload: handleUpload,
          onDeleteAttachment: handleDeleteAttachment,
          uploadBusy: uploadBusy,
          uploadErr: uploadErr,
          onOpenTask: function (taskId) {
            props.onClose();
            if (props.onOpenTask) props.onOpenTask(taskId);
          },
                    requestDialog: props.requestDialog,
        }) : null,
        data ? h("div", { className: "hermes-kanban-drawer-comment-foot" },
          h("div", {
            className: "hermes-kanban-comment-hint text-xs text-muted-foreground",
            title: tx(t, "commentHintTitle",
              "Comments are the channel for talking to a task's worker. They land on the thread immediately — no need to block the task first. A running worker picks the thread up on its next kanban_show() or respawn; blocking is only for when you want the worker to STOP and wait for your input."),
          },
            "Комментарий будет доступен исполнителю при следующем обращении к задаче.",
          ),
          h("div", { className: "hermes-kanban-drawer-comment-row" },
            h(Input, {
              value: newComment,
              onChange: function (e) { setNewComment(e.target.value); },
              onKeyDown: function (e) {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault(); handleComment();
                }
              },
              placeholder: tx(t, "addComment", "Add a comment… (Enter to submit)"),
              className: "h-8 text-sm flex-1",
            }),
            h(Button, {
              onClick: handleComment,
              size: "sm",
            }, tx(t, "comment", "Comment")),
          ),
        ) : null,
      ),
    );
  }

  function _fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  // Attachments section in the task drawer (#35338). Upload button +
  // list with download links and a delete (×) per row. The download
  // link hits GET /attachments/:id which streams the file; the worker
  // context surfaces the same files' absolute paths so a kanban worker
  // can read them with the file/terminal tools.
  function AttachmentsSection(props) {
    const i18n = props.i18n;
    const atts = props.attachments || [];
    const fileRef = useRef(null);
    const [dlErr, setDlErr] = useState(null);
    // Download via authenticated fetch → blob → synthetic anchor click.
    // A plain <a href> can't carry the auth the dashboard middleware requires,
    // so fetch authenticated and hand the browser a blob URL instead.
    function downloadAttachment(a) {
      // SDK.authedFetch handles auth in BOTH modes (loopback token header /
      // gated cookie) and applies the dashboard base-path prefix. The old
      // hand-rolled Authorization:Bearer + credentials:'same-origin' sent an
      // empty token and 401'd in gated mode.
      const url = withBoard(`${API}/attachments/${a.id}`, props.boardSlug);
      setDlErr(null);
      SDK.authedFetch(url)
        .then(function (resp) {
          if (!resp.ok) {
            return resp.text().then(function (txt) {
              throw new Error(parseApiErrorMessage(new Error(resp.status + ": " + txt)));
            });
          }
          return resp.blob();
        })
        .then(function (blob) {
          const objUrl = URL.createObjectURL(blob);
          const link = document.createElement("a");
          link.href = objUrl;
          link.download = a.filename || "attachment";
          document.body.appendChild(link);
          link.click();
          document.body.removeChild(link);
          setTimeout(function () { URL.revokeObjectURL(objUrl); }, 10000);
        })
        .catch(function (e) { setDlErr(String(e.message || e)); });
    }
    return h("div", { className: "hermes-kanban-section" },
      h("div", { className: "hermes-kanban-section-head" },
        `${tx(i18n, "attachments", "Attachments")} (${atts.length})`),
      h("input", {
        ref: fileRef,
        type: "file",
        multiple: true,
        style: { display: "none" },
        onChange: function (e) {
          if (props.onUpload) props.onUpload(e.target.files);
          // Reset so selecting the same file again re-triggers onChange.
          try { e.target.value = ""; } catch (_e) { /* ignore */ }
        },
      }),
      h("div", { className: "flex items-center gap-2 mb-2" },
        h(Button, {
          size: "sm",
          variant: "outline",
          disabled: !!props.uploadBusy,
          onClick: function () { if (fileRef.current) fileRef.current.click(); },
        }, props.uploadBusy
            ? tx(i18n, "uploading", "Uploading…")
            : tx(i18n, "uploadFile", "Upload file")),
      ),
      (props.uploadErr || dlErr)
        ? h("div", { className: "text-xs text-destructive mb-2" }, props.uploadErr || dlErr)
        : null,
      atts.length === 0
        ? h("div", { className: "text-xs text-muted-foreground" },
            tx(i18n, "noAttachments", "— no attachments —"))
        : atts.map(function (a) {
            return h("div", {
              key: a.id,
              className: "flex items-center justify-between gap-2 py-1 text-sm",
            },
              h("button", {
                type: "button",
                className: "hermes-kanban-attachment-link truncate",
                title: a.filename,
                onClick: function () { downloadAttachment(a); },
              }, a.filename),
              h("span", { className: "text-xs text-muted-foreground whitespace-nowrap" },
                _fmtBytes(a.size)),
              h("button", {
                type: "button",
                className: "hermes-kanban-drawer-close",
                title: tx(i18n, "removeAttachment", "Remove attachment"),
                onClick: function () {
                  if (props.requestDialog) {
                    props.requestDialog({
                      kind: "confirm",
                      title: tx(i18n, "removeAttachment", "Remove attachment"),
                      description: tx(i18n, "confirmRemoveAttachment",
                        "Remove this attachment?"),
                      confirmLabel: tx(i18n, "common.delete", "Delete"),
                      destructive: true,
                    }).then(function (r) {
                      if (r.confirmed && props.onDelete) props.onDelete(a.id);
                    }).catch(function () { /* cancelled */ });
                  } else if (window.confirm(tx(i18n, "confirmRemoveAttachment",
                      "Remove this attachment?"))) {
                    if (props.onDelete) props.onDelete(a.id);
                  }
                },
              }, "×"),
            );
          }),
    );
  }

  function TaskDetail(props) {
    const { t: i18n, localizedTimeAgo, uiText } = useI18n();
    const t = props.data.task;
    const allComments = props.data.comments || [];
    const isServiceComment = function (comment) {
      return comment.author === "team-controller" && /^(?:Назначение по доступному лимиту:|Ожидание ресурса подписки:)/.test(comment.body || "");
    };
    const comments = allComments.filter(function (comment) { return !isServiceComment(comment); });
    const serviceComments = allComments.filter(isServiceComment);
    const events = props.data.events || [];
    const attachments = props.data.attachments || [];
    const links = props.data.links || { parents: [], children: [] };
    const childResults = (props.data.child_results || []).filter(function (child) { return child.result || child.latest_summary; });
    const operatorContext = {
      comments: comments,
      events: events,
      runs: props.data.runs || [],
      links: links,
      allTasks: props.allTasks || [],
      diagnostics: t.diagnostics || props.data.diagnostics || [],
      teamTask: props.teamTask || null,
    };
    const operatorState = typeof operatorStatus === "function"
      ? operatorStatus(t, operatorContext)
      : null;
    const compactTitle = typeof compactTaskTitle === "function"
      ? compactTaskTitle(t.title || "")
      : (t.title || tx(i18n, "untitled", "(untitled)"));
    const drawerText = function (en, ru) { return uiText ? uiText(en, ru) : en; };
    const assigneeLabel = t.assignee
      ? (typeof operatorRoleLabel === "function" ? operatorRoleLabel(t.assignee) : t.assignee)
      : drawerText("Unassigned", "Не назначен");

    function plainPreview(source, limit) {
      const plain = String(source || "")
        .replace(/```[\s\S]*?```/g, " фрагмент кода ")
        .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
        .replace(/[`*_>#~|]/g, " ")
        .replace(/\s+/g, " ")
        .trim();
      if (plain.length <= limit) return plain;
      return plain.slice(0, Math.max(0, limit - 1)).trimEnd() + "…";
    }

    function reportBlock(label, source, key) {
      if (!source) return null;
      if (String(source).length <= 700) {
        return h(MarkdownBlock, { source: source, enabled: props.renderMarkdown });
      }
      return h("div", { key: key, className: "hermes-kanban-long-report" },
        h("p", { className: "hermes-kanban-long-report__preview" }, plainPreview(source, 220)),
        disclosure(label,
          h(MarkdownBlock, { source: source, enabled: props.renderMarkdown }),
        ),
      );
    }

    function renderComment(c) {
      return h("div", { key: c.id, className: "hermes-kanban-comment" },
        h("div", { className: "hermes-kanban-comment-head" },
          h("span", { className: "hermes-kanban-comment-author" }, c.author || "anon"),
          h("span", { className: "hermes-kanban-comment-ago" }, localizedTimeAgo(c.created_at)),
        ),
        reportBlock(drawerText("Full comment", "Полный комментарий"), c.body, c.id),
      );
    }

    function disclosure(label, content, className) {
      return h("details", {
        className: cn("hermes-kanban-disclosure", className || ""),
      },
        h("summary", { className: "hermes-kanban-detail-heading" }, label),
        content,
      );
    }

    return h("div", { className: "hermes-kanban-drawer-body" },
      h("div", { className: "hermes-kanban-drawer-title" },
        h("span", { className: cn("hermes-kanban-dot", COLUMN_DOT[t.status]) }),
        props.editing
          ? h(TitleEditor, {
              initial: t.title || "",
              onSave: function (newTitle) {
                return props.onPatch({ title: newTitle }).then(function () { props.setEditing(false); });
              },
              onCancel: function () { props.setEditing(false); },
            })
          : h("span", {
              className: "hermes-kanban-drawer-title-text",
              title: `${t.title || ""}\n${tx(i18n, "clickToEdit", "Click to edit")}`,
              onClick: function () { props.setEditing(true); },
            }, compactTitle),
      ),
      h("div", { className: "hermes-kanban-detail-assignee" },
        h("span", { className: "hermes-kanban-detail-assignee__label" }, drawerText("Assignee", "Исполнитель")),
        h("span", { className: "hermes-kanban-detail-assignee__value" }, assigneeLabel),
        props.teamTask && props.teamTask.request_id ? h("a", {
          href: "/engineering-team?request=" + encodeURIComponent(props.teamTask.request_id),
          className: "hermes-kanban-request-link",
        }, "Открыть запрос команды") : null,
      ),
      operatorState ? h("section", {
        className: cn("hermes-kanban-operator-status", "hermes-kanban-operator-status--" + (operatorState.tone || "neutral")),
        "aria-label": drawerText("Current status", "Текущий статус"),
      },
        h("h3", { className: "hermes-kanban-operator-status__title" }, operatorState.title),
        operatorState.reason ? h("p", { className: "hermes-kanban-operator-status__reason" }, operatorState.reason) : null,
        operatorState.action ? h("p", { className: "hermes-kanban-operator-status__action" }, operatorState.action) : null,
        operatorState.href ? h("a", {
          className: "hermes-kanban-operator-status__link",
          href: operatorState.href,
        }, operatorState.linkLabel || drawerText("Open request", "Открыть запрос")) : null,
      ) : null,
      (function () {
        var finalResult = t.result || t.latest_summary || null;
        var isDone = t.status === "done";
        var isParent = links.children.length > 0;
        if (finalResult) {
          var label = t.result
            ? tx(i18n, "result", "Result")
            : tx(i18n, "finalResult", "Final Result (run summary)");
          return h("div", { className: "hermes-kanban-section" },
            h("div", { className: "hermes-kanban-section-head" }, label),
            reportBlock(drawerText("Full report", "Полный отчёт"), finalResult, "task-result"),
          );
        }
        if (isDone && isParent) {
          return h("div", { className: "hermes-kanban-section" },
            h("div", { className: "hermes-kanban-section-head" }, tx(i18n, "result", "Result")),
            h("div", { className: "hermes-kanban-done-no-result hermes-kanban-done-parent-note" },
              tx(i18n, "doneParentNote",
                "This card is an orchestrator / parent task. Review the child results section for the substantive work."),
            ),
          );
        }
        if (isDone) {
          return h("div", { className: "hermes-kanban-section" },
            h("div", { className: "hermes-kanban-section-head" }, tx(i18n, "result", "Result")),
            h("div", { className: "hermes-kanban-done-no-result" },
              tx(i18n, "doneNoResult",
                "No final result was recorded. Check Run History, Logs, or Child Tasks for the worker output."),
            ),
          );
        }
        return null;
      })(),
      childResults.length > 0 ? disclosure(`Результаты связанных задач (${childResults.length})`, h("div", { className: "hermes-kanban-section" },
        h("div", { className: "hermes-kanban-section-head" },
          `${tx(i18n, "childResults", "Child Results")} (${childResults.length})`),
        childResults.map(function (child) {
          var childResult = child.result || child.latest_summary || null;
          return h("div", { key: child.id, className: "hermes-kanban-comment" },
            h("div", { className: "hermes-kanban-comment-head" },
              h("span", { className: "hermes-kanban-comment-author" },
                compactTaskTitle(child.title || tx(i18n, "untitled", "(untitled)"))),
              h(Badge, { variant: "outline" }, getColumnLabel(i18n, child.status)),
              h("button", {
                type: "button",
                className: "hermes-kanban-diag-action-btn",
                onClick: function () { if (props.onOpenTask) props.onOpenTask(child.id); },
              }, tx(i18n, "open", "Open")),
            ),
            childResult
              ? reportBlock(drawerText("Full report", "Полный отчёт"), childResult, child.id + "-result")
              : h("div", { className: "text-xs text-muted-foreground" },
                  tx(i18n, "noChildResult", "No result recorded yet.")),
          );
        }),
      )) : null,
      h(AttachmentsSection, {
        attachments: attachments,
        boardSlug: props.boardSlug,
        onUpload: props.onUpload,
        onDelete: props.onDeleteAttachment,
        uploadBusy: props.uploadBusy,
        uploadErr: props.uploadErr,
        i18n: i18n,
        requestDialog: props.requestDialog,
      }),
      h("div", { className: "hermes-kanban-section" },
        h("div", { className: "hermes-kanban-section-head" },
          `${tx(i18n, "comments", "Comments")} (${comments.length})`),
        comments.length === 0
          ? h("div", { className: "text-xs text-muted-foreground" },
              tx(i18n, "noComments", "— no comments —"))
          : comments.slice(-3).map(renderComment),
      ),
      comments.length > 3 ? disclosure(
        drawerText(`Older comments (${comments.length - 3})`, `Предыдущие комментарии (${comments.length - 3})`),
        comments.slice(0, -3).map(renderComment),
      ) : null,
      disclosure(drawerText("Technical details", "Технические подробности"),
        h(React.Fragment, null,
          disclosure(drawerText("Task details", "Данные задачи"),
            h("div", { className: "hermes-kanban-drawer-meta" },
              h(MetaRow, { label: drawerText("Task ID", "ID задачи"), value: t.id }),
              h(MetaRow, { label: tx(i18n, "status", "Status"), value: getColumnLabel(i18n, t.status) }),
              h(AssigneeEditor, { task: t, onPatch: props.onPatch }),
              h(PriorityEditor, { task: t, onPatch: props.onPatch }),
              h(ModelEditor, { task: t, onPatch: props.onPatch }),
              t.tenant ? h(MetaRow, { label: tx(i18n, "tenant", "Tenant"), value: t.tenant }) : null,
              h(MetaRow, {
                label: tx(i18n, "workspace", "Workspace"),
                value: `${t.workspace_kind}${t.workspace_path ? ": " + t.workspace_path : ""}`,
              }),
              (t.skills && t.skills.length > 0) ? h(MetaRow, {
                label: tx(i18n, "skills", "Skills"),
                value: t.skills.join(", "),
              }) : null,
              t.goal_mode ? h(MetaRow, {
                label: tx(i18n, "goalMode", "Goal mode"),
                value: t.goal_max_turns ? `on (max ${t.goal_max_turns} turns)` : "on",
              }) : null,
              t.created_by ? h(MetaRow, { label: tx(i18n, "createdBy", "Created by"), value: t.created_by }) : null,
            ),
          ),
          disclosure(drawerText("Task management", "Управление задачей"),
            h(StatusActions, {
              task: t,
              onPatch: props.onPatch,
              onSpecify: props.onSpecify,
              onDecompose: props.onDecompose,
            }),
          ),
          disclosure(drawerText("Agent instructions", "Инструкция агенту"),
            h(BodyEditor, {
              task: t,
              renderMarkdown: props.renderMarkdown,
              onPatch: props.onPatch,
            }),
          ),
          disclosure(drawerText("Links and dependencies", "Связи и зависимости"),
            h(DependencyEditor, {
              task: t,
              links: links,
              allTasks: props.allTasks,
              onAddParent: props.onAddParent,
              onRemoveParent: props.onRemoveParent,
              onAddChild: props.onAddChild,
              onRemoveChild: props.onRemoveChild,
            }),
          ),
          (t.diagnostics || []).length > 0 ? disclosure(drawerText("Diagnostics", "Диагностика"),
            h(DiagnosticsSection, {
              task: t,
              boardSlug: props.boardSlug,
              assignees: props.assignees,
              diagnostics: t.diagnostics || [],
              onRefresh: props.onRefresh,
            }),
          ) : null,
          (props.homeChannels || []).length > 0 ? disclosure(drawerText("Notifications", "Уведомления"),
            h(HomeSubsSection, {
              homeChannels: props.homeChannels || [],
              homeBusy: props.homeBusy || {},
              onToggle: props.onToggleHomeSub,
            }),
          ) : null,
          serviceComments.length > 0 ? disclosure(`Служебные сообщения (${serviceComments.length})`,
            serviceComments.map(renderComment),
          ) : null,
          disclosure(`${drawerText("Events", "События")} (${events.length})`,
            h("div", { className: "hermes-kanban-section" },
              events.slice().reverse().slice(0, 20).map(function (e) {
                const isDiag = isDiagnosticEvent(e.kind);
                const phantoms = isDiag ? phantomIdsFromEvent(e) : [];
                return h("div", {
                  key: e.id,
                  className: cn("hermes-kanban-event", isDiag ? "hermes-kanban-event--hallucination" : ""),
                },
                  isDiag
                    ? h("div", { className: "hermes-kanban-event-header" },
                        h("span", { className: "hermes-kanban-event-warning-icon" }, "⚠"),
                        h("span", { className: "hermes-kanban-event-warning-label" }, getDiagnosticEventLabel(i18n, e.kind) || e.kind),
                        h("span", { className: "hermes-kanban-event-ago" }, localizedTimeAgo(e.created_at)),
                      )
                    : h("div", { className: "hermes-kanban-event-header-plain" },
                        h("span", { className: "hermes-kanban-event-kind" }, e.kind),
                        h("span", { className: "hermes-kanban-event-ago" }, localizedTimeAgo(e.created_at)),
                      ),
                  isDiag && phantoms.length > 0 ? h("div", { className: "hermes-kanban-event-phantom-row" },
                    h("span", { className: "hermes-kanban-event-phantom-label" }, tx(i18n, "phantomIds", "Phantom ids:")),
                    phantoms.map(function (pid) {
                      return h("code", { key: pid, className: "hermes-kanban-event-phantom-chip" }, pid);
                    }),
                  ) : null,
                  e.payload && !isDiag ? h("code", { className: "hermes-kanban-event-payload" }, JSON.stringify(e.payload)) : null,
                );
              }),
            ),
          ),
          disclosure(drawerText("Technical details", "Логи и запуски"),
            h(React.Fragment, null,
              h(WorkerLogSection, { taskId: t.id, boardSlug: props.boardSlug }),
              h(RunHistorySection, { runs: props.data.runs || [] }),
            ),
          ),
        ),
      ),
    );
  }

  // Per-attempt history. Closed runs first (most recent last), then the
  // active run if any. Each row shows profile / outcome / elapsed /
  // summary. Collapsed by default when there are more than three runs.
  function RunHistorySection(props) {
    const { t, uiText, localizedTimeAgo } = useI18n();
    const runs = props.runs || [];
    const [expanded, setExpanded] = useState(false);
    if (runs.length === 0) return null;
    const showAll = expanded || runs.length <= 3;
    const visible = showAll ? runs : runs.slice(-3);

    const fmtElapsed = function (run) {
      if (!run || !run.started_at) return "";
      const end = run.ended_at || Math.floor(Date.now() / 1000);
      const secs = Math.max(0, end - run.started_at);
      if (secs < 60) return `${secs}s`;
      if (secs < 3600) return `${Math.round(secs / 60)}m`;
      return `${(secs / 3600).toFixed(1)}h`;
    };

    return h("div", { className: "hermes-kanban-section" },
      h("div", { className: "hermes-kanban-section-head-row" },
        h("span", { className: "hermes-kanban-section-head" },
          `${tx(t, "runHistory", "Run history")} (${runs.length})`),
        !showAll
          ? h("button", {
              type: "button",
              onClick: function () { setExpanded(true); },
              className: "hermes-kanban-edit-link",
              title: tx(t, "showAllAttempts", "Show all attempts"),
            }, `+${runs.length - 3} earlier`)
          : null,
      ),
      visible.map(function (r) {
        const outcomeClass = r.ended_at
          ? `hermes-kanban-run--${r.outcome || r.status || "ended"}`
          : "hermes-kanban-run--active";
        return h("div", { key: r.id, className: cn("hermes-kanban-run", outcomeClass) },
          h("div", { className: "hermes-kanban-run-head" },
            h("span", { className: "hermes-kanban-run-outcome" },
              r.ended_at ? (r.outcome || r.status || tx(t, "ended", "ended")) : tx(t, "active", "active")),
            h("span", { className: "hermes-kanban-run-profile" },
              r.profile ? `@${r.profile}` : tx(t, "noProfile", "(no profile)")),
            h("span", { className: "hermes-kanban-run-elapsed" }, fmtElapsed(r)),
            h("span", { className: "hermes-kanban-run-ago" },
              localizedTimeAgo(r.started_at)),
          ),
          r.summary
            ? h("div", { className: "hermes-kanban-run-summary" }, r.summary)
            : null,
          r.error
            ? h("div", { className: "hermes-kanban-run-error" }, r.error)
            : null,
          (r.metadata && Object.keys(r.metadata).length > 0)
            ? (function () {
                var json = JSON.stringify(r.metadata, null, 2);
                var collapsed = json.length > 300;
                return h("details", {
                    className: "hermes-kanban-run-meta-block",
                    open: !collapsed,
                  },
                  h("summary", { className: "hermes-kanban-run-meta-label" }, uiText("Metadata", "Метаданные")),
                  h("code", { className: "hermes-kanban-run-meta" }, json),
                );
              })()
            : null,
        );
      }),
    );
  }

  // Worker log: loads lazily (one GET on mount), refresh button, tail cap.
  function WorkerLogSection(props) {
    const { t, uiText } = useI18n();
    const [state, setState] = useState({ loading: false, data: null, err: null });
    const load = useCallback(function () {
      setState({ loading: true, data: null, err: null });
      SDK.fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/log?tail=100000`, props.boardSlug))
        .then(function (d) { setState({ loading: false, data: d, err: null }); })
        .catch(function (e) { setState({ loading: false, data: null, err: String(e.message || e) }); });
    }, [props.taskId, props.boardSlug]);

    // Auto-load when the section mounts; the user opened the drawer so the
    // cost is one small HTTP round-trip.
    useEffect(function () { load(); }, [load]);

    const data = state.data;
    let body;
    if (state.loading) {
      body = h("div", { className: "text-xs text-muted-foreground" },
        tx(t, "loadingLog", "Loading log…"));
    } else if (state.err) {
      body = h("div", { className: "text-xs text-destructive" }, state.err);
    } else if (!data || !data.exists) {
      body = h("div", { className: "text-xs text-muted-foreground italic" },
        tx(t, "noWorkerLog",
          "— no worker log yet (task hasn't spawned or log was rotated away) —"));
    } else {
      body = h("pre", { className: "hermes-kanban-pre hermes-kanban-log" },
        data.content || "(empty)");
    }

    return h("div", { className: "hermes-kanban-section" },
      h("div", { className: "hermes-kanban-section-head-row" },
        h("span", { className: "hermes-kanban-section-head" },
          tx(t, "workerLog", "Worker log") + (data && data.size_bytes ? ` (${data.size_bytes} B)` : "")),
        h("button", {
          type: "button",
          onClick: load,
          className: "hermes-kanban-edit-link",
              title: uiText("Refresh log", "Обновить журнал"),
        }, "refresh"),
      ),
      body,
      data && data.truncated
        ? h("div", { className: "text-xs text-muted-foreground" },
            tx(t, "logTruncated", "(showing last 100 KB — full log at "),
            data.path,
            tx(t, "logAt", ")"))
        : null,
    );
  }

  function MetaRow(props) {
    return h("div", { className: "hermes-kanban-meta-row" },
      h("span", { className: "hermes-kanban-meta-label" }, props.label),
      h("span", { className: "hermes-kanban-meta-value" }, props.value),
    );
  }

  function TitleEditor(props) {
    const { t } = useI18n();
    const [v, setV] = useState(props.initial);
    const save = function () {
      const trimmed = v.trim();
      if (!trimmed) return;
      props.onSave(trimmed);
    };
    return h("div", { className: "hermes-kanban-edit-row" },
      h(Input, {
        value: v, autoFocus: true,
        onChange: function (e) { setV(e.target.value); },
        onKeyDown: function (e) {
          if (e.key === "Enter") { e.preventDefault(); save(); }
          if (e.key === "Escape") props.onCancel();
        },
        className: "h-8 text-sm flex-1",
      }),
      h(Button, { onClick: save,
        size: "sm",
      }, tx(t, "save", "Save")),
      h(Button, { onClick: props.onCancel,
        size: "sm",
      }, tx(t, "cancel", "Cancel")),
    );
  }

  function AssigneeEditor(props) {
    const { t } = useI18n();
    const [editing, setEditing] = useState(false);
    const [v, setV] = useState(props.task.assignee || "");
    useEffect(function () { setV(props.task.assignee || ""); }, [props.task.assignee]);
    if (!editing) {
      return h("div", { className: "hermes-kanban-meta-row" },
        h("span", { className: "hermes-kanban-meta-label" }, tx(t, "assignee", "Assignee")),
        h("span", {
          className: "hermes-kanban-meta-value hermes-kanban-editable",
          onClick: function () { setEditing(true); },
          title: tx(t, "clickToEditAssignee", "Click to edit assignee"),
        }, props.task.assignee || tx(t, "unassigned", "unassigned")),
      );
    }
    const save = function () {
      props.onPatch({ assignee: v.trim() || "" }).then(function () { setEditing(false); });
    };
    return h("div", { className: "hermes-kanban-meta-row" },
      h("span", { className: "hermes-kanban-meta-label" }, tx(t, "assignee", "Assignee")),
      h(Input, {
        value: v, autoFocus: true,
        onChange: function (e) { setV(e.target.value); },
        onKeyDown: function (e) {
          if (e.key === "Enter") { e.preventDefault(); save(); }
          if (e.key === "Escape") setEditing(false);
        },
        placeholder: tx(t, "emptyAssignee", "(empty = unassign)"),
        className: "h-7 text-xs flex-1",
        style: { textTransform: "none" },
        autoCapitalize: "none",
        autoCorrect: "off",
        spellCheck: false,
      }),
    );
  }

  function PriorityEditor(props) {
    const { t } = useI18n();
    const [editing, setEditing] = useState(false);
    const [v, setV] = useState(String(props.task.priority || 0));
    useEffect(function () { setV(String(props.task.priority || 0)); }, [props.task.priority]);
    if (!editing) {
      return h("div", { className: "hermes-kanban-meta-row" },
        h("span", { className: "hermes-kanban-meta-label" }, tx(t, "priority", "Priority")),
        h("span", {
          className: "hermes-kanban-meta-value hermes-kanban-editable",
          onClick: function () { setEditing(true); },
          title: tx(t, "clickToEdit", "Click to edit"),
        }, String(props.task.priority)),
      );
    }
    const save = function () {
      props.onPatch({ priority: Number(v) || 0 }).then(function () { setEditing(false); });
    };
    return h("div", { className: "hermes-kanban-meta-row" },
      h("span", { className: "hermes-kanban-meta-label" }, tx(t, "priority", "Priority")),
      h(Input, {
        type: "number", value: v, autoFocus: true,
        onChange: function (e) { setV(e.target.value); },
        onKeyDown: function (e) {
          if (e.key === "Enter") { e.preventDefault(); save(); }
          if (e.key === "Escape") setEditing(false);
        },
        className: "h-7 text-xs w-20",
      }),
    );
  }

  // Module-level cache for the model-options catalog so opening several
  // task drawers doesn't refetch. { providers: [{slug,label,models}] }
  let _modelCatalogCache = null;
  let _modelCatalogPromise = null;
  function fetchModelCatalog() {
    if (_modelCatalogCache) return Promise.resolve(_modelCatalogCache);
    if (_modelCatalogPromise) return _modelCatalogPromise;
    _modelCatalogPromise = SDK.fetchJSON(`${API}/model-options`)
      .then(function (data) {
        _modelCatalogCache = data && Array.isArray(data.providers) ? data : { providers: [] };
        return _modelCatalogCache;
      })
      .catch(function () {
        _modelCatalogPromise = null; // allow retry on next open
        return { providers: [] };
      });
    return _modelCatalogPromise;
  }

  // Per-task model override dropdown. Value encoding: "" = profile
  // default; "<slug>\u0000<model>" = provider+model pair (the separator
  // can't appear in either half). A catalog fetch failure degrades to a
  // free-text input so the override is still settable.
  function ModelEditor(props) {
    const { t } = useI18n();
    const task = props.task;
    const [editing, setEditing] = useState(false);
    const [catalog, setCatalog] = useState(_modelCatalogCache);
    const [busy, setBusy] = useState(false);
    const [freeText, setFreeText] = useState("");

    useEffect(function () {
      if (!editing || catalog) return;
      let alive = true;
      fetchModelCatalog().then(function (data) {
        if (alive) setCatalog(data);
      });
      return function () { alive = false; };
    }, [editing, catalog]);

    const current = task.model_override
      ? (task.provider_override
          ? `${task.provider_override}: ${task.model_override}`
          : task.model_override)
      : tx(t, "modelProfileDefault", "profile default");

    if (!editing) {
      return h("div", { className: "hermes-kanban-meta-row" },
        h("span", { className: "hermes-kanban-meta-label" }, tx(t, "model", "Model")),
        h("span", {
          className: cn(
            "hermes-kanban-meta-value hermes-kanban-editable",
            !task.model_override ? "text-muted-foreground" : "",
          ),
          onClick: function () { setEditing(true); },
          title: tx(t, "clickToEditModel",
            "Click to override the model for this task's next run"),
        }, current),
      );
    }

    const apply = function (patch) {
      setBusy(true);
      props.onPatch(patch).then(function () {
        setEditing(false);
      }).catch(function () {
        // onPatch surfaces its own toast; just re-enable the control.
      }).then(function () { setBusy(false); });
    };

    const onPick = function (value) {
      if (value === "") {
        apply({ clear_model_override: true });
        return;
      }
      const sep = value.indexOf("\u0000");
      if (sep === -1) {
        apply({ model_override: value });
        return;
      }
      apply({
        provider_override: value.slice(0, sep),
        model_override: value.slice(sep + 1),
      });
    };

    const providers = (catalog && catalog.providers) || [];
    const loading = editing && !catalog;
    const currentValue = task.model_override
      ? (task.provider_override
          ? `${task.provider_override}\u0000${task.model_override}`
          : task.model_override)
      : "";

    // Free-text fallback when the catalog is empty (inventory unavailable
    // or zero authenticated providers).
    if (!loading && providers.length === 0) {
      const saveFree = function () {
        const v = freeText.trim();
        if (!v) { apply({ clear_model_override: true }); return; }
        apply({ model_override: v });
      };
      return h("div", { className: "hermes-kanban-meta-row" },
        h("span", { className: "hermes-kanban-meta-label" }, tx(t, "model", "Model")),
        h(Input, {
          value: freeText, autoFocus: true, disabled: busy,
          placeholder: tx(t, "modelFreeTextPlaceholder", "model name (empty = profile default)"),
          onChange: function (e) { setFreeText(e.target.value); },
          onKeyDown: function (e) {
            if (e.key === "Enter") { e.preventDefault(); saveFree(); }
            if (e.key === "Escape") setEditing(false);
          },
          className: "h-7 text-xs flex-1",
          style: { textTransform: "none" },
          autoCapitalize: "none", autoCorrect: "off", spellCheck: false,
        }),
      );
    }

    // Ensure the current override is selectable even when it's not in the
    // catalog (e.g. set from the CLI with a model the catalog doesn't list).
    let currentInCatalog = currentValue === "";
    for (let i = 0; i < providers.length && !currentInCatalog; i++) {
      const p = providers[i];
      for (let j = 0; j < p.models.length; j++) {
        const enc = `${p.slug}\u0000${p.models[j]}`;
        if (enc === currentValue || p.models[j] === currentValue) {
          currentInCatalog = true;
          break;
        }
      }
    }

    return h("div", { className: "hermes-kanban-meta-row" },
      h("span", { className: "hermes-kanban-meta-label" }, tx(t, "model", "Model")),
      loading
        ? h("span", { className: "hermes-kanban-meta-value text-muted-foreground" },
            tx(t, "modelLoading", "loading models…"))
        : h("select", {
            className: "hermes-kanban-recovery-select",
            value: currentValue,
            disabled: busy,
            autoFocus: true,
            onChange: function (e) { onPick(e.target.value); },
            onKeyDown: function (e) {
              if (e.key === "Escape") setEditing(false);
            },
          },
            h("option", { value: "" },
              tx(t, "modelProfileDefaultOption", "(profile default)")),
            !currentInCatalog
              ? h("option", { value: currentValue }, current)
              : null,
            providers.map(function (p) {
              return h("optgroup", { key: p.slug, label: p.label || p.slug },
                p.models.map(function (m) {
                  return h("option", {
                    key: `${p.slug}\u0000${m}`,
                    value: `${p.slug}\u0000${m}`,
                  }, m);
                }),
              );
            }),
          ),
    );
  }

  function BodyEditor(props) {
    const { t, uiText } = useI18n();
    const [editing, setEditing] = useState(false);
    const [v, setV] = useState(props.task.body || "");
    useEffect(function () { setV(props.task.body || ""); }, [props.task.body]);
    const save = function () {
      props.onPatch({ body: v }).then(function () { setEditing(false); });
    };
    return h("div", { className: "hermes-kanban-section" },
      h("div", { className: "hermes-kanban-section-head-row" },
        h("span", { className: "hermes-kanban-section-head" }, tx(t, "description", "Description")),
        editing
          ? h("div", { className: "flex gap-1" },
              h(Button, { onClick: save,
                size: "sm",
              }, tx(t, "save", "Save")),
              h(Button, { onClick: function () { setEditing(false); setV(props.task.body || ""); },
                size: "sm",
              }, tx(t, "cancel", "Cancel")),
            )
          : h("button", {
              type: "button",
              onClick: function () { setEditing(true); },
              className: "hermes-kanban-edit-link",
              title: uiText("Edit description", "Изменить описание"),
            }, tx(t, "edit", "edit")),
      ),
      editing
        ? h("textarea", {
            className: "hermes-kanban-textarea",
            value: v,
            rows: 8,
            onChange: function (e) { setV(e.target.value); },
          })
        : props.task.body
          ? h(MarkdownBlock, { source: props.task.body, enabled: props.renderMarkdown })
          : h("div", { className: "text-xs text-muted-foreground italic" },
              tx(t, "noDescription", "— no description —")),
    );
  }

  function DependencyEditor(props) {
    const { t } = useI18n();
    const { task, links, allTasks } = props;
    const [newParent, setNewParent] = useState("");
    const [newChild, setNewChild] = useState("");
    // Filter out self + existing links when offering the "add" dropdown.
    const candidatesFor = function (excludeSet) {
      return (allTasks || []).filter(function (tk) {
        return tk.id !== task.id && !excludeSet.has(tk.id);
      });
    };
    const parentExclude = new Set([task.id, ...(links.parents || [])]);
    const childExclude  = new Set([task.id, ...(links.children || [])]);

    return h("div", { className: "hermes-kanban-section" },
      h("div", { className: "hermes-kanban-section-head" }, tx(t, "dependencies", "Dependencies")),
      h("div", { className: "hermes-kanban-deps-row" },
        h("span", { className: "hermes-kanban-deps-label" }, tx(t, "parents", "Parents:")),
        h("div", { className: "hermes-kanban-deps-chips" },
          (links.parents || []).length === 0
            ? h("span", { className: "hermes-kanban-deps-empty" }, tx(t, "none", "none"))
            : (links.parents || []).map(function (id) {
                return h("span", { key: id, className: "hermes-kanban-dep-chip" },
                  id,
                  h("button", {
                    type: "button",
                    className: "hermes-kanban-dep-chip-x",
                    onClick: function () { props.onRemoveParent(id); },
                    title: tx(t, "removeDependency", "Remove dependency"),
                  }, "×"),
                );
              }),
        ),
      ),
      h("div", { className: "hermes-kanban-deps-row" },
        h(Select, Object.assign({
          value: newParent,
          className: "h-7 text-xs flex-1",
        }, selectChangeHandler(setNewParent)),
          h(SelectOption, { value: "" }, tx(t, "addParent", "— add parent —")),
          candidatesFor(parentExclude).map(function (tk) {
            return h(SelectOption, { key: tk.id, value: tk.id },
              `${tk.id} — ${(tk.title || "").slice(0, 50)}`);
          }),
        ),
        h(Button, {
          onClick: function () {
            if (!newParent) return;
            props.onAddParent(newParent).then(function () { setNewParent(""); });
          },
          disabled: !newParent,
          size: "sm",
        }, "+ parent"),
      ),
      h("div", { className: "hermes-kanban-deps-row" },
        h("span", { className: "hermes-kanban-deps-label" }, tx(t, "children", "Children:")),
        h("div", { className: "hermes-kanban-deps-chips" },
          (links.children || []).length === 0
            ? h("span", { className: "hermes-kanban-deps-empty" }, tx(t, "none", "none"))
            : (links.children || []).map(function (id) {
                return h("span", { key: id, className: "hermes-kanban-dep-chip" },
                  id,
                  h("button", {
                    type: "button",
                    className: "hermes-kanban-dep-chip-x",
                    onClick: function () { props.onRemoveChild(id); },
                    title: tx(t, "removeDependency", "Remove dependency"),
                  }, "×"),
                );
              }),
        ),
      ),
      h("div", { className: "hermes-kanban-deps-row" },
        h(Select, Object.assign({
          value: newChild,
          className: "h-7 text-xs flex-1",
        }, selectChangeHandler(setNewChild)),
          h(SelectOption, { value: "" }, tx(t, "addChild", "— add child —")),
          candidatesFor(childExclude).map(function (tk) {
            return h(SelectOption, { key: tk.id, value: tk.id },
              `${tk.id} — ${(tk.title || "").slice(0, 50)}`);
          }),
        ),
        h(Button, {
          onClick: function () {
            if (!newChild) return;
            props.onAddChild(newChild).then(function () { setNewChild(""); });
          },
          disabled: !newChild,
          size: "sm",
        }, "+ child"),
      ),
    );
  }

  function StatusActions(props) {
    const { t, uiText } = useI18n();
    const task = props.task;
    const [specifyBusy, setSpecifyBusy] = useState(false);
    const [specifyMsg, setSpecifyMsg] = useState(null);
    const [decomposeBusy, setDecomposeBusy] = useState(false);
    const [decomposeMsg, setDecomposeMsg] = useState(null);
    const b = function (label, patch, enabled, confirmMsg) {
      return h(Button, {
        onClick: function () { if (enabled !== false) props.onPatch(patch, { confirm: confirmMsg }); },
        disabled: enabled === false,
        size: "sm",
      }, label);
    };

    // "Specify" appears only when the task is in the Triage column — the
    // one column where an auxiliary LLM pass is meaningful. Elsewhere
    // the backend would return ok:false with "not in triage" anyway,
    // so hiding the button keeps the action row uncluttered.
    const specifyButton = (task.status === "triage" && props.onSpecify)
      ? h(Button, {
          onClick: function () {
            if (specifyBusy) return;
            setSpecifyBusy(true);
            setSpecifyMsg(null);
            props.onSpecify().then(function (res) {
              if (res && res.ok) {
                const suffix = res.new_title
                  ? ` — retitled: ${res.new_title}`
                  : "";
                setSpecifyMsg({ ok: true, text: uiText(`Specified${suffix}`, `Спецификация подготовлена${res.new_title ? ` — новое название: ${res.new_title}` : ""}`) });
              } else {
                setSpecifyMsg({
                  ok: false,
                  text: uiText("Specify failed: ", "Не удалось подготовить спецификацию: ") + ((res && res.reason) || "unknown error"),
                });
              }
            }).catch(function (err) {
              setSpecifyMsg({
                ok: false,
                text: uiText("Specify failed: ", "Не удалось подготовить спецификацию: ") + (err.message || String(err)),
              });
            }).then(function () {
              setSpecifyBusy(false);
            });
          },
          disabled: specifyBusy,
          size: "sm",
        }, specifyBusy ? uiText("Specifying…", "Подготовка…") : "✨ " + uiText("Specify", "Подготовить спецификацию"))
      : null;

    // "Decompose" is the built-in decomposer fan-out. Like Specify, only
    // makes sense on triage-column tasks — elsewhere the backend short-
    // circuits with ok:false. When the decomposer returns fanout:false
    // we render the same single-task message as Specify; when it fans
    // out we report the child count for quick at-a-glance verification.
    const decomposeButton = (task.status === "triage" && props.onDecompose)
      ? h(Button, {
          onClick: function () {
            if (decomposeBusy) return;
            setDecomposeBusy(true);
            setDecomposeMsg(null);
            props.onDecompose().then(function (res) {
              if (res && res.ok) {
                if (res.fanout && res.child_ids && res.child_ids.length) {
                  setDecomposeMsg({
                    ok: true,
                    text: uiText(`Decomposed into ${res.child_ids.length} children: ${res.child_ids.join(", ")}`, `Создано дочерних задач: ${res.child_ids.length} (${res.child_ids.join(", ")})`),
                  });
                } else {
                  const suffix = res.new_title
                    ? ` — retitled: ${res.new_title}`
                    : "";
                  setDecomposeMsg({
                    ok: true,
                    text: uiText(`Single task (no fanout)${suffix}`, `Создана одна задача без разбиения${res.new_title ? ` — новое название: ${res.new_title}` : ""}`),
                  });
                }
              } else {
                setDecomposeMsg({
                  ok: false,
                  text: uiText("Decompose failed: ", "Не удалось декомпозировать задачу: ") + ((res && res.reason) || "unknown error"),
                });
              }
            }).catch(function (err) {
              setDecomposeMsg({
                ok: false,
                text: uiText("Decompose failed: ", "Не удалось декомпозировать задачу: ") + (err.message || String(err)),
              });
            }).then(function () {
              setDecomposeBusy(false);
            });
          },
          disabled: decomposeBusy,
          size: "sm",
        }, decomposeBusy ? uiText("Decomposing…", "Декомпозиция…") : "⚗ " + uiText("Decompose", "Разложить на подзадачи"))
      : null;

    return h("div", null,
      h("div", { className: "hermes-kanban-actions" },
        specifyButton,
        decomposeButton,
        b("→ " + getColumnLabel(t, "triage"), { status: "triage" }, task.status !== "triage"),
        b("→ " + getColumnLabel(t, "ready"), { status: "ready" }, task.status !== "ready"),
        // No direct → running button: /tasks/:id PATCH rejects status=running
        // with 400 (issue #19535). Tasks enter running only through the
        // dispatcher's claim_task path, which atomically creates the run row,
        // claim lock, and worker process metadata.
        b(tx(t, "block", "Block"),     { status: "blocked" },
          task.status === "running" || task.status === "ready",
          getDestructiveConfirm(t, "blocked")),
        b(tx(t, "unblock", "Unblock"),   { status: "ready" },    task.status === "blocked"),
        b(tx(t, "complete", "Complete"),  { status: "done" },
          task.status === "running" || task.status === "ready" || task.status === "blocked",
          getDestructiveConfirm(t, "done")),
        b(tx(t, "archive", "Archive"),   { status: "archived" }, task.status !== "archived",
          getDestructiveConfirm(t, "archived")),
      ),
      specifyMsg ? h("div", {
        className: specifyMsg.ok
          ? "hermes-kanban-msg-ok"
          : "hermes-kanban-msg-err",
      }, specifyMsg.text) : null,
      decomposeMsg ? h("div", {
        className: decomposeMsg.ok
          ? "hermes-kanban-msg-ok"
          : "hermes-kanban-msg-err",
      }, decomposeMsg.text) : null,
    );
  }


  // One toggle per gateway platform the user has a home channel set on
  // (telegram, discord, slack, etc.). Toggling on creates a kanban_notify_subs
  // row routed to that platform's home; toggling off removes it. Nothing
  // renders when no platforms have a home configured — this section stays
  // invisible for users who haven't set one up.
  function HomeSubsSection(props) {
    const { t } = useI18n();
    const channels = props.homeChannels || [];
    if (channels.length === 0) return null;
    const busy = props.homeBusy || {};
    return h("div", { className: "hermes-kanban-section" },
      h("div", { className: "hermes-kanban-section-head" },
        tx(t, "notifyHomeChannels", "Notify home channels")),
      h("div", { className: "hermes-kanban-home-subs" },
        channels.map(function (hc) {
          const isBusy = !!busy[hc.platform];
          const label = hc.subscribed ? "✓ " + hc.platform : hc.platform;
          const target = `${hc.name} (${hc.chat_id}${hc.thread_id ? " / " + hc.thread_id : ""})`;
          const title = hc.subscribed
            ? `${tx(t, "sendingUpdates", "Sending updates to")} ${target}. Click to stop.`
            : `${tx(t, "sendNotifications", "Send completed / blocked / gave_up notifications to")} ${target}.`;
          return h(Button, {
            key: hc.platform,
            size: "sm",
            title: title,
            disabled: isBusy || !props.onToggle,
            onClick: function () {
              if (props.onToggle) props.onToggle(hc.platform, hc.subscribed);
            },
            className: hc.subscribed
              ? "hermes-kanban-home-sub hermes-kanban-home-sub--on"
              : "hermes-kanban-home-sub",
          }, label);
        })
      )
    );
  }

  // -------------------------------------------------------------------------
  // Register
  // -------------------------------------------------------------------------

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("kanban", KanbanPage);
  }
})();
