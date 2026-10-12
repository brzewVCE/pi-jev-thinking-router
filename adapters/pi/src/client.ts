/**
 * jev-router Pi adapter — SPI v1 core client + effort routing.
 *
 * Before each agent turn: send the turn state to the jev-router core daemon
 * (POST /decide, CONTRACT.md SPI v1), get an effort decision back, apply
 * pi.setThinkingLevel(). v1 is effort-only: the core never routes models, the
 * executing model stays whatever the user picked.
 *
 * Fail-open: any error (core down, timeout, unparsable decision) keeps the
 * current thinking level — the turn is never broken and effort is never
 * escalated against the user's will.
 */

/** SPI v1 /decide request (CONTRACT.md § POST /decide). */
export interface DecideRequest {
  host: string;
  session_id: string;
  turn_id?: string;
  task: string;
  recent_turns?: string;
  has_images: boolean;
  current_effort: string;
  supported_levels: string[];
  cap?: string;
  budget_ms?: number;
}

/** SPI v1 decision shape. `effort` is always within the sent supported_levels. */
export interface RouterDecision {
  effort: string;
  changed: boolean;
  source: string; // jev | cache | hysteresis | fallback
  reason_code: string; // closed set per CONTRACT.md
  confidence?: number;
  policy_version?: string;
  elapsed_ms?: number;
}

export interface DecideResponse {
  ok: boolean;
  decision?: RouterDecision;
}

export interface CoreClientOptions {
  url: string; // e.g. http://127.0.0.1:8790
  timeoutMs?: number; // must exceed core JEV_TIMEOUT_MS + HTTP overhead
}

/** GET /health response shape (SPI v1; extra keys tolerated). */
export interface CoreHealth {
  ok: boolean;
  backend?: string;
  model?: string | null;
  kind?: string;
  policy_version?: string;
  credential_present?: boolean;
  warmup?: string; // pending | ok | failed:<reason>
  state?: { last_messages: number; max_tokens: number };
  protocol_version?: number;
}

export const PROTOCOL = "1";

/** Runtime guard for the wire decision — external input is never trusted blindly. */
function isRouterDecision(v: unknown): v is RouterDecision {
	if (typeof v !== "object" || v === null) return false;
	const d = v as Record<string, unknown>;
	return (
		typeof d.effort === "string" &&
		typeof d.changed === "boolean" &&
		typeof d.reason_code === "string"
	);
}

export class CoreClient {
  constructor(private opts: CoreClientOptions) {}

  /** POST /decide. Returns the decision or null on ANY failure (fail-open). */
  async decide(req: DecideRequest): Promise<RouterDecision | null> {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), this.opts.timeoutMs ?? 3000);
    try {
      const res = await fetch(`${this.opts.url}/decide`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Router-Protocol": PROTOCOL,
        },
        body: JSON.stringify(req),
        signal: ctrl.signal,
      });
      const proto = res.headers.get("X-Router-Protocol");
      if (proto && Number(proto) !== 1) {
        console.warn(`jev-router: unsupported core protocol ${proto}`);
        return null;
      }
      const payload: unknown = await res.json();
      if (typeof payload !== "object" || payload === null) return null;
      const obj = payload as Record<string, unknown>;
      return obj.ok === true && isRouterDecision(obj.decision) ? obj.decision : null;
    } catch {
      return null; // fail-open
    } finally {
      clearTimeout(t);
    }
  }

  async health(): Promise<boolean> {
    try {
      const res = await fetch(`${this.opts.url}/health`, { signal: AbortSignal.timeout(1000) });
      return res.ok;
    } catch {
      return false;
    }
  }

  /** Typed /health: state window + protocol version, or null when core unreachable. */
  async healthInfo(): Promise<CoreHealth | null> {
    try {
      const res = await fetch(`${this.opts.url}/health`, { signal: AbortSignal.timeout(1000) });
      if (!res.ok) return null;
      const body: unknown = await res.json();
      if (typeof body !== "object" || body === null) return null;
      // All CoreHealth fields are optional and unknown keys are tolerated
      // (SPI v1 forward-compat), so an object-shaped body is sufficient here.
      return body as CoreHealth;
    } catch {
      return null;
    }
  }
}

/** Minimal surface of the Pi extension API we depend on (effort-only). */
export interface PiLike {
  setThinkingLevel(level: string): void;
}

/**
 * Apply a v1 decision: thinking level only.
 * No decision / changed=false / level outside the host palette / already at the
 * target level => no-op (returns false). Never touches the model.
 */
export function applyDecision(
  pi: PiLike,
  decision: RouterDecision | null,
  validLevels: readonly string[],
  currentThinking?: string,
): boolean {
  if (!decision || !decision.changed) return false;
  const effort = decision.effort;
  if (typeof effort !== "string" || !validLevels.includes(effort)) return false;
  if (currentThinking !== undefined && currentThinking === effort) return false;
  pi.setThinkingLevel(effort);
  return true;
}
