# BUILD-PI — raport: samowystarczalny pakiet `jev-adaptive-router`

Data: 2026-10-02. Rola: BUILD agent. Commit NIE robiony (diff do review).
Zmiany wyłącznie w `adapters/pi/` + `scripts/sync-core.sh` + `.gitignore` + `SMOKE-PI.md` + ten plik.
`adapters/hermes/` i `/workspace/jev-routing/core/` nietknięte (dowiezione testami).

## 1. Co zmienione (plik:linia)

### scripts/sync-core.sh (NOWY, 38 linii)
- `:14` źródło: `JEV_CORE_SRC` (default `/workspace/jev-routing/core`), `:15-16` dest liczony z `BASH_SOURCE` → uruchamialny z dowolnego cwd (udowodnione: run z repo-root i z /tmp).
- `:28` kopiuje pliki top-level bez `test_*.py` i `_testbase.py`; `:31-35` rekurencja bez `__pycache__` (defensywa na przyszłość). Wynik: 10 plików w `adapters/pi/core/`.

### .gitignore
- `:9` `adapters/pi/core/` — artefakt buildu, źródło zostaje w /workspace/jev-routing.

### adapters/pi/src/daemon.ts (NOWY, 338 linii) — cała logika probe/spawn, wstrzykiwalna
- `:27-31` stałe: probe timeout 300 ms, readiness 5000 ms / 250 ms, max 3 porty, bazowy 8790.
- `:35-40` `CORE_DEFAULTS`: `JEV_KIND=typesafe JEV_API_URL=http://localhost:8091/v1 JEV_MODEL=jevk5-0.2 JEV_TIMEOUT_MS=3000`.
- `:83` `urlFor`; `:93` `resolveDaemonConfig` — priorytet `JEVRROUTER_<SUFFIX>` > `JEV_<SUFFIX>` > default; okno portów od `JEV_CORE_PORT`; pass-through wszystkich `JEV_*` (w tym `JEV_API_KEY` — przekazywany dziecku, nigdy nie logowany); `JEV_HOME` = katalog stanu.
- `:132` `resolveStateDir` — `XDG_STATE_HOME` lub `~/.local/state`, podkatalog `jev-adaptive-router`.
- `:147` `defaultProbe` — GET /health z `X-Router-Protocol: 1`, 300 ms; klasyfikacja: header==1 → compatible, każda inna odpowiedź HTTP (brak nagłówka / zły major / 404) → occupied, błąd/timeout → free.
- `:165` `defaultSpawn` — `python3 -m core.jev_core`, detached, stdio→daemon.log, unref; ENOENT propagowany jako reject z `code`.
- `:251` `ensureDaemon` — kolejność: (1) brak `core/jev_core.py` → failed z hint `sync-core.sh`; (2) stale detection: żywy pidfile + odpowiadający port → adopt bez spawnu; (3) pętla portów 8790→8792: compatible→adopt, occupied→następny, free→re-check pidfile, spawn, zapis pidfile `"<pid> <port>"`, re-probe 20×250 ms; (4) ENOENT → failed po JEDNEJ próbie (brak retry storm); (5) timeout gotowości → failed (degraded). Nigdy nie rzuca.

### adapters/pi/src/index.ts (391 linii)
- `:16-20` nagłówek dokumentuje LSP-pattern; `:29-32` nowe envy w docs.
- `:42-45` importy (`node:url`, daemon.ts); `:47-50` `EXPLICIT_URL`/`CORE_URL`/`PACKAGE_DIR` (root pakietu z `import.meta.url`).
- `:99-110` `daemonLine` — rendering trybu daemon dla statusu.
- `:166-185` bootstrap: przy braku `JEVRROUTER_URL` start `ensureDaemon` w momencie aktywacji; `client.setUrl` po adopted/spawned; `.catch` belt → failed.
- `:219-221` render: gałąź „core unavailable (fail-open)” w degraded.
- `:228-251` session_start: `await daemonReady`, health na efektywnym URL, osobne powiadomienie degraded z przyczyną.
- `:255` before_agent_start: `await daemonReady` przed pierwszym /decide.
- `:311-316` `/router` i `:364-372` `/route status`: linia `daemon: …` (tryb/port/pid/błąd) + `client.url`.

### adapters/pi/src/client.ts (169 linii)
- `:75-80` naprawa baseline: `constructor(private opts)` → jawne pole (Node strip-only nie wspiera parameter properties — **przed moimi zmianami wszystkie 14 testów route.test.ts padało**).
- `:82-85` `setUrl`; `:87-90` `get url()`.

### adapters/pi/src/daemon.test.ts (NOWY, 288 linii, 16 testów)
Cover: defaulty configu, priorytety JEVRROUTER_>JEV_, okno portów, XDG; adopt (kompatybilny daemon, żywy pidfile), stale pidfile → spawn, foreign 8790 → spawn 8791 + pidfile + `JEV_CORE_PORT=8791`, wszystkie 3 zajęte → failed bez spawnu, sekwencja spawn+readiness (sleeps policzone), ENOENT → 1 próba + hint, timeout gotowości → 20 polls, brak vendored core → hint sync-core, klasyfikacja defaultProbe (compatible/occupied/wrong-major/free) ze stubowanym fetch. Zero pythona, zero sieci.

### adapters/pi/package.json (NOWY kształt)
- `name: jev-adaptive-router`, `version 0.1.0`, `keywords` z `pi-package`, `files: ["src","core","skills"]`, `peerDependencies: {"@earendil-works/pi-coding-agent": "*"}` (wg research §1/§5), manifesty `pi.extensions`+`pi.skills` i `omp.extensions` zachowane (surowy .ts, bez buildu), usunięte `"build": "tsc"`, `test` → `node --test src/route.test.ts src/daemon.test.ts`.

### adapters/pi/skills/jev-router/SKILL.md (NOWY)
Frontmatter `name`+`description` (wg research §3), layout jeden poziom głęboko (`skills/jev-router/SKILL.md` — omp nie rekurencjonuje). Ciało: /route on|off|status|last|show, tabele JEVRROUTER_*/JEV_*, degraded/fail-open, curl /health + pidfile + daemon.log.

### adapters/pi/README.md (NOWY)
Instalacja (npm: docelowo, local path dziś), konfiguracja env (2 tabele), diagram sekwencji mermaid extension→probe/spawn→daemon→JevK5, troubleshooting (port zajęty, brak pythona, degraded, backend down).

## 2. Wyniki testów (surowe)

```console
$ cd adapters/pi && node --test src/route.test.ts src/daemon.test.ts
# tests 30
# suites 12
# pass 30
# fail 0
# cancelled 0
# skipped 0
# todo 0
# duration_ms 92.773976
```
route.test.ts: 14/14 (plik 285 linii nietknięty — adaptacji wymagał tylko source client.ts, patrz parameter property). daemon.test.ts: 16/16.

```console
$ cd /workspace/jev-routing && python3 -m unittest discover -s core -p 'test_*.py'
Ran 154 tests in 75.402s

OK
```
Rdzeń nietknięty — 154/154.

## 3. Smoke na żywym Pi (pełne logi: SMOKE-PI.md)

- **A SPAWN**: `JEV_CORE_PORT=8795 pi -e ./src/index.ts -p --no-session` → exit 0; pidfile `170332 8795`; proces `python3 -m core.jev_core` PPID=1, cwd=adapters/pi; `/health` → protocol 1, typesafe/jevk5-0.2, warmup ok; `/decide` → **decision z JevK5** (`source: jev`, `reason_code: capped`, conf 0.476, 211 ms); receipt sesji pi (`host: pi`, task „reply with exactly: OK”) w JEV_HOME daemona.
- **B/B2 ADOPT**: default window (8790 zajęty kompatybilnym daemonem) oraz window na 8795 → pusty katalog stanu (zero spawnu), a receipt adoptowanego daemona pokazuje /decide z sesji.
- **C DEGRADED**: `JEVRROUTER_PYTHON=/nonexistent/python3` → sesja przechodzi (exit 0, fail-open), jeden attempt, brak pidfile'a, `/route status` pokazuje `UNAVAILABLE — python not found …`.
- Daemon smoke A zabity po teście (8795 → 000).

## 4. Znane ograniczenia

1. `pi` w kontenerze to omp/18.2.11; kanoniczny `@earendil-works/pi` (jiti) nie był tu dostępny — manifest i surowy .ts zgodne z research, ale realny jiti-load nie zweryfikowany lokalnie. `[INFERENCE]` oba hosty ładują ten sam entry.
2. Git-install z podkatalogu monorepo nie działa (research §5) — dystrybucja: npm publish albo local path.
3. `JEVRROUTER_URL` ustawiony = tryb external: zero probe/spawn (świadomie; dzięki temu route.test.ts pozostaje hermetyczny).
4. Wyścig dwóch Pi startujących jednocześnie na tym samym wolnym porcie: oba mogą spawnować; drugi daemon failnie na bindzie, pidfile nadpisany — niegroźne, ale pidfile może wskazywać na umarły pid do następnego cyklu (stale detection to podnosi).
5. Pętla gotowości liczona pollami (20×250 ms), nie zegarem — z realnym depsem ≈ 5 s; z wstrzykiwanym sleepem deterministyczna.
6. Brak `bin`/CLI w pakiecie — celowo (research: bin tylko dla własnego CLI).
7. `tsc --noEmit` nie odpalany (brak zainstalowanych typów hosta w repo; npm install zabroniony). Weryfikacja składniowa: strip-only load przez node --test + realne sesje pi.

BUILD DONE
