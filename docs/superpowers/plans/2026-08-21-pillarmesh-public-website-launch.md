# PillarMesh Public Website Launch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the locally verified PillarMesh website through a public GitHub repository and Netlify, then connect `pillarmesh.com` without disrupting email and prove the live routes and design-partner form end to end.

**Architecture:** Publish the bootstrap `main` and reviewed `feat/public-website` branches to `PillarMesh/pillarmesh-site`, use a pull request to obtain a Netlify deploy preview, and merge only after preview approval. Keep GoDaddy authoritative DNS, mutate only Netlify-required web records after an email-readiness gate, and retain sanitized operational evidence in the private PillarMesh repository.

**Tech Stack:** GitHub CLI authenticated as `ks2002119`, GitHub Actions, Netlify credit-based Free plan, Netlify Git deployment and Forms, GoDaddy DNS, `dig`, `curl`, Playwright

**Spec:** `docs/superpowers/specs/2026-08-21-pillarmesh-public-website-design.md`

## Global Constraints

- Set `SITE_REPO` to the public website checkout and `PILLARMESH_REPO` to the private PillarMesh
  checkout before the first command. Every path below is written against those two variables
  because this plan works across two repositories, and a command run in the wrong one can commit
  private launch evidence into the repository Task 3 makes public. Address the repository
  explicitly with `git -C` rather than relying on the current directory.
- This is Plan 2 of 2. Do not begin until every completion gate in `2026-08-21-pillarmesh-public-website-build.md` passes and the user approves the local site.
- REQUIRED DOMAIN SKILL during Netlify work: use `netlify-deploy` and follow its current instructions before any Netlify mutation.
- Use GitHub account `ks2002119` for every authenticated GitHub operation and verify it in the same shell invocation.
- Repository creation, public visibility, push, pull request, merge, Netlify site creation, notification enablement, live form submission, DNS mutation, and production publication are external effects. Stop at each named approval gate unless the user has explicitly authorized that effect.
- Create `PillarMesh/pillarmesh-site` as a public repository only after a final public-content diff review. Never fall back to another owner or private organization repository to bypass permissions or Netlify plan limits.
- Keep the Netlify account on the credit-based Free plan with auto recharge disabled. Recheck current pricing and form billing on the configuration date.
- Deploy static `dist/` only. Do not install an adapter, Function, database, CMS, analytics product, or paid add-on during launch.
- Keep GoDaddy authoritative. Do not change nameservers.
- Inventory and preserve MX, SPF, DMARC, ownership, DKIM, and other mail records. Change only apex and `www` records that Netlify explicitly requires.
- A partially configured mailbox blocks DNS cutover and form email notification, but it does not block deploy-preview review on a Netlify subdomain.
- Do not report email working from DNS records alone. Require one new inbound and one new outbound message traced to the intended mailbox.
- Do not report the form working from a success page alone. Require a new verified Netlify submission, notification receipt, and deletion of synthetic data from Netlify and the mailbox.
- Store no credentials, browser cookies, account identifiers, Netlify tokens, form PII, or raw email content in Git, evidence files, shell arguments, or chat output.
- Preserve the last successful Netlify deploy as the rollback target. Do not execute rollback unless the new production deploy is defective.
- Every evidence entry distinguishes read-only observation, external mutation, terminal outcome, and untested gap.

## Approval gates

1. **Mailbox test messages:** before sending the inbound and outbound messages used to establish mail readiness.
2. **Repository publication:** before creating the public GitHub repository or pushing.
3. **Pull request creation:** before opening the public PR.
4. **Netlify creation/link:** before creating or connecting the Netlify site.
5. **Preview acceptance:** user reviews the deploy preview before merge.
6. **Merge and production deploy:** before merging the PR.
7. **Form notification and synthetic submission:** before configuring delivery to the mailbox or sending the test enquiry.
8. **DNS cutover:** after mail readiness, exact record snapshot, and rollback values are recorded.

## File map

### Public website repository (`$SITE_REPO`)

- `.github/workflows/verify.yml`: locked build, content, browser, and static-output gates.
- `docs/runbooks/form-retention.md`: monthly 90-day submission and mailbox deletion procedure.
- `README.md`: production URL and verified launch date only after terminal verification.

### Private PillarMesh repository (`$PILLARMESH_REPO`)

- `docs/operations/website/2026-08-21-launch-evidence.md`: sanitized preflight, approvals, immutable identifiers, DNS before/after values, deploy identity, terminal form evidence, gaps, and rollback reference.

The evidence file never records form field values, mailbox addresses beyond the public
`contact@pillarmesh.com`, credentials, cookies, Netlify access tokens, or private account URLs.

---

### Task 1: Re-establish local and external readiness

**Files:**
- Create: `$PILLARMESH_REPO/docs/operations/website/2026-08-21-launch-evidence.md`
- Create: `$SITE_REPO/docs/runbooks/form-retention.md`

**Interfaces:**
- Consumes: completed website branch, current GitHub identity, current Netlify plan, current authoritative DNS, and current Mailotte mailbox state.
- Produces: a sanitized readiness record with explicit `ready`, `not ready`, or `not verified` outcomes for each launch boundary.

- [ ] **Step 1: Re-run the local website clean-room gate**

From `$SITE_REPO`:

```bash
cd "$SITE_REPO"
test "$(git branch --show-current)" = "feat/public-website"
test -z "$(git status --porcelain)"
npm ci
npm run check
npm run format:check
npm run check:content
npm run test
npm run build
git diff --check main...HEAD
```

Expected: all commands exit 0. Any failure returns work to Plan 1.

- [ ] **Step 2: Verify GitHub identity and repository absence read-only**

```bash
gh auth switch --hostname github.com --user ks2002119
gh auth setup-git
test "$(gh api user --jq '.login')" = "ks2002119"
if gh repo view PillarMesh/pillarmesh-site --json name; then
  echo "PillarMesh/pillarmesh-site already exists; stop and inspect it"
  exit 1
fi
```

If the repository exists, stop and inspect its default branch, visibility, remotes, and open pull
requests. Do not create or overwrite a second repository.

- [ ] **Step 3: Inspect Netlify account and pricing without mutation**

Read the current Netlify account dashboard and official billing documentation. Record:

- whether the account is credit-based or legacy;
- current plan name;
- remaining monthly credits;
- auto-recharge state;
- whether a `pillarmesh` site already exists;
- whether GitHub organization access is authorized; and
- the current form billing rule.

Expected for the approved economical path: credit-based Free, auto recharge off, no existing
PillarMesh site, and Forms documented as free and unlimited. A legacy account or existing site
requires a revised cost and ownership decision before mutation.

- [ ] **Step 4: Inventory authoritative DNS read-only**

```bash
dig +short NS pillarmesh.com
dig +short A pillarmesh.com @ns39.domaincontrol.com
dig +short AAAA pillarmesh.com @ns39.domaincontrol.com
dig +short CNAME www.pillarmesh.com @ns39.domaincontrol.com
dig +short MX pillarmesh.com @ns39.domaincontrol.com
dig +short TXT pillarmesh.com @ns39.domaincontrol.com
dig +short TXT _dmarc.pillarmesh.com @ns39.domaincontrol.com
```

Use the authenticated GoDaddy zone view to inventory ownership and DKIM selectors that cannot be
discovered reliably by guessing. Record exact current web records and sanitized mail-record
presence in the private evidence file.

- [ ] **Step 5: Approval gate: verify the mailbox terminally before declaring it ready**

Confirm in the approved mail administration UI that the domain and `contact` mailbox are active
and DKIM is published. Present the two-message test and obtain authorization before sending. With
that approval:

1. send one message from an unrelated test mailbox to `contact@pillarmesh.com` and confirm it
   appears in the PillarMesh mailbox;
2. reply from `contact@pillarmesh.com` to the test mailbox and confirm external receipt; and
3. record only stable correlation subjects and terminal outcomes, not bodies or recipient data.

If account creation, DKIM, inbound receipt, or outbound receipt is incomplete, record `not ready`
and explicitly block Tasks 7 through 9. Continue only through deploy-preview review.

- [ ] **Step 6: Enable the public contact address only after terminal mailbox proof**

If and only if Step 5 passes, add a failing browser assertion that the footer and security page
contain `mailto:contact@pillarmesh.com`. Run it while `SITE.emailVerified` is false and confirm it
fails. Change only `emailVerified` to `true` in `src/config/site.ts`, rerun the focused test, then
commit:

```bash
cd "$SITE_REPO"
git add src/config/site.ts tests/e2e/smoke.spec.ts tests/e2e/product-pages.spec.ts
git diff --cached --check
git commit -m "feat: enable verified contact mailbox"
```

If Step 5 does not pass, leave the flag false and the design-partner form remains the only public
contact path.

- [ ] **Step 7: Write the form-retention runbook**

Create `docs/runbooks/form-retention.md` with:

```markdown
# Design-partner form retention

Run monthly. Review verified and spam submissions in Netlify and matching notification messages.
Delete unsuccessful or inactive enquiries older than 90 days from both systems. Keep an active
design-partner record only while needed for that relationship or a documented legal obligation.
Never export submissions to a spreadsheet, CRM, analytics system, or model provider. Record the
review date and counts only; never record names, addresses, companies, challenge text, or message
bodies in Git.
```

- [ ] **Step 8: Commit the public runbook and the private evidence in their own repositories**

These two commits go to two different repositories. Address each one explicitly and assert the
branch first, because the runbook is published by Task 3 and the evidence must never be.

The public runbook, in the website repository:

```bash
cd "$SITE_REPO"
test "$(git branch --show-current)" = "feat/public-website"
git add docs/runbooks/form-retention.md
git diff --cached --check
git commit -m "docs: add form retention runbook"
```

The private evidence skeleton, in the PillarMesh repository, only after checking that it contains
no secrets or PII:

```bash
test "$(git -C "$PILLARMESH_REPO" branch --show-current)" = "chore/public-website-design"
git -C "$PILLARMESH_REPO" add docs/operations/website/2026-08-21-launch-evidence.md
git -C "$PILLARMESH_REPO" diff --cached --check
git -C "$PILLARMESH_REPO" commit -m "docs: record website launch readiness"
```

Then prove the separation held, before anything is pushed:

```bash
test -z "$(git -C "$SITE_REPO" ls-files docs/operations)"
```

Expected: empty. A non-empty result means the evidence file was written into the website
repository; remove it there and redo this step before continuing to Task 3.

---

### Task 2: Add public continuous verification

**Files:**
- Create: `.github/workflows/verify.yml` in the website repository
- Modify: `README.md` in the website repository

**Interfaces:**
- Consumes: locked npm scripts from Plan 1.
- Produces: a GitHub Actions `verify` job required before merge.

- [ ] **Step 1: Add a failing workflow-presence test to the content gate**

Extend `scripts/check-built-site.mjs` with a repository check that fails when
`.github/workflows/verify.yml` is absent or does not contain `npm ci`, `npm run check`,
`npm run format:check`, `npm run check:content`, `npm run test`, and `npm run build`.

Run: `npm run build`

Expected: FAIL with `missing verification workflow`.

- [ ] **Step 2: Add the minimal locked workflow**

```yaml
name: verify

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

jobs:
  verify:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-node@v5
        with:
          node-version-file: .nvmrc
          cache: npm
      - run: npm ci
      - run: npx playwright install --with-deps chromium
      - run: npm run check
      - run: npm run format:check
      - run: npm run check:content
      - run: npm run test
      - run: npm run build
```

Pin actions to the current supported major after checking official release notes. If the current
major differs from the snippet, update the file and this plan's execution evidence rather than
using a deprecated release.

- [ ] **Step 3: Verify locally and commit**

```bash
cd "$SITE_REPO"
npm run build
git add .github/workflows/verify.yml scripts/check-built-site.mjs README.md
git diff --cached --check
git commit -m "ci: verify public website"
```

Expected: the missing-workflow assertion failed before the YAML existed and the full local gate
passes after it.

---

### Task 3: Publish the GitHub repository and open the review pull request

**Files:**
- No additional source files
- Modify: private launch evidence with repository URL, branch SHAs, PR number, and workflow run

**Interfaces:**
- Consumes: local `main` bootstrap branch, `feat/public-website`, clean public diff, verified GitHub identity.
- Produces: public `PillarMesh/pillarmesh-site`, protected default branch `main`, pushed feature branch, and one review PR.

- [ ] **Step 1: Recheck the exact public diff immediately before publication**

```bash
cd "$SITE_REPO"
git status --short --branch
git diff --check main...feat/public-website
git diff --stat main...feat/public-website
git log --oneline --decorate main..feat/public-website
npm run check:content
```

Expected: a clean worktree, only intended site additions, no secrets, and no remote.

- [ ] **Step 2: Approval gate: obtain authorization for public repository creation and push**

State the exact owner, repository name, visibility, branches, and commands. Do not proceed from
earlier design approval alone.

- [ ] **Step 3: Create the empty public repository and push both branches**

In one authenticated shell:

```bash
cd "$SITE_REPO"
gh auth switch --hostname github.com --user ks2002119
gh auth setup-git
test "$(gh api user --jq '.login')" = "ks2002119"
gh repo create PillarMesh/pillarmesh-site --public --description \
  "Public website and documentation for the PillarMesh managed data engineering platform"
git remote add origin https://github.com/PillarMesh/pillarmesh-site.git
git push -u origin main
git push -u origin feat/public-website
gh repo edit PillarMesh/pillarmesh-site --default-branch main
```

Expected: both remote branch tips exactly match their local tips. If organization permission
fails, stop; do not create the repository under a personal account.

- [ ] **Step 4: Configure minimum branch protection before review**

Use the branch-protection API to require the `verify` status check, disallow force pushes and
deletions, require conversation resolution, and require a pull request before changes reach
`main`:

```bash
PROTECTION_JSON_PATH="$(mktemp -t pillarmesh-branch-protection.XXXXXX)"
```

Create the resolved temporary file with `apply_patch` and this JSON:

```json
{
  "required_status_checks": { "strict": true, "contexts": ["verify"] },
  "enforce_admins": true,
  "required_pull_request_reviews": { "required_approving_review_count": 0 },
  "restrictions": null,
  "required_conversation_resolution": true,
  "allow_force_pushes": false,
  "allow_deletions": false
}
```

Apply and verify it:

```bash
gh api --method PUT repos/PillarMesh/pillarmesh-site/branches/main/protection \
  --input "$PROTECTION_JSON_PATH"
gh api repos/PillarMesh/pillarmesh-site/branches/main/protection
```

Move the temporary file to trash or delete that exact file after verification. Record the
resulting protection summary.

- [ ] **Step 5: Approval gate: obtain authorization to open the public PR**

Present the final diff stat and proposed PR description. The description must say the product is
in development, identify the form as inactive until Netlify setup, list local checks, and state
that DNS is unchanged.

- [ ] **Step 6: Open the pull request and wait for the workflow terminal state**

```bash
PR_BODY_PATH="$(mktemp -t pillarmesh-site-pr-body.XXXXXX)"
```

Create the resolved temporary file with `apply_patch`. Include the approved product-stage note,
inactive-form boundary, local verification results, and unchanged-DNS statement. Then run:

```bash
gh pr create --repo PillarMesh/pillarmesh-site \
  --base main \
  --head feat/public-website \
  --title "feat: launch PillarMesh public website" \
  --body-file "$PR_BODY_PATH"
gh pr checks --repo PillarMesh/pillarmesh-site --watch
```

Move the temporary PR body to trash or delete that exact file after PR creation.
Expected: the PR is open and the `verify` workflow passes. A path-filtered skip or pending job is
not a pass.

- [ ] **Step 7: Record immutable repository evidence privately**

Record the repository URL, visibility, default branch SHA, feature branch SHA, PR number, workflow
run URL, and terminal conclusion. Do not merge.

---

### Task 4: Create the Netlify site and verify the deploy preview

**Files:**
- Modify: private launch evidence with Netlify plan, generated site hostname, deploy ID, commit SHA, and preview checks

**Interfaces:**
- Consumes: open GitHub PR with passing `verify`, current Netlify account, and `netlify.toml`.
- Produces: one Netlify site connected to the GitHub repository and a deploy preview for the exact PR SHA.

- [ ] **Step 1: Read and apply the current `netlify-deploy` skill**

Confirm the skill's authentication, preview, production, and cost-safety procedures. If the skill
requires a tool or credential that is unavailable, stop and report the exact blocker instead of
using an undocumented token flow.

- [ ] **Step 2: Approval gate: obtain authorization to create and link the Netlify site**

State the account, credit-based Free plan, auto-recharge-off condition, repository, production
branch, build command, and publish directory.

- [ ] **Step 3: Create the site with exact static settings**

In the Netlify dashboard:

1. import `PillarMesh/pillarmesh-site`;
2. select `main` as the production branch;
3. use the repository `netlify.toml` build command;
4. confirm publish directory `dist`;
5. add no environment variable or secret;
6. enable deploy previews for pull requests;
7. keep Functions, database, analytics add-ons, and auto recharge disabled; and
8. enable Forms detection, but do not configure notifications or submit the form yet.

Record the generated `*.netlify.app` hostname and site/deploy identifiers privately.

- [ ] **Step 4: Prove the preview belongs to the PR SHA**

Compare the Netlify deploy's commit reference with:

```bash
cd "$SITE_REPO"
git rev-parse feat/public-website
gh pr view --repo PillarMesh/pillarmesh-site --json headRefOid --jq '.headRefOid'
```

Expected: local branch, PR head, and Netlify deploy commit are identical.

- [ ] **Step 5: Run remote route and browser checks against the preview**

```bash
printf 'Paste the exact Netlify deploy-preview URL: '
IFS= read -r PILLARMESH_PREVIEW_URL
case "$PILLARMESH_PREVIEW_URL" in https://*.netlify.app) ;; *) exit 1 ;; esac
PLAYWRIGHT_BASE_URL="$PILLARMESH_PREVIEW_URL" npm run test:e2e
curl --fail --silent --show-error --location --output /dev/null "$PILLARMESH_PREVIEW_URL/"
```

Resolve the actual preview hostname from Netlify immediately before the command; never guess it.
Expected: browser tests pass, every asset is served by the preview deploy, unknown routes return
`404`, CSP produces no console error, and the form is detected in Netlify Forms.

- [ ] **Step 6: User preview-acceptance gate**

Give the user the exact deploy-preview URL and request review of desktop, mobile, copy, docs,
design-partner form, privacy, and product-stage language. Record approval or required changes.
Any change returns to the feature branch, re-runs CI, creates a new deploy, and invalidates the old
preview evidence.

---

### Task 5: Merge and prove the Netlify production-domain deployment

**Files:**
- Modify: private launch evidence with merge SHA, production deploy ID, production hostname, and rollback target

**Interfaces:**
- Consumes: approved preview, passing PR checks, current remote refs.
- Produces: merged public repository and a verified production deploy on the generated Netlify hostname, not yet on `pillarmesh.com`.

- [ ] **Step 1: Atomically re-establish Git ground truth**

```bash
cd "$SITE_REPO"
gh auth switch --hostname github.com --user ks2002119
gh auth setup-git
test "$(gh api user --jq '.login')" = "ks2002119"
git fetch origin --prune
git rev-parse HEAD origin/main feat/public-website origin/feat/public-website
test ! -d "$(git rev-parse --git-common-dir)/rebase-merge"
test ! -e "$(git rev-parse --git-common-dir)/index.lock"
gh pr view --repo PillarMesh/pillarmesh-site --json state,mergeable,headRefOid,statusCheckRollup
```

Stop if refs moved unexpectedly, a rebase or lock exists, the preview SHA differs from the PR
head, or checks are not terminal success.

- [ ] **Step 2: Approval gate: obtain authorization to merge and publish to the Netlify production hostname**

State the PR, exact head SHA, passing checks, approved preview, generated production hostname,
and that custom DNS remains unchanged.

- [ ] **Step 3: Merge without bypassing branch protection**

```bash
cd "$SITE_REPO"
gh pr merge --repo PillarMesh/pillarmesh-site --squash --delete-branch=false
git fetch origin --prune
gh pr view --repo PillarMesh/pillarmesh-site --json state,mergedAt,mergeCommit
```

Expected: PR state is `MERGED` and the returned merge commit is on `origin/main`.

- [ ] **Step 4: Wait for the exact production deploy terminal state**

Use the Netlify deployment view or approved deployment tool to identify the deploy whose commit is
the merge commit. Wait for `ready`. Record the prior successful deploy ID as the rollback target.
A generic site status or old successful deploy is not sufficient.

- [ ] **Step 5: Verify the generated production hostname**

```bash
printf 'Paste the exact Netlify production URL: '
IFS= read -r PILLARMESH_PRODUCTION_URL
case "$PILLARMESH_PRODUCTION_URL" in https://*.netlify.app) ;; *) exit 1 ;; esac
PLAYWRIGHT_BASE_URL="$PILLARMESH_PRODUCTION_URL" npm run test:e2e
```

Resolve the hostname from Netlify at execution time. Also inspect response headers for CSP,
content type, referrer policy, and permissions policy. Confirm Forms lists
`design-partner-enquiry`. Do not submit yet.

---

### Task 6: Configure and prove form notification on the Netlify hostname

**Files:**
- Modify: private launch evidence with notification configuration outcome, synthetic submission correlation, verified receipt, and deletion outcome

**Interfaces:**
- Consumes: active and terminally verified `contact@pillarmesh.com` mailbox, ready production deploy, and detected Netlify form.
- Produces: one verified synthetic design-partner submission and notification, followed by complete test-data deletion.

- [ ] **Step 1: Reconfirm mailbox readiness and form pricing**

Repeat the active-domain, DKIM, inbound, and outbound checks from Task 1 if more than 24 hours have
passed. Reconfirm the Netlify account is credit-based Free and Forms remain free and unlimited.
Any failure blocks this task.

- [ ] **Step 2: Approval gate: obtain authorization to configure notification and send one synthetic form**

State the exact public mailbox, fields, synthetic values, and deletion procedure. The challenge
text must contain no real company or customer data.

- [ ] **Step 3: Configure verified-submission notification**

In Netlify project configuration, create one email notification for verified submissions from
`design-partner-enquiry` to `contact@pillarmesh.com`. Configure no Slack, webhook, CRM, spreadsheet,
or automation destination.

- [ ] **Step 4: Submit one new synthetic enquiry through the production Netlify hostname**

Use the browser exactly as a visitor would. Record a non-sensitive correlation value such as
`PM-WEB-ACCEPTANCE-20260821` in the challenge field. A success page proves only HTTP acceptance.

- [ ] **Step 5: Trace the submission to terminal evidence**

Confirm:

1. the submission appears under verified, not spam, in Netlify;
2. all four allowed business fields and the acknowledgement are present;
3. no unexpected field or file exists;
4. the notification reaches `contact@pillarmesh.com`; and
5. the correlation value matches.

If notification is missing, inspect the exact Netlify submission and mail delivery evidence.
Do not resubmit repeatedly without diagnosing the first outcome.

- [ ] **Step 6: Delete all synthetic PII and prove deletion**

Delete the synthetic submission from Netlify and the notification from the mailbox, including
trash if the mailbox retains deleted mail. Confirm the correlation search returns nothing in both
systems. Record only terminal deletion outcomes in private evidence.

---

### Task 7: Prepare the email-safe custom-domain cutover

**Files:**
- Modify: private launch evidence with current DNS, exact Netlify-required records, TTL, rollback values, and cutover approval

**Interfaces:**
- Consumes: verified production hostname, verified form, terminally verified mail, authoritative GoDaddy zone.
- Produces: an exact mutation set limited to web records and an exact rollback set.

- [ ] **Step 1: Re-inventory DNS immediately before planning the mutation**

Repeat all authoritative `dig` commands from Task 1 and compare them with the GoDaddy zone view.
Stop if any record differs from the earlier snapshot until the concurrent change is explained.

- [ ] **Step 2: Add `pillarmesh.com` and `www.pillarmesh.com` in Netlify without changing DNS**

Use Netlify domain management to request both names. Record the exact apex and `www` records
Netlify currently requires. Do not copy values from this plan or an old guide.

- [ ] **Step 3: Validate the mutation set**

The mutation set may replace only:

- existing apex `A` or `AAAA` web records as required by Netlify; and
- the existing `www` CNAME.

It must leave NS, MX, SPF, DMARC, ownership, DKIM, and every other mail record byte-for-byte
unchanged. Capture the old web values as rollback values and lower TTL only if doing so does not
affect mail records.

- [ ] **Step 4: Approval gate: present the exact before/after DNS diff**

Show the authoritative current records, exact records Netlify requested, TTLs, unchanged mail
records, rollback values, expected propagation behavior, and the already verified Netlify
hostname. Do not mutate from general launch approval alone.

---

### Task 8: Cut over DNS and verify every affected path

**Files:**
- Modify: private launch evidence with mutation time, authoritative observations, Netlify TLS state, live route results, and mail results

**Interfaces:**
- Consumes: approved exact DNS diff and rollback set.
- Produces: `pillarmesh.com` and `www.pillarmesh.com` serving the verified Netlify deploy while Mailotte records and delivery remain intact.

- [ ] **Step 1: Apply only the approved web-record changes in GoDaddy**

Use the authenticated GoDaddy DNS interface. Resolve each target by name and type before editing.
Do not touch nameservers, MX, TXT, CAA, ownership, or DKIM records.

- [ ] **Step 2: Verify authoritative DNS before waiting on recursive propagation**

```bash
dig +short A pillarmesh.com @ns39.domaincontrol.com
dig +short AAAA pillarmesh.com @ns39.domaincontrol.com
dig +short CNAME www.pillarmesh.com @ns39.domaincontrol.com
dig +short MX pillarmesh.com @ns39.domaincontrol.com
dig +short TXT pillarmesh.com @ns39.domaincontrol.com
dig +short TXT _dmarc.pillarmesh.com @ns39.domaincontrol.com
```

Expected: web values match the approved Netlify set and mail values match the pre-cutover
snapshot. If authoritative values are wrong, apply the recorded rollback immediately.

- [ ] **Step 3: Wait for Netlify custom-domain and TLS terminal readiness**

Check Netlify domain state until both apex and `www` have valid TLS and the production deploy is
attached. Do not call a pending certificate live.

- [ ] **Step 4: Verify redirects, routes, headers, assets, and real `404`**

Run browser tests against `https://pillarmesh.com`:

```bash
PLAYWRIGHT_BASE_URL="https://pillarmesh.com" npm run test:e2e
curl --fail --silent --show-error --location --output /dev/null https://pillarmesh.com/
curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' \
  https://pillarmesh.com/this-route-does-not-exist/
```

Expected: all browser tests pass, the homepage returns success, the unknown route returns `404`,
`www` follows the configured canonical redirect, and security headers match the verified Netlify
hostname.

- [ ] **Step 5: Re-prove inbound and outbound mail after cutover**

Send one new inbound and one new outbound correlation message and trace both to external receipt.
DNS equality alone is not delivery evidence. Delete the synthetic messages after verification.

- [ ] **Step 6: Re-prove the form on the custom domain and delete test data**

With approval for one final synthetic enquiry, submit through `https://pillarmesh.com/design-partners/`,
trace verified Netlify receipt and mailbox notification, and delete the submission and message.
This verifies the user-facing custom-domain path rather than only the Netlify hostname.

- [ ] **Step 7: Hold or rollback on terminal failure**

Rollback the web DNS records to the captured prior values if the custom domain has invalid TLS,
critical routes fail, assets are blocked, or mail delivery regresses and cannot be corrected
within the approved change window. A form-notification defect with otherwise healthy web and mail
paths blocks launch completion but does not by itself justify a DNS rollback.

---

### Task 9: Close the launch with evidence, cost state, and no residual test data

**Files:**
- Modify: `README.md` in the public website repository
- Modify: `docs/operations/website/2026-08-21-launch-evidence.md` in the private PillarMesh repository

**Interfaces:**
- Consumes: terminal GitHub, Netlify, DNS, mail, route, accessibility, and form outcomes.
- Produces: public production URL, private evidence record, current cost state, rollback reference, and explicit gaps.

- [ ] **Step 1: Confirm no synthetic PII or temporary artifacts remain**

Search Netlify verified/spam submissions and the mailbox for both correlation values. Confirm no
matching record remains. Confirm `/tmp` PR body material, Playwright reports, screenshots with
form data, and local `.netlify` credentials are untracked and removed when no longer needed.

- [ ] **Step 2: Record current cost and limit state**

Record plan name, auto-recharge off, remaining credits, production-deploy count, bandwidth,
request usage, Functions usage equal to zero, and Forms billing rule. Do not estimate zero cost
from architecture alone; use the account's current usage view.

- [ ] **Step 3: Update the public README only with verified facts**

Add:

```markdown
## Production

The public site is served at <https://pillarmesh.com>. Marketing pages and curated documentation
are static Astro/Starlight output deployed through Netlify. Product capability remains explicitly
in development; the website is not the PillarMesh application.
```

- [ ] **Step 4: Complete the private evidence conclusion**

The conclusion lists:

- GitHub repository, PR, merge commit, and workflow terminal state;
- Netlify site, production deploy ID, commit, and prior rollback deploy;
- authoritative DNS before and after;
- apex, `www`, TLS, route, asset, header, and `404` outcomes;
- post-cutover inbound and outbound mail outcomes;
- form verified receipt, notification, and synthetic-data deletion outcomes;
- current plan, credit, and auto-recharge state; and
- every skipped, partial, or unverified item.

- [ ] **Step 5: Commit evidence and public README separately**

In the public website repository, create a normal branch from freshly fetched `origin/main`, add
the README change, run the full gate, and open a small PR only with explicit authorization. In the
private PillarMesh repository, stage only the evidence file on its existing documentation branch,
inspect the staged diff for secrets and PII, and commit. Address the repository explicitly, as in
Task 1 Step 8, so the evidence cannot reach the public repository:

```bash
git -C "$PILLARMESH_REPO" add docs/operations/website/2026-08-21-launch-evidence.md
git -C "$PILLARMESH_REPO" diff --cached
git -C "$PILLARMESH_REPO" commit -m "docs: record public website launch"
test -z "$(git -C "$SITE_REPO" ls-files docs/operations)"
```

Do not push either follow-up merely because the launch itself was authorized.

## Completion gate

Plan 2 is complete only when:

- `PillarMesh/pillarmesh-site` is public under the correct owner with protected `main`;
- the implementation PR is merged with terminal successful verification;
- the exact merge commit is the ready Netlify production deploy;
- the user approved the deploy preview before merge;
- the account is on the recorded plan with auto recharge off and no unexpected paid feature;
- apex and `www` have valid TLS and serve the intended deploy;
- every route, asset, header, sitemap, robots file, Pagefind index, and real `404` passes on the custom domain;
- authoritative mail records are preserved and new inbound and outbound messages reach terminal receipt after cutover;
- a new custom-domain form submission reaches verified Netlify receipt and mailbox notification;
- all synthetic submissions and messages are deleted and no PII appears in Git or evidence;
- the rollback deploy and prior DNS values are recorded; and
- all remaining gaps are named without implying they passed.
