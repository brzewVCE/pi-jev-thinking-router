Jestes BUILD agentem. NIE INSTALUJ NIC (npm install zabroniony; omp/pi wbudowane).
Pracuj WYLACZNIE w /workspace/jev-router-core/adapters/pi/ (plus nowy plik
/workspace/jev-router-core/scripts/sync-core.sh). Nie ruszaj adapters/hermes ani core/.

Kontekst: SPI v1 (docs/SPI.md, CONTRACT w /workspace/jev-routing/CONTRACT.md),
rdzeń daemon: /workspace/jev-routing/core/ (stdlib-only python3, 154 testy przechodza).
Badanie pakietowania: docs/RESEARCH-PI-PACKAGING.md — PRZECZYTAJ JE PRZED ZMIANAMI
i zastosuj jego ustalenia (surowy TS bez buildu, manifest pi.*, skills w pakiecie,
peerDependencies "@earendil-works/pi-coding-agent": "*", keywords ["pi-package"]).

CEL: adapter Pi staje sie samowystarczalnym pakietem npm "jev-adaptive-router"
wzorca LSP: pi install npm:jev-adaptive-router -> rozszerzenie startuje WDOSBUDOWANY
rdzen jev-core jako lokalny daemon, bez zadnej infrastruktury po stronie uzytkownika.

WYMAGANIA:

1. sync-core.sh: kopiuje /workspace/jev-routing/core/ -> adapters/pi/core/
   (bez __pycache__, bez plikow test_*). Uruchamialny z repo-root. Dodaj
   adapters/pi/core/ do .gitignore (to artefakt buildu, zrodlo jest gdzie indziej).

2. Spawn daemona w src/index.ts (aktywacja rozszerzenia):
   - probe GET http://127.0.0.1:8790/health z naglowkiem X-Router-Protocol: 1,
     timeout 300 ms. Jezeli odpowiada i naglowek X-Router-Protocol: 1 -> przyjdz
     istniejacy daemon (nie spawnuj).
   - Jezeli port zajety przez COS INNEGO (odpowiada bez naglowka protokolu lub 404)
     -> sprobaj 8791, potem 8792 (max 3 porty).
   - Jezeli nic nie slucha -> spawn: JEVRROUTER_PYTHON (default "python3")
     -m core.jev_core z cwd = katalog pakietu (gdzie lezy core/), detached,
     stdio do pliku loga w katalogu stanu (~/.local/state/jev-adaptive-router/
     albo stdenv XDG_HOME), pidfile tamze. Przed spawnem: jesli pidfile istnieje
     i kill -0 dziala i port odpowiada -> nie spawnuj ponownie (stale detection).
   - Env dla daemona z defaults + override'ow JEVRROUTER_* / JEV_* (patrz 3).
   - Po spawnie: ponowny probe do 5 s (co 250 ms). Nieudany -> tryb degraded:
     widget/status pokazuje "core unavailable", routing fail-open jak dotad.
   - python3 nie znaleziony (ENOENT spawnu) -> komunikat w /route status, brak retry storm.

3. Domyślny backend decyzyjny (envy z defaultami, przekazywane daemonowi):
   JEV_KIND=typesafe  JEV_API_URL=http://localhost:8091/v1  JEV_MODEL=jevk5-0.2
   JEV_CORE_PORT=8790 JEV_TIMEOUT_MS=3000 JEV_HOME=<katalog stanu>.
   Wszystkie nadpisalne przez JEVRROUTER_* / JEV_* w srodowisku Pi.

4. package.json: name "jev-adaptive-router", version 0.1.0, keywords ["pi-package"],
   files: ["src","core","skills"], peerDependencies hosta wg research, zachowaj
   manifest pi.extensions i omp.extensions (surowy .ts, bez build step).
   Usun "build": "tsc" jesli research mowi ze zbędne (testy node --test zostaw).

5. skills/jev-router/SKILL.md w pakiecie: frontmatter name+description (wg
   RESEARCH-PI-PACKAGING.md), tresc: obsluga /route on|off|status|last|show,
   zmienne JEVRROUTER_* i JEV_*, co znaczy degraded/fail-open, jak sprawdzic
   daemon (curl /health). Zgodnie z dokumentem research: omp skanuje skills
   tylko jeden poziom glabosci.

6. Testy: zaktualizuj/adaptuj src/route.test.ts (node --test) pod nowy modul
   spawn (wyodrebn logike port-probe/spawn do src/daemon.ts, testowalna bez
   uruchamiania prawdziwego pythona: injectable probe/spawn fns). Odpal
   "node --test src/route.test.ts" i nowe testy daemon.ts — wszystkie musza
   przechodzic. Odpal tez "python3 -m unittest discover -s core -p 'test_*.py'"
   w /workspace/jev-routing zeby udowodnic ze rdzen nietkniety.

7. SMOKE NA ZYWYM PI: jezeli kontener ma "pi" w PATH — odpal testowa sesje
   (pi -e lub pi z --extension sciezka do adapters/pi/src/index.ts) z
   JEVRROUTER_URL wskazujacym na wlasnie spawnety daemon; udowodnij: daemon
   wstal sam (pidfile + /health), /decide zwraca decision z JevK5 (jezeli
   :8091 zyje — sprawdz curl'em; jezeli nie, udokumentuj i pokaz decision
   z fallbacku). Zapisz surowe logi do /workspace/jev-router-core/SMOKE-PI.md.

8. README.md w adapters/pi: instalacja (docelowo npm:jev-adaptive-router, teraz
   local path), konfiguracja env, wykres LSP (extension -> spawn -> daemon ->
   JevK5), rozwiazywanie problemow (port zajety, brak pythona, degraded).

ZASADY: fail-open nienaruszalny; sekrety nigdzie nie logowane; nie psuj
istniejacych 285 linii testow route.test.ts (adaptuj jesli trzeba); trzymaj
diff w adapters/pi + scripts/sync-core.sh + .gitignore + SMOKE-PI.md.
Commit NIE rob (diff zostanie do review). Na koncie: raport do
/workspace/jev-router-core/BUILD-PI.md: co zmienione plik:linia, wyniki
testow (surowe), wyniki smoke, znane ograniczenia. OSTATNIA LINIA 'BUILD DONE'.
