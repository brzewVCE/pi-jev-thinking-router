/**
 * jev-router Pi/omp extension entrypoint (SPI v1, effort-only).
 *
 * Per user prompt: ask the jev-router core daemon for a reasoning-effort
 * decision (POST /decide), apply pi.setThinkingLevel(), and make the routing
 * visible:
 *   - widget above the editor: current effort + source/reason/confidence/latency
 *   - status-bar entry: compact "⚡<effort>"
 *   - notification on every routing change (JEVRROUTER_NOTIFY=0 to silence)
 *   - /router slash command: core health + last decision on demand
 *
 * v1 never changes the model — only the thinking level. Fail-open: core down /
 * timeout / bad decision keeps the current thinking level; the widget shows
 * the degraded state instead of hiding it.
 *
 * Config (env, mirrors the Hermes adapter):
 *   JEVRROUTER_URL             core daemon base URL (default http://127.0.0.1:8790)
 *   JEVRROUTER_TIMEOUT_MS      per-call timeout (default 3000)
 *   JEVRROUTER_ENABLED         "0" disables the extension entirely (default "1")
 *   JEVRROUTER_NOTIFY          "0" suppresses per-turn notifications (default "1")
 *   JEVRROUTER_CAP             user ceiling level (optional; core default = current_effort)
 *   JEVRROUTER_DEFAULT_EFFORT  effort assumed when the harness reports none (default "medium")
 * Slash command `/route` (per-session, in-memory):
 *   route on|off        enable/disable routing decisions (off = zero core calls)
 *   route status        core health + session flags + last decision
 *   route last          last routing decision this session
 *   route show on|off   widget / status bar / notification visibility
 * Startup default: JEVRROUTER_UI_DEFAULT=on|off (per-session flags are not
 * persisted across restarts on Pi; the env default pins the startup state).
 * The legacy `/router` command (health + last) is kept unchanged.
 */
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { CoreClient, applyDecision } from "./client.ts";

const CORE_URL = (process.env.JEVRROUTER_URL ?? "http://127.0.0.1:8790").replace(/\/+$/, "");
const TIMEOUT_MS = Number(process.env.JEVRROUTER_TIMEOUT_MS ?? "3000");
const ENABLED = process.env.JEVRROUTER_ENABLED !== "0";
const NOTIFY = process.env.JEVRROUTER_NOTIFY !== "0";
const CAP = (process.env.JEVRROUTER_CAP ?? "").trim();
const DEFAULT_EFFORT = (process.env.JEVRROUTER_DEFAULT_EFFORT ?? "medium").trim() || "medium";

const UI_KEY = "jev-router";

/** Per-session routing/visibility flags. A toggle in one session never leaks. */
interface UiState {
	enabled: boolean;
	show: boolean;
}

function parseBool(raw: string | undefined | null): boolean | undefined {
	const s = (raw ?? "").trim().toLowerCase();
	if (["on", "1", "true", "enabled", "yes"].includes(s)) return true;
	if (["off", "0", "false", "disabled", "no"].includes(s)) return false;
	return undefined;
}

/** Startup defaults: built-in <- JEVRROUTER_UI_DEFAULT (on|off). */
const UI_DEFAULTS: UiState = {
	enabled: parseBool(process.env.JEVRROUTER_UI_DEFAULT) ?? true,
	show: true,
};

const uiSessions = new Map<string, UiState>();

function uiFor(sessionId: string): UiState {
	let st = uiSessions.get(sessionId);
	if (!st) {
		st = { ...UI_DEFAULTS };
		uiSessions.set(sessionId, st);
	}
	return st;
}

/** One-line rendering of a decision for `route last` / `route status`. */
function fmtRoute(v: RouteView): string {
	const conf = v.confidence === undefined ? "" : ` conf=${v.confidence.toFixed(2)}`;
	const lat = v.latencyMs === undefined ? "" : ` ${v.latencyMs}ms`;
	return `${v.applied ? "APPLIED" : "not applied"} → thinking=${v.effort} ` +
		`src=${v.source ?? "-"} reason=${v.reasonCode ?? "-"}${conf}${lat}` +
		`${v.note ? ` · ${v.note}` : ""}`;
}

/** Thinking levels Pi can actually run — the full SPI v1 canon. Validated before stamping. */
const THINKING_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"] as const;

interface RouteView {
	effort: string;
	source?: string;
	reasonCode?: string;
	confidence?: number;
	latencyMs?: number;
	applied: boolean;
	note?: string;
}

interface StateWindow {
	last_messages: number;
	max_tokens: number;
}

function textOf(content: unknown): string {
	if (typeof content === "string") return content;
	if (Array.isArray(content)) {
		return content
			.filter((block): block is { type: "text"; text: unknown } =>
				typeof block === "object" && block !== null && (block as { type?: unknown }).type === "text")
			.map((block) => (typeof block.text === "string" ? block.text : ""))
			.join(" ");
	}
	return "";
}

/**
 * Condensed recent-turns window (SPI v1 `recent_turns`): last N user/assistant
 * messages, tail-capped. This is DATA for the selector, never instructions.
 */
function buildRecentTurns(branch: readonly unknown[], win: StateWindow): string {
	const prior: string[] = [];
	for (const raw of branch) {
		if (typeof raw !== "object" || raw === null) continue;
		const entry = raw as { type?: unknown; message?: { role?: unknown; content?: unknown } };
		if (entry.type !== "message") continue;
		const role = entry.message?.role;
		if (role !== "user" && role !== "assistant") continue;
		const text = textOf(entry.message?.content);
		if (text.trim()) prior.push(`${role}: ${text}`);
	}
	const window = prior.slice(-Math.max(0, win.last_messages));
	let joined = window.join("\n");
	const limit = win.max_tokens * 4; // ~4 chars/token, same heuristic as Hermes
	if (joined.length > limit) joined = joined.slice(-limit);
	return joined;
}

export default function jevRouterExtension(pi: ExtensionAPI): void {
	const client = new CoreClient({ url: CORE_URL, timeoutMs: TIMEOUT_MS });
	let stateWindow: StateWindow = { last_messages: 4, max_tokens: 1500 };
	let last: RouteView | null = null;
	let turn = 0;

	pi.setLabel("jev-router");

	function render(ctx: ExtensionContext): void {
		if (!ctx.hasUI) return;
		const ui = uiFor(ctx.sessionManager.getSessionId());
		if (!ui.show) {
			// Visibility off: clear the widget + status bar. Routing itself keeps
			// running (or not) per `ui.enabled` — show only controls the UI surface.
			ctx.ui.setWidget(UI_KEY, undefined);
			ctx.ui.setStatus(UI_KEY, undefined);
			return;
		}
		if (!ui.enabled) {
			ctx.ui.setWidget(UI_KEY, ["⚡ jev-router: routing OFF (thinking stays manual)"], { placement: "aboveEditor" });
			ctx.ui.setStatus(UI_KEY, "⚡ router: off");
			return;
		}
		if (last) {
			const conf = last.confidence === undefined ? "" : ` · conf ${last.confidence.toFixed(2)}`;
			const lat = last.latencyMs === undefined ? "" : ` · ${last.latencyMs}ms`;
			ctx.ui.setWidget(
				UI_KEY,
				[
					`${last.applied ? "✓" : "✗"} jev-router → thinking=${last.effort}`,
					`  src=${last.source ?? "-"} · reason=${last.reasonCode ?? "-"}${conf}${lat}${last.note ? ` · ${last.note}` : ""}`,
				],
				{ placement: "aboveEditor" },
			);
			ctx.ui.setStatus(UI_KEY, `⚡ ${last.effort}`);
		} else {
			ctx.ui.setWidget(UI_KEY, ["⚡ jev-router: armed (no decision yet)"], { placement: "aboveEditor" });
			ctx.ui.setStatus(UI_KEY, "⚡ router: armed");
		}
	}

	pi.on("session_start", async (_event, ctx) => {
		const info = await client.healthInfo();
		if (info?.state) stateWindow = info.state;
		if (NOTIFY && uiFor(ctx.sessionManager.getSessionId()).show) {
			if (info) {
				ctx.ui.notify(
					`jev-router: core online @ ${CORE_URL} (SPI v1, warmup: ${info.warmup ?? "?"}, window: ${stateWindow.last_messages} msgs / ${stateWindow.max_tokens} tok)`,
					"info",
				);
			} else {
				ctx.ui.notify(
					`jev-router: core OFFLINE @ ${CORE_URL} — routing disabled, thinking stays manual (fail-open)`,
					"warning",
				);
			}
		}
		render(ctx);
	});

	pi.on("before_agent_start", async (event, ctx) => {
		if (!ENABLED) return;
		const sessionId = ctx.sessionManager.getSessionId();
		// `route off` disables the decisions themselves, not just the UI:
		// the turn makes ZERO /decide calls.
		if (!uiFor(sessionId).enabled) return;
		const currentEffort = pi.getThinkingLevel() ?? DEFAULT_EFFORT;
		try {
			const prompt = event.prompt ?? "";
			const recent = buildRecentTurns(ctx.sessionManager.getBranch(), stateWindow);
			if (!prompt.trim() && !recent.trim()) return;
			turn += 1;
			const decision = await client.decide({
				host: "pi",
				session_id: sessionId,
				turn_id: String(turn),
				task: prompt.trim() || recent,
				recent_turns: prompt.trim() ? recent : "",
				has_images: (event.images?.length ?? 0) > 0,
				current_effort: currentEffort,
				supported_levels: [...THINKING_LEVELS],
				...(CAP ? { cap: CAP } : {}),
				budget_ms: TIMEOUT_MS,
			});
			if (!decision) {
				last = {
					effort: currentEffort,
					applied: false,
					note: "no usable decision from core (fail-open)",
				};
				render(ctx);
				return;
			}
			const applied = applyDecision(pi, decision, THINKING_LEVELS, currentEffort);
			last = {
				effort: decision.effort,
				source: decision.source,
				reasonCode: decision.reason_code,
				confidence: decision.confidence,
				latencyMs: decision.elapsed_ms,
				applied,
				note: applied ? undefined : `${decision.reason_code}: keeping ${currentEffort}`,
			};
			render(ctx);
			if (applied && NOTIFY && uiFor(sessionId).show) {
				ctx.ui.notify(`⚡ jev-router → thinking=${decision.effort} (${decision.reason_code})`, "info");
			}
		} catch (e: unknown) {
			// Fail-open by design: never break the turn; surface the degradation.
			const msg = e instanceof Error ? e.message : String(e);
			last = { effort: currentEffort, applied: false, note: `router error: ${msg}` };
			render(ctx);
		}
	});

	pi.registerCommand("router", {
		description: "jev-router: core health + last routing decision",
		handler: async (_args, ctx) => {
			const info = await client.healthInfo();
			const lines = [
				`core: ${CORE_URL} — ${info ? `online (protocol ${info.protocol_version ?? "?"}, warmup: ${info.warmup ?? "?"})` : "OFFLINE (fail-open)"}`,
				`state window: ${stateWindow.last_messages} msgs / ${stateWindow.max_tokens} tok · turns decided: ${turn}`,
				last
					? `last: ${last.applied ? "APPLIED" : "not applied"} → thinking=${last.effort} · src=${last.source ?? "-"} · reason=${last.reasonCode ?? "-"}${last.confidence !== undefined ? ` · conf=${last.confidence.toFixed(2)}` : ""}${last.note ? ` · ${last.note}` : ""}`
					: "last: none yet",
			];
			ctx.ui.notify(lines.join("\n"), "info");
		},
	});

	pi.registerCommand("route", {
		description: "jev-router: route on|off | status | last | show on|off",
		handler: async (args, ctx) => {
			const sessionId = ctx.sessionManager.getSessionId();
			const ui = uiFor(sessionId);
			const parts = (args ?? "").trim().split(/\s+/).filter(Boolean);
			const sub = (parts[0] ?? "").toLowerCase();
			if (sub === "on" || sub === "off") {
				ui.enabled = sub === "on";
				render(ctx);
				ctx.ui.notify(
					`⚡ jev-router: routing ${sub.toUpperCase()} for this session — ` +
						(ui.enabled ? "decisions requested per turn" : "zero /decide calls; thinking stays manual"),
					"info",
				);
				return;
			}
			if (sub === "show") {
				const val = parseBool(parts[1]);
				if (val === undefined) {
					ctx.ui.notify("usage: route show on|off", "warning");
					return;
				}
				ui.show = val;
				render(ctx);
				ctx.ui.notify(
					`⚡ jev-router: widget/status bar/notifications ${val ? "ON" : "OFF"} for this session`,
					"info",
				);
				return;
			}
			if (sub === "last") {
				ctx.ui.notify(
					last ? `jev last: ${fmtRoute(last)}` : "jev: no decision recorded yet for this session",
					"info",
				);
				return;
			}
			if (sub === "status") {
				const info = await client.healthInfo();
				ctx.ui.notify(
					[
						`⚡ jev-router status (session ${sessionId})`,
						`  routing: ${ui.enabled ? "ON" : "OFF"} · visibility (show): ${ui.show ? "ON" : "OFF"}`,
						`  core: ${CORE_URL} — ${info ? `online (protocol ${info.protocol_version ?? "?"}, warmup: ${info.warmup ?? "?"})` : "OFFLINE (fail-open)"}`,
						`  turns decided: ${turn} · ${last ? `last: ${fmtRoute(last)}` : "last: none yet"}`,
					].join("\n"),
					"info",
				);
				return;
			}
			ctx.ui.notify(
				[
					"jev-router route — usage:",
					"  route on | off       enable/disable routing decisions (off = zero core calls)",
					"  route status        core health + session flags + last decision",
					"  route last          last routing decision this session",
					"  route show on|off   widget / status bar / notification visibility",
				].join("\n"),
				"info",
			);
		},
	});
}
