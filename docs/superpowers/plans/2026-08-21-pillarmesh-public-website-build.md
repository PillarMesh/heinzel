# PillarMesh Public Website Build Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a complete, developer-focused PillarMesh marketing and documentation site that runs locally as a verified static Astro/Starlight application.

**Architecture:** Create a separate local `pillarmesh-site` Git repository containing custom Astro marketing pages and Starlight documentation below `/docs`. Share one Evidence Ledger design system, render only static assets, and use a plain HTML Netlify form whose live processing remains disabled until the launch plan.

**Tech Stack:** Active Node.js LTS pinned at execution time, npm with `package-lock.json`, Astro, Starlight, TypeScript strict mode, plain CSS, Pagefind, Playwright, axe-core

**Spec:** `docs/superpowers/specs/2026-08-21-pillarmesh-public-website-design.md`

## Global Constraints

- Set `WORKSPACE` to the directory that holds your checkouts and `SITE_REPO="$WORKSPACE/pillarmesh-site"` before the first command; every path below is written against those variables so the plan is machine-independent. Work in a new local repository at `$SITE_REPO`; stop if that path already exists instead of overwriting it.
- The repository contains public-safe content only and never imports directories or files from the private PillarMesh repository.
- The category line is exactly `The managed data engineering platform`.
- The primary promise is exactly `Data engineering that proves its work.`
- Every capability page exposes `In development. Seeking design partners.` without relying on hover or JavaScript.
- The primary audience knows basic Python, SQL, Spark-style processing, schemas, pipelines, tests, retries, and backfills.
- Spark familiarity is an audience assumption, not a supported-engine claim. PostgreSQL and ClickHouse are the planned initial managed warehouse engines.
- Planned behavior uses prospective language. No availability, scale, performance, connector-count, certification, customer, or production claim may be invented.
- The site ships static output only: no adapter, SSR, Functions, database, CMS, authentication, personalization, advertising pixel, or hosted analytics.
- Documentation lives below `/docs` and is curated independently; no internal specification is published wholesale.
- The design-partner form collects only name, work email, company, and a short data-engineering challenge. It accepts no file uploads.
- All visible controls and content target WCAG 2.2 AA, work at 320 CSS pixels and 200 percent zoom, and respect `prefers-reduced-motion`.
- Use semantic HTML and CSS before JavaScript. Essential navigation, documentation, and form submission work without client-side JavaScript.
- Use npm packages for locally served font assets; production pages make no third-party font request.
- Every behavioral test is observed failing for its intended assertion before implementation makes it pass.
- This plan stops before GitHub repository creation, push, pull request, Netlify account mutation, form notification, DNS mutation, publication, or live submission.

## Program decomposition

This is Plan 1 of 2. It produces a locally complete and reviewable site. Continue with
`docs/superpowers/plans/2026-08-21-pillarmesh-public-website-launch.md` only after all completion
gates below pass and the user approves the local result.

## File map

### Repository and configuration

- `.gitignore`: generated files, local environment, Playwright output, and Netlify local state.
- `AGENTS.md`: repository-specific public-content, Git, verification, and external-effect rules.
- `.nvmrc`: active Node.js LTS selected and pinned during execution.
- `package.json` and `package-lock.json`: locked build and verification toolchain.
- `astro.config.mjs`: static Astro plus Starlight configuration and `/docs` sidebar.
- `src/content.config.ts`: Starlight content collection.
- `tsconfig.json`: Astro strict TypeScript configuration.
- `playwright.config.ts`: local and externally hosted browser-test configuration.
- `netlify.toml`: static build, publish path, redirects, and security headers.

### Shared site implementation

- `src/config/site.ts`: typed site identity, stage, navigation, capability maturity, and public URLs.
- `src/layouts/SiteLayout.astro`: common metadata, skip link, stage notice, header, main, and footer.
- `src/styles/tokens.css`: Evidence Ledger color, type, spacing, and motion tokens.
- `src/styles/global.css`: reset, typography, layout, focus, responsive, and reduced-motion rules.
- `src/styles/docs.css`: Starlight token mapping and documentation refinements.
- `src/components/BrandMark.astro`: accessible PillarMesh mark and wordmark.
- `src/components/SiteHeader.astro`: desktop and disclosure-based mobile navigation.
- `src/components/SiteFooter.astro`: policy and contact navigation.
- `src/components/StageNotice.astro`: persistent in-development disclosure.
- `src/components/MaturityBadge.astro`: explicit `planned`, `implemented`, and `verified` labels.
- `src/components/Hero.astro`: approved category, promise, stage, and calls to action.
- `src/components/EvidenceArtifact.astro`: conceptual contract-to-evidence visual with text equivalent.
- `src/components/CapabilityGrid.astro`: typed planned capability cards.
- `src/components/ProcessFlow.astro`: connect, declare, approve, operate, and prove lifecycle.
- `src/components/DesignPartnerForm.astro`: static Netlify-compatible form.

### Routes and documentation

- `src/pages/index.astro`: home.
- `src/pages/platform.astro`: managed platform boundary.
- `src/pages/how-it-works.astro`: developer-facing lifecycle.
- `src/pages/architecture.astro`: public compiler-centered architecture.
- `src/pages/trust.astro`: trust, maturity, and shared-responsibility boundaries.
- `src/pages/design-partners/index.astro`: programme and application form.
- `src/pages/design-partners/thanks.astro`: accepted-submission acknowledgement.
- `src/pages/privacy.astro`: public-site form privacy and retention notice.
- `src/pages/accessibility.astro`: accessibility commitment and contact path.
- `src/pages/security.astro`: responsible disclosure and current boundaries.
- `src/pages/product-stage.astro`: one canonical stage and claim notice.
- `src/pages/404.astro`: real accessible not-found page.
- `src/content/docs/docs/index.mdx`: `/docs` overview.
- `src/content/docs/docs/core-concepts.md`: contracts, plans, runtime, reconciliation, and evidence.
- `src/content/docs/docs/operating-model.md`: supervised automation and human authority.
- `src/content/docs/docs/architecture.md`: public system boundaries.
- `src/content/docs/docs/glossary.md`: stable product vocabulary.
- `src/content/docs/docs/faq.md`: direct technical questions and limits.

### Static assets and verification

- `public/favicon.svg`: Evidence Ledger brand mark.
- `public/robots.txt`: public crawl policy and sitemap location.
- `public/social/og-default.png`: generated 1200 by 630 social card.
- `scripts/render-social-card.mjs`: deterministic Playwright rendering for the social card.
- `scripts/check-public-content.mjs`: internal-path, secret-pattern, placeholder, and prohibited-claim gate.
- `scripts/check-built-site.mjs`: route, metadata, canonical, sitemap, form, and asset gate.
- `tests/content/check-public-content.test.mjs`: positive and negative claim-guard fixtures.
- `tests/e2e/smoke.spec.ts`: navigation and route smoke coverage.
- `tests/e2e/accessibility.spec.ts`: axe, keyboard, zoom-width, and reduced-motion coverage.
- `tests/e2e/form.spec.ts`: form structure, validation, success route, and no-file boundary.
- `tests/e2e/metadata.spec.ts`: titles, descriptions, canonicals, structured data, sitemap, robots, and `404`.
- `README.md`: local commands, public-content boundary, and explicit launch-plan handoff.

---

### Task 1: Bootstrap the isolated website repository and locked test harness

**Files:**
- Create: `$SITE_REPO/.gitignore`
- Create: `$SITE_REPO/AGENTS.md`
- Create: `$SITE_REPO/.nvmrc`
- Create: `$SITE_REPO/package.json`
- Create: `$SITE_REPO/package-lock.json`
- Create: `$SITE_REPO/astro.config.mjs`
- Create: `$SITE_REPO/src/content.config.ts`
- Create: `$SITE_REPO/tsconfig.json`
- Create: `$SITE_REPO/playwright.config.ts`
- Create: `$SITE_REPO/src/pages/index.astro`
- Create: `$SITE_REPO/tests/content/toolchain.test.mjs`
- Create: `$SITE_REPO/tests/e2e/smoke.spec.ts`

**Interfaces:**
- Consumes: the approved design specification only; no source file from the private repository.
- Produces: npm scripts `check`, `build`, `format:check`, `test:content`, `test:e2e`, and `test`; a static `/` route; Playwright configuration that can test local or externally hosted builds.

- [ ] **Step 1: Prove the target path is unused and select the active Node.js LTS**

Run from `$WORKSPACE`:

```bash
test ! -e "$SITE_REPO"
node --version
npm --version
```

Expected: the path check passes. Confirm the installed Node release is an active LTS from the
official Node.js release page. If it is not, install the active LTS before continuing. Record the
major version, such as `24`, in `.nvmrc`; do not use `node` or `latest` as an unpinned value.

- [ ] **Step 2: Scaffold Starlight without initializing or installing**

```bash
cd "$WORKSPACE"
npm create astro@latest pillarmesh-site -- --template starlight --no-install --no-git --yes
cd "$SITE_REPO"
```

Expected: Astro creates a Starlight project in the exact target path.

- [ ] **Step 3: Add repository rules and a public-safe bootstrap README**

Replace the generated `.gitignore`, create `AGENTS.md`, and replace `README.md` before the first
commit. `README.md` contains only:

```markdown
# PillarMesh Website

Public website and curated documentation for PillarMesh. The product is in development and is
seeking design partners.
```

`AGENTS.md` contains these enforceable rules:

```markdown
# PillarMesh Website Instructions

- Treat every tracked file as public. Never import or copy private PillarMesh directories.
- Use prospective language for planned product behavior and show the product-stage notice.
- Do not add secrets, customer data, analytics pixels, external fonts, SSR, Functions, or a database.
- Work only on `feat/*`, `fix/*`, or `chore/*` branches and use Conventional Commits.
- Add a focused failing test before behavior and prove at least one boundary case.
- Run `npm ci`, `npm run check`, `npm run format:check`, `npm run test`, and `npm run build` before commit.
- Do not create a remote, push, open or merge a PR, configure Netlify, submit a live form, or mutate DNS without explicit approval.
```

The `.gitignore` contains:

```gitignore
node_modules/
dist/
.astro/
.netlify/
playwright-report/
test-results/
.DS_Store
.env
.env.*
!.env.example
```

- [ ] **Step 4: Establish Git history without committing on `main`**

```bash
git init
git switch --orphan chore/bootstrap
git add .gitignore AGENTS.md README.md
git commit -m "chore: bootstrap website repository"
git branch main
git switch -c feat/public-website
```

Expected: `main` and `feat/public-website` point to the bootstrap commit, and the active branch is
`feat/public-website`. The Astro scaffold remains untracked on the feature branch. There is no
remote. Delete the generated example pages under `src/content/docs/` with `apply_patch` so they do
not collide with the custom `/` route or publish template copy. Delete any generated sample image
that is referenced only by those example pages.

- [ ] **Step 5: Install the locked build and test dependencies**

```bash
npm install astro @astrojs/starlight @astrojs/check @astrojs/sitemap typescript \
  @fontsource-variable/atkinson-hyperlegible-next @fontsource/ibm-plex-mono
npm install --save-dev prettier prettier-plugin-astro @playwright/test @axe-core/playwright
npx playwright install chromium
```

Expected: `package-lock.json` is updated and Chromium installs successfully. Do not install an
Astro Netlify adapter because the build is fully static.

- [ ] **Step 6: Add the failing PillarMesh smoke test**

Create `playwright.config.ts`:

```ts
import { defineConfig, devices } from '@playwright/test';

const externalBaseUrl = process.env.PLAYWRIGHT_BASE_URL;

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: externalBaseUrl ?? 'http://127.0.0.1:4321',
    trace: 'retain-on-failure',
  },
  webServer: externalBaseUrl
    ? undefined
    : {
        command: 'npm run dev -- --host 127.0.0.1',
        url: 'http://127.0.0.1:4321',
        reuseExistingServer: !process.env.CI,
      },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
```

Create `tests/content/toolchain.test.mjs` so the static boundary exists from the first feature
commit:

```js
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

test('Astro is explicitly static and has no deployment adapter', async () => {
  const config = await readFile(new URL('../../astro.config.mjs', import.meta.url), 'utf8');

  assert.match(config, /output:\s*['"]static['"]/);
  assert.doesNotMatch(config, /adapter\s*:/);
});
```

Create `tests/e2e/smoke.spec.ts`:

```ts
import { expect, test } from '@playwright/test';

test('home identifies PillarMesh and its honest product stage', async ({ page }) => {
  await page.goto('/');

  await expect(page).toHaveTitle(/PillarMesh/);
  await expect(
    page.getByRole('heading', { level: 1, name: 'Data engineering that proves its work.' }),
  ).toBeVisible();
  await expect(page.getByText('In development. Seeking design partners.')).toBeVisible();
});
```

- [ ] **Step 7: Run the smoke test and observe the content assertion fail**

Run: `npx playwright test tests/e2e/smoke.spec.ts`

Expected: FAIL because the generated starter does not contain the approved heading and stage.

- [ ] **Step 8: Add the minimal static home route and package scripts**

Set `output: 'static'` explicitly in `astro.config.mjs`, retain the Starlight integration, and
create `src/pages/index.astro`:

```astro
---
const title = 'PillarMesh | Managed data engineering platform';
---

<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width" />
    <title>{title}</title>
  </head>
  <body>
    <main>
      <p>The managed data engineering platform</p>
      <h1>Data engineering that proves its work.</h1>
      <p>In development. Seeking design partners.</p>
    </main>
  </body>
</html>
```

Set these scripts in `package.json`:

```json
{
  "scripts": {
    "dev": "astro dev",
    "check": "astro check",
    "build": "astro build",
    "preview": "astro preview",
    "format": "prettier --write .",
    "format:check": "prettier --check .",
    "test:content": "node --test tests/content/*.test.mjs",
    "test:e2e": "playwright test",
    "test": "npm run test:content && npm run test:e2e"
  }
}
```

- [ ] **Step 9: Verify the minimal project and commit**

```bash
npm run check
npm run build
npm run test
git add .gitignore AGENTS.md .nvmrc package.json package-lock.json astro.config.mjs \
  src/content.config.ts tsconfig.json playwright.config.ts src/pages/index.astro tests/content \
  tests/e2e/smoke.spec.ts
git diff --cached --check
git commit -m "chore: establish website foundation"
```

Expected: all three commands pass and the commit contains only the local foundation.

---

### Task 2: Build the Evidence Ledger shell and accessible navigation

**Files:**
- Create: `src/config/site.ts`
- Create: `src/layouts/SiteLayout.astro`
- Create: `src/styles/tokens.css`
- Create: `src/styles/global.css`
- Create: `src/styles/docs.css`
- Create: `src/components/BrandMark.astro`
- Create: `src/components/SiteHeader.astro`
- Create: `src/components/SiteFooter.astro`
- Create: `src/components/StageNotice.astro`
- Modify: `src/pages/index.astro`
- Modify: `tests/e2e/smoke.spec.ts`

**Interfaces:**
- Consumes: Astro static rendering and font packages from Task 1.
- Produces: `SITE`, `PRIMARY_NAVIGATION`, `PRODUCT_STAGE`, `SiteLayout`, and global Evidence Ledger tokens consumed by every later route.

- [ ] **Step 1: Extend the failing smoke test for shell behavior**

Add:

```ts
test('global shell exposes skip navigation and every primary destination', async ({ page }) => {
  await page.goto('/');

  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: 'Skip to content' })).toBeFocused();
  await expect(page.getByRole('navigation', { name: 'Primary' })).toContainText('Platform');
  await expect(page.getByRole('navigation', { name: 'Primary' })).toContainText('Docs');
  await expect(page.getByRole('link', { name: 'Become a design partner' })).toBeVisible();
});
```

- [ ] **Step 2: Run the new test and confirm the missing-shell failure**

Run: `npx playwright test tests/e2e/smoke.spec.ts -g "global shell"`

Expected: FAIL because no skip link or named primary navigation exists.

- [ ] **Step 3: Define the typed public site contract**

Create `src/config/site.ts`:

```ts
export const SITE = {
  name: 'PillarMesh',
  url: 'https://pillarmesh.com',
  email: 'contact@pillarmesh.com',
  emailVerified: false,
  description:
    'A managed data engineering platform being built for small, hands-on data teams.',
} as const;

export const PRODUCT_STAGE = 'In development. Seeking design partners.' as const;

export const PRIMARY_NAVIGATION = [
  { href: '/platform/', label: 'Platform' },
  { href: '/how-it-works/', label: 'How it works' },
  { href: '/architecture/', label: 'Architecture' },
  { href: '/trust/', label: 'Trust' },
  { href: '/docs/', label: 'Docs' },
] as const;
```

- [ ] **Step 4: Implement tokens and the semantic shell**

Define the approved colors in `src/styles/tokens.css` and import the local font packages in
`src/styles/global.css`:

```css
@import '@fontsource-variable/atkinson-hyperlegible-next';
@import '@fontsource/ibm-plex-mono/400.css';
@import '@fontsource/ibm-plex-mono/600.css';

:root {
  --color-page: #07131b;
  --color-deep: #050a0e;
  --color-surface: #0b1e20;
  --color-accent: #79e8bf;
  --color-stage: #f0b15f;
  --color-text: #eaf7f5;
  --color-muted: #a3b7b9;
  --font-sans: 'Atkinson Hyperlegible Next Variable', system-ui, sans-serif;
  --font-mono: 'IBM Plex Mono', ui-monospace, monospace;
}
```

`SiteLayout.astro` must render this order:

```astro
<a class="skip-link" href="#main-content">Skip to content</a>
<StageNotice />
<SiteHeader />
<main id="main-content" tabindex="-1"><slot /></main>
<SiteFooter />
```

Use `<details>` and `<summary>` for the mobile menu so navigation remains operable without
JavaScript. `BrandMark.astro` uses inline SVG with `aria-hidden="true"` beside visible text.
`SiteFooter.astro` renders a `mailto:` contact link only when `SITE.emailVerified` is true;
otherwise it links to `/design-partners/`. Add an assertion to the shell test that no `mailto:`
link exists while the flag is false.

- [ ] **Step 5: Replace the temporary home document with `SiteLayout`**

```astro
---
import SiteLayout from '../layouts/SiteLayout.astro';
---

<SiteLayout
  title="PillarMesh | Managed data engineering platform"
  description="A managed data engineering platform being built for small, hands-on data teams."
>
  <p class="eyebrow">The managed data engineering platform</p>
  <h1>Data engineering that proves its work.</h1>
</SiteLayout>
```

- [ ] **Step 6: Run focused checks and commit**

```bash
npm run check
npx playwright test tests/e2e/smoke.spec.ts
git add src/config src/layouts src/styles src/components src/pages/index.astro tests/e2e/smoke.spec.ts
git diff --cached --check
git commit -m "feat: add accessible Evidence Ledger shell"
```

Expected: the skip-link assertion fails before Step 4 and passes after it; no horizontal overflow
appears at 320 CSS pixels.

---

### Task 3: Implement the developer-focused homepage

**Files:**
- Create: `src/components/Hero.astro`
- Create: `src/components/EvidenceArtifact.astro`
- Create: `src/components/CapabilityGrid.astro`
- Create: `src/components/ProcessFlow.astro`
- Create: `src/components/MaturityBadge.astro`
- Create: `src/config/capabilities.ts`
- Modify: `src/pages/index.astro`
- Modify: `tests/e2e/smoke.spec.ts`

**Interfaces:**
- Consumes: `SiteLayout`, `PRODUCT_STAGE`, and Evidence Ledger tokens.
- Produces: `Maturity = 'planned' | 'implemented' | 'verified'`, `Capability`, `CAPABILITIES`, and reusable visual components for later pages.

- [ ] **Step 1: Add failing homepage content and boundary tests**

```ts
test('home addresses hands-on data engineers without claiming Spark support', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByText('Python, SQL, schemas, retries, and backfills')).toBeVisible();
  await expect(page.getByText('PostgreSQL and ClickHouse')).toBeVisible();
  await expect(page.getByText(/Spark is supported/i)).toHaveCount(0);
  await expect(page.getByText('Conceptual execution evidence')).toBeVisible();
});
```

- [ ] **Step 2: Run the focused test and confirm missing content**

Run: `npx playwright test tests/e2e/smoke.spec.ts -g "hands-on data engineers"`

Expected: FAIL on the first expected audience line.

- [ ] **Step 3: Define capabilities with mandatory maturity**

Create `src/config/capabilities.ts`:

```ts
export type Maturity = 'planned' | 'implemented' | 'verified';

export interface Capability {
  readonly title: string;
  readonly description: string;
  readonly maturity: Maturity;
}

export const CAPABILITIES: readonly Capability[] = [
  { title: 'Source acquisition', description: 'Operate supported source connections.', maturity: 'planned' },
  { title: 'Managed warehouse', description: 'Run a dedicated PostgreSQL or ClickHouse data plane.', maturity: 'planned' },
  { title: 'Transformations', description: 'Compile inspectable, versioned transformation artifacts.', maturity: 'planned' },
  { title: 'Catalog and BI', description: 'Operate governed discovery, dashboards, and reports.', maturity: 'planned' },
  { title: 'Recovery', description: 'Coordinate replay, backup, restore, and bounded remediation.', maturity: 'planned' },
  { title: 'Evidence', description: 'Reconcile source-to-consumer outcomes with attributable facts.', maturity: 'planned' },
] as const;
```

`MaturityBadge.astro` accepts only `maturity: Maturity` and renders visible text.

- [ ] **Step 4: Build the approved homepage sequence**

Implement these sections in `src/pages/index.astro`:

```astro
<Hero
  eyebrow="The managed data engineering platform"
  title="Data engineering that proves its work."
  description="PillarMesh is being built for small data teams that need to operate sources, a dedicated warehouse, transformations, catalog, dashboards, and evidence as one governed system."
/>
<EvidenceArtifact label="Conceptual execution evidence" />
<section aria-labelledby="for-engineers">
  <h2 id="for-engineers">Built for engineers who already know the moving parts</h2>
  <p>Python, SQL, schemas, retries, and backfills are familiar. Operating the whole stack should not consume the team.</p>
</section>
<CapabilityGrid capabilities={CAPABILITIES} />
<ProcessFlow steps={['Connect', 'Declare', 'Approve', 'Operate', 'Prove']} />
```

The evidence artifact has a visible `Conceptual` label and a hidden text equivalent explaining
contract, admitted plan, run, and evidence. It must not resemble an implemented application
screenshot.

- [ ] **Step 5: Verify desktop, mobile, and reduced-motion rendering**

```bash
npm run check
npx playwright test tests/e2e/smoke.spec.ts
```

Expected: all homepage tests pass at the default project. Temporarily add a mobile viewport of
320 by 800 and confirm the test fails if `overflow-x` is introduced, then restore the working CSS.

- [ ] **Step 6: Commit the homepage**

```bash
git add src/components src/config/capabilities.ts src/pages/index.astro tests/e2e/smoke.spec.ts
git diff --cached --check
git commit -m "feat: add developer-focused homepage"
```

---

### Task 4: Add platform, process, architecture, and trust routes

**Files:**
- Create: `src/pages/platform.astro`
- Create: `src/pages/how-it-works.astro`
- Create: `src/pages/architecture.astro`
- Create: `src/pages/trust.astro`
- Create: `src/pages/product-stage.astro`
- Create: `tests/e2e/product-pages.spec.ts`

**Interfaces:**
- Consumes: `SiteLayout`, `CAPABILITIES`, `MaturityBadge`, and `ProcessFlow`.
- Produces: five stable public routes and one canonical stage notice linked from every capability page.

- [ ] **Step 1: Add the failing route and claim tests**

```ts
import { expect, test } from '@playwright/test';

const routes = [
  ['/platform/', 'One accountable managed data platform'],
  ['/how-it-works/', 'From declared outcome to attributable evidence'],
  ['/architecture/', 'Contracts are durable. Plans are replaceable.'],
  ['/trust/', 'Trust boundaries before trust claims'],
  ['/product-stage/', 'PillarMesh is in development'],
] as const;

for (const [route, heading] of routes) {
  test(`${route} renders its canonical heading and stage`, async ({ page }) => {
    await page.goto(route);
    await expect(page.getByRole('heading', { level: 1, name: heading })).toBeVisible();
    await expect(page.getByText('In development. Seeking design partners.')).toBeVisible();
  });
}

// The forbidden thing is the affirmative claim, not the words. A bare substring guard
// would also reject "PillarMesh is not a general-purpose workflow scheduler", which is
// exactly the boundary the design asks the page to state, so each pattern carries the
// affirming verb that only an affirmative sentence supplies.
test('platform states its scope instead of claiming arbitrary destinations or a general scheduler', async ({ page }) => {
  await page.goto('/platform/');
  await expect(page.getByText(/(works with|supports|runs on) any warehouse/i)).toHaveCount(0);
  await expect(page.getByText(/\bis a general-purpose workflow scheduler/i)).toHaveCount(0);
  await expect(page.getByText(/PostgreSQL or ClickHouse/i)).toBeVisible();
});
```

- [ ] **Step 2: Run the new suite and observe `404` failures**

Run: `npx playwright test tests/e2e/product-pages.spec.ts`

Expected: FAIL because the five routes do not exist.

- [ ] **Step 3: Implement each route from the approved section contract**

Each page uses `SiteLayout`, one `h1`, a short developer-facing introduction, the global stage,
and explicit scope. The platform page must include:

```astro
<h1>One accountable managed data platform</h1>
<p>
  PillarMesh is being built to operate supported sources through a dedicated PostgreSQL or
  ClickHouse warehouse, transformations, catalog, dashboards, recovery, and evidence.
</p>
<CapabilityGrid capabilities={CAPABILITIES} />
```

The how-it-works page uses `ProcessFlow` and explains `No Valid Plan`. The architecture page
separates Integration Contract, physical plan, execution graph, runtime, reconciliation, and
evidence. The trust page states that no certifications or production SLA are claimed. The stage
page defines `planned`, `implemented`, and `verified` and states that existing fixtures and M0
records do not prove current product availability.

- [ ] **Step 4: Verify routes and manually invalidate one denial**

Run: `npx playwright test tests/e2e/product-pages.spec.ts`

Expected: PASS. Then mutate the page in both directions, because a guard that rejects the
denial as well as the claim is worse than no guard:

1. Temporarily add `Works with any warehouse` to `platform.astro`. Expected: the first negative
   assertion fails. Remove it.
2. Temporarily add `PillarMesh is not a general-purpose workflow scheduler and does not run on
   any warehouse.` to `platform.astro`. Expected: PASS, because that sentence states a boundary
   rather than claiming one. If it fails, the guard is matching words instead of claims and must
   be narrowed before continuing.

Rerun with the page restored and confirm PASS.

- [ ] **Step 5: Commit the product routes**

```bash
git add src/pages/platform.astro src/pages/how-it-works.astro src/pages/architecture.astro \
  src/pages/trust.astro src/pages/product-stage.astro tests/e2e/product-pages.spec.ts
git diff --cached --check
git commit -m "feat: explain platform and trust boundaries"
```

---

### Task 5: Add curated Starlight documentation below `/docs`

**Files:**
- Modify: `astro.config.mjs`
- Modify: `src/content.config.ts`
- Modify: `src/styles/docs.css`
- Create: `src/content/docs/docs/index.mdx`
- Create: `src/content/docs/docs/core-concepts.md`
- Create: `src/content/docs/docs/operating-model.md`
- Create: `src/content/docs/docs/architecture.md`
- Create: `src/content/docs/docs/glossary.md`
- Create: `src/content/docs/docs/faq.md`
- Create: `tests/e2e/docs.spec.ts`

**Interfaces:**
- Consumes: Starlight, `docsLoader()`, shared fonts and Evidence Ledger colors.
- Produces: static Pagefind-backed documentation routes `/docs/`, `/docs/core-concepts/`, `/docs/operating-model/`, `/docs/architecture/`, `/docs/glossary/`, and `/docs/faq/`.

- [ ] **Step 1: Add failing documentation route and boundary tests**

```ts
import { expect, test } from '@playwright/test';

test('documentation starts at /docs and names the product stage', async ({ page }) => {
  await page.goto('/docs/');
  await expect(page.getByRole('heading', { level: 1, name: 'PillarMesh documentation' })).toBeVisible();
  await expect(page.getByText('In development. Seeking design partners.')).toBeVisible();
  await expect(page.getByRole('link', { name: 'Core concepts' })).toBeVisible();
});

test('documentation offers no fake quickstart', async ({ page }) => {
  await page.goto('/docs/');
  await expect(page.getByRole('link', { name: /install|quickstart|api reference/i })).toHaveCount(0);
});
```

- [ ] **Step 2: Run the tests and confirm `/docs/` is absent**

Run: `npx playwright test tests/e2e/docs.spec.ts`

Expected: FAIL with a not-found page.

- [ ] **Step 3: Configure nested Starlight content and sidebar**

Use the current Starlight loaders in `src/content.config.ts`:

```ts
import { defineCollection } from 'astro:content';
import { docsLoader } from '@astrojs/starlight/loaders';
import { docsSchema } from '@astrojs/starlight/schema';

export const collections = {
  docs: defineCollection({ loader: docsLoader(), schema: docsSchema() }),
};
```

Configure `astro.config.mjs` with `output: 'static'`, `site: 'https://pillarmesh.com'`, the
`@astrojs/sitemap` integration, Pagefind, custom CSS, a custom `404`, and this sidebar:

```js
sidebar: [
  { slug: 'docs' },
  {
    label: 'Concepts',
    items: [
      { slug: 'docs/core-concepts' },
      { slug: 'docs/operating-model' },
      { slug: 'docs/architecture' },
    ],
  },
  {
    label: 'Reference',
    items: [{ slug: 'docs/glossary' }, { slug: 'docs/faq' }],
  },
],
```

- [ ] **Step 4: Write the six public documents**

Every file starts with unique `title`, `description`, and this visible frontmatter banner:

```yaml
banner:
  content: In development. Seeking design partners.
```

The overview states there is no supported installation or public API yet. Core concepts defines
Integration Contract, IIR, physical plan, execution graph, reconciliation, and evidence. The
operating model distinguishes automatic bounded work from human authority. Architecture covers
public planes and `No Valid Plan`. Glossary includes only terms used publicly. FAQ answers engine,
Spark, destination, scheduler, AI-authority, availability, and design-partner questions directly.

- [ ] **Step 5: Apply shared tokens and verify static search output**

Map Starlight CSS custom properties to the Evidence Ledger palette in `src/styles/docs.css`.
Then run:

```bash
npm run build
test -d dist/pagefind
npx playwright test tests/e2e/docs.spec.ts
```

Expected: Pagefind assets exist and all documentation tests pass. Temporarily rename the nested
`docs` directory, confirm `/docs/` fails, restore it, and rerun.

- [ ] **Step 6: Commit curated documentation**

```bash
git add astro.config.mjs src/content.config.ts src/styles/docs.css src/content/docs tests/e2e/docs.spec.ts
git diff --cached --check
git commit -m "docs: add curated public documentation"
```

---

### Task 6: Add the privacy-bounded design-partner form and policy pages

**Files:**
- Create: `src/components/DesignPartnerForm.astro`
- Create: `src/pages/design-partners/index.astro`
- Create: `src/pages/design-partners/thanks.astro`
- Create: `src/pages/privacy.astro`
- Create: `src/pages/accessibility.astro`
- Create: `src/pages/security.astro`
- Create: `tests/e2e/form.spec.ts`

**Interfaces:**
- Consumes: `SiteLayout`, Netlify static HTML form detection, and the verified mailbox address only as display text.
- Produces: form name `design-partner-enquiry`, exact fields `name`, `email`, `company`, `challenge`, honeypot `website`, and success route `/design-partners/thanks/`.

- [ ] **Step 1: Add failing form structure and privacy tests**

```ts
import { expect, test } from '@playwright/test';

test('design partner form collects only the approved fields', async ({ page }) => {
  await page.goto('/design-partners/');
  const form = page.locator('form[name="design-partner-enquiry"]');

  await expect(form.getByLabel('Name')).toBeVisible();
  await expect(form.getByLabel('Work email')).toBeVisible();
  await expect(form.getByLabel('Company')).toBeVisible();
  await expect(form.getByLabel('Current data engineering challenge')).toBeVisible();
  await expect(form.locator('input[type="file"]')).toHaveCount(0);
  await expect(form).toHaveAttribute('action', '/design-partners/thanks/');
  await expect(form.getByRole('link', { name: 'privacy notice' })).toBeVisible();

  const namedControls = await form.locator('[name]').evaluateAll((elements) =>
    elements.map((element) => element.getAttribute('name')).sort(),
  );
  expect(namedControls).toEqual([
    'challenge',
    'company',
    'email',
    'form-name',
    'name',
    'website',
  ]);
});

test('browser validation blocks an empty enquiry', async ({ page }) => {
  await page.goto('/design-partners/');
  await page.getByRole('button', { name: 'Submit design partner enquiry' }).click();
  await expect(page).toHaveURL(/\/design-partners\/$/);
  await expect(page.getByLabel('Name')).toBeFocused();
});
```

- [ ] **Step 2: Run the tests and observe the missing route failure**

Run: `npx playwright test tests/e2e/form.spec.ts`

Expected: FAIL because `/design-partners/` does not exist.

- [ ] **Step 3: Implement the static form without client-side submission code**

Create `DesignPartnerForm.astro` with this form contract:

```astro
<form
  name="design-partner-enquiry"
  method="POST"
  action="/design-partners/thanks/"
  data-netlify="true"
  netlify-honeypot="website"
>
  <input type="hidden" name="form-name" value="design-partner-enquiry" />
  <p class="honeypot" aria-hidden="true">
    <label>Leave this field empty <input name="website" tabindex="-1" autocomplete="off" /></label>
  </p>
  <label>Name <input name="name" autocomplete="name" required /></label>
  <label>Work email <input type="email" name="email" autocomplete="email" required /></label>
  <label>Company <input name="company" autocomplete="organization" required /></label>
  <label>
    Current data engineering challenge
    <textarea name="challenge" minlength="20" maxlength="2000" required></textarea>
  </label>
  <p>By submitting, you acknowledge the <a href="/privacy/">privacy notice</a>.</p>
  <button type="submit">Submit design partner enquiry</button>
</form>
```

CSS hides the honeypot visually without `display: none`; it remains outside keyboard order.

- [ ] **Step 4: Add honest programme, success, privacy, accessibility, and security copy**

The application page states that submission is an enquiry, not acceptance, product access, or a
commercial relationship. The success page uses conditional wording because visiting that route
directly is not proof that Netlify accepted a submission, and it does not promise a response time.
Privacy lists only the four business fields, Netlify processing, 90-day inactive-enquiry deletion,
access/deletion contact, and no CRM or model-provider transfer. Accessibility names WCAG 2.2 AA
as the target. Security links to the verified contact mailbox only after mailbox verification in
Plan 2; until then it instructs users to use the design-partner form and never submit secrets.

- [ ] **Step 5: Verify the generated static form contract**

```bash
npm run build
rg -n 'data-netlify="true"|name="design-partner-enquiry"|netlify-honeypot="website"' \
  dist/design-partners/index.html
npx playwright test tests/e2e/form.spec.ts
```

Expected: the static HTML includes the form contract and all tests pass. Temporarily remove
`required` from `name`, confirm the empty-form test fails by navigating to the success route,
restore it, and rerun.

- [ ] **Step 6: Commit the form and policies**

```bash
git add src/components/DesignPartnerForm.astro src/pages/design-partners src/pages/privacy.astro \
  src/pages/accessibility.astro src/pages/security.astro tests/e2e/form.spec.ts
git diff --cached --check
git commit -m "feat: add privacy-bounded design partner enquiry"
```

---

### Task 7: Add truthful metadata, social assets, `404`, and static security headers

**Files:**
- Modify: `src/layouts/SiteLayout.astro`
- Modify: `astro.config.mjs`
- Create: `src/pages/404.astro`
- Create: `public/favicon.svg`
- Create: `public/robots.txt`
- Create: `public/social/og-default.png`
- Create: `scripts/render-social-card.mjs`
- Create: `netlify.toml`
- Create: `tests/e2e/metadata.spec.ts`

**Interfaces:**
- Consumes: `SITE`, Evidence Ledger tokens, Playwright Chromium, and static `dist/` output.
- Produces: page-specific canonical metadata, Organization/WebSite/BreadcrumbList JSON-LD only, a real `404`, one generated social card, and Netlify static response policy.

- [ ] **Step 1: Add failing metadata and `404` tests**

```ts
import { expect, test } from '@playwright/test';

test('home emits truthful canonical metadata', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('link[rel="canonical"]')).toHaveAttribute('href', 'https://pillarmesh.com/');
  await expect(page.locator('meta[property="og:image"]')).toHaveAttribute(
    'content',
    'https://pillarmesh.com/social/og-default.png',
  );
  await expect(page.locator('script[type="application/ld+json"]')).not.toContainText('offers');
  await expect(page.locator('script[type="application/ld+json"]')).not.toContainText('aggregateRating');
});

test('unknown route returns the accessible not-found page', async ({ page }) => {
  const response = await page.goto('/this-route-does-not-exist/');
  expect(response?.status()).toBe(404);
  await expect(page.getByRole('heading', { level: 1, name: 'Page not found' })).toBeVisible();
});
```

- [ ] **Step 2: Run the tests and confirm missing metadata or `404`**

Run: `npx playwright test tests/e2e/metadata.spec.ts`

Expected: FAIL on canonical, social image, or custom `404` assertions.

- [ ] **Step 3: Implement canonical metadata and restricted structured data**

`SiteLayout.astro` accepts `title`, `description`, and optional `pathname`. Build canonical URLs
with `new URL(pathname ?? Astro.url.pathname, SITE.url)`. Emit only `Organization` and `WebSite`
on the home page and `BreadcrumbList` on nested marketing pages. Do not emit `Product`,
`SoftwareApplication`, `Offer`, `Review`, or `AggregateRating`.

- [ ] **Step 4: Generate the committed 1200 by 630 social card**

Create `scripts/render-social-card.mjs` using Playwright:

```js
import { chromium } from '@playwright/test';

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1 });
await page.setContent(`<!doctype html><style>
  body{margin:0;width:1200px;height:630px;display:grid;place-items:center;background:#07131b;color:#eaf7f5;font-family:Arial,sans-serif}
  main{width:1020px}.eyebrow{color:#79e8bf;font:700 24px monospace;text-transform:uppercase;letter-spacing:.1em}
  h1{margin:35px 0 22px;font-size:82px;line-height:.98;letter-spacing:-.05em}.stage{color:#f0b15f;font:22px monospace}
</style><main><div class="eyebrow">The managed data engineering platform</div><h1>Data engineering that proves its work.</h1><div class="stage">PillarMesh · In development</div></main>`);
await page.screenshot({ path: 'public/social/og-default.png' });
await browser.close();
```

Run: `node scripts/render-social-card.mjs`

Expected: a deterministic 1200 by 630 PNG is created.

- [ ] **Step 5: Add crawl, `404`, and static response configuration**

Set `disable404Route: true` in Starlight and create the custom route. Add `robots.txt` with the
production sitemap URL. Add `netlify.toml`:

```toml
[build]
  command = "npm ci && npm run build"
  publish = "dist"

[[headers]]
  for = "/*"
  [headers.values]
    X-Content-Type-Options = "nosniff"
    Referrer-Policy = "strict-origin-when-cross-origin"
    Permissions-Policy = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    Content-Security-Policy = "default-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; img-src 'self' data:; font-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'"
```

Do not add HSTS preload or `includeSubDomains`; the future application subdomain is a separate
boundary. Recompute the CSP from the built resources if Starlight Pagefind requires a narrower
additional source. Do not add `*`.

- [ ] **Step 6: Verify and commit metadata and delivery policy**

```bash
npm run build
npx playwright test tests/e2e/metadata.spec.ts
git add src/layouts/SiteLayout.astro astro.config.mjs src/pages/404.astro public \
  scripts/render-social-card.mjs netlify.toml tests/e2e/metadata.spec.ts
git diff --cached --check
git commit -m "feat: add metadata and static delivery policy"
```

Expected: metadata and `404` tests pass, and every asset referenced by the CSP is same-origin.

---

### Task 8: Add public-content and built-output gates

**Files:**
- Create: `scripts/check-public-content.mjs`
- Create: `scripts/check-built-site.mjs`
- Create: `tests/content/check-public-content.test.mjs`
- Modify: `package.json`

**Interfaces:**
- Consumes: UTF-8 source files and static `dist/` HTML.
- Produces: `findPublicContentViolations(root: string) -> Promise<readonly string[]>`, CLI exit code 1 on violations, and build validation for every required route.

- [ ] **Step 1: Add failing positive and negative claim-guard tests**

```js
import assert from 'node:assert/strict';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { findPublicContentViolations } from '../../scripts/check-public-content.mjs';

test('rejects internal paths and unsupported availability claims', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'pillarmesh-content-'));
  await writeFile(
    path.join(root, 'page.md'),
    'Read /Users/example/workspace/pillarmesh/docs/private.md. Available now with any warehouse.',
  );

  const violations = await findPublicContentViolations(root);

  assert.ok(violations.some((value) => value.includes('internal absolute path')));
  assert.ok(violations.some((value) => value.includes('unsupported availability claim')));
});

test('accepts approved prospective product language', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'pillarmesh-content-'));
  await writeFile(path.join(root, 'page.md'), 'PillarMesh is being built for small data teams.');

  assert.deepEqual(await findPublicContentViolations(root), []);
});
```

- [ ] **Step 2: Run the test and confirm the missing module failure**

Run: `node --test tests/content/check-public-content.test.mjs`

Expected: FAIL with `ERR_MODULE_NOT_FOUND` for `check-public-content.mjs`.

- [ ] **Step 3: Implement the narrow source scanner**

The exported function recursively scans `.astro`, `.md`, `.mdx`, `.ts`, `.js`, `.json`, `.css`,
and `.toml` outside ignored build/dependency directories. It reports file and rule for:

```js
const RULES = [
  ['internal absolute path', /\/Users\/|PycharmProjects|\.worktrees\//i],
  ['placeholder marker', /\b(?:TODO|TBD|FIXME|XXX)\b/],
  ['unsupported availability claim', /\b(?:available now|generally available|works with any warehouse)\b/i],
  ['unsupported universal connector claim', /\b(?:all connectors|every connector|universal connector)\b/i],
  ['credential material', /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|(?:api|secret)[_-]?key\s*[:=]\s*["'][^"']+/i],
];
```

Allow no blanket ignore file. If a phrase is required for a negative explanation, rewrite it in
approved language rather than suppressing the rule.

- [ ] **Step 4: Implement required-route and generated-form validation**

`check-built-site.mjs` fails unless `dist/` contains the 18 routes in the file map, one `h1`, a
non-empty description, canonical URL, and social image on every HTML page. It additionally checks
that `dist/design-partners/index.html` contains `form-name`, `design-partner-enquiry`, `website`,
and no `type="file"`; `dist/pagefind/` exists; `dist/robots.txt`, `dist/sitemap-index.xml`,
`dist/favicon.svg`, and `dist/social/og-default.png` exist; and no HTML contains `file://` or an
internal absolute path.

- [ ] **Step 5: Wire the gates into package scripts and prove a boundary failure**

Add:

```json
{
  "scripts": {
    "check:content": "node scripts/check-public-content.mjs src public netlify.toml",
    "check:build": "node scripts/check-built-site.mjs dist",
    "build": "astro build && node scripts/check-built-site.mjs dist",
    "test:content": "node --test tests/content/*.test.mjs"
  }
}
```

Run:

```bash
npm run test:content
npm run check:content
npm run build
```

Expected: PASS. Add a temporary internal absolute path to a scratch source page, confirm
`check:content` exits 1 with the exact rule, remove it, and rerun to PASS.

- [ ] **Step 6: Commit the content and build gates**

```bash
git add scripts/check-public-content.mjs scripts/check-built-site.mjs \
  tests/content/check-public-content.test.mjs package.json package-lock.json
git diff --cached --check
git commit -m "test: guard public claims and build output"
```

---

### Task 9: Complete accessibility, responsive, and local release verification

**Files:**
- Create: `tests/e2e/accessibility.spec.ts`
- Modify: `tests/e2e/smoke.spec.ts`
- Modify: `README.md`
- Modify: any source file required to close a demonstrated accessibility or responsive failure

**Interfaces:**
- Consumes: every route and component from Tasks 1 through 8.
- Produces: automated axe coverage, manual review checklist, documented local commands, and the completion evidence required before Plan 2.

- [ ] **Step 1: Add the integrated accessibility and boundary tests**

```ts
import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

const criticalRoutes = ['/', '/platform/', '/how-it-works/', '/architecture/', '/trust/', '/docs/', '/design-partners/'];

for (const route of criticalRoutes) {
  test(`${route} has no automatically detectable WCAG A or AA violation`, async ({ page }) => {
    await page.goto(route);
    const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']).analyze();
    expect(results.violations).toEqual([]);
  });
}

test('site has no horizontal overflow at 320 CSS pixels', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto('/');
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320);
});

test('reduced motion disables non-essential transitions and animation', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  const duration = await page.locator('body').evaluate((element) => getComputedStyle(element).getPropertyValue('--motion-duration').trim());
  expect(duration).toBe('0ms');
});
```

- [ ] **Step 2: Run the suite and record every real failure**

Run: `npx playwright test tests/e2e/accessibility.spec.ts`

Expected: either PASS or focused failures naming the violated rule and route. Do not weaken axe
tags or exclude components to make failures disappear.

- [ ] **Step 3: Fix only demonstrated accessibility and responsive defects**

For each failure, add a focused regression assertion, confirm it fails against the defect, make
the smallest semantic HTML or CSS correction, and rerun the focused test. Typical fixes include
label association, heading order, focus contrast, disclosure naming, table overflow, and diagram
text alternatives.

- [ ] **Step 4: Perform the manual preview checklist locally**

Run `npm run dev -- --host 127.0.0.1` and inspect every route with:

- keyboard only, including skip link, mobile disclosure, documentation search, and form errors;
- browser zoom at 200 percent;
- 320, 768, 1024, and 1440 CSS pixel widths;
- reduced motion enabled;
- forced dark and light browser preferences, while retaining the Evidence Ledger dark theme;
- images disabled to verify text equivalents;
- browser console open; and
- one unknown route to confirm visible `404` behavior.

Record any gap in `README.md`; do not call a skipped manual check passing.

- [ ] **Step 5: Document the local workflow and external stop boundary**

`README.md` must include:

````markdown
## Verify locally

```sh
npm ci
npm run check
npm run format:check
npm run check:content
npm run test
npm run build
```

This repository contains public website material only. Publishing begins with the separate
PillarMesh website launch plan; local success does not authorize a GitHub remote, Netlify site,
form notification, DNS change, or production claim.
````

- [ ] **Step 6: Run the complete clean-room gate**

```bash
rm -rf node_modules dist .astro playwright-report test-results
npm ci
npm run check
npm run format:check
npm run check:content
npm run test
npm run build
git status --short
```

Expected: every command exits 0; `git status --short` shows only intentional uncommitted fixes.
The deletion targets are exact generated directories inside the website repository.

- [ ] **Step 7: Review the public diff and commit completion**

```bash
git diff --check
git diff --stat main...HEAD
git diff main...HEAD -- . ':(exclude)package-lock.json'
git add README.md tests/e2e/accessibility.spec.ts tests/e2e/smoke.spec.ts src
git diff --cached --check
git commit -m "test: complete local website verification"
```

Expected: the branch adds only public website material, no internal paths, generated reports,
credentials, or unsupported claims.

## Completion gate

Plan 1 is complete only when:

- every mapped route exists in `dist/` and is reachable locally;
- the Evidence Ledger direction and developer-focused copy match the approved design;
- all capabilities remain visibly planned unless new terminal evidence supports another state;
- Starlight documentation and Pagefind search build below `/docs`;
- the form is present in static HTML and collects only approved fields;
- policy pages state the exact privacy, retention, accessibility, security, and product-stage boundaries;
- the full clean-room command sequence exits 0;
- manual keyboard, zoom, responsive, reduced-motion, and form-error checks are recorded honestly;
- the local branch diff contains no private or accidental material; and
- the user approves the local result before Plan 2 starts.
