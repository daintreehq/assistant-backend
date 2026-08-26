# Deploying, and the one thing that must never happen by accident

The deployment is `cloudbuild.yaml`: pushing to `main` builds an image, raises a
no-traffic candidate, smokes it, promotes it, and retires the candidate's URL.
Nothing is built or pushed from a developer machine. The scripts here support
that pipeline — they do not replace it.

| file | what it is |
|---|---|
| `bootstrap.sh` | idempotent one-time GCP setup: registry, secrets, both service accounts, the build trigger, the domain mapping |
| `smoke.sh` | the assertion suite. Run by hand against any host, and run by the build against the candidate as the pre-promotion gate |
| `rollback.sh` | moves traffic back to the previous Ready revision, selected by name |

## The thing that must never happen by accident

This server holds the upstream credential and funds every turn from it. A caller
supplies no key. So a Cloud Run service that anybody may invoke, in a posture that
does not require an account, is an open model endpoint billed to Daintree — and
that is one IAM binding away at every moment.

Two properties keep it from happening on its own, and both are worth understanding
before you change either.

**`bootstrap.sh` no longer grants public access.** It used to, on every run,
ungated — while its own header told you that re-running it is how you finish the
setup. The service it creates now is IAM-private and stays that way until somebody
performs a cutover on purpose.

**A Cloud Run invoker binding is per-SERVICE, not per-revision.** The release
candidate is deployed `--no-traffic --tag=rc-<build>`, which gives it a URL of its
own. On a public service that URL is public, whatever share of traffic the
revision holds. A tag is therefore an exposure, not a convenience — which is why
the build retires this build's tag on every path, including the ones where it
failed, and why the posture check below happens *before* the candidate exists
rather than after.

## The private rollout, in order

`AUTH_MODE` and `ENTITLEMENT_MODE` are separate axes on purpose: identity says the
token is genuine, entitlement says the account behind it has paid, and merging the
two rollouts is how a billing outage starts presenting as a signed-out user. So
they are moved one at a time — but every step below `enforce`/`enforce` happens on
a service **nobody outside can invoke**.

Raise the staging service and leave it private:

```bash
gcloud builds triggers run deploy-staging \
  --substitutions=_ENVIRONMENT=staging,_AUTH_MODE=open
```

Then walk the identity axis, re-releasing at each step, checking each one with an
identity token because the service is private:

```bash
TOKEN="$(gcloud auth print-identity-token)"
curl -sS -H "Authorization: Bearer $TOKEN" https://<service-url>/version
```

1. `_AUTH_MODE=open` — the door reads nothing. Confirms the revision comes up,
   the manifest publishes the right Supabase project, and the runbooks load.
2. `_AUTH_MODE=observe` — a credential that IS present is verified and the outcome
   recorded; nothing a caller can see changes. This is where a JWKS or issuer
   mistake surfaces without refusing anybody.
3. `_AUTH_MODE=enforce` **with `_STAGING_SUBJECT_GATE_REVISION` set** — a verified
   token is now required, *and* its subject must be in the published membership
   list. The build refuses this combination unless the
   `staging-allowed-subject-digests` secret has an enabled version, because a
   private beta that admits every verified account is not private.

Then the entitlement axis, `off` → `observe` → `enforce`, with
`_DAINTREE_ENTITLEMENT_URL` set to the staging website's endpoint. The build pins
that URL to `staging.daintree.org` and to the one route on the website that is
supposed to receive a forwarded bearer.

Publishing the membership list is an operator act, performed from the account
cutover runbook. `bootstrap.sh` creates the secret's container and grants the
runtime service account read access to it; it never generates, reads, or prints a
subject or a digest, and neither does the build. What a release asserts about the
list is the safe projection `/version` publishes — whether the gate decides
anything, how many subjects it admits, and the operator-chosen label for that
membership. Never a member, and never an answer to "is this subject admitted": a
public unauthenticated endpoint that could be asked that would be a membership
oracle, which is worse than publishing the list because nobody can see it
happening.

## The cutover

Public invocation is attached last, once the deployed revision has been *seen* to
enforce identity, enforce entitlement, and hold a non-empty gate:

```bash
PUBLIC_CUTOVER=daintree-assistant ./infra/gcp/bootstrap.sh
```

Typing the service name is the confirmation — an exported variable a shell
happened to carry from something else cannot satisfy it. The script then reads
`/version` back from the deployment with an identity token and refuses the grant
unless all three facts hold. Refusing is the only useful behaviour here: the
mistake this catches is a cutover aimed at yesterday's revision, or at staging
while thinking of production, and neither of those looks wrong from the shell it
is typed into.

Revoking is as deliberate as granting, and `bootstrap.sh` will not do it either:

```bash
gcloud run services remove-iam-policy-binding daintree-assistant \
  --region=us-central1 --member=allUsers --role=roles/run.invoker
```

## Where `open` and `observe` may live

On loopback, or on an IAM-private service. Those are the only two.

A Cloud Run **tag** on a public service is not a private place — it is a public
URL with a different hostname. Neither is "nobody knows the URL": Cloud Run URLs
are derived from the service name and the project, and the certificate is in the
public transparency logs.

The build enforces this for staging, and the shape of the rule is worth stating
because it is narrower than it first looks. `cloudbuild.yaml` reads the service's
invoker policy before deploying, and refuses a **staging** release onto a publicly
invokable service unless it enforces both axes and mounts a non-empty gate. It
does *not* assert that every public deployment must enforce, and that is not an
oversight: production runs `open`/`off` today as a deliberate product posture, so
a blanket rule would refuse the release that is currently correct — and a gate
nobody can ship through is a gate somebody deletes. Staging is different because
staging *is* the private beta: the subject gate exists only there, `config.py`
refuses to let production hold a membership list at all, and so "a public staging
service that does not enforce" has no legitimate reading. Moving production is a
product decision, and it will arrive as its own rule.

## What the pipeline guarantees, and what it does not

**Guaranteed.** A candidate that fails its deploy, its environment readback, its
smoke, or its promotion cannot leave a resolving `rc-` URL behind: the three
fallible steps record success in marker files instead of aborting the build, and a
final step that cannot be skipped retires *this build's own tag* — read from the
file the deploy wrote, never derived a second time, so it can neither name nor
delete a concurrent build's candidate — and only then decides whether the build is
red.

**Not guaranteed.** Three bounds, each worth knowing before you rely on the gate.

A build that is cancelled, or that hits the top-level timeout, kills the cleanup
step with it, and the tag survives. Cloud Build has no always-run semantics for
that case and nothing inside a build can cover it.

The posture gate reads the **service's own** IAM policy. An invoker role inherited
from the project, folder or organisation would make the service public and be
invisible to it — reading effective policy needs
`resourcemanager.projects.getIamPolicy`, which the deploy service account
deliberately does not hold, and widening a deploy identity to strengthen a check
on the deploy is the wrong trade. So: **never grant `roles/run.invoker` above the
service.** Nothing in this project does today.

And the gate reads the policy *before* deploying, so an IAM change made during the
build is not seen. The cutover check in `bootstrap.sh` is the complementary half:
one guards the release against the exposure, the other guards the exposure against
the release.

**Complementary is not coordinated, and the gap between them is a real race.** Each
reads the other's state once, and neither holds a lock, so this interleaving passes
both checks and publishes an open staging service:

1. an `open`/`observe` staging build reads the IAM policy, finds the service private,
   and is permitted;
2. a cutover reads the *currently serving* revision — still the previous, enforced
   one — and approves;
3. the build promotes its open revision to 100%;
4. the cutover grants `allUsers`.

Nothing here is misconfigured; the two operations are simply not ordered against each
other. Until one of them takes a lock, **do not run a cutover while a release to the
same service is in flight** — wait for the build to finish and re-read
`/version`'s posture before granting the invoker binding. That is an operator rule,
not something either script currently enforces.

Check the tags on the service after a cancelled release:

```bash
gcloud run services describe daintree-assistant --region=us-central1 \
  --format='value(status.traffic[].tag)'
```

## Reading a deployment

```bash
./infra/gcp/smoke.sh --url https://assistant.daintree.org
```

Free, and needs no credential — the server holds its own. Add `--full` to send two
real turns, which spend Daintree's money rather than yours; that is why it is
opt-in and nothing else here is.

Every expectation the release cares about is *stated* rather than discovered:
`--expect-build-sha`, `--expect-env`, `--expect-posture`, `--expect-entitlement`,
the whole OAuth manifest, and `--expect-gate-revision` or `--expect-no-gate`. The
script adapts to a deployment it is not told about, which is what makes it useful
against an unknown host and exactly wrong as a release gate — so the build tells
it everything, in both directions, because an assertion that silently does not run
is worse than one that was never asked for.

There is deliberately no way to hand this script a user's access token. Access
tokens live in the CLI process's memory on purpose, and a facility that printed
one for a shell to capture would demote the whole design to a convention, with a
CI log the first place the value landed. Authenticated acceptance — an allowlisted
account working, a non-allowlisted one being refused — belongs to the operator
cutover runbook, driven through the real CLI.

Note that Cloud Run reserves `/healthz` and answers it itself, so an external
probe uses `/health` or `/readyz`. `/healthz` still serves the container's own
liveness probe.
