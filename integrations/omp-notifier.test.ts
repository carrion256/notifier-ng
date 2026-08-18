/**
 * Bun-exercised privacy contract for the OMP adapter (omp-notifier.ts).
 *
 * Drives the real exported adapter through its documented extension surface:
 * registers the agent_end handler, emits terminal events with a session-file
 * path, and captures the exact NDJSON record the adapter writes to the ingest
 * subprocess via a minimal capture sink (NOTIFIER_NG_INGEST override, the
 * documented installer hook). Asserts the subject never contains the session
 * path, stays stable for repeated deliveries of the same session, and keeps
 * the normalized event schema.
 *
 * Run as: bun test integrations/omp-notifier.test.ts
 */
import { afterEach, expect, test } from "bun:test";
import { chmod, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import notifierOmp from "./omp-notifier.ts";

/** Minimal ingest that forwards stdin verbatim to the embedded capture file. */
function captureSink(outFile: string): string {
	return `#!/usr/bin/env bun
// Test-only ingest capture sink: writes stdin verbatim, exits 0.
await Bun.write(${JSON.stringify(outFile)}, await new Response(Bun.stdin).text());
`;
}

const SESSION_FILE = "/home/alice/private/session.jsonl";

type AgentEndHandler = (event: unknown, ctx: unknown) => Promise<void>;

function installAdapter(): AgentEndHandler {
	let handler: AgentEndHandler | undefined;
	const pi = {
		on(_name: string, fn: AgentEndHandler): void {
			handler = fn;
		},
	};
	notifierOmp(pi as never);
	if (!handler) throw new Error("adapter did not register an agent_end handler");
	return handler;
}

let captureDir: string;

async function setupHarness(): Promise<{ emit: AgentEndHandler; outFile: string }> {
	captureDir = await mkdtemp(path.join(tmpdir(), "omp-notifier-test-"));
	const capture = path.join(captureDir, "ingest-capture");
	const outFile = path.join(captureDir, "record.ndjson");
	await Bun.write(capture, captureSink(outFile));
	await chmod(capture, 0o755);
	process.env.NOTIFIER_NG_INGEST = capture;
	return { emit: installAdapter(), outFile };
}

afterEach(async () => {
	if (captureDir) await rm(captureDir, { recursive: true, force: true });
});

async function capturedRecord(outFile: string): Promise<Record<string, unknown>> {
	const text = (await Bun.file(outFile).text()).trim();
	if (text.length === 0) throw new Error("ingest captured no record");
	return JSON.parse(text) as Record<string, unknown>;
}

const SCHEMA_KEYS = [
	"version",
	"source",
	"subject",
	"state",
	"mode",
	"event_id",
	"title",
	"message",
	"timestamp",
	"metadata",
];

function assertSchema(record: Record<string, unknown>): void {
	expect(Object.keys(record).sort()).toEqual([...SCHEMA_KEYS].sort());
	expect(record.version).toBe(1);
	expect(record.source).toBe("omp");
	expect(record.state).toBe("idle");
	expect(record.mode).toBe("event");
	expect(typeof record.subject).toBe("string");
	expect(typeof record.event_id).toBe("string");
	expect(record.event_id).toMatch(/^omp:agent_end:/);
	expect(record.title).toBe("OMP agent idle");
	expect(typeof record.message).toBe("string");
	expect(typeof record.timestamp).toBe("string");
	expect(record.metadata).toMatchObject({ cwd: "/srv/work" });
}

test("context session id becomes the subject; the session-file path is never emitted", async () => {
	const { emit, outFile } = await setupHarness();
	await emit(
		{ isTerminal: true, sessionFile: SESSION_FILE, messages: [] },
		{
			cwd: "/srv/work",
			sessionManager: {
				getSessionId: () => "sess-42",
				getSessionFile: () => SESSION_FILE,
			},
		},
	);
	const record = await capturedRecord(outFile);
	assertSchema(record);
	expect(record.subject).toBe("session:sess-42");
	expect(JSON.stringify(record)).not.toContain("/home/alice");
	expect(JSON.stringify(record)).not.toContain("session.jsonl");
});

test("path-only fallback subject is a stable opaque hash with no path components", async () => {
	const { emit, outFile } = await setupHarness();
	const event = { isTerminal: true, sessionFile: SESSION_FILE, messages: [] };
	const ctx = {
		cwd: "/srv/work",
		sessionManager: { getSessionFile: () => SESSION_FILE },
	};
	await emit(event, ctx);
	const first = await capturedRecord(outFile);
	assertSchema(first);
	const subject = first.subject as string;
	expect(subject).toMatch(/^session-file:[0-9a-f]{12}$/);
	expect(subject).not.toContain("/");
	expect(JSON.stringify(first)).not.toContain("/home/alice");
	expect(JSON.stringify(first)).not.toContain("session.jsonl");

	// Stable across repeated deliveries of the same session (dedup continuity).
	await emit(event, ctx);
	const second = await capturedRecord(outFile);
	expect(second.subject).toBe(subject);
	expect(second.event_id).toBe(first.event_id);

	// Distinct session files keep distinct subjects (dedup separation).
	const otherFile = "/var/other/place/session.jsonl";
	await emit(
		{ ...event, sessionFile: otherFile },
		{ ...ctx, sessionManager: { getSessionFile: () => otherFile } },
	);
	const third = await capturedRecord(outFile);
	assertSchema(third);
	expect(third.subject).not.toBe(subject);
	expect(JSON.stringify(third)).not.toContain("/var/other");
});

test("no session id and no session file: nothing is delivered", async () => {
	const { emit, outFile } = await setupHarness();
	await emit(
		{ isTerminal: true, messages: [] },
		{
			cwd: "/srv/work",
			sessionManager: { getSessionId: () => undefined, getSessionFile: () => undefined },
		},
	);
	expect(await Bun.file(outFile).exists()).toBe(false);
});
