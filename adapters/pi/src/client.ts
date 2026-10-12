/**
 * jev-router Pi adapter — core client + turn routing.
 *
 * Before each agent turn: send conversation state to the jev-router-core
 * daemon, get a decision, apply pi.setModel() + pi.setThinkingLevel().
 * Fail-open: any error keeps the current model.
 *
 * Child routing (phase 2): classify the delegate instruction and pass
 * {model, thinking} into pi-subagents spawn parameters.
 */

export interface RouterDecision {
  model?: string;
  thinking?: string;
  source?: string;
  rule?: string | null;
  spawn?: { allowed: boolean; forced: boolean; vetoed: boolean; needs_spawn_prob?: number };
}

export interface DecideResponse {
  ok: boolean;
  decision?: RouterDecision;
  answers?: Record<string, unknown>;
  error?: string;
}

export interface CoreClientOptions {
  url: string;            // e.g. http://127.0.0.1:8790
  timeoutMs?: number;     // default 3000 (must exceed core classifier 2000ms + overhead)
  harness?: string;       // default "pi"
}

export class CoreClient {
  constructor(private opts: CoreClientOptions) {}

  async decide(state: string, context: Record<string, unknown> = {}): Promise<RouterDecision | null> {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), this.opts.timeoutMs ?? 3000);
    try {
      const res = await fetch(`${this.opts.url}/decide`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          state,
          context,
          harness: this.opts.harness ?? "pi",
          session: String(context.session_id ?? ""),
        }),
        signal: ctrl.signal,
      });
      const proto = res.headers.get("X-Router-Protocol");
      if (proto && Number(proto) !== 1) {
        console.warn(`jev-router: unsupported core protocol ${proto}`);
        return null;
      }
      const payload = (await res.json()) as DecideResponse;
      return payload.ok && payload.decision ? payload.decision : null;
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
}

/** Minimal surface of the Pi extension API we depend on. */
export interface PiLike {
  setModel(model: string): void;
  setThinkingLevel(level: string): void;
}

export function applyDecision(pi: PiLike, decision: RouterDecision | null, current?: { model?: string; thinking?: string }): boolean {
  if (!decision?.model) return false;
  let applied = false;
  if (decision.model !== current?.model) {
    pi.setModel(decision.model);
    applied = true;
  }
  const thinking = decision.thinking;
  if (thinking === "off") {
    // C2: explicitly downgrade so a trivial lane clears an existing high level.
    if (current?.thinking && current.thinking !== "off") {
      pi.setThinkingLevel("off");
      applied = true;
    }
  } else if (thinking && thinking !== current?.thinking) {
    pi.setThinkingLevel(thinking);
    applied = true;
  }
  return applied;
}
