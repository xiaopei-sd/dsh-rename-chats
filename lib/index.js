/**
 * dsh-rename-chats — a loopback-only HTTP bridge for the rename-chats Skill.
 *
 * Why a bridge: DSH exposes the official `session/rename` contract, but a
 * plain Skill script cannot obtain the browser launch token that the desktop
 * host keeps in process memory. A host plugin can call the same in-process
 * services without scraping cookies or editing session logs. The bridge is
 * only a transport; preview/confirmation, batching, protection and recovery
 * stay in the Skill's Python state machine.
 *
 * Security shape:
 *   - one random 192-bit path segment per host process;
 *   - loopback-only binding and loopback-only request acceptance;
 *   - the capability URL is written to a 0600 file under $DSH_HOME;
 *   - no credentials, no cookie access, no direct SQLite/log writes.
 */

import { randomBytes } from "node:crypto";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  readFileSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

import {
  BRIDGE_FILE,
  BRIDGE_VERSION,
  PLUGIN_VERSION,
  ROUTE_PREFIX,
  extractHumanMessages,
  isRecord,
  lastEventTime,
  normalizeSessionSummary,
  sameTitle,
} from "./protocol.js";

export const name = "rename-chats";
export const inject = ["webServer", "sessions", "sessionQuery", "sessionController", "workspaceRegistry"];

const MAX_BODY_BYTES = 1_000_000;
const MAX_SESSIONS = 40;
const TITLE_CACHE_TTL_MS = 10_000;
const TITLE_CACHE_MAX = 200;
const TITLE_READ_CONCURRENCY = 4;

/** @param {readonly unknown[]} items @param {number} limit @param {(item: unknown, index: number) => Promise<unknown>} fn */
async function mapLimit(items, limit, fn) {
  const results = new Array(items.length);
  let next = 0;
  const worker = async () => {
    while (true) {
      const index = next;
      next += 1;
      if (index >= items.length) return;
      results[index] = await fn(items[index], index);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, () => worker()));
  return results;
}

/** @param {string} message @param {number} status @param {string} code @param {unknown} [details] */
class BridgeError extends Error {
  constructor(message, status = 400, code = "bad-request", details = undefined) {
    super(message);
    this.name = "BridgeError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

/** @param {unknown} value */
function asString(value) {
  return typeof value === "string" ? value : undefined;
}

/** @param {string | undefined} value @param {number} fallback */
function clampedLimit(value, fallback = MAX_SESSIONS) {
  const parsed = Number.parseInt(value ?? "", 10);
  if (!Number.isFinite(parsed) || parsed < 1) return fallback;
  return Math.min(parsed, MAX_SESSIONS);
}

/** @param {import("node:http").IncomingMessage} req */
function isLoopbackRequest(req) {
  const address = req.socket?.remoteAddress;
  return address === "127.0.0.1" || address === "::1" || address === "::ffff:127.0.0.1";
}

/** @param {import("node:http").IncomingMessage} req @param {number} limit */
function readJsonBody(req, limit = MAX_BODY_BYTES) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > limit) {
        reject(new BridgeError("request body is too large", 413, "body-too-large"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("error", (error) => reject(error));
    req.on("end", () => {
      if (chunks.length === 0) {
        resolve({});
        return;
      }
      try {
        const parsed = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        if (!isRecord(parsed)) throw new BridgeError("JSON body must be an object");
        resolve(parsed);
      } catch (error) {
        reject(error instanceof BridgeError ? error : new BridgeError("request body is not valid JSON"));
      }
    });
  });
}

/** @param {import("node:http").ServerResponse} res @param {number} status @param {unknown} body */
function sendJson(res, status, body) {
  if (res.headersSent) return;
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "content-length": Buffer.byteLength(payload),
  });
  res.end(payload);
}

/** @param {unknown} error */
function errorBody(error) {
  if (error instanceof BridgeError) {
    return { ok: false, error: { code: error.code, message: error.message, ...(error.details === undefined ? {} : { details: error.details }) } };
  }
  return { ok: false, error: { code: "internal", message: String(error instanceof Error ? error.message : error) } };
}

/** @param {string} dshHome */
function bridgePathFor(dshHome) {
  return join(dshHome, "rename-chats", BRIDGE_FILE);
}

/** @param {string} dshHome */
function readBridgeFile(dshHome) {
  const path = bridgePathFor(dshHome);
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return undefined;
  }
}

/**
 * Write the capability URL once the web server has bound its port. Port 0
 * means the origin is only known after listen; retry briefly instead of
 * failing plugin startup.
 * @param {{ webServer: { getOrigin(): string } }} hostCtx
 * @param {string} baseUrl
 * @param {{ dshHome: string, warn: (message: string) => void }} options
 */
function publishBridgeFile(hostCtx, baseUrl, options) {
  const path = bridgePathFor(options.dshHome);
  try {
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    writeFileSync(
      path,
      `${JSON.stringify({
        version: BRIDGE_VERSION,
        pluginVersion: PLUGIN_VERSION,
        baseUrl,
        pid: process.pid,
        startedAt: new Date().toISOString(),
      }, null, 2)}\n`,
      { encoding: "utf8", mode: 0o600 },
    );
    chmodSync(path, 0o600);
  } catch (error) {
    options.warn(`dsh-rename-chats: could not write ${path}: ${String(error)}`);
  }
}

/** @param {string} dshHome */
function removeBridgeFileIfOwned(dshHome) {
  const path = bridgePathFor(dshHome);
  const current = readBridgeFile(dshHome);
  if (current?.pid !== process.pid) return;
  try {
    if (existsSync(path)) unlinkSync(path);
  } catch {
    // A stale capability file is harmless: health checks fail and the next
    // host start rewrites it. Never make plugin disposal throw.
  }
}

/**
 * Register one prefix route and publish its capability URL. The URL is
 * stored under the DSH home, not in the profile, because it is per-process.
 * @param {object} ctx
 */
export function apply(ctx, config = {}) {
  const dshHome = process.env.DSH_HOME || join(homedir(), ".dsh");
  const warn = (message) => {
    try {
      ctx.logger?.warn?.(message);
    } catch {
      // Logging must never take the bridge down.
    }
  };

  ctx.inject(
    ["webServer", "sessions", "sessionQuery", "sessionController", "workspaceRegistry"],
    (hostCtx) => {
      const token = randomBytes(24).toString("hex");
      const prefix = `${ROUTE_PREFIX}/${token}`;
      const titleCache = new Map();
      let bridgeTimer;

      /** @param {string} sessionId */
      const readTitleCached = async (sessionId) => {
        const now = Date.now();
        const hit = titleCache.get(sessionId);
        if (hit && hit.expiresAt > now) return hit.snapshot;
        const snapshot = await hostCtx.sessionQuery.readTitleSnapshot(sessionId);
        titleCache.delete(sessionId);
        titleCache.set(sessionId, { snapshot, expiresAt: now + TITLE_CACHE_TTL_MS });
        while (titleCache.size > TITLE_CACHE_MAX) {
          const oldest = titleCache.keys().next().value;
          if (oldest === undefined) break;
          titleCache.delete(oldest);
        }
        return snapshot;
      };

      /** @param {string} sessionId */
      const invalidateTitle = (sessionId) => {
        titleCache.delete(sessionId);
      };

      const requireSessionController = () => {
        const controller = hostCtx.sessionController;
        if (!controller || typeof controller.rename !== "function") {
          throw new BridgeError("session-controller service is unavailable", 503, "service-unavailable");
        }
        return controller;
      };

      const listWorkspaceSessions = async (workspaceId, includeSubagents, includeArchived, limit) => {
        const registry = hostCtx.workspaceRegistry;
        const workspace = workspaceId ? registry.get(workspaceId) : undefined;
        if (workspaceId && !workspace) throw new BridgeError(`workspace "${workspaceId}" was not found`, 404, "workspace-not-found");
        const records = await hostCtx.sessionQuery.listSessions();
        const archived = new Set(Array.isArray(registry.archivedSessionIds) ? registry.archivedSessionIds : []);
        const pinned = new Set(Array.isArray(registry.pinnedSessionIds) ? registry.pinnedSessionIds : []);
        const allowedIds = workspace ? new Set(workspace.sessionIds) : undefined;
        const selected = [];
        for (const record of records) {
          const header = record?.header ?? record;
          const id = asString(header?.id);
          if (!id) continue;
          if (allowedIds && !allowedIds.has(id)) continue;
          if (!includeArchived && archived.has(id)) continue;
          const origin = asString(header?.origin) ?? "ordinary";
          const parent = asString(header?.parentSession);
          if (!includeSubagents && (origin === "subagent" || parent)) continue;
          selected.push(record);
        }
        selected.sort((a, b) => {
          const left = Number(a?.header?.createdAt ?? 0);
          const right = Number(b?.header?.createdAt ?? 0);
          return right - left;
        });
        const rows = await mapLimit(selected.slice(0, limit), TITLE_READ_CONCURRENCY, async (record) => {
          const header = record?.header ?? record;
          const id = asString(header?.id);
          if (!id) return undefined;
          let titleSnapshot;
          try {
            titleSnapshot = await readTitleCached(id);
          } catch {
            titleSnapshot = undefined;
          }
          return normalizeSessionSummary(record, titleSnapshot ?? {}, {
            archived: archived.has(id),
            pinned: pinned.has(id) || Boolean(workspace?.pinnedSessionIds?.includes?.(id)),
            live: hostCtx.sessions.get(id) !== undefined,
          });
        });
        return { workspaceId: workspaceId ?? null, sessions: rows.filter(Boolean), total: selected.length };
      };

      const readOne = async (sessionId) => {
        const snapshot = await hostCtx.sessionQuery.readSession(sessionId);
        const titleSnapshot = await hostCtx.sessionQuery.readTitleSnapshot(sessionId);
        const header = snapshot.session;
        const messages = extractHumanMessages(snapshot.events, { first: 3, last: 9 });
        const lastActivityAt = lastEventTime(snapshot.events);
        return {
          sessionId,
          title: titleSnapshot?.title?.title ?? null,
          titleSource: titleSnapshot?.title?.source?.kind ?? null,
          titleEventSeq: titleSnapshot?.title?.eventSeq ?? null,
          createdAt: typeof header.createdAt === "number" ? header.createdAt : null,
          createdAtUnit: "milliseconds",
          lastActivityAt,
          lastActivityAtUnit: "milliseconds",
          cwd: asString(header.cwd) ?? null,
          origin: asString(header.origin) ?? "ordinary",
          messageCount: messages.count,
          messages: messages.messages,
        };
      };

      const renameOne = async (request) => {
        const sessionId = asString(request.sessionId);
        if (!sessionId) throw new BridgeError("sessionId is required");
        if (!Object.prototype.hasOwnProperty.call(request, "expectedTitle")) {
          throw new BridgeError("expectedTitle is required; read the current title before writing");
        }
        if (!(request.expectedTitle === null || typeof request.expectedTitle === "string")) {
          throw new BridgeError("expectedTitle must be a string or null");
        }
        const title = asString(request.title);
        if (!title || !title.trim()) throw new BridgeError("title must contain visible characters");
        let currentSnapshot;
        try {
          currentSnapshot = await hostCtx.sessionQuery.readTitleSnapshot(sessionId);
        } catch (error) {
          const code = asString(error?.code) ?? "session-not-found";
          throw new BridgeError(String(error?.message ?? error), code === "session/not-found" ? 404 : 500, code);
        }
        const header = currentSnapshot?.session ?? {};
        if (asString(header?.origin) === "subagent" || asString(header?.parentSession)) {
          throw new BridgeError("subagent sessions are not renamed by this bridge", 403, "subagent-not-allowed");
        }
        if (hostCtx.workspaceRegistry.archivedSessionIds?.includes?.(sessionId)) {
          throw new BridgeError("archived sessions are not renamed; restore it first", 409, "session-archived");
        }
        const currentTitle = currentSnapshot?.title?.title ?? null;
        if (!sameTitle(currentTitle, request.expectedTitle)) {
          throw new BridgeError("session title changed before the write", 409, "title-changed", {
            expectedTitle: request.expectedTitle,
            currentTitle,
          });
        }
        const controller = requireSessionController();
        let accepted;
        try {
          accepted = await controller.rename({ sessionId, title });
        } catch (error) {
          const code = asString(error?.code) ?? "rename-failed";
          const status = code === "session/not-found" ? 404 : code === "session/title-invalid" ? 422 : 409;
          throw new BridgeError(String(error?.message ?? error), status, code);
        }
        if (!accepted || typeof accepted.title !== "string") {
          throw new BridgeError("session-controller returned no accepted title", 502, "rename-unverified");
        }
        let afterSnapshot;
        try {
          afterSnapshot = await hostCtx.sessionQuery.readTitleSnapshot(sessionId);
        } catch (error) {
          throw new BridgeError("title write could not be re-read from the session log", 500, "verification-failed", {
            acceptedTitle: accepted.title,
            cause: String(error?.message ?? error),
          });
        }
        const afterTitle = afterSnapshot?.title?.title ?? null;
        if (!sameTitle(afterTitle, accepted.title)) {
          throw new BridgeError("title write could not be verified from the session log", 500, "verification-failed", {
            acceptedTitle: accepted.title,
            currentTitle: afterTitle,
          });
        }
        invalidateTitle(sessionId);
        return {
          ok: true,
          sessionId,
          requestedTitle: title,
          title: accepted.title,
          eventSeq: afterSnapshot?.title?.eventSeq ?? accepted.seq ?? null,
          titleSource: afterSnapshot?.title?.source?.kind ?? null,
          normalized: accepted.title !== title,
          verified: true,
        };
      };

      /** @param {import("node:http").IncomingMessage} req @param {import("node:http").ServerResponse} res @param {string} actionPath */
      const handle = async (req, res, actionPath) => {
        try {
          if (!isLoopbackRequest(req)) throw new BridgeError("loopback requests only", 403, "loopback-only");
          const url = new URL(req.url ?? "/", "http://127.0.0.1");
          if (actionPath === "/health" && req.method === "GET") {
            sendJson(res, 200, {
              ok: true,
              plugin: name,
              pluginVersion: PLUGIN_VERSION,
              bridgeVersion: BRIDGE_VERSION,
              pid: process.pid,
              workspaces: hostCtx.workspaceRegistry.list().map((workspace) => ({
                id: workspace.id,
                path: workspace.path,
                title: workspace.title,
                sessionCount: Array.isArray(workspace.sessionIds) ? workspace.sessionIds.length : 0,
              })),
            });
            return;
          }
          if (actionPath === "/sessions" && req.method === "GET") {
            const workspaceId = url.searchParams.get("workspaceId") ?? undefined;
            const includeSubagents = url.searchParams.get("includeSubagents") === "1";
            const includeArchived = url.searchParams.get("includeArchived") === "1";
            const limit = clampedLimit(url.searchParams.get("limit") ?? undefined);
            const result = await listWorkspaceSessions(workspaceId, includeSubagents, includeArchived, limit);
            sendJson(res, 200, { ok: true, ...result });
            return;
          }
          if (actionPath === "/read" && req.method === "POST") {
            const body = await readJsonBody(req);
            const sessionId = asString(body.sessionId);
            if (!sessionId) throw new BridgeError("sessionId is required");
            const result = await readOne(sessionId);
            sendJson(res, 200, { ok: true, ...result });
            return;
          }
          if (actionPath === "/rename" && req.method === "POST") {
            const body = await readJsonBody(req);
            const result = await renameOne(body);
            sendJson(res, 200, result);
            return;
          }
          throw new BridgeError("unknown bridge action", 404, "not-found");
        } catch (error) {
          sendJson(res, error instanceof BridgeError ? error.status : 500, errorBody(error));
        }
      };

      const route = {
        kind: "prefix",
        path: prefix,
        handler: (req, res) => {
          const url = new URL(req.url ?? "/", "http://127.0.0.1");
          const actionPath = url.pathname.startsWith(prefix) ? url.pathname.slice(prefix.length) : "";
          void handle(req, res, actionPath || "/health");
        },
      };

      hostCtx.effect(() => {
        const dispose = hostCtx.webServer.register(route);
        const attemptPublish = (attempt = 0) => {
          try {
            const origin = hostCtx.webServer.getOrigin().replace(/\/$/, "");
            publishBridgeFile(hostCtx, `${origin}${prefix}`, { dshHome, warn });
          } catch (error) {
            if (attempt < 6) {
              bridgeTimer = setTimeout(() => attemptPublish(attempt + 1), 100);
              bridgeTimer.unref?.();
            } else {
              warn(`dsh-rename-chats: could not determine bridge origin: ${String(error)}`);
            }
          }
        };
        attemptPublish();
        return () => {
          if (bridgeTimer) clearTimeout(bridgeTimer);
          dispose();
          removeBridgeFileIfOwned(dshHome);
        };
      }, "dsh-rename-chats: loopback bridge route");
    },
  );
}

/** Exported for offline diagnostics and unit tests; not part of the public API. */
export const _internal = {
  bridgePathFor,
  readBridgeFile,
  removeBridgeFileIfOwned,
  publishBridgeFile,
};
