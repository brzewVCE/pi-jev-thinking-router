# RESEARCH: Pakiet/rozszerzenie i SKILL dla pi-coding-agent

Data: 2026-10-02. Rola: researcher. Nic nie instalowano — tylko registry npm + oficjalne docs (HTTP read).

## 0. Linia produkcyjna: KTO jest "oficjalnym pi"

Trzy byty, łatwo pomylić:

| npm | repo | bin | katalog konfig. | status |
|---|---|---|---|---|
| `@mariozechner/pi-coding-agent` | `badlogic/pi-mono` | `pi` | `~/.pi` | **deprecated** — "please use @earendil-works/pi-coding-agent instead going forward" (pole `deprecated` w registry, dot. wszystkich wersji 0.x) |
| `@earendil-works/pi-coding-agent` | `earendil-works/pi` (dawniej pi-mono; latest 1.0.0) | `pi` | `~/.pi` | **oficjalne pi** (Mario Zechner + mitsuhiko jako maintainers) |
| `@oh-my-pi/pi-coding-agent` | `can1357/oh-my-pi` (latest 18.4.10) | `omp` | `~/.omp` | fork (oh-my-pi); zachowuje kompatybilność z manifestem `pi.*` |

Dokumentacja kanoniczna pi: `https://github.com/earendil-works/pi/tree/main/packages/coding-agent/docs` — pliki `extensions.md`, `skills.md`, `packages.md`, `configuration.md`, `settings.md`, `cli.md`.
Dokumentacja omp: `https://github.com/can1357/oh-my-pi/tree/main/docs` — `extension-loading.md`, `skills.md`.

Nasz lokalny `adapters/pi/package.json` deklaruje oba klucze (`pi.extensions` + `omp.extensions`) i importuje typy z `@oh-my-pi/pi-coding-agent` — to znaczy, że celujemy w oba hosty. Poniżej najpierw pi (kanon), potem różnice omp.

---

## 1. Minimalne rozszerzenie (extension)

### 1.1 Najmniejsza forma: pojedynczy plik .ts, BEZ buildu

Z `docs/extensions.md` (pi):

> Create `~/.pi/agent/extensions/hello.ts`:
> ```typescript
> import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
>
> export default function (pi: ExtensionAPI) {
>   pi.registerCommand("hello", {
>     description: "Show a greeting",
>     handler: async (name, ctx) => {
>       ctx.ui.notify(`Hello, ${name || "world"}!`, "info");
>     },
>   });
> }
> ```
> Pi uses `jiti`, so local TypeScript extensions do not need a separate compilation step.

**Odpowiedź wprost: surowy `.ts`, nie kompilowany JS.** Pi (przez `jiti`) i omp (przez Bun loader) ładują `.ts` bezpośrednio. Oba realne przykłady z npm (pkt 4) publikują surowy TypeScript — brak skryptu `build`, brak `dist/`.

### 1.2 Struktura katalogowa pakietu-rozszerzenia

Z `docs/packages.md` (pi) — "conventional layout":

```text
my-pi-package/
├── package.json
├── extensions/
├── skills/
├── prompts/
└── themes/
```

> Without a `pi` manifest, Pi discovers TypeScript and JavaScript extensions, skill directories, Markdown prompts, and JSON themes from those directories.

Manifest jawny (gdy zasoby żyją gdzie indziej):

```json
{
  "name": "my-pi-package",
  "keywords": ["pi-package"],
  "pi": {
    "extensions": ["./src/extension.ts"],
    "skills": ["./resources/skills"],
    "prompts": ["./resources/prompts/*.md"],
    "themes": ["./resources/themes/*.json"]
  }
}
```

> Paths are relative to the package root. Arrays accept glob patterns and exclusions.

Pojedynczy plik = małe rozszerzenie; katalog z `index.ts` = wieloplikowa implementacja (`extensions.md`: "Pi loads direct TypeScript or JavaScript files and subdirectories containing an `index.ts` or `index.js` entry point"). Nasz layout `src/index.ts` + `src/client.ts` jest OK bo manifest wskazuje entry; importy względne działają przez jiti/Bun.

### 1.3 Klucz `pi.extensions` w package.json

- Forma: tablica ścieżek relatywnych do roota pakietu, plik `.ts`/`.js` (omp dopuszcza też `.mjs`/`.cjs`) albo katalog z `index.ts`.
- omp (`docs/extension-loading.md`): "Plugin extension entries come from package `omp.extensions` / `pi.extensions` manifests" oraz **"omp takes precedence over pi when both manifest objects exist"**. Manifest directory-resolution omp: `package.json` z `omp.extensions` → `index.ts` → `index.js` → scan 1 poziomu.
- Deklaracja nie jest wymagana jeśli używamy konwencjonalnego katalogu `extensions/` w pakiecie pi.

---

## 2. Instalacja: `pi install npm:X` / `pi install git:url`

Z `docs/packages.md` (pi):

```bash
pi install npm:@example/pi-tools@1.0.0
pi install git:github.com/example/pi-tools@v1
pi install ./local-package
pi -e npm:@example/pi-tools        # jednorazowo, bez zapisu do settings
```

| Source | Behavior (cytat) |
|---|---|
| npm | "Installed under the Pi npm directory" |
| git | "Cloned and reconciled to the selected ref" |
| URL | `https://github.com/...` — "Treated as a git source" |
| Local | "Loaded from the resolved path without copying" |

- Wpis ląduje w `~/.pi/agent/settings.json` (`packages: [...]`); `--local`/`-l` → `.pi/settings.json` projektu (ładowany dopiero po project trust).
- `pi list`, `pi remove <source>`, `pi update --extensions`, `pi config` — zarządzanie.
- Filtry per-źródło w settings (obiektem `{source, extensions, skills, ...}`) — zawężają, nie rozszerzają manifestu.

### Czego wymaga npm-pakiet (pytanie "bin? files? publish?")

**Wymagane:**
1. **Opublikowany na npm** (public; scoped pakiety z `publishConfig.access: "public"`).
2. **Wykrywalne zasoby**: konwencjonalne katalogi (`extensions/`, `skills/`, `prompts/`, `themes/`) LUB manifest `pi` w package.json. Bez jednego i drugiego pakiet zainstaluje się, ale nic nie wniesie.
3. **Zależności runtime w `dependencies`** — "Pi installs package dependencies when it installs an npm or git source."
4. **Pakiety hosta w `peerDependencies` z `"*"`** — cytowany katalog hosta pi: `@earendil-works/pi-ai`, `@earendil-works/pi-agent-core`, `@earendil-works/pi-coding-agent`, `@earendil-works/pi-tui`, `typebox`. "Declare the host-provided packages listed above in `peerDependencies` with a `\"*\"` range and do not bundle them. Pi suppresses automatic peer installation for managed npm packages." Błąd konfiguracji (host-pakiet w `dependencies`) → ostrzeżenie rozszerzenia, bo fizyczna kopia dubluje klasy/rejestry.

**Niewymagane:**
- **`bin`** — nie jest potrzebny do instalacji/ładowania (potrzebny tylko jeśli pakiet daje własne CLI; patrz pi-jev `jev-gate`).
- **`files`** — nie wymagane przez pi; standardowa mechanika npm (tarball = co nie jest gitignored). Oba przykłady nie mają `files`.
- **build/`dist`** — nie; surowy `.ts` w tarballu jest ładowany przez jiti.
- **`main`/`exports`** — nie dla celów pi (przykłady ustawiają `main` na `extensions/index.ts` tylko dla wygody typechecku).
- Keyword **`pi-package`** — nie wymuszony, ale "makes an npm package eligible for discovery in the Pi package gallery (pi.dev/packages)". Opcjonalne `pi.image`/`pi.video` → podgląd w galerii.

**Git-install:** repo traktowane jak zwykły katalog pakietu — te same reguły (konwencjonalne katalogi lub `pi` manifest w package.json repo). Ref pinowany: "Git tags and commits are also pinned; package updates reconcile the checkout but do not move a configured ref."

**Local:** "`./pi-tools` — Loaded from the resolved path without copying"; "Local packages are not installed or modified, so their dependency tree remains the package author's responsibility." Ścieżka do pliku = jeden extension; katalog = normalna dyscovery pakietu. To nasza ścieżka dew: `pi -e /path/to/jev-router-core/adapters/pi` lub `"packages": ["/path/to/..."]` w settings (tak robi README pi-fast-jev-compaction).

---

## 3. Skills poza plikiem .md — TAK, pełne katalogi z zasobami

### 3.1 Kanoniczny layout (pi, `docs/skills.md`)

> A skill is a directory containing `SKILL.md`:
> ```text
> pdf-tools/
> ├── SKILL.md
> ├── scripts/
> │   └── extract.sh
> ├── references/
> │   └── formats.md
> └── assets/
>     └── template.json
> ```

Frontmatter + ciało:

```markdown
---
name: pdf-tools
description: Extract text and tables from PDF files. Use when reading, converting, or inspecting PDFs.
---

# PDF tools

Read `references/formats.md` before converting a document. Run scripts relative to this skill directory.
```

**Pola frontmatter (spec Agent Skills, agentskills.io — pi go implementuje):**

| Field | Purpose |
|---|---|
| `name` | Command and display name |
| `description` | Routing description shown to the model |
| `license` | License name or bundled license file |
| `compatibility` | Environment requirements |
| `metadata` | Additional key-value metadata |
| `allowed-tools` | Experimental pre-approved tool list |
| `disable-model-invocation` | Hide the skill from automatic model selection |

**Nie ma pola `instructions`** — instrukcją jest **ciało markdownu** po frontmatter. `name`: lowercase/numery/hyphen, ≤64 znaki; `description` ≤1024. "Pi neither requires nor warns when the declared name differs from the parent directory" (inne implementacje mogą wymagać — więc dopasowuj). "Malformed SKILL.md files and declared skills without descriptions are not loaded."

### 3.2 Jak działa ładowanie

> At startup, Pi scans configured skill locations and adds each skill's name, description, and path to the system prompt. It does not add the full instructions. When a task matches, the model reads `SKILL.md` and follows its instructions.

Progressive disclosure: metadata w system prompcie, pełna treść czytana na żądanie; `/skill:name [args]` wymusza załadowanie (args dokleiane jako user request). "Pi tells the model where the skill lives so it can resolve those paths" — ścieżki relatywne od katalogu skilla.

### 3.3 Gdzie trafiają (pi)

Z `docs/configuration.md` + `docs/skills.md`:

| Lokalizacja | Ścieżka |
|---|---|
| User (agent dir, domyślnie `~/.pi/agent`) | `~/.pi/agent/skills/` — "User skills and supporting files" |
| Projekt | `.pi/skills/` (po project trust) |
| Agent Skills standard | `~/.agents/skills/` i `.agents/skills/` (ancestor-walk do roota repo) |
| Settings | `skills: string[]` w `settings.json` (pliki lub katalogi; globs, `!`/`+`/`-`) |
| CLI | `--skill <path>` (powtarzalne), `-ns/--no-skills` |
| **Pakiet** | `skills/` konwencjonalnie lub `pi.skills` w manifest |

"Directories containing `SKILL.md` are discovered recursively" (pi). Standalone `.md` skills są akceptowane, ale "a directory containing `SKILL.md` is the portable form and should be preferred".

### 3.4 Skill w pakiecie npm — TAK

`docs/skills.md`: "Use a [Pi package](packages.md) to distribute one or more skills through npm or git." `docs/packages.md`: "Pi packages install and distribute extensions, skills, prompt templates, and themes as one unit" — przez konwencjonalny `skills/` lub `"pi": { "skills": [...] }`. Mieszany pakiet (extension + skill) jest w pełni wspierany i filtrowalny per typ przez settings.

Zbiory przykładów: `github.com/anthropics/skills`, `github.com/badlogic/pi-skills`.

### 3.5 Różnice omp (fork) — `can1357/oh-my-pi/docs/skills.md`

- Layout: **`<skills-root>/<skill-name>/SKILL.md`, nie-rekursywnie** (jeden poziom) — "Nested patterns like `<skills-root>/group/<skill>/SKILL.md` are not discovered recursively". (Pi rekursywnie — tu się różnią!)
- Frontmatter: `name`, `description`, `globs`, `alwaysApply`, `hide`, `disable-model-invocation`, dodatkowe klucze zachowane; `name` domyślnie = nazwa katalogu; `description` wymagany dla providerów native/omp-plugins/github/customDirectories.
- Adresowanie: `skill://<name>` (→ SKILL.md) i `skill://<name>/<relative-path>` (→ zasoby w katalogu, z guardami anty-traversal) — zasoby skilla są czytane przez tool `read`.
- Skill z pakietu: provider `omp-plugins` — "skills/ bundled next to extension packages loaded through extensions:, --extension/-e, or installed plugins". Czyli skill obok extension w tym samym pakiecie działa też na omp.
- Native roots omp: `~/.omp/agent/skills/`, `.omp/skills/` (plus providerzy claude/codex/agents/github).
- `/skill:<name>` command + `skills.enableSkillCommands`.

---

## 4. Realne przykłady z npm

### 4.1 `pi-fast-jev-compaction@0.2.0` (rossz / ross-jill-ws)

package.json (kluczowe pola, z registry):

```json
{
  "name": "pi-fast-jev-compaction",
  "type": "module",
  "keywords": ["pi-package", "pi", "compaction", "jev", "typesafe", "..."],
  "peerDependencies": { "@earendil-works/pi-coding-agent": "*" },
  "pi": {
    "extensions": ["extensions/index.ts"],
    "image": "https://github.com/ross-jill-ws/pi-fast-jev-compaction/blob/main/assets/pi-jev-compact.gif?raw=true"
  },
  "publishConfig": { "access": "public" },
  "scripts": { "test": "node --test tests/*.test.ts" }
}
```

Brak `build`, brak `bin`, brak `files` — **surowy TS w tarballu** (19 plików, 3.1 MB — GIF/MP4 w `assets/`). Layout z jego README:

```text
extensions/index.ts     commands, session_before_compact / session_compact hooks, fallback, debug bundles
extensions/adapter.ts   pi messages <-> jev messages, normalization, applying decisions, file lists
extensions/render.ts    the verbatim transcript and summary text
extensions/goal.ts      jev's goal from the user's prompts
extensions/debug.ts     bundle writer and report
extensions/config.ts    environment variables
extensions/jev/         the fast-jev-compaction core, vendored
references/             how-it-works.md, debug-mode.md, benchmarks.md
tests/                  unit tests with a fake jev, no network
```

Cytat README (Install):

```bash
pi install npm:pi-fast-jev-compaction
pi install git:github.com/ross-jill-ws/pi-fast-jev-compaction
pi -e npm:pi-fast-jev-compaction
# local dev: pi -e /path/to/pi-fast-jev-compaction/extensions/index.ts
# lub ~/.pi/agent/settings.json: { "packages": ["/path/to/pi-fast-jev-compaction"] }
```

Wymaga "pi 0.84 or newer"; konfiguracja przez zmienne środowiskowe (`TYPESAFE_API_KEY`, `PI_JEV_COMPACT_*`). Uwaga: `references/*.md` to tu zwykłe docs w repo, NIE skill — skill wymaga `SKILL.md`.

### 4.2 `pi-jev@0.7.0` (theooliveira / TheoOliveira/pi-jev)

package.json (z registry):

```json
{
  "name": "pi-jev",
  "version": "0.7.0",
  "type": "module",
  "main": "extensions/index.ts",
  "pi": { "extensions": ["./extensions/index.ts"] },
  "bin": { "jev-gate": "bin/jev-gate.js", "pi-jev-gate": "bin/jev-gate.js" },
  "keywords": ["pi-package", "pi-extension", "pi-coding-agent", "typesafe", "jev", "system-one", "tool-router"],
  "dependencies": { "tsx": "^4.19.0", "@typesafe-ai/sdk": "^0.6.0" },
  "peerDependencies": {
    "@sinclair/typebox": "*",
    "@earendil-works/pi-ai": "*",
    "@earendil-works/pi-tui": "*",
    "@earendil-works/pi-coding-agent": "*"
  },
  "engines": { "node": ">=20.0.0" },
  "scripts": { "smoke": "pi -ne -e ./extensions/index.ts -p --no-session \"list loaded tools\"" }
}
```

Wnioski z tego przykładu: `bin` tylko dla osobnego CLI (gate), nie dla pi; `@typesafe-ai/sdk` w `dependencies` (pi zainstaluje je przy install pakietu), host-pakiety w `peerDependencies "*"`; `tsx` w deps bo testy/smoke odpalają TS poza pi; smoke-test wzorzec: `pi -ne -e ./extensions/index.ts -p --no-session "..."`.

---

## 5. WNIOSKI dla pakietu `jev-router-pi`

Stan obecny (`adapters/pi/package.json`): `pi.extensions` + `omp.extensions` → `./src/index.ts`, `type: module`, `main: src/client.ts`, `engines.node >=22.19`. To jest **poprawne i wystarczające** do `pi install ./...` / `pi -e` / `omp`. Do publicznego npm-brakuje:

1. **Zostaw surowy `.ts` — NIE buduj JS.** Oba kanoniczne przykłady i docs (`jiti`) to potwierdzają. Skrypt `build: tsc` może zostać tylko jako `typecheck` (`tsc --noEmit`), nie jako wymóg dystrybucji. Uwaga: `engines.node` nie ma znaczenia dla omp (engines fork-a to `bun >=1.3.14`); nie jest wymagany przez pi-install.
2. **Przenieś entry na konwencję lub zostań przy manifestie.** Dwie równorzędne opcje:
   - minimum zmian: obecny manifest `pi.extensions: ["./src/index.ts"]` + `omp.extensions` (omp preferuje `omp.*`, oba klucze już mamy — zostawić),
   - bardziej kanonicznie dla pi: katalog `extensions/index.ts` (wtedy manifest zbędny). Rekomendacja: **zostań przy jawnym manifeście** — jeden entry, dwa hosty, zero przenosin.
3. **Dodaj przed publikacją na npm:**
   - `keywords: ["pi-package", "omp", "jev", ...]` → galeria pi.dev/packages; opcjonalnie `pi.image`.
   - `peerDependencies`: `@earendil-works/pi-coding-agent: "*"` (dla pi) — NIE umieszczaj pakietów hosta w `dependencies` (pi zgłasza ostrzeżenie). Dla omp typy z `@oh-my-pi/pi-coding-agent` trzymaj w `devDependencies` (omp i tak podmienia specifier legacy-pi na host-bundled copy przy ładowaniu).
   - `dependencies`: tylko realne runtime importy poza hostem (u nas na dziś: zero — `client.ts` używa `fetch`).
   - `publishConfig.access` tylko dla scope; `files` opcjonalnie (albo `.npmignore`) — pamiętaj, że w tarballu musi fizycznie być `src/`.
4. **Skills: TAK, ale tylko jeśli dorzucamy warstwę instrukcji.** Sam router (behaviour) = extension. Jeśli chcemy, by model wiedział *jak sterować routerem* (`/route on|off`, zmienne `JEVRROUTER_*`, diagnostyka "route status"), to jest klasyczny przypadek skilla z `SKILL.md` (name + description routingowe, ciało = instrukcje). Dostarczenie w tym samym pakiecie: katalog `skills/jev-router/SKILL.md` obok `extensions/` albo `"pi": { "skills": ["./skills"] }`. Na omp zadziała provider `omp-plugins` (skills/ obok pakietu extension) — ale layout musi być `skills/<name>/SKILL.md` jeden poziom głęboko (omp nie rekurencji). Frontmatter: `name` + `description` obligatoryjne (bez description skill nie załadowany); bez pola `instructions` — instrukcje to ciało.
5. **Rekomendacja końcowa: OBA (extensions + skills), jeden pakiet.**
   - `extensions` (już jest): rejestruje `/route`, widget, status-bar, hooki — kod.
   - `skills/jev-router/SKILL.md`: wiedza o komendach/env/diagnostyce, ładowana progresywnie (~0 koszt kontekstu dopóki niepotrzebna).
   - Dystrybucja: `pi install npm:jev-router-pi` / `pi install git:github.com:<org>/jev-router-core` (git wymaga, by pakiet leżał w root repo albo wskazywał manifestem — u nas podkatalog `adapters/pi`, więc dla git-install publikuj osobne repo/tag lub użyjcie npm; local path `pi install ./adapters/pi` działa bez zmian).
   - Nie dubluj: to co skrypt wykonuje (decyzje effort) zostaje w extension; skill tylko dokumentuje sterowanie dla modelu.

Źródła: registry.npmjs.org (`@earendil-works/pi-coding-agent`, `@mariozechner/pi-coding-agent`, `@oh-my-pi/pi-coding-agent`, `pi-fast-jev-compaction`, `pi-jev`); raw.githubusercontent `earendil-works/pi/main/packages/coding-agent/docs/{extensions,skills,packages,configuration,settings,cli}.md`; `can1357/oh-my-pi/main/docs/{extension-loading,skills}.md`.

RESEARCH DONE
