/**
 * jev-router Pi adapter — embedded core daemon lifecycle (LSP pattern).
 *
 * The npm package vendors the jev-routing core (`core/` next to `src/`). On
 * extension activation we make sure a SPI v1 daemon answers locally:
 *
 *   1. probe GET http://127.0.0.1:<port>/health with `X-Router-Protocol: 1`
 *      (300 ms timeout). Compatible answer => adopt, never spawn.
 *   2. port held by something else (any reply without the protocol header,
 *      a mismatched major, or 404) => try the next port (max 3).
 *   3. nothing listening => spawn `<python> -m core.jev_core` detached from
 *      the package dir, stdio appended to <state>/daemon.log, pid in
 *      <state>/jev-core.pid. Before spawning: a live pidfile whose port
 *      answers means a daemon is already ours => adopt (stale detection).
 *   4. after spawn: re-probe every 250 ms for up to 5 s. Failure => degraded
 *      mode; the adapter keeps routing fail-open exactly as before.
 *
 * Every probe/spawn primitive is injectable so the logic is testable without
 * starting a real Python. Nothing here ever logs secrets: the daemon env is
 * passed to the child untouched and only non-sensitive fields are surfaced.
 */
import { spawn as nodeSpawn, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const PROBE_TIMEOUT_MS = 300;
export const READY_TIMEOUT_MS = 5000;
export const READY_POLL_MS = 250;
export const MAX_PORTS = 3;
export const DEFAULT_PORT = 8790;
export const STATE_DIR_NAME = "jev-adaptive-router";

/** Decision-backend defaults for the embedded daemon (requirement: typesafe JevK5). */
export const CORE_DEFAULTS: Record<string, string> = {
	JEV_KIND: "typesafe",
	JEV_API_URL: "http://localhost:8091/v1",
	JEV_MODEL: "jevk5-0.2",
	JEV_TIMEOUT_MS: "3000",
};

export type ProbeOutcome = "compatible" | "occupied" | "free";

export interface DaemonStatus {
	mode: "external" | "adopted" | "spawned" | "failed";
	url: string;
	port?: number;
	pid?: number;
	/** Human-readable failure reason (degraded mode). Never contains secrets. */
	error?: string;
}

export interface DaemonConfig {
	host: string;
	ports: number[];
	python: string;
	/** Package root (the directory that contains `core/`). */
	packageDir: string;
	/** State directory: log + pidfile + JEV_HOME for the daemon. */
	stateDir: string;
	/** Full env for the daemon child (defaults + overrides resolved). */
	env: Record<string, string>;
}

export interface SpawnRequest {
	python: string;
	args: string[];
	cwd: string;
	env: Record<string, string>;
	logPath: string;
	port: number;
}

export interface DaemonDeps {
	/** GET /health classification for one base URL. */
	probe(url: string, timeoutMs: number): Promise<ProbeOutcome>;
	/** Start the daemon detached; resolves with its pid, rejects on spawn error (e.g. ENOENT). */
	spawn(req: SpawnRequest): Promise<number>;
	pidAlive(pid: number): boolean;
	sleep(ms: number): Promise<void>;
}

export function urlFor(host: string, port: number): string {
	return `http://${host}:${port}`;
}

/**
 * Resolve daemon configuration from an environment record.
 * Precedence for every knob: JEVRROUTER_<SUFFIX> > JEV_<SUFFIX> > built-in default.
 * Any other JEV_* present in the environment is passed through verbatim
 * (JEV_API_KEY included — forwarded to the child, never logged here).
 */
export function resolveDaemonConfig(
	env: NodeJS.ProcessEnv,
	packageDir: string,
): DaemonConfig {
	const stateDir = resolveStateDir(env);
	const pick = (suffix: string, fallback: string): string => {
		const a = (env[`JEVRROUTER_${suffix}`] ?? "").trim();
		if (a) return a;
		const b = (env[`JEV_${suffix}`] ?? "").trim();
		if (b) return b;
		return fallback;
	};

	const basePort = Number(pick("CORE_PORT", String(DEFAULT_PORT)));
	const ports: number[] = [];
	if (Number.isInteger(basePort) && basePort > 0 && basePort < 65536) {
		for (let i = 0; i < MAX_PORTS && basePort + i <= 65535; i++) ports.push(basePort + i);
	}
	if (ports.length === 0) ports.push(DEFAULT_PORT);

	const daemonEnv: Record<string, string> = { ...CORE_DEFAULTS, JEV_HOME: stateDir };
	// Explicit JEV_* / JEVRROUTER_* overrides win over the defaults above.
	for (const [k, v] of Object.entries(env)) {
		if (v === undefined) continue;
		if (k.startsWith("JEV_")) daemonEnv[k] = v;
		else if (k.startsWith("JEVRROUTER_")) daemonEnv[`JEV_${k.slice("JEVRROUTER_".length)}`] = v;
	}
	daemonEnv.JEV_CORE_PORT = String(ports[0]);

	return {
		host: "127.0.0.1",
		ports,
		python: (env.JEVRROUTER_PYTHON ?? "python3").trim() || "python3",
		packageDir,
		stateDir,
		env: daemonEnv,
	};
}

export function resolveStateDir(env: NodeJS.ProcessEnv): string {
	const xdg = (env.XDG_STATE_HOME ?? "").trim();
	if (xdg) return path.join(xdg, STATE_DIR_NAME);
	const home = env.HOME || os.homedir();
	return path.join(home, ".local", "state", STATE_DIR_NAME);
}

// -- default (production) deps -------------------------------------------------

/**
 * Probe one daemon base URL. Classification:
 *  - any HTTP reply with `X-Router-Protocol: 1`            => compatible
 *  - any other HTTP reply (foreign service, 404, wrong major) => occupied
 *  - connection refused / timeout / network failure        => free
 */
export async function defaultProbe(url: string, timeoutMs: number): Promise<ProbeOutcome> {
	try {
		const res = await fetch(`${url}/health`, {
			headers: { "X-Router-Protocol": "1" },
			signal: AbortSignal.timeout(timeoutMs),
		});
		const proto = res.headers.get("x-router-protocol");
		return proto === "1" ? "compatible" : "occupied";
	} catch {
		return "free";
	}
}

export interface SpawnError extends Error {
	code?: string;
}

/** Spawn the vendored daemon detached; stdio appended to the daemon log. */
export function defaultSpawn(req: SpawnRequest): Promise<number> {
	const { promise, resolve, reject } = Promise.withResolvers<number>();
	let logFd: number;
	try {
		logFd = fs.openSync(req.logPath, "a");
	} catch (e) {
		reject(e instanceof Error ? e : new Error(String(e)));
		return promise;
	}
	let child: ChildProcess;
	try {
		child = nodeSpawn(req.python, req.args, {
			cwd: req.cwd,
			env: req.env,
			detached: true,
			stdio: ["ignore", logFd, logFd],
		});
	} catch (e) {
		fs.closeSync(logFd);
		reject(e instanceof Error ? e : new Error(String(e)));
		return promise;
	}
	child.once("error", (err: SpawnError) => {
		fs.closeSync(logFd);
		reject(err);
	});
	child.once("spawn", () => {
		fs.closeSync(logFd);
		child.unref();
		resolve(child.pid ?? -1);
	});
	return promise;
}

export function defaultPidAlive(pid: number): boolean {
	try {
		process.kill(pid, 0);
		return true;
	} catch {
		return false;
	}
}

export const defaultDeps: DaemonDeps = {
	probe: defaultProbe,
	spawn: defaultSpawn,
	pidAlive: defaultPidAlive,
	sleep: (ms: number) => {
		const { promise, resolve } = Promise.withResolvers<void>();
		setTimeout(resolve, ms);
		return promise;
	},
};

// -- lifecycle -----------------------------------------------------------------

interface PidFileInfo {
	pid: number;
	port: number;
}

function readPidfile(pidPath: string): PidFileInfo | null {
	try {
		const raw = fs.readFileSync(pidPath, "utf8").trim();
		const [pidRaw, portRaw] = raw.split(/\s+/);
		const pid = Number(pidRaw);
		const port = Number(portRaw);
		if (!Number.isInteger(pid) || pid <= 0) return null;
		return { pid, port: Number.isInteger(port) ? port : 0 };
	} catch {
		return null;
	}
}

function writePidfile(pidPath: string, pid: number, port: number): void {
	try {
		fs.writeFileSync(pidPath, `${pid} ${port}\n`);
	} catch {
		// Pidfile is best-effort bookkeeping; losing it only weakens stale detection.
	}
}

/**
 * Ensure a SPI v1 daemon answers locally. Never throws; on any failure the
 * caller degrades to fail-open routing against the (dead) default URL.
 */
export async function ensureDaemon(
	cfg: DaemonConfig,
	deps: DaemonDeps = defaultDeps,
): Promise<DaemonStatus> {
	const coreEntry = path.join(cfg.packageDir, "core", "jev_core.py");
	if (!fs.existsSync(coreEntry)) {
		return {
			mode: "failed",
			url: urlFor(cfg.host, cfg.ports[0]),
			error: `vendored core missing (${coreEntry}); run scripts/sync-core.sh`,
		};
	}

	fs.mkdirSync(cfg.stateDir, { recursive: true });
	const logPath = path.join(cfg.stateDir, "daemon.log");
	const pidPath = path.join(cfg.stateDir, "jev-core.pid");

	// Stale detection: our daemon may already run (started by another session)
	// and simply answer on the pidfile's port.
	const known = readPidfile(pidPath);
	if (known && deps.pidAlive(known.pid)) {
		const u = urlFor(cfg.host, known.port || cfg.ports[0]);
		if ((await deps.probe(u, PROBE_TIMEOUT_MS)) === "compatible") {
			return { mode: "adopted", url: u, port: known.port, pid: known.pid };
		}
	}

	for (const port of cfg.ports) {
		const u = urlFor(cfg.host, port);
		const outcome = await deps.probe(u, PROBE_TIMEOUT_MS);
		if (outcome === "compatible") return { mode: "adopted", url: u, port };
		if (outcome === "occupied") continue; // foreign listener; try the next port

		// Free port: re-check the pidfile right before spawning (a daemon may have
		// finished booting between the first probe and here).
		const fresh = readPidfile(pidPath);
		if (fresh && deps.pidAlive(fresh.pid)) {
			const fu = urlFor(cfg.host, fresh.port || port);
			if ((await deps.probe(fu, PROBE_TIMEOUT_MS)) === "compatible") {
				return { mode: "adopted", url: fu, port: fresh.port, pid: fresh.pid };
			}
		}

		let pid: number;
		try {
			pid = await deps.spawn({
				python: cfg.python,
				args: ["-m", "core.jev_core"],
				cwd: cfg.packageDir,
				env: { ...cfg.env, JEV_CORE_PORT: String(port) },
				logPath,
				port,
			});
		} catch (e) {
			const enoent = typeof e === "object" && e !== null && (e as SpawnError).code === "ENOENT";
			const msg = enoent
				? `python not found (${cfg.python}); set JEVRROUTER_PYTHON to a python3 interpreter`
				: `spawn failed: ${e instanceof Error ? e.message : String(e)}`;
			// One attempt per activation — no retry storm.
			return { mode: "failed", url: u, port, error: msg };
		}
		writePidfile(pidPath, pid, port);

		// Wait for readiness: READY_TIMEOUT_MS/READY_POLL_MS polls. Poll-count
		// bounded (not wall-clock) so injected sleeps stay deterministic in tests.
		const maxPolls = Math.ceil(READY_TIMEOUT_MS / READY_POLL_MS);
		for (let poll = 0; ; poll++) {
			if ((await deps.probe(u, PROBE_TIMEOUT_MS)) === "compatible") {
				return { mode: "spawned", url: u, port, pid };
			}
			if (poll >= maxPolls) break;
			await deps.sleep(READY_POLL_MS);
		}
		return {
			mode: "failed",
			url: u,
			port,
			pid,
			error: `daemon on port ${port} did not answer /health within ${READY_TIMEOUT_MS} ms (see ${logPath})`,
		};
	}

	return {
		mode: "failed",
		url: urlFor(cfg.host, cfg.ports[0]),
		error: `no free port in ${cfg.ports.join(", ")} — all held by foreign services`,
	};
}
