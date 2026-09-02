# PillarMesh Public Website Design

**Status:** Approved for implementation planning

**Date:** 2026-08-21

**Applies to:** The first public PillarMesh product website, curated public documentation,
design-partner application, and Netlify delivery configuration

**Governing product sources:**

- `docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md`;
- `docs/architecture/decisions/ADR-0003-managed-data-engineering-platform.md`; and
- `docs/architecture/repository-layout.md`.

## 1. Purpose

PillarMesh needs a public website that explains and lends credibility to the product idea
without implying that the product is generally available. The first release targets hands-on
data engineers and technical leads in small data teams. It must make the product's technical
thesis understandable, expose enough architecture to support evaluation, and invite suitable
design partners.

The website is not the PillarMesh product console. It does not authenticate users, connect to
customer systems, execute integrations, display tenant data, or expose internal operational
records.

## 2. Approved product positioning

The category line is:

> The managed data engineering platform

The primary promise is:

> Data engineering that proves its work.

The supporting statement is:

> PillarMesh is being built for small data teams that need to operate sources, a dedicated
> warehouse, transformations, catalog, dashboards, and evidence as one governed system.

Every page that discusses product capability must also make the current stage discoverable:

> In development. Seeking design partners.

This wording is deliberately prospective. The public website must not turn planned scope into
an availability, scale, performance, connector-count, compliance, or customer-outcome claim.

## 3. Audience

### 3.1 Primary reader

The primary reader is a hands-on data engineer, senior data engineer, or technical data lead on
a small team. They are familiar with:

- Python and SQL;
- schemas, keys, transformations, and tests;
- Spark-style distributed processing concepts;
- warehouses, pipelines, backfills, retries, and schema drift; and
- the operational burden of assembling ingestion, transformation, catalog, BI, and monitoring
  tools.

The site assumes this baseline. It does not teach introductory programming or data engineering.
It does explain PillarMesh-specific concepts such as Integration Contracts, compilation,
legality, deterministic plans, `No Valid Plan`, reconciliation, and attributable evidence.

Familiarity with Spark is an audience characteristic, not a product capability claim. The site
must not imply that Spark is an initial engine or runtime. The planned initial managed warehouse
engines are PostgreSQL and ClickHouse.

### 3.2 Secondary reader

The secondary reader is an engineering manager assessing whether PillarMesh could reduce the
operational burden on a small data team. Content should let this reader understand ownership,
approval, cost, recovery, and trust boundaries without shifting the site into procurement-led
enterprise language.

### 3.3 Excluded audience assumptions

The first release is not optimized for:

- data engineering beginners;
- non-technical executive education;
- investor storytelling;
- procurement workflows; or
- users seeking a generally available self-service product.

## 4. Architecture decision

Use one Astro site with Starlight documentation, deployed as static assets on Netlify. Marketing
pages and public documentation share one repository, design system, navigation, build, and
deployment. Documentation is served below `/docs`. The future authenticated product remains a
separate deployment boundary at `app.pillarmesh.com`.

**Rationale:** A single portable static build has the lowest initial cost and operational burden
while preserving content ownership, technical flexibility, preview deployments, and a clean
future boundary for the authenticated application.

Two alternatives were rejected:

1. Separate marketing and documentation deployments would duplicate navigation, styling,
   deployment, and domain work before independent release cycles are needed.
2. A visual website builder plus a hosted documentation service would add recurring cost,
   fragmented design, and platform lock-in without improving the initial product explanation.

## 5. Repository boundary

The website belongs in a separate `pillarmesh-site` repository. The intended steady state is a
public repository containing only assets and content that are already suitable for publication.
Repository creation and public visibility remain explicit approval-dependent implementation
actions.

The website repository owns:

- Astro and Starlight configuration;
- public page and documentation content;
- the Evidence Ledger design system;
- public diagrams and images;
- accessibility, content, link, and build tests;
- Netlify configuration; and
- public policy pages for the website.

The main PillarMesh repository remains the authority for architecture and product boundaries.
Its internal documents are source material for deliberate public adaptations. The website build
must never import, copy a directory, or automatically publish content from the internal
repository. This manual publication boundary prevents operational details, unreviewed plans,
private evidence, internal identifiers, or stale implementation claims from reaching the public
site.

## 6. Information architecture

The initial global navigation contains:

1. Platform
2. How it works
3. Architecture
4. Trust
5. Docs
6. Become a design partner

### 6.1 Home `/`

The home page establishes the category, audience, promise, product stage, and technical thesis.
Its sections are:

1. Hero with category, promise, supporting statement, stage, and primary actions.
2. The operational problem for small data teams.
3. One accountable managed platform: sources, warehouse, transformations, catalog, dashboards,
   reconciliation, and evidence.
4. Connect, declare, approve, operate, and prove.
5. Contract-first, deterministic, and attributable design principles.
6. Current scope and explicit non-goals.
7. Design-partner invitation.

### 6.2 Platform `/platform`

This page explains the planned managed product boundary:

- supported source acquisition;
- a dedicated PostgreSQL or ClickHouse warehouse;
- transformations and semantic models;
- catalog and governed discovery;
- dashboards and reports;
- trigger, state, replay, and bounded backfill behavior; and
- reconciliation, recovery, and evidence.

Each capability is labeled according to its actual maturity. Planned components use prospective
language. No capability is presented as live without current terminal evidence.

### 6.3 How it works `/how-it-works`

This page explains the developer-facing lifecycle:

1. Connect supported operational sources.
2. Declare the required business outcome and constraints.
3. Compile candidate work and reject illegal or infeasible outcomes with `No Valid Plan`.
4. Review and authorize meaning, access, risk, and material cost.
5. Operate the admitted plan in the managed data plane.
6. Reconcile the source-to-consumer outcome and retain attributable evidence.

The page uses one representative end-to-end example. Any contract, SQL, Python, or command
fragment that is not a released public interface is visibly labeled `Illustrative`. It must not
look executable or be described as supported.

### 6.4 Architecture `/architecture`

This page provides sufficient depth for a technical evaluator without reproducing the internal
specification. It covers:

- durable Integration Contracts versus replaceable physical and execution plans;
- the semantic, compilation, managed data, experience, and operations boundaries;
- deterministic compilation and signed-artifact verification;
- legality and feasibility before execution;
- human authority over meaning, policy, access, material cost, migration, and destructive work;
- evidence and reconciliation as distinct from logs and monitoring; and
- explicit product limits.

### 6.5 Trust `/trust`

The trust page states principles and current boundaries rather than claiming certifications that
do not exist. It covers:

- tenant, credential, and public/private artifact separation;
- encryption and access-control intent only where supported by approved architecture;
- attributable decisions and evidence;
- supervised automation and denial behavior;
- data minimization for the public website;
- the product's in-development status;
- security contact and responsible disclosure; and
- links to privacy and accessibility statements.

### 6.6 Design partners `/design-partners`

This page explains:

- the target participant profile;
- what a design partnership does and does not imply;
- the expected conversation and evaluation process;
- that form submission is an enquiry, not acceptance or product access; and
- how submitted information is retained and deleted.

The form collects only:

- name;
- work email;
- company; and
- a short description of the current data engineering challenge.

It must not accept credentials, datasets, file uploads, sensitive business records, or free-form
attachments.

### 6.7 Public documentation `/docs`

The first documentation set contains:

- Overview: promise, product fit, stage, and current limits;
- Core concepts: contracts, compiler, plans, runtime, reconciliation, and evidence;
- Operating model: supervised automation and human authority;
- Architecture: public boundary and lifecycle explanations; and
- Glossary and FAQ: stable vocabulary and direct answers to likely technical questions.

There are no installation, SDK, API, connector, or production-operation guides until those
interfaces exist and have been verified. A documentation placeholder must not masquerade as a
usable quickstart.

### 6.8 Footer and policy routes

The footer links to:

- Privacy;
- Accessibility;
- Security;
- product-stage notice;
- the public source repository if approved; and
- `contact@pillarmesh.com` after mailbox delivery is verified.

The footer reserves no link to `app.pillarmesh.com` until that application has a safe public
entry point.

## 7. Content and claim discipline

Public writing follows these rules:

- Use plain technical language and concrete engineering problems.
- Use Python, SQL, schema, partition, retry, backfill, drift, lineage, and test vocabulary
  naturally rather than explaining foundational concepts.
- Explain PillarMesh-specific semantics at first use.
- Prefer inspectable examples and diagrams to generic claims.
- Distinguish `in development`, `planned`, `implemented`, and `verified`.
- Use `verified` only when a new relevant transaction has reached its terminal state.
- Do not infer production behavior from specifications, fixtures, mocks, builds, existing rows,
  screenshots, or HTTP success alone.
- Do not claim universal connectors, arbitrary destinations, unrestricted SQL mutation,
  general-purpose workflow scheduling, fully autonomous semantics, or universal exactly-once
  behavior.
- Do not present PostgreSQL-to-Snowflake M0 as the current product destination model.
- Do not publish customer logos, testimonials, benchmark numbers, certifications, or uptime
  commitments without attributable current evidence and permission.
- Avoid generic phrases such as "unlock your data," "single pane of glass," and "AI-powered"
  when they replace an observable explanation.

A pre-publication review compares every behavioral claim with the current managed-platform
addendum, applicable ADRs, implementation, and verification evidence. Conflicts fail the content
gate and block release.

## 8. Visual system

The approved direction is **Evidence Ledger**: technical, structured, and quietly confident.
Trust is expressed through grids, state, evidence, and inspectable system relationships rather
than shields, stock photography, glowing AI imagery, or generic cloud illustrations.

### 8.1 Core palette

- page background: `#07131B`;
- deep background: `#050A0E`;
- surface: `#0B1E20`;
- primary mint: `#79E8BF`;
- in-development amber: `#F0B15F`;
- primary text: `#EAF7F5`; and
- muted text: `#A3B7B9`.

Every final color pairing must meet WCAG 2.2 AA contrast requirements in its actual context.

### 8.2 Typography

Use a readable sans serif for prose and a restrained monospace face for identifiers, state, and
evidence labels. Fonts are self-hosted from licensed, repository-tracked assets with system
fallbacks. No production page fetches a font from a third-party origin.

### 8.3 Graphic language

Use:

- compact execution and evidence artifacts;
- system flow diagrams;
- contract, plan, run, and evidence state labels;
- fine grid lines and bounded cards;
- mint for admitted or verified states; and
- amber for in-development, review, or attention states.

Avoid decorative dashboards that could be mistaken for an implemented product screen. A visual
artifact is labeled `Conceptual` unless it is a faithful capture of a verified public interface.

### 8.4 Motion and responsive behavior

Motion is limited to small state transitions and progressive diagram reveals. Essential content
does not depend on animation. `prefers-reduced-motion` disables non-essential movement. The
content order, forms, tables, diagrams, and navigation remain usable at 320 CSS pixels and at
200 percent zoom.

## 9. Technical composition

### 9.1 Build

The site uses:

- Astro for custom pages and shared layouts;
- Starlight for `/docs` navigation, search, Markdown, and MDX;
- TypeScript in strict mode for configuration and interactive components;
- semantic HTML and CSS before client-side JavaScript; and
- static output to `dist/`.

The implementation selects the active Node.js LTS available on the implementation date and pins
it in both the repository and Netlify configuration. Dependencies are pinned through
`package-lock.json`. Netlify runs the locked install and production build declared in
`netlify.toml`.

The first release has no server-side rendering, Netlify Functions, database, authentication,
CMS, personalization, advertising pixels, or third-party behavioral analytics.

### 9.2 Shared components

The initial component boundaries are:

- site header and mobile navigation;
- global product-stage notice;
- hero and call-to-action group;
- capability and principle cards;
- system-flow and evidence-artifact diagrams;
- maturity badge;
- design-partner form;
- site footer; and
- Starlight theme overrides using the same tokens.

Components remain content-oriented and accept explicit typed properties. They do not fetch
product state or infer maturity from page location.

### 9.3 Metadata and discovery

Every public page provides a unique title, description, canonical URL, social preview, and one
top-level heading. The build emits a sitemap and robots policy. Structured data is limited to
truthful `Organization`, `WebSite`, and `BreadcrumbList` information. The site does not emit
product offers, ratings, reviews, availability, or customer data that do not exist.

## 10. Netlify delivery

The site uses Git-based deployment:

1. A pull request creates a deploy preview.
2. Required checks and human review run against the preview.
3. Merging to the protected production branch triggers a production build.
4. A failed build leaves the prior successful production deploy in place.
5. The new deploy is not called live until domain, page, asset, and form checks pass against the
   production URL.

The initial account remains on the Netlify credit-based Free plan. Auto recharge stays disabled.
Usage and billing notifications stay enabled. The implementation records the plan type because
legacy and credit-based accounts meter forms differently.

As checked on 2026-08-21, Netlify documents Forms as free and unlimited on credit-based plans,
while production deployments, bandwidth, requests, and compute consume credits. The relevant
references are:

- <https://docs.netlify.com/manage/forms/usage-and-billing/>;
- <https://docs.netlify.com/manage/forms/setup/>;
- <https://docs.netlify.com/manage/forms/submissions/>;
- <https://docs.netlify.com/manage/forms/spam-filters/>; and
- <https://docs.netlify.com/manage/accounts-and-billing/billing/billing-for-credit-based-plans/how-credits-work/>.

Pricing and limits must be rechecked when Netlify is configured and before a paid-plan decision.

## 11. Design-partner form

The form is a progressively enhanced static HTML form detected by Netlify. It uses:

- a unique form name;
- `POST` submission;
- Netlify form detection;
- the built-in spam filter;
- a hidden honeypot field;
- native HTML constraints;
- explicit visible labels and accessible descriptions;
- a required acknowledgement linking to the privacy notice; and
- a dedicated success page.

The first release does not add reCAPTCHA because the honeypot and built-in filter provide a
lower-tracking starting point. A later anti-abuse change requires a separate privacy and
accessibility review.

Submission is not considered operationally verified from the success page alone. Launch
verification creates a new synthetic enquiry, confirms it appears as a verified submission,
confirms the configured notification reaches the intended verified mailbox, and deletes the
test submission and message.

### 11.1 Privacy and retention

The privacy notice states:

- the four fields collected;
- that Netlify processes and stores the submission;
- the purpose of evaluating a design-partner conversation;
- who can access submissions;
- the retention rule;
- how to request access or deletion; and
- that submission does not create an account or commercial relationship.

Unsuccessful or inactive enquiries are deleted from Netlify and notification mailboxes within
90 days. Information for an active design-partner relationship is retained only while needed for
that relationship or an applicable legal obligation. The initial process is a documented monthly
manual review. No form submission is copied to a CRM, spreadsheet, model provider, or analytics
system in the first release.

## 12. DNS and email safety

`pillarmesh.com` remains registered and authoritatively managed at GoDaddy for the initial
Netlify connection. The implementation uses the exact DNS records Netlify supplies for the
custom domain; it does not move nameservers merely to deploy the website.

Before any DNS mutation:

1. Inventory the current apex, `www`, MX, SPF, DMARC, ownership, DKIM, and other mail-related
   records through the authoritative nameserver and the GoDaddy zone view.
2. Confirm the final Mailotte mailbox and DKIM state rather than assuming the earlier partial
   setup completed.
3. Capture the records that will change and the rollback values.
4. Deploy and verify the site on its Netlify preview domain.
5. Change only the web records required by Netlify.
6. Verify `pillarmesh.com`, `www.pillarmesh.com`, TLS, redirects, MX, SPF, DMARC, DKIM, inbound
   mail, and outbound mail after propagation.

The website launch must not trade a working or partially configured mail domain for a successful
HTTP response.

## 13. Security and failure behavior

The static site sets appropriate response headers through Netlify configuration, including a
content security policy, content-type protection, a conservative referrer policy, and a narrow
permissions policy. The final policy is generated from the resources the built site actually
loads. It is not weakened pre-emptively for future integrations.

No secret belongs in the repository, Astro public environment, built assets, form markup, deploy
preview, or chat-visible command. Netlify configuration contains no production credentials.

Failure behavior is explicit:

- build, type, content, accessibility, or link failures block production deployment;
- the last successful production deploy remains the rollback target;
- missing pages return a real accessible `404`, not the home page with `200`;
- form validation identifies the field and preserves non-sensitive input where practical;
- a form success message appears only after Netlify accepts the submission;
- spam rejection does not disclose filter details;
- a missing notification is a launch failure even if the submission exists in Netlify; and
- a documentation search failure does not make the content inaccessible through navigation.

## 14. Accessibility requirements

The target is WCAG 2.2 AA. The initial release must provide:

- semantic landmarks and heading order;
- keyboard-operable navigation and form controls;
- visible focus indicators;
- skip navigation;
- accessible mobile navigation state;
- text alternatives for meaningful diagrams;
- a non-visual equivalent for every system flow;
- labeled form fields and associated error messages;
- sufficient contrast in default, hover, focus, error, and disabled states;
- reduced-motion support;
- no essential hover-only content; and
- usable content at zoom and narrow viewport boundaries.

Automated accessibility checks are necessary but not sufficient. Keyboard, screen-reader naming,
zoom, reduced-motion, and form-error behavior receive manual review on the deploy preview.

## 15. Verification strategy

### 15.1 Local and continuous checks

The repository defines locked commands for:

- dependency installation;
- TypeScript and Astro validation;
- production build;
- formatting and linting;
- internal-link and fragment validation;
- route, canonical, sitemap, and metadata validation;
- public-content leakage and prohibited-claim checks;
- unit tests for content helpers and interactive components; and
- browser tests for navigation, responsive layout, reduced motion, `404`, and form behavior.

At least one negative or boundary test accompanies each behavior. Examples include a broken
heading fragment, missing accessible form label, narrow viewport navigation, rejected empty
challenge, and an illustrative code block missing its maturity label.

### 15.2 Preview review

The deploy preview is reviewed for:

- layout at representative mobile, tablet, and desktop widths;
- keyboard operation and focus order;
- contrast and reduced motion;
- copy and maturity-label accuracy;
- absence of internal paths, identifiers, credentials, and private evidence;
- social previews and search metadata;
- no browser console errors; and
- measured performance with production assets.

### 15.3 Production verification

A production launch requires new evidence for:

- apex and `www` routing;
- TLS and redirect behavior;
- every public route and the real `404` response;
- CSS, font, image, sitemap, and robots assets;
- current mail DNS and inbound/outbound mail;
- one new design-partner form submission through verified receipt and notification;
- deletion of the synthetic submission and notification; and
- rollback to the last good Netlify deploy without executing it unless needed.

A successful build, deploy status, rendered homepage, HTTP `200`, form success page, existing
submission, or email-notification configuration is not by itself end-to-end proof.

## 16. Cost boundary

The target fixed platform cost for the first release is zero dollars per month beyond the
existing domain renewal. The design stays within that boundary by using static delivery, one
deployment, no functions, no database, no CMS, no paid analytics, and the credit-based Free-plan
form service.

Approaching the Netlify credit allowance triggers a usage review. A paid plan is not enabled
automatically. The review compares traffic, production-deploy cadence, asset weight, request
volume, and current Netlify pricing before recommending a plan change.

## 17. Implementation boundary

This design authorizes implementation planning, not external mutation. Creating or publishing a
GitHub repository, configuring Netlify, changing DNS, enabling form notifications, publishing the
site, or sending a test form remain explicit plan steps with their required approvals and
verification boundaries.

The first implementation plan must keep these phases distinct:

1. local site and content foundation;
2. local verification;
3. repository and deploy-preview setup;
4. user review of the preview;
5. Netlify production configuration;
6. DNS and email-safe cutover; and
7. live form and route verification.
