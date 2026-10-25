# The service stamps provenance annotations on the namespaces it writes

This reverses an earlier decision. `create_namespace` used to add **no** label or
annotation of its own: provenance was the API gateway's audit trail plus one INFO
log line naming the user. Now that namespace labels can also be set and changed,
every namespace the service creates or relabels carries a small set of
**provenance annotations** saying who wrote it, when, and in which request.

**Why reverse it.** The gateway trail and the service log answer "who did this?"
only for someone who can read them. The people looking at a namespace are often
looking at it through `kubectl`, in a cluster, with no access to either. An
annotation puts the answer where they are already looking, and the request id
in it is the thread back to the full record in the logs.

**What it is not: an audit record.** Anyone with `patch` on namespaces — through
`kubectl`, not just this service — can edit or delete the annotations, and they
hold only the latest write, never a history. They are a pointer *to* the audit
trail, not a replacement for it; the logs remain the source of truth. Do not
build anything that trusts them as evidence.

**Annotations, not labels.** Labels exist to be selected on and are restricted to
63 characters of `[A-Za-z0-9._-]` — an email or an RFC 3339 timestamp is not a
valid label value. Nobody needs to select namespaces by who last touched them.
A selectable `managed-by` label was considered and rejected for now: there is no
selection use case yet, and the natural key (`app.kubernetes.io/managed-by`)
would need an exception to the **Reserved label key** rule.

**The key prefix is a deployment setting, not a constant.** It must be a domain
the operator controls, which differs per organisation; nothing in the code can
pick it correctly. For the same reason it has **no default**: the service refuses
to start without one, and refuses a value that is not a valid DNS subdomain or
that falls under `kubernetes.io` / `k8s.io`. A built-in placeholder would be
silently stamped onto every namespace in a deployment that forgot to set it, and
an unset value disabling the stamps would lose them just as silently. The same
goes for the shipped env files: only `.env.dev` and `.env.test` set one;
`.env` (loaded everywhere) and `.env.prod` deliberately do not, so production
must supply its own. Consequence: changing it on a running deployment leaves the
old annotations behind under the old prefix, and the service will neither read
nor update them.

## What is written

- On create: `created-by`, `created-at`, `last-request-id`. `created-*` is written
  once and never overwritten by anything later.
- On a label update: `last-modified-by`, `last-modified-at`, `last-request-id` —
  also on namespaces the service did not create, which therefore carry
  `last-modified-*` without `created-*`.

"By" is the authenticated account from the caller's token — the only identity the
service has verified. No caller-asserted identity (e.g. an on-behalf-of header)
is recorded.

These annotations belong to the service: no caller-facing field can set or
remove them, and any future annotations API must treat the prefix as reserved.
