/**
 * Tests for the embedded-daemon lifecycle (src/daemon.ts).
 *
 * No real Python is ever spawned: probe/spawn/pidAlive/sleep are injected.
 * Filesystem effects (pidfile, state dir) run against real temp dirs because
 * they are cheap and prove the on-disk contract (pidfile = "<pid> <port>").
 */
import { describe, test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
	defaultProbe,
	ensureDaemon,
	resolveDaemonConfig,
	resolveStateDir,
	READY_POLL_MS,
	READY_TIMEOUT_MS,
	type DaemonConfig,
	type DaemonDeps,
	type ProbeOutcome,
	type SpawnRequest,
} from "./daemon.ts";

function mkTmp(prefix: string): string {
	return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

/** A package dir that passes the vendored-core existence check. */
function fakePackageDir(): string {
	const dir = mkTmp("jev-pkg-");
	fs.mkdirSync(path.join(dir, "core"), { recursive: true });
	fs.writeFileSync(path.join(dir, "core", "jev_core.py"), "# fake\n");
	return dir;
}

function baseConfig(over: Partial<DaemonConfig> = {}): DaemonConfig {
	const stateDir = mkTmp("jev-state-");
	return {
		host: "127.0.0.1",
		ports: [8790, 8791, 8792],
		python: "python3",
		packageDir: fakePackageDir(),
		stateDir,
		env: { JEV_KIND: "typesafe", JEV_HOME: stateDir },
		...over,
	};
}

interface FakeOpts {
	/** Outcomes consumed per probe call; last value repeats when exhausted. */
	probe?: ProbeOutcome[];
	spawn?: (req: SpawnRequest) => Promise<number>;
	alive?: (pid: number) => boolean;
}

interface Fake {
	deps: DaemonDeps;
	probes: string[];
	spawns: SpawnRequest[];
	sleeps: number;
}

function fakeDeps(opts: FakeOpts = {}): Fake {
	const fake: Fake = { probes: [], spawns: [], sleeps: 0, deps: undefined as unknown as DaemonDeps };
	let i = 0;
	fake.deps = {
		probe: async (url: string) => {
			fake.probes.push(url);
			const seq = opts.probe ?? ["compatible"];
			const out = seq[Math.min(i, seq.length - 1)];
			i++;
			return out;
		},
		spawn: async (req: SpawnRequest) => {
			fake.spawns.push(req);
			if (opts.spawn) return opts.spawn(req);
			return 4242;
		},
		pidAlive: opts.alive ?? (() => true),
		sleep: async () => {
			fake.sleeps++;
		},
	};
	return fake;
}

function readPidfile(stateDir: string): { pid: number; port: number } | null {
	try {
		const [pid, port] = fs
			.readFileSync(path.join(stateDir, "jev-core.pid"), "utf8")
			.trim()
			.split(/\s+/)
			.map(Number);
		return { pid, port };
	} catch {
		return null;
	}
}

describe("resolveDaemonConfig", () => {
	test("built-in defaults: typesafe JevK5 on 8790-8792, state dir as JEV_HOME", () => {
		const cfg = resolveDaemonConfig({ HOME: "/home/u" }, "/pkg");
		assert.deepEqual(cfg.ports, [8790, 8791, 8792]);
		assert.equal(cfg.python, "python3");
		assert.equal(cfg.env.JEV_KIND, "typesafe");
		assert.equal(cfg.env.JEV_API_URL, "http://localhost:8091/v1");
		assert.equal(cfg.env.JEV_MODEL, "jevk5-0.2");
		assert.equal(cfg.env.JEV_TIMEOUT_MS, "3000");
		assert.equal(cfg.env.JEV_HOME, path.join("/home/u", ".local", "state", "jev-adaptive-router"));
		assert.equal(cfg.env.JEV_CORE_PORT, "8790");
	});

	test("JEV_* overrides win over defaults; JEVRROUTER_* wins over JEV_*", () => {
		const cfg = resolveDaemonConfig(
			{ JEV_MODEL: "from-jev", JEVRROUTER_MODEL: "from-router", JEV_KIND: "openai-compat", JEV_API_KEY: "k" },
			"/pkg",
		);
		assert.equal(cfg.env.JEV_MODEL, "from-router");
		assert.equal(cfg.env.JEV_KIND, "openai-compat");
		assert.equal(cfg.env.JEV_API_KEY, "k", "key passes through for the child");
	});

	test("JEV_CORE_PORT shifts the whole probe window; JEVRROUTER_PYTHON picks the interpreter", () => {
		const cfg = resolveDaemonConfig({ JEV_CORE_PORT: "8800", JEVRROUTER_PYTHON: "python3.12" }, "/pkg");
		assert.deepEqual(cfg.ports, [8800, 8801, 8802]);
		assert.equal(cfg.env.JEV_CORE_PORT, "8800");
		assert.equal(cfg.python, "python3.12");
	});

	test("XDG_STATE_HOME relocates the state dir", () => {
		assert.equal(
			resolveStateDir({ XDG_STATE_HOME: "/xdg/state", HOME: "/home/u" }),
			path.join("/xdg/state", "jev-adaptive-router"),
		);
	});
});

describe("ensureDaemon adoption", () => {
	test("compatible daemon on the first port is adopted; nothing spawns", async () => {
		const cfg = baseConfig();
		const f = fakeDeps({ probe: ["compatible"] });
		const st = await ensureDaemon(cfg, f.deps);
		assert.equal(st.mode, "adopted");
		assert.equal(st.url, "http://127.0.0.1:8790");
		assert.equal(f.spawns.length, 0);
	});

	test("live pidfile whose port answers is adopted without spawning", async () => {
		const cfg = baseConfig();
		fs.writeFileSync(path.join(cfg.stateDir, "jev-core.pid"), "999 8790\n");
		const f = fakeDeps({ probe: ["compatible"] });
		const st = await ensureDaemon(cfg, f.deps);
		assert.equal(st.mode, "adopted");
		assert.equal(st.pid, 999);
		assert.equal(f.spawns.length, 0);
	});

	test("stale pidfile (dead pid) does not block the spawn", async () => {
		const cfg = baseConfig();
		fs.writeFileSync(path.join(cfg.stateDir, "jev-core.pid"), "999 8790\n");
		const f = fakeDeps({ probe: ["free", "compatible"], alive: () => false });
		const st = await ensureDaemon(cfg, f.deps);
		assert.equal(st.mode, "spawned");
		assert.equal(f.spawns.length, 1);
	});
});

describe("ensureDaemon port fallback", () => {
	test("foreign listener on 8790 => spawn on 8791", async () => {
		const cfg = baseConfig();
		const f = fakeDeps({ probe: ["occupied", "free", "compatible"] });
		const st = await ensureDaemon(cfg, f.deps);
		assert.equal(st.mode, "spawned");
		assert.equal(st.port, 8791);
		assert.equal(st.url, "http://127.0.0.1:8791");
		assert.equal(f.spawns[0].env.JEV_CORE_PORT, "8791", "daemon told to bind the chosen port");
		assert.deepEqual(readPidfile(cfg.stateDir), { pid: 4242, port: 8791 });
	});

	test("all three ports foreign => failed, no spawn", async () => {
		const cfg = baseConfig();
		const f = fakeDeps({ probe: ["occupied", "occupied", "occupied"] });
		const st = await ensureDaemon(cfg, f.deps);
		assert.equal(st.mode, "failed");
		assert.match(st.error ?? "", /no free port/);
		assert.equal(f.spawns.length, 0);
	});
});

describe("ensureDaemon spawn", () => {
	test("nothing listening => spawn on 8790 after readiness polls", async () => {
		const cfg = baseConfig();
		const f = fakeDeps({ probe: ["free", "free", "free", "compatible"] });
		const st = await ensureDaemon(cfg, f.deps);
		assert.equal(st.mode, "spawned");
		assert.equal(st.port, 8790);
		assert.equal(st.pid, 4242);
		assert.equal(f.sleeps, 2, "250 ms polls between the three free probes");
		const req = f.spawns[0];
		assert.deepEqual(req.args, ["-m", "core.jev_core"]);
		assert.equal(req.cwd, cfg.packageDir);
		assert.equal(req.python, "python3");
		assert.equal(req.logPath, path.join(cfg.stateDir, "daemon.log"));
		assert.equal(req.env.JEV_HOME, cfg.stateDir);
	});

	test("spawn ENOENT => failed with a python hint and exactly one attempt", async () => {
		const cfg = baseConfig();
		const enoent = Object.assign(new Error("spawn python3 ENOENT"), { code: "ENOENT" });
		const f = fakeDeps({
			probe: ["free"],
			spawn: () => Promise.reject(enoent),
		});
		const st = await ensureDaemon(cfg, f.deps);
		assert.equal(st.mode, "failed");
		assert.match(st.error ?? "", /python not found/);
		assert.equal(f.spawns.length, 1, "no retry storm");
	});

	test("daemon never answers after spawn => degraded after the full poll budget", async () => {
		const cfg = baseConfig();
		const f = fakeDeps({ probe: ["free"] }); // always free
		const st = await ensureDaemon(cfg, f.deps);
		assert.equal(st.mode, "failed");
		assert.match(st.error ?? "", /did not answer \/health/);
		assert.equal(f.sleeps, Math.ceil(READY_TIMEOUT_MS / READY_POLL_MS));
	});

	test("missing vendored core => failed with sync-core hint, zero probes", async () => {
		const empty = mkTmp("jev-nopkg-");
		const cfg = baseConfig({ packageDir: empty });
		const f = fakeDeps();
		const st = await ensureDaemon(cfg, f.deps);
		assert.equal(st.mode, "failed");
		assert.match(st.error ?? "", /sync-core/);
		assert.equal(f.probes.length, 0);
	});
});

describe("defaultProbe classification", () => {
	type FetchInit = { headers?: Record<string, string>; signal?: unknown };
	function stubFetch(impl: (url: string, init?: FetchInit) => Promise<unknown>): FetchInit[] {
		const seen: FetchInit[] = [];
		const g = globalThis as unknown as { fetch: (url: string, init?: FetchInit) => Promise<unknown> };
		g.fetch = async (url: string, init?: FetchInit) => {
			seen.push(init ?? {});
			return impl(url, init);
		};
		return seen;
	}
	const realFetch = globalThis.fetch;

	test("X-Router-Protocol: 1 => compatible; header is sent on the probe", async () => {
		try {
			const seen = stubFetch(async () => ({
				headers: { get: (n: string) => (n === "x-router-protocol" ? "1" : null) },
			}));
			assert.equal(await defaultProbe("http://h:1", 300), "compatible");
			assert.equal(seen[0]?.headers?.["X-Router-Protocol"], "1");
		} finally {
			globalThis.fetch = realFetch;
		}
	});

	test("reply without the protocol header (or wrong major) => occupied", async () => {
		try {
			stubFetch(async () => ({ headers: { get: () => null } }));
			assert.equal(await defaultProbe("http://h:2", 300), "occupied");
			stubFetch(async () => ({ headers: { get: (n: string) => (n === "x-router-protocol" ? "2" : null) } }));
			assert.equal(await defaultProbe("http://h:3", 300), "occupied");
		} finally {
			globalThis.fetch = realFetch;
		}
	});

	test("connection error / timeout => free", async () => {
		try {
			stubFetch(async () => {
				throw new Error("ECONNREFUSED");
			});
			assert.equal(await defaultProbe("http://h:4", 300), "free");
		} finally {
			globalThis.fetch = realFetch;
		}
	});
});
