/**
 * Pure protocol helpers for the DSH rename-chats bridge.
 *
 * This module deliberately has no dependency on Cordis or the host context,
 * so the normalization rules can be unit-tested without booting DSH.
 */

export const PLUGIN_VERSION = "0.1.0";
export const BRIDGE_FILE = "bridge.json";
export const BRIDGE_VERSION = 1;
export const ROUTE_PREFIX = "/__dsh-rename-chats";

/** @param {string | undefined} cwd */
export function basenameOf(cwd) {
  if (typeof cwd !== "string" || cwd.length === 0) return "";
  const parts = cwd.replace(/\\/g, "/").split("/").filter(Boolean);
  return parts.length === 0 ? "" : parts[parts.length - 1];
}

/** @param {unknown} value */
export function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/**
 * Text blocks accepted from a DSH `user/message` `content` array.
 * Non-text blocks (images, files, tool results) are intentionally skipped.
 * @param {unknown} content
 */
export function contentText(content) {
  if (typeof content === "string") return content.trim();
  if (!Array.isArray(content)) return "";
  const parts = [];
  for (const block of content) {
    if (!isRecord(block) || block.type !== "text") continue;
    if (typeof block.text === "string" && block.text.trim()) parts.push(block.text.trim());
  }
  return parts.join("\n\n").trim();
}

/**
 * Collect real human messages from a DSH event log.
 *
 * DSH also records runtime context, skill catalogs, and time context as
 * `user/message`; only `data.source.kind === "user"` is an actual human turn.
 * This filter is the difference between a useful title and an injected prompt.
 * @param {Array<Record<string, unknown>>} events
 * @param {{ first?: number, last?: number }} [limits]
 */
export function extractHumanMessages(events, limits = {}) {
  if (!Array.isArray(events)) return { count: 0, messages: [] };
  const all = [];
  for (const event of events) {
    if (!isRecord(event) || event.type !== "user/message") continue;
    const data = event.data;
    if (!isRecord(data) || !isRecord(data.source) || data.source.kind !== "user") continue;
    const text = contentText(data.content) || (typeof data.text === "string" ? data.text.trim() : "");
    if (!text) continue;
    all.push({
      seq: Number.isInteger(event.seq) ? event.seq : undefined,
      text: text.slice(0, 4000),
    });
  }
  const first = Math.max(0, limits.first ?? 3);
  const last = Math.max(0, limits.last ?? 9);
  let messages = all;
  if (all.length > first + last) {
    messages = [...all.slice(0, first), ...all.slice(-last)];
  }
  return { count: all.length, messages };
}

/**
 * Normalize one DSH session list row. `titleSnapshot` is optional because
 * sessions without an eligible first input have no title event.
 * @param {{ header?: Record<string, unknown>, session?: Record<string, unknown> }} record
 * @param {{ title?: { title?: string, eventSeq?: number, source?: { kind?: string } } }} [titleSnapshot]
 * @param {{ archived?: boolean, pinned?: boolean, live?: boolean }} [flags]
 */
export function normalizeSessionSummary(record, titleSnapshot = {}, flags = {}) {
  const header = isRecord(record.header) ? record.header : isRecord(record.session) ? record.session : {};
  const title = titleSnapshot?.title?.title;
  const titleText = typeof title === "string" && title.length > 0 ? title : null;
  const createdAt = typeof header.createdAt === "number" ? header.createdAt : null;
  return {
    id: typeof header.id === "string" ? header.id : "",
    title: titleText,
    displayTitle: titleText || basenameOf(header.cwd) || (typeof header.id === "string" ? header.id : ""),
    titleSource: titleSnapshot?.title?.source?.kind ?? null,
    titleEventSeq: titleSnapshot?.title?.eventSeq ?? null,
    createdAt,
    createdAtUnit: "milliseconds",
    cwd: typeof header.cwd === "string" ? header.cwd : null,
    origin: typeof header.origin === "string" ? header.origin : "ordinary",
    parentSession: typeof header.parentSession === "string" ? header.parentSession : null,
    archived: flags.archived === true,
    pinned: flags.pinned === true,
    live: flags.live === true,
  };
}

/**
 * Whether a session is an ordinary, non-archived, user-visible conversation.
 * @param {ReturnType<typeof normalizeSessionSummary>} session
 */
export function isOrdinarySession(session) {
  return Boolean(session.id) && !session.archived && session.origin !== "subagent" && !session.parentSession;
}

/**
 * The same title check used before and after a bridge write.
 * @param {string | null | undefined} left
 * @param {string | null | undefined} right
 */
export function sameTitle(left, right) {
  return (left ?? null) === (right ?? null);
}
