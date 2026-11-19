import { useCallback, useEffect, useLayoutEffect, useState } from "react";
import { KeyRound, Package, Power, Server, Trash2, X, Zap } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Select, SelectOption } from "@nous-research/ui/ui/components/select";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { api } from "@/lib/api";
import type {
  McpCatalogDiagnostic,
  McpCatalogEntry,
  McpHttpAuth,
  McpServer,
  McpTestResult,
} from "@/lib/api";
import { DeleteConfirmDialog } from "@/components/DeleteConfirmDialog";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { useConfirmDelete } from "@nous-research/ui/hooks/use-confirm-delete";
import { useModalBehavior } from "@/hooks/useModalBehavior";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { usePageHeader } from "@/contexts/usePageHeader";
import { cn, themedBody } from "@/lib/utils";
import {
  buildMcpServerCreate,
  type McpTransport,
} from "@/lib/mcp-server-create";
import { completeMcpDashboardOAuth } from "@/lib/mcp-dashboard-oauth";
import { useI18n } from "@/i18n";
import { en } from "@/i18n/en";

function isHttpUrl(value: string): boolean {
  return /^https?:\/\//i.test(value.trim());
}

function truncateText(value: string, maxLength: number): string {
  return value.length > maxLength ? value.slice(0, maxLength) + "..." : value;
}

const TRANSPORT_TONE: Record<string, "success" | "warning" | "secondary"> = {
  http: "success",
  stdio: "warning",
  unknown: "secondary",
};

export default function McpPage() {
  const { t } = useI18n();
  const ui = t.mcpUi ?? en.mcpUi!;
  const [servers, setServers] = useState<McpServer[]>([]);
  const [catalog, setCatalog] = useState<McpCatalogEntry[]>([]);
  const [diagnostics, setDiagnostics] = useState<McpCatalogDiagnostic[]>([]);
  const [loading, setLoading] = useState(true);
  const { toast, showToast } = useToast();
  const { setEnd } = usePageHeader();

  // Add server modal state
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<McpTransport>("http");
  const [url, setUrl] = useState("");
  const [httpAuth, setHttpAuth] = useState<McpHttpAuth>("none");
  const [bearerToken, setBearerToken] = useState("");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [env, setEnv] = useState("");
  const [creating, setCreating] = useState(false);
  const closeCreateModal = useCallback(() => {
    setBearerToken("");
    setCreateModalOpen(false);
  }, []);
  const createModalRef = useModalBehavior({
    open: createModalOpen,
    onClose: closeCreateModal,
  });

  // Test results keyed by server name
  const [testing, setTesting] = useState<string | null>(null);
  const [authenticating, setAuthenticating] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, McpTestResult>>(
    {},
  );

  // Enable/disable state
  const [togglingName, setTogglingName] = useState<string | null>(null);
  const [restartNote, setRestartNote] = useState<string | null>(null);

  // Catalog install modal state
  const [installEntry, setInstallEntry] = useState<McpCatalogEntry | null>(
    null,
  );
  const [installEnv, setInstallEnv] = useState<Record<string, string>>({});
  const [installingName, setInstallingName] = useState<string | null>(null);
  const closeInstallModal = useCallback(() => setInstallEntry(null), []);
  const installModalRef = useModalBehavior({
    open: installEntry !== null,
    onClose: closeInstallModal,
  });

  const loadServers = useCallback(() => {
    return api
      .getMcpServers()
      .then((res) => setServers(res.servers))
      .catch((e) => showToast(`${ui.error} ${e}`, "error"));
  }, [showToast, ui.error]);

  const loadCatalog = useCallback(() => {
    return api
      .getMcpCatalog()
      .then((res) => {
        setCatalog(res.entries);
        setDiagnostics(res.diagnostics);
      })
      .catch((e) => showToast(`${ui.error} ${e}`, "error"));
  }, [showToast, ui.error]);

  useEffect(() => {
    Promise.all([loadServers(), loadCatalog()]).finally(() =>
      setLoading(false),
    );
  }, [loadServers, loadCatalog]);

  const handleCreate = async () => {
    let body;
    try {
      body = buildMcpServerCreate({
        name,
        transport,
        url,
        httpAuth,
        bearerToken,
        command,
        args,
        env,
      });
    } catch (error) {
      showToast(
        error instanceof Error ? error.message : ui.invalidServer,
        "error",
      );
      return;
    }

    setCreating(true);
    try {
      await api.addMcpServer(body);
      showToast(
        transport === "http" && httpAuth === "oauth"
          ? ui.addedOAuth
          : ui.addDone,
        "success",
      );
      setName("");
      setUrl("");
      setHttpAuth("none");
      setBearerToken("");
      setCommand("");
      setArgs("");
      setEnv("");
      setTransport("http");
      setCreateModalOpen(false);
      loadServers();
    } catch (e) {
      showToast(`${ui.addFailed} ${e}`, "error");
    } finally {
      setCreating(false);
    }
  };

  const handleTest = async (server: McpServer) => {
    setTesting(server.name);
    try {
      const result = await api.testMcpServer(server.name);
      setTestResults((prev) => ({ ...prev, [server.name]: result }));
      if (result.ok) {
        showToast(`${server.name}: ${ui.toolCount.replace("{count}", String(result.tools.length))}`, "success");
      } else {
        showToast(`${server.name}: ${result.error ?? ui.failed}`, "error");
      }
    } catch (e) {
      showToast(`${ui.error} ${e}`, "error");
    } finally {
      setTesting(null);
    }
  };

  const handleAuthenticate = async (server: McpServer) => {
    setAuthenticating(server.name);
    try {
      const result = await completeMcpDashboardOAuth({
        serverName: server.name,
        start: api.authMcpServer,
        status: api.getMcpOAuthFlow,
        open: window.open.bind(window),
      });
      setTestResults((prev) => ({
        ...prev,
        [server.name]: { ok: true, tools: result.tools ?? [] },
      }));
      showToast(`${server.name}: ${ui.oauthComplete}`, "success");
    } catch (e) {
      showToast(`${ui.oauthError} ${e}`, "error");
    } finally {
      setAuthenticating(null);
    }
  };

  const handleToggleEnabled = async (server: McpServer) => {
    const next = !server.enabled;
    setTogglingName(server.name);
    try {
      await api.setMcpServerEnabled(server.name, next);
      setServers((prev) =>
        prev.map((s) => (s.name === server.name ? { ...s, enabled: next } : s)),
      );
      setRestartNote(
        ui.restartNote,
      );
    } catch (e) {
      showToast(`${ui.error} ${e}`, "error");
    } finally {
      setTogglingName(null);
    }
  };

  const serverDelete = useConfirmDelete({
    onDelete: useCallback(
      async (serverName: string) => {
        try {
          await api.removeMcpServer(serverName);
          showToast(`${ui.deleteServer} «${truncateText(serverName, 30)}»`, "success");
          setTestResults((prev) => {
            const next = { ...prev };
            delete next[serverName];
            return next;
          });
          loadServers();
        } catch (e) {
          showToast(`${ui.error} ${e}`, "error");
          throw e;
        }
      },
      [loadServers, showToast, ui.deleteServer, ui.error],
    ),
  });

  // ── Catalog install ──────────────────────────────────────────────────
  const runInstall = useCallback(
    async (entry: McpCatalogEntry, envMap: Record<string, string>) => {
      setInstallingName(entry.name);
      try {
        const res = await api.installMcpCatalogEntry(entry.name, envMap, true);
        if (res.background) {
          showToast(ui.installingBackground, "success");
        } else {
          showToast(`${ui.installedEntry} «${truncateText(entry.name, 30)}»`, "success");
        }
        setInstallEntry(null);
        setInstallEnv({});
        await Promise.all([loadServers(), loadCatalog()]);
      } catch (e) {
        showToast(`${ui.failedInstall} ${e}`, "error");
      } finally {
        setInstallingName(null);
      }
    },
    [loadServers, loadCatalog, showToast, ui.failedInstall, ui.installedEntry, ui.installingBackground],
  );

  const handleInstallClick = (entry: McpCatalogEntry) => {
    if (entry.required_env.length > 0) {
      const initial: Record<string, string> = {};
      entry.required_env.forEach((item) => {
        initial[item.name] = "";
      });
      setInstallEnv(initial);
      setInstallEntry(entry);
    } else {
      void runInstall(entry, {});
    }
  };

  const handleInstallSubmit = () => {
    if (!installEntry) return;
    const missing = installEntry.required_env.filter(
      (item) => item.required && !(installEnv[item.name] ?? "").trim(),
    );
    if (missing.length > 0) {
      showToast(`${missing[0].prompt}: ${ui.required}`, "error");
      return;
    }
    const envMap: Record<string, string> = {};
    Object.entries(installEnv).forEach(([k, v]) => {
      if (v.trim()) envMap[k] = v.trim();
    });
    void runInstall(installEntry, envMap);
  };

  // Put "Add Server" button in page header
  useLayoutEffect(() => {
    setEnd(
      <Button
        className="uppercase"
        size="sm"
        onClick={() => setCreateModalOpen(true)}
      >
        {ui.addServerButton}
      </Button>,
    );
    return () => {
      setEnd(null);
    };
  }, [setEnd, loading, ui.addServerButton]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  const diagnosticsByName: Record<string, McpCatalogDiagnostic[]> = {};
  diagnostics.forEach((d) => {
    (diagnosticsByName[d.name] ??= []).push(d);
  });

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />

      <DeleteConfirmDialog
        open={serverDelete.isOpen}
        onCancel={serverDelete.cancel}
        onConfirm={serverDelete.confirm}
        title={ui.removeTitle}
        description={
          serverDelete.pendingId
            ? `«${truncateText(serverDelete.pendingId, 40)}» — ${ui.removeDescription}`
            : ui.removeDescription
        }
        loading={serverDelete.isDeleting}
      />

      {/* Add server modal */}
      {createModalOpen && (
        <div
          ref={createModalRef}
          className="fixed inset-0 z-[100] flex items-center justify-center bg-background/85 p-4"
          onClick={(e) => e.target === e.currentTarget && closeCreateModal()}
          role="dialog"
          aria-modal="true"
          aria-labelledby="create-mcp-title"
        >
          <div
            className={cn(
              themedBody,
              "relative w-full max-w-lg border border-border bg-card shadow-2xl flex flex-col",
            )}
          >
            <Button
              ghost
              size="icon"
              onClick={closeCreateModal}
              className="absolute right-2 top-2 text-muted-foreground hover:text-foreground"
              aria-label={t.common.close}
            >
              <X />
            </Button>

            <header className="p-5 pb-3 border-b border-border">
              <h2
                id="create-mcp-title"
                className="font-mondwest text-display text-base tracking-wider"
              >
                {ui.addServer}
              </h2>
            </header>

            <div className="p-5 grid gap-4">
              <div className="grid gap-2">
                <Label htmlFor="mcp-name">{ui.name}</Label>
                <Input
                  id="mcp-name"
                  autoFocus
                  placeholder="my-server"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </div>

              <div className="grid gap-2">
                <Label htmlFor="mcp-transport">{ui.transport}</Label>
                <Select
                  id="mcp-transport"
                  value={transport}
                  onValueChange={(value) => {
                    const nextTransport = value as McpTransport;
                    setTransport(nextTransport);
                    if (nextTransport === "stdio") setBearerToken("");
                  }}
                >
                  <SelectOption value="http">{ui.httpSse}</SelectOption>
                  <SelectOption value="stdio">{ui.stdio}</SelectOption>
                </Select>
              </div>

              {transport === "http" ? (
                <>
                  <div className="grid gap-2">
                    <Label htmlFor="mcp-url">URL</Label>
                    <Input
                      id="mcp-url"
                      placeholder="https://example.com/mcp"
                      value={url}
                      onChange={(e) => setUrl(e.target.value)}
                    />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="mcp-auth">{ui.authentication}</Label>
                    <Select
                      id="mcp-auth"
                      value={httpAuth}
                      onValueChange={(value) => {
                        const nextAuth = value as McpHttpAuth;
                        setHttpAuth(nextAuth);
                        if (nextAuth !== "header") setBearerToken("");
                      }}
                    >
                      <SelectOption value="none">{ui.none}</SelectOption>
                      <SelectOption value="header">{ui.bearerToken}</SelectOption>
                      <SelectOption value="oauth">{ui.oauth}</SelectOption>
                    </Select>
                  </div>
                  {httpAuth === "header" && (
                    <div className="grid gap-2">
                      <Label htmlFor="mcp-bearer-token">{ui.bearerToken}</Label>
                      <Input
                        id="mcp-bearer-token"
                        type="password"
                        autoComplete="new-password"
                        placeholder={ui.tokenHint}
                        value={bearerToken}
                        onChange={(e) => setBearerToken(e.target.value)}
                      />
                      <p className="text-xs text-muted-foreground">
                        {ui.configStoredSecret}
                      </p>
                    </div>
                  )}
                  {httpAuth === "oauth" && (
                    <p className="text-xs text-muted-foreground">
                      {ui.oauthInstruction}
                    </p>
                  )}
                </>
              ) : (
                <>
                  <div className="grid gap-2">
                    <Label htmlFor="mcp-command">{ui.command}</Label>
                    <Input
                      id="mcp-command"
                      placeholder="npx"
                      value={command}
                      onChange={(e) => setCommand(e.target.value)}
                    />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="mcp-args">{ui.args}</Label>
                    <Input
                      id="mcp-args"
                      placeholder="-y @modelcontextprotocol/server-foo"
                      value={args}
                      onChange={(e) => setArgs(e.target.value)}
                    />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="mcp-env">
                      {ui.environment}
                    </Label>
                    <textarea
                      id="mcp-env"
                      className="flex min-h-[80px] w-full border border-border bg-background/40 px-3 py-2 text-sm font-courier shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-foreground/30 focus-visible:border-foreground/25"
                      placeholder={"API_KEY=secret\nDEBUG=1"}
                      value={env}
                      onChange={(e) => setEnv(e.target.value)}
                    />
                  </div>
                </>
              )}

              <div className="flex justify-end">
                <Button
                  className="uppercase"
                  size="sm"
                  onClick={handleCreate}
                  disabled={creating}
                  prefix={creating ? <Spinner /> : undefined}
                >
                  {creating ? ui.adding : ui.add}
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Catalog install modal (required env vars) */}
      {installEntry && (
        <div
          ref={installModalRef}
          className="fixed inset-0 z-[100] flex items-center justify-center bg-background/85 p-4"
          onClick={(e) => e.target === e.currentTarget && setInstallEntry(null)}
          role="dialog"
          aria-modal="true"
          aria-labelledby="install-mcp-title"
        >
          <div
            className={cn(
              themedBody,
              "relative w-full max-w-lg border border-border bg-card shadow-2xl flex flex-col",
            )}
          >
            <Button
              ghost
              size="icon"
              onClick={() => setInstallEntry(null)}
              className="absolute right-2 top-2 text-muted-foreground hover:text-foreground"
              aria-label={t.common.close}
            >
              <X />
            </Button>

            <header className="p-5 pb-3 border-b border-border">
              <h2
                id="install-mcp-title"
                className="font-mondwest text-display text-base tracking-wider"
              >
                {ui.installEntry.replace("{name}", installEntry.name)}
              </h2>
            </header>

            <div className="p-5 grid gap-4">
              <p className="text-xs text-muted-foreground">
                {ui.requiredValues}
              </p>
              {installEntry.required_env.map((item) => (
                <div className="grid gap-2" key={item.name}>
                  <Label htmlFor={`install-env-${item.name}`}>
                    {item.prompt}
                    {item.required ? " *" : ""}
                  </Label>
                  <Input
                    id={`install-env-${item.name}`}
                    type="password"
                    placeholder={item.name}
                    value={installEnv[item.name] ?? ""}
                    onChange={(e) =>
                      setInstallEnv((prev) => ({
                        ...prev,
                        [item.name]: e.target.value,
                      }))
                    }
                  />
                </div>
              ))}

              <div className="flex justify-end">
                <Button
                  className="uppercase"
                  size="sm"
                  onClick={handleInstallSubmit}
                  disabled={installingName === installEntry.name}
                  prefix={
                    installingName === installEntry.name ? (
                      <Spinner />
                    ) : undefined
                  }
                >
                  {installingName === installEntry.name
                    ? ui.installing
                    : ui.install}
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ── Your MCP servers ── */}
      <div className="flex flex-col gap-3">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <H2
            variant="sm"
            className="flex items-center gap-2 text-muted-foreground"
          >
            <Server className="h-4 w-4" />
            {ui.yourServers} ({servers.length})
          </H2>
        </div>

        {restartNote && <p className="text-xs text-warning">{restartNote}</p>}

        {servers.length === 0 && (
          <Card>
            <CardContent className="py-8 text-center text-sm text-muted-foreground">
              {ui.noServers}
            </CardContent>
          </Card>
        )}

        {servers.map((server) => {
          const envCount = Object.keys(server.env ?? {}).length;
          const result = testResults[server.name];

          return (
            <Card key={server.name}>
              <CardContent
                className={cn(
                  "flex items-start gap-4 py-4",
                  !server.enabled && "opacity-60",
                )}
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="font-medium text-sm truncate">
                      {server.name}
                    </span>
                    <Badge
                      tone={TRANSPORT_TONE[server.transport] ?? "secondary"}
                    >
                      {server.transport === "http" ? ui.httpSse : ui.stdio}
                    </Badge>
                    {server.auth && (
                      <Badge tone="outline">
                        {ui.authentication}: {server.auth === "header" ? ui.bearer : ui.oauth}
                      </Badge>
                    )}
                    {!server.enabled && <Badge tone="outline">{ui.disabled}</Badge>}
                  </div>
                  <div className="flex items-center gap-4 text-xs text-muted-foreground">
                    {server.transport === "http" ? (
                      <span className="font-mono truncate">
                        {server.url ?? "—"}
                      </span>
                    ) : (
                      <span className="font-mono truncate">
                        {[server.command, ...(server.args ?? [])]
                          .filter(Boolean)
                          .join(" ") || "—"}
                      </span>
                    )}
                    {envCount > 0 && (
                      <span>
                        {ui.envVars.replace("{count}", String(envCount))}
                      </span>
                    )}
                  </div>
                  {result && (
                    <div className="mt-2 text-xs">
                      {result.ok ? (
                        <p className="text-success">
                          {result.tools.length === 0
                            ? ui.connectedNoTools
                            : `${ui.tools} ${result.tools
                                .map((tool) => tool.name)
                                .join(", ")}`}
                        </p>
                      ) : (
                        <p className="text-destructive">
                          {result.error ?? ui.connectionFailed}
                        </p>
                      )}
                    </div>
                  )}
                </div>

                <div className="flex items-center gap-1 shrink-0">
                  {server.auth === "oauth" && (
                    <Button
                      ghost
                      size="sm"
                      title={ui.authOAuth}
                      onClick={() => handleAuthenticate(server)}
                      disabled={authenticating === server.name}
                      prefix={
                        authenticating === server.name ? (
                          <Spinner />
                        ) : (
                          <KeyRound />
                        )
                      }
                    >
                      {ui.authenticate}
                    </Button>
                  )}

                  <Button
                    ghost
                    size="sm"
                    title={server.enabled ? ui.disable : ui.enable}
                    aria-label={server.enabled ? ui.disable : ui.enable}
                    onClick={() => handleToggleEnabled(server)}
                    disabled={togglingName === server.name}
                    prefix={
                      togglingName === server.name ? <Spinner /> : <Power />
                    }
                    className={server.enabled ? "text-success" : undefined}
                  >
                    {server.enabled ? ui.disable : ui.enable}
                  </Button>

                  <Button
                    ghost
                    size="icon"
                    title={ui.testConnection}
                    aria-label={ui.testConnection}
                    onClick={() => handleTest(server)}
                    disabled={testing === server.name}
                  >
                    {testing === server.name ? <Spinner /> : <Zap />}
                  </Button>

                  <Button
                    ghost
                    destructive
                    size="icon"
                    title={ui.delete}
                    aria-label={ui.delete}
                    onClick={() => serverDelete.requestDelete(server.name)}
                  >
                    <Trash2 />
                  </Button>
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>

      {/* ── Catalog ── */}
      <div className="flex flex-col gap-3">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <H2
            variant="sm"
            className="flex items-center gap-2 text-muted-foreground"
          >
            <Package className="h-4 w-4" />
            {ui.catalog} ({catalog.length})
          </H2>
        </div>

        <p className="text-xs text-muted-foreground">
          {ui.catalogDescription}
        </p>

        {catalog.length === 0 && (
          <Card>
            <CardContent className="py-8 text-center text-sm text-muted-foreground">
              {ui.noCatalog}
            </CardContent>
          </Card>
        )}

        {catalog.map((entry) => {
          const entryDiags = diagnosticsByName[entry.name] ?? [];
          const isInstalling = installingName === entry.name;

          return (
            <Card key={entry.name}>
              <CardContent className="flex items-start gap-4 py-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1 flex-wrap">
                    <span className="font-medium text-sm truncate">
                      {entry.name}
                    </span>
                    <Badge
                      tone={TRANSPORT_TONE[entry.transport] ?? "secondary"}
                    >
                        {entry.transport === "http" ? ui.httpSse : ui.stdio}
                    </Badge>
                    <Badge tone="outline">{ui.authType} {entry.auth_type === "api_key" ? ui.apiKey : entry.auth_type === "none" ? ui.none : ui.oauth}</Badge>
                    {isHttpUrl(entry.source) ? (
                      <a
                        href={entry.source}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-xs text-primary underline underline-offset-2 hover:opacity-80"
                      >
                        {ui.source}
                      </a>
                    ) : (
                      entry.source && (
                        <Badge tone="outline">{entry.source}</Badge>
                      )
                    )}
                    {entry.installed && <Badge tone="success">{ui.installed}</Badge>}
                    {entry.installed && !entry.enabled && (
                      <Badge tone="outline">{ui.disabled}</Badge>
                    )}
                  </div>
                  {entry.description && (
                    <p className="text-xs text-muted-foreground">
                      {entry.description}
                    </p>
                  )}
                  {/* Connection detail: what the agent actually talks to. */}
                  {entry.transport === "http" && entry.url && (
                    <p className="mt-1 text-xs text-muted-foreground">
                      <span className="font-medium">{ui.endpoint}</span>{" "}
                      <code className="font-mono">{entry.url}</code>
                    </p>
                  )}
                  {entry.transport === "stdio" && entry.command && (
                    <p className="mt-1 text-xs text-muted-foreground break-all">
                      <span className="font-medium">{ui.runs}</span>{" "}
                      <code className="font-mono">
                        {[entry.command, ...entry.args].join(" ")}
                      </code>
                    </p>
                  )}
                  {/* Git bootstrap — surfaced so users see what gets cloned/run
                      before they install (matches the docs trust model). */}
                  {entry.install_url && (
                    <p className="mt-1 text-xs text-muted-foreground break-all">
                      <span className="font-medium">{ui.installsFrom}</span>{" "}
                      {isHttpUrl(entry.install_url) ? (
                        <a
                          href={entry.install_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-primary underline underline-offset-2 hover:opacity-80"
                        >
                          {entry.install_url}
                        </a>
                      ) : (
                        <code className="font-mono">{entry.install_url}</code>
                      )}
                      {entry.install_ref && <span> @ {entry.install_ref}</span>}
                    </p>
                  )}
                  {entry.bootstrap.length > 0 && (
                    <details className="mt-1 text-xs text-muted-foreground">
                      <summary className="cursor-pointer select-none">
                        {ui.bootstrapCommands.replace("{count}", String(entry.bootstrap.length))}
                      </summary>
                      <ul className="mt-1 ml-3 list-disc space-y-0.5">
                        {entry.bootstrap.map((cmd, i) => (
                          <li
                            key={`${entry.name}-bs-${i}`}
                            className="break-all"
                          >
                            <code className="font-mono">{cmd}</code>
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
                  {entry.post_install && (
                    <details className="mt-1 text-xs text-muted-foreground">
                      <summary className="cursor-pointer select-none">
                        {ui.setupNotes}
                      </summary>
                      <p className="mt-1 whitespace-pre-wrap">
                        {entry.post_install.trim()}
                      </p>
                    </details>
                  )}
                  {entryDiags.map((d, i) => (
                    <p
                      key={`${entry.name}-diag-${i}`}
                      className="text-xs text-warning mt-1"
                    >
                      {d.message}
                    </p>
                  ))}
                </div>

                <div className="flex items-center gap-1 shrink-0">
                  {entry.installed ? (
                    <Badge tone="success">{ui.installed}</Badge>
                  ) : (
                    <Button
                      className="uppercase"
                      size="sm"
                      onClick={() => handleInstallClick(entry)}
                      disabled={isInstalling}
                      prefix={isInstalling ? <Spinner /> : undefined}
                    >
                      {isInstalling ? ui.installing : ui.install}
                    </Button>
                  )}
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
