/**
 * Tests for the Pi adapter's `route` command: routing toggle affects /decide
 * calls (off = zero core requests), session isolation, `show` controls the
 * widget/status bar/notifications, `last`/`status` reply strings, and
 * fail-open with a dead core.
 *
 * Run: bun test   (from adapters/pi)
 */
import { describe, test } from "node:test";
import assert from "node:assert/strict";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

/** Response-shaped fake consumed by CoreClient. */
interface FakeResponse {
	ok: boolean;
	status: number;
	headers: { get(name: string): string | null };
	json(): Promise<unknown>;
}

type FetchFn = (url: string, init?: unknown) => Promise<FakeResponse>;
type FetchCall = { url: string; init: unknown };

const HEALTH = {
	ok: true, backend: "configured", model: "jev-test", kind: "openai-compat",
	policy_version: "1", credential_present: false, warmup: "ok",
	state: { last_messages: 3, max_tokens: 250 }, protocol_version: 1,
};
const DECIDE_LOW = {
	ok: true,
	decision: { effort: "low", changed: true, source: "jev", reason_code: "jev_selected", elapsed_ms: 12 },
};

function jsonResponse(body: unknown): FakeResponse {
	return {
		ok: true,
		status: 200,
		headers: { get: (name: string) => (name.toLowerCase() === "x-router-protocol" ? "1" : null) },
		json: async () => body,
	};
}

/** Replaces global fetch for the current test; records every call. */
function stubFetch(handler: FetchFn): FetchCall[] {
	const calls: FetchCall[] = [];
	// Test seam: the adapter calls the real global fetch; we swap it for a fake.
	const g = globalThis as unknown as { fetch: FetchFn };
	g.fetch = async (url: string, init?: unknown) => {
		calls.push({ url, init });
		return handler(url, init);
	};
	return calls;
}

const healthyFetch: FetchFn = async (url) => {
	if (url.endsWith("/health")) return jsonResponse(HEALTH);
	if (url.endsWith("/decide")) return jsonResponse(DECIDE_LOW);
	throw new Error(`unexpected url: ${url}`);
};

interface TurnEvent {
	prompt?: string;
	images?: unknown[];
}

interface UiDouble {
	widgets: Array<[string, unknown]>;
	statuses: Array<[string, string | undefined]>;
	notifications: Array<[string, string | undefined]>;
	setWidget(key: string, content: unknown, opts?: unknown): void;
	setStatus(key: string, text: string | undefined): void;
	notify(message: string, type?: string): void;
}

interface CtxDouble {
	hasUI: boolean;
	ui: UiDouble;
	sessionManager: { getSessionId(): string; getBranch(): unknown[] };
	model?: { reasoning?: boolean; thinkingLevelMap?: Record<string, unknown> };
}

interface PiDouble {
	events: Record<string, (event: TurnEvent, ctx: CtxDouble) => Promise<void>>;
	commands: Record<string, { description?: string; handler: (args: string, ctx: CtxDouble) => Promise<void> }>;
	thinking: { level: string | undefined };
}

function makePi(): PiDouble {
	const events: PiDouble["events"] = {};
	const commands: PiDouble["commands"] = {};
	const thinking = { level: "medium" as string | undefined };
	const pi = {
		events,
		commands,
		thinking,
		setLabel(_label: string) {},
		on(name: string, h: (event: TurnEvent, ctx: CtxDouble) => Promise<void>) { events[name] = h; },
		registerCommand(name: string, opts: { description?: string; handler: (args: string, ctx: CtxDouble) => Promise<void> }) {
			commands[name] = opts;
		},
		getThinkingLevel() { return thinking.level; },
		setThinkingLevel(level: string) { thinking.level = level; },
	};
	return pi;
}

function makeCtx(sessionId: string): CtxDouble {
	const ui: UiDouble = {
		widgets: [],
		statuses: [],
		notifications: [],
		setWidget(key, content) { this.widgets.push([key, content]); },
		setStatus(key, text) { this.statuses.push([key, text]); },
		notify(message, type) { this.notifications.push([message, type]); },
	};
	return { hasUI: true, ui, sessionManager: { getSessionId: () => sessionId, getBranch: () => [] } };
}

let importCounter = 0;
/**
 * Fresh extension instance per call with the given env applied at module load.
 * Dynamic import is intentional: the adapter reads env at import time, so a
 * cache-busted specifier is the only way to test distinct startup configs.
 */
async function loadExtension(env: Record<string, string>): Promise<(pi: PiDouble) => void> {
	const prev: Record<string, string | undefined> = {};
	for (const k of Object.keys(env)) prev[k] = process.env[k];
	Object.assign(process.env, env);
	const mod = await import(`./index.ts?cb=${++importCounter}`);
	for (const [k, v] of Object.entries(prev)) {
		if (v === undefined) delete process.env[k];
		else process.env[k] = v;
	}
	// The double covers exactly the ExtensionAPI surface the adapter uses.
	return mod.default as unknown as (pi: PiDouble) => void;
}

async function booted(env: Record<string, string> = {}) {
	const ext = await loadExtension({ JEVRROUTER_URL: "http://core.test", ...env });
	const pi = makePi();
	ext(pi);
	const ctx = makeCtx("s1");
	const calls = stubFetch(healthyFetch);
	await pi.events.session_start({}, ctx);
	calls.length = 0;
	return { pi, ctx, calls };
}

const decideCalls = (calls: FetchCall[]) => calls.filter((c) => c.url.endsWith("/decide")).length;

describe("route command registration", () => {
	test("registers route and keeps the legacy router command", async () => {
		const { pi } = await booted();
		assert.ok(pi.commands.route, "route command registered");
		assert.ok(pi.commands.router, "legacy router command kept");
	});
});

describe("routing toggle affects core calls", () => {
	test("off => zero /decide calls; on resumes them", async () => {
		const { pi, ctx, calls } = await booted();
		await pi.events.before_agent_start({ prompt: "hello" }, ctx);
		assert.equal(decideCalls(calls), 1);
		await pi.commands.route.handler("off", ctx);
		assert.match(ctx.ui.notifications.at(-1)![0], /routing OFF for this session/);
		await pi.events.before_agent_start({ prompt: "hello" }, ctx);
		assert.equal(decideCalls(calls), 1, "off makes no new core calls");
		await pi.commands.route.handler("on", ctx);
		await pi.events.before_agent_start({ prompt: "hello" }, ctx);
		assert.equal(decideCalls(calls), 2);
	});

	test("on applies the decision to the thinking level", async () => {
		const { pi, ctx } = await booted();
		await pi.events.before_agent_start({ prompt: "hello" }, ctx);
		assert.equal(pi.thinking.level, "low");
	});
});

describe("session isolation", () => {
	test("off in one session does not affect another", async () => {
		const { pi, calls } = await booted();
		const ctx1 = makeCtx("s1");
		const ctx2 = makeCtx("s2");
		await pi.commands.route.handler("off", ctx1);
		await pi.events.before_agent_start({ prompt: "x" }, ctx2);
		assert.equal(decideCalls(calls), 1, "s2 still routes");
		await pi.events.before_agent_start({ prompt: "x" }, ctx1);
		assert.equal(decideCalls(calls), 1, "s1 stays off");
	});
});

describe("show toggle controls the UI surface", () => {
	test("show off clears widget and status bar", async () => {
		const { pi, ctx } = await booted();
		await pi.commands.route.handler("show off", ctx);
		assert.equal(ctx.ui.widgets.at(-1)![1], undefined, "widget cleared");
		assert.equal(ctx.ui.statuses.at(-1)![1], undefined, "status bar cleared");
	});

	test("show off suppresses per-turn notifications but keeps routing", async () => {
		const { pi, ctx, calls } = await booted();
		await pi.commands.route.handler("show off", ctx);
		ctx.ui.notifications.length = 0;
		await pi.events.before_agent_start({ prompt: "x" }, ctx);
		assert.equal(decideCalls(calls), 1, "routing still active");
		assert.equal(ctx.ui.notifications.length, 0, "no notifications while show=off");
	});

	test("show on keeps the widget rendering the decision", async () => {
		const { pi, ctx } = await booted();
		await pi.events.before_agent_start({ prompt: "x" }, ctx);
		const widget = ctx.ui.widgets.at(-1)![1];
		assert.ok(Array.isArray(widget), "widget content is a string array");
		assert.match(widget.join("\n"), /thinking=low/);
	});
});

describe("last / status / usage strings", () => {
	test("last reports the decision fields", async () => {
		const { pi, ctx } = await booted();
		await pi.events.before_agent_start({ prompt: "x" }, ctx);
		await pi.commands.route.handler("last", ctx);
		const msg = ctx.ui.notifications.at(-1)![0];
		assert.match(msg, /thinking=low/);
		assert.match(msg, /reason=jev_selected/);
		assert.match(msg, /12ms/);
	});

	test("last with no decision yet", async () => {
		const { pi, ctx } = await booted();
		await pi.commands.route.handler("last", ctx);
		assert.match(ctx.ui.notifications.at(-1)![0], /no decision recorded yet/);
	});

	test("status reports routing, visibility and core state", async () => {
		const { pi, ctx } = await booted();
		await pi.commands.route.handler("status", ctx);
		const msg = ctx.ui.notifications.at(-1)![0];
		assert.match(msg, /routing: ON/);
		assert.match(msg, /visibility \(show\): ON/);
		assert.match(msg, /online/);
	});

	test("unknown subcommand returns usage", async () => {
		const { pi, ctx } = await booted();
		await pi.commands.route.handler("bogus", ctx);
		assert.match(ctx.ui.notifications.at(-1)![0], /route on \| off/);
	});

	test("show without a value returns usage", async () => {
		const { pi, ctx } = await booted();
		await pi.commands.route.handler("show", ctx);
		assert.match(ctx.ui.notifications.at(-1)![0], /usage: route show on\|off/);
	});
});

describe("fail-open with a dead core", () => {
	test("turn never breaks; thinking stays as-is; command still works", async () => {
		const ext = await loadExtension({ JEVRROUTER_URL: "http://127.0.0.1:1" });
		const pi = makePi();
		ext(pi);
		const ctx = makeCtx("s1");
		stubFetch(async () => {
			throw new Error("ECONNREFUSED");
		});
		await pi.events.session_start({}, ctx);
		await pi.events.before_agent_start({ prompt: "x" }, ctx);
		assert.equal(pi.thinking.level, "medium", "dead core keeps the current level");
		await pi.commands.route.handler("off", ctx);
		assert.match(ctx.ui.notifications.at(-1)![0], /routing OFF/);
		await pi.commands.route.handler("status", ctx);
		assert.match(ctx.ui.notifications.at(-1)![0], /OFFLINE/);
	});
});

describe("JEVRROUTER_UI_DEFAULT", () => {
	test("off => new sessions start with routing disabled", async () => {
		const { pi, ctx, calls } = await booted({ JEVRROUTER_UI_DEFAULT: "off" });
		await pi.events.before_agent_start({ prompt: "x" }, ctx);
		assert.equal(decideCalls(calls), 0);
		await pi.commands.route.handler("on", ctx);
		await pi.events.before_agent_start({ prompt: "x" }, ctx);
		assert.equal(decideCalls(calls), 1);
	});
});

describe("dynamic model thinking levels", () => {
	test("model without reasoning skips /decide calls completely", async () => {
		const { pi, ctx, calls } = await booted();
		ctx.model = { reasoning: false };
		await pi.events.before_agent_start({ prompt: "x" }, ctx);
		assert.equal(decideCalls(calls), 0, "non-reasoning model makes 0 decide calls");
	});

	test("model with reasoning excludes 'off' from supported_levels", async () => {
		const { pi, ctx, calls } = await booted();
		ctx.model = { reasoning: true };
		await pi.events.before_agent_start({ prompt: "x" }, ctx);
		assert.equal(decideCalls(calls), 1);
		const decideCall = calls.find((c) => c.url.endsWith("/decide"))!;
		const init = decideCall.init as { body: string };
		const body = JSON.parse(init.body);
		assert.ok(!body.supported_levels.includes("off"), "'off' must be excluded");
		assert.deepEqual(body.supported_levels, ["minimal", "low", "medium", "high"]);
	});

	test("model with thinkingLevelMap respects null overrides and xhigh/max", async () => {
		const { pi, ctx, calls } = await booted();
		ctx.model = {
			reasoning: true,
			thinkingLevelMap: {
				minimal: null, // explicitly disabled
				xhigh: "some-provider-mapping",
			},
		};
		await pi.events.before_agent_start({ prompt: "x" }, ctx);
		const decideCall = calls.find((c) => c.url.endsWith("/decide"))!;
		const init = decideCall.init as { body: string };
		const body = JSON.parse(init.body);
		assert.deepEqual(body.supported_levels, ["low", "medium", "high", "xhigh"]);
	});
});
