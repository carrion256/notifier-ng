/**
 * notifier-ng OMP extension adapter.
 *
 * Thin factory (documented OMP extension API) that turns a terminal
 * `agent_end` into exactly one normalized idle record and hands it to
 * `notifier_ng.py ingest` over stdin (NDJSON).
 *
 * Deliberately no `session_shutdown` handler: it would only duplicate the
 * terminal `agent_end` idle record for the same session end (idle vs. stopped
 * semantics on one exit), so it is omitted.
 */
import path from "node:path";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

/**
 * notifier-ng ingest entrypoint.
 *
 * NOTIFIER_NG_INGEST (absolute path to notifier_ng.py) is the explicit
 * override — the installer's wrapper hook (install.py step 2) pins it to the
 * checkout when a symlink cannot be created. Without it, the core resolves
 * sibling-relative to this adapter (integrations/ sits next to notifier_ng.py),
 * so the hook works from any checkout location with no machine-specific path.
 * The override is read at delivery time, not at module load: ESM evaluates an
 * imported adapter before the importer's body, so an installer wrapper that
 * assigns the variable must still be honored when the first record ships.
 */
function ingestPath(): string {
	const configured = process.env.NOTIFIER_NG_INGEST?.trim();
	return configured ? configured : path.join(import.meta.dir, "..", "notifier_ng.py");
}

/** Stable source name for records produced by this adapter. */
const SOURCE = "omp";

/** Source-side ceilings for the optional summarizer context payload. */
const MAX_CONTEXT_MESSAGES = 20;
const MAX_CONTEXT_CHARS = 20_000;

interface ContentLike {
	type?: string;
	text?: string;
}

interface MessageLike {
	role?: string;
	content?: string | ContentLike[];
	timestamp?: number;
	responseId?: string;
	stopReason?: string;
}

/** One context item; role restricted so only user/assistant turns reach the summarizer. */
interface ContextItemLike {
	role: "user" | "assistant";
	text: string;
}

/** Structural view of the agent_end payload (run-time fields may exceed the declared types). */
interface AgentEndEventLike {
	isTerminal?: boolean;
	sessionFile?: string;
	messages?: MessageLike[];
}

interface SessionManagerLike {
	getSessionFile?(): string | undefined;
	getSessionId?(): string | undefined;
}

interface HandlerContextLike {
	cwd?: string;
	sessionManager?: SessionManagerLike;
}

/** Deterministic, key-sorted serialization (stable event_id fallback). */
function canonicalJson(value: unknown): string {
	if (value === null || typeof value !== "object") {
		return JSON.stringify(value);
	}
	if (Array.isArray(value)) {
		return `[${value.map(canonicalJson).join(",")}]`;
	}
	const entries = Object.entries(value as Record<string, unknown>)
		.filter(([, v]) => v !== undefined)
		.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
	return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonicalJson(v)}`).join(",")}}`;
}

/** SHA-256 hex of arbitrary input (deterministic fingerprint; keeps event_id derivation stable). */
function sha256Hex(input: string): string {
	return new Bun.CryptoHasher("sha256").update(input).digest("hex");
}

function logError(pi: ExtensionAPI, message: string): void {
	// Documented API exposes pi.logger; the installed runtime does not. Probe at runtime.
	const loggerApi = pi as { logger?: { error?(msg: string): void } };
	if (loggerApi.logger?.error) {
		loggerApi.logger.error(message);
	} else {
		console.error(`[notifier-ng] ${message}`);
	}
}
/** Fixed non-secret session environment allowlist (shared by contract with the
 * Python adapters; never a wildcard prefix export, so token-like NZM_*
 * variables stay out). Grouped by origin to preserve the metadata schema. */
const ZELLIJ_ENV_ALLOWLIST = ["ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"] as const;
const NZM_ENV_ALLOWLIST = ["NZM_SESSION_NAME", "NZM_FLEET_PANE", "NZM_FLEET_ROLE"] as const;

function envSubset<T extends string>(names: readonly T[]): Partial<Record<T, string>> {
	const out: Partial<Record<T, string>> = {};
	for (const name of names) {
		const value = process.env[name];
		if (value !== undefined && value !== "") {
			out[name] = value;
		}
	}
	return out;
}

/** Whitespace-normalized text of a message (string content or text blocks only). */
function normalizedText(message: MessageLike | undefined): string | undefined {
	if (message === undefined) return undefined;
	const raw =
		typeof message.content === "string"
			? message.content
			: Array.isArray(message.content)
				? message.content
						.filter((part) => part?.type === "text" && typeof part.text === "string")
						.map((part) => part.text)
						.join("\n")
				: "";
	const normalized = raw.replace(/\s+/g, " ").trim();
	return normalized.length === 0 ? undefined : normalized;
}

function messageText(message: MessageLike | undefined): string | undefined {
	const normalized = normalizedText(message);
	if (normalized === undefined) return undefined;
	return normalized.length <= 280 ? normalized : `${normalized.slice(0, 279).trimEnd()}…`;
}

function taskResult(task: string | undefined, result: string | undefined): string {
	const parts: string[] = [];
	if (task) parts.push(`Task: ${task}`);
	if (result) parts.push(`Result: ${result}`);
	return parts.join("\n") || "Agent finished with no text response";
}

/**
 * Optional summarizer context: newest user/assistant messages with text,
 * oldest-first, capped at 20 items and 20,000 aggregate chars (newest kept
 * under the char cap; a single over-long item is trimmed to fit).
 */
function buildContext(messages: MessageLike[]): ContextItemLike[] {
	const candidates: ContextItemLike[] = [];
	for (const message of messages) {
		const role = message?.role;
		const text = normalizedText(message);
		if ((role === "user" || role === "assistant") && text !== undefined) {
			candidates.push({ role, text });
		}
	}
	const items = candidates.slice(-MAX_CONTEXT_MESSAGES);
	const kept: ContextItemLike[] = [];
	let total = 0;
	for (let i = items.length - 1; i >= 0; i--) {
		const room = MAX_CONTEXT_CHARS - total;
		if (room <= 0) break;
		const item = items[i];
		const text = item.text.length <= room ? item.text : item.text.slice(0, room);
		kept.unshift({ role: item.role, text });
		total += text.length;
	}
	return kept;
}
/** One-way path-free subject for a session file: short SHA-256 of the resolved path. */
function sessionFileSubject(sessionFile: string): string {
	return `session-file:${sha256Hex(path.resolve(sessionFile)).slice(0, 12)}`;
}

/**
 * Stable, path-free subject: the session id when one is available, else a
 * one-way short hash of the session file. Never emits the session-file path
 * itself, so default status-only notifications cannot disclose local
 * directory layout; the hash keeps the subject stable and unique per session
 * file without leaking directory components.
 */
function deriveSubject(event: AgentEndEventLike, ctx: HandlerContextLike): string | undefined {
	const sessionId: string | undefined = ctx.sessionManager?.getSessionId?.();
	if (typeof sessionId === "string" && sessionId.length > 0) {
		return `session:${sessionId}`;
	}
	const sessionFile: string | undefined =
		typeof event.sessionFile === "string" && event.sessionFile.length > 0
			? event.sessionFile
			: ctx.sessionManager?.getSessionFile?.();
	if (typeof sessionFile === "string" && sessionFile.length > 0) {
		return sessionFileSubject(sessionFile);
	}
	return undefined;
}

/**
 * Stable event_id: the terminal assistant message id, else its timestamp,
 * else a deterministic fingerprint of the last message (or the subject when
 * the run produced no messages at all).
 */
function deriveEventId(event: AgentEndEventLike, subject: string): string {
	const messages = Array.isArray(event.messages) ? event.messages : [];
	let lastAssistant: MessageLike | undefined;
	for (let i = messages.length - 1; i >= 0; i--) {
		if (messages[i]?.role === "assistant") {
			lastAssistant = messages[i];
			break;
		}
	}
	if (typeof lastAssistant?.responseId === "string" && lastAssistant.responseId.length > 0) {
		return `omp:agent_end:${lastAssistant.responseId}`;
	}
	if (typeof lastAssistant?.timestamp === "number") {
		return `omp:agent_end:${lastAssistant.timestamp}`;
	}
	const last = messages[messages.length - 1];
	return `omp:agent_end:${last ? sha256Hex(canonicalJson(last)) : sha256Hex(subject)}`;
}

/** Spawn ingest, feed one normalized NDJSON record on stdin, log failures, never throw. */
async function deliver(pi: ExtensionAPI, record: Record<string, unknown>): Promise<void> {
	const ingest = ingestPath();
	if (!(await Bun.file(ingest).exists())) {
		logError(pi, `notifier-ng ingest not found at ${ingest}; set NOTIFIER_NG_INGEST to the checkout's notifier_ng.py`);
		return;
	}
	const proc = Bun.spawn([ingest, "ingest"], {
		stdin: "pipe",
		stdout: "ignore",
		stderr: "pipe",
	});
	proc.stdin!.write(`${JSON.stringify(record)}\n`);
	proc.stdin!.end();

	const exitCode = await proc.exited;
	if (exitCode !== 0) {
		let stderr = "";
		try {
			stderr = (await new Response(proc.stderr).text()).trim().slice(0, 500);
		} catch {
			// stderr already consumed/closed; exit code alone is enough to report.
		}
		logError(pi, `notifier-ng ingest exited ${exitCode}${stderr ? `: ${stderr}` : ""}`);
	}
}

async function handleAgentEnd(pi: ExtensionAPI, event: AgentEndEventLike, ctx: HandlerContextLike): Promise<void> {
	const subject = deriveSubject(event, ctx);
	if (subject === undefined) {
		return; // no stable subject, nothing to report
	}
	const cwd = typeof ctx.cwd === "string" ? ctx.cwd : process.cwd();

	const metadata: Record<string, unknown> = { cwd };
	const zellij = envSubset(ZELLIJ_ENV_ALLOWLIST);
	const nzm = envSubset(NZM_ENV_ALLOWLIST);
	if (Object.keys(zellij).length > 0) metadata.zellij = zellij;
	if (Object.keys(nzm).length > 0) metadata.nzm = nzm;

	const messages = Array.isArray(event.messages) ? event.messages : [];
	const lastAssistantIndex = messages.findLastIndex((message) => message?.role === "assistant");
	const lastAssistant = lastAssistantIndex >= 0 ? messages[lastAssistantIndex] : undefined;
	const lastUser =
		lastAssistantIndex >= 0
			? messages.slice(0, lastAssistantIndex).findLast((message) => message?.role === "user")
			: messages.findLast((message) => message?.role === "user");
	const contextItems = buildContext(messages);

	const record: Record<string, unknown> = {
		version: 1,
		source: SOURCE,
		subject,
		state: "idle",
		mode: "event",
		event_id: deriveEventId(event, subject),
		title: "OMP agent idle",
		message: taskResult(messageText(lastUser), messageText(lastAssistant)),
		timestamp: new Date().toISOString(),
		metadata,
		...(contextItems.length > 0 ? { context: { items: contextItems } } : {}),
	};

	await deliver(pi, record);
}

export default function notifierOmp(pi: ExtensionAPI): void {
	pi.on("agent_end", async (event, ctx) => {
		// Run-time agent_end payload may carry isTerminal/sessionFile beyond the declared type; probe structurally.
		const agentEnd = event as unknown as AgentEndEventLike;
		if (agentEnd.isTerminal === false) {
			return; // non-terminal agent-loop notification: no idle record
		}
		// Installed ReadonlySessionManager exposes getSessionFile/getSessionId; keep only those.
		const sessionManager = ctx.sessionManager as SessionManagerLike;
		try {
			await handleAgentEnd(pi, agentEnd, {
				cwd: ctx.cwd,
				sessionManager,
			});
		} catch (error: unknown) {
			logError(pi, `notifier-ng agent_end delivery failed: ${String(error)}`);
		}
	});
}
