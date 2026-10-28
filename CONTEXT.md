# Context

Glossary for `cluster-service`. Terms only — no implementation detail, no specs.
When a term here conflicts with how a word is being used in conversation or in
code, the conflict is the interesting part: resolve it and update this file.

## Node lifecycle

**Cordon** — Marking a node unschedulable so no *new* pods land on it. Says
nothing about the pods already running there.

**Drain** — Cordon, then remove every pod the caller is permitted to remove.
The goal is an empty node; a partially drained node is a failed drain, not a
partial success.

## Drain eligibility

Every pod on a node falls into exactly one of three categories. The distinction
is what the drain options operate on.

**Skipped** — Pods a drain never touches, whatever the options say: DaemonSet
pods (the controller would immediately replace them), mirror pods (owned by the
kubelet, not the API server), and completed pods (nothing left to move). Not
configurable, and evaluated *before* Protected — a DaemonSet pod using emptyDir
is Skipped, never Blocked.

**Protected** — Pods that are removable, but only if the caller explicitly opts
in. Two kinds: *unmanaged* (no `ownerReferences`, so nothing will recreate it)
and *emptyDir* (its volume dies with it, so the data exists nowhere else). Each
is unlocked by its own option.

**Eligible** — Everything else. Removed with no options set.

**Blocked** — A Protected pod whose unlocking option was not supplied. Any
Blocked pod refuses the whole drain *before* the first eviction, because a
half-drained node is strictly worse than an untouched one: the pods already
gone cannot be recalled, and the node is still not empty. The refusal names
every Blocked pod and every option needed, so one call reveals the full set
rather than one blocker per retry.

## Removal

**Eviction** — Removing a pod through the Eviction API, which honours
PodDisruptionBudgets. The default. A budget that forbids the disruption makes
eviction fail indefinitely, not slowly.

**Deletion** — Removing a pod directly, ignoring PodDisruptionBudgets. What
`disable_eviction` switches to. Distinct from Eviction in *who may say no*: a
budget can refuse an Eviction and cannot refuse a Deletion.

**Grace period** — How long a pod is given to shut down after being told to.
Zero means no graceful shutdown at all; the response reports this as a **forced
deletion** because a pod that traps SIGTERM is killed outright rather than
honoured.

**Still terminating** — A pod whose removal was accepted but which had not gone
when the wait budget ran out. **Not a failure and not a timeout**: nothing
refused the request, and a pod with a long grace period is behaving exactly as
configured. Distinct from Blocked, which means the removal was never attempted.

**Wait budget** — How long a drain watches for pods to disappear before
returning. It bounds the *reporting*, never the *outcome*: spending it changes
what the response says, not what was asked of the cluster.

## Batches

**Per-node failure** — A failure that is a property of one node and is
collected into the batch's results, letting the other nodes proceed.

**Cluster-level failure** — A failure that would repeat identically for every
node (unreachable API server, dead credentials, a certificate that fails TLS —
any failure where no answer about a particular node came back at all).
Propagated as a single error, because reporting it per node would claim N
machines are broken when the real answer is one.

## Access policy

**Access policy** — Rules in `USERS_JSON_PATH` that refuse a write by its
parameters (cluster, node, namespace), independent of scopes. A global layer
for everyone plus an optional layer per user; a user's rules can only tighten
the global ones.

**Policed action** — A write the access policy can refuse, named
`<resource>.<verb>` (`node.drain`, `namespace.create`). Reads are never policed.

**Policy denial** — A batch node the access policy refused: a *per-node
failure* with error code `POLICY_DENIED` and no Kubernetes status, because it
was never sent to Kubernetes. Told apart from other per-node failures by its
error code, not by a status of its own.

## Configuration resources

**ConfigMap** — Non-secret configuration stored in a namespace. Identified by
namespace *and* name together: the same name in two namespaces is two different
ConfigMaps, so a ConfigMap is never addressed by name alone.

**ConfigMap listing** — Says *which* ConfigMaps exist and what shape they have
(names, keys, labels) — never a value. Seeing that a ConfigMap exists reveals
less than seeing what it says, so an overview never carries values.

**ConfigMap content** — The values themselves, for one ConfigMap at a time.
More revealing than a listing, so it is bounded where it matters: the service
can read ConfigMaps only in namespaces its Kubernetes RBAC grants one by one
(least privilege), and any `cluster_api` holder may read what those namespaces
contain. Annotations that carry a copy of the values (an earlier applied
version) are not content: they may disagree with the current values, so they
are left out rather than shown as a second truth.

**Secret** — Deliberately **not** readable through this service. Not "a
ConfigMap with a different kind": its exposure risk is of a different order, so
any future need to read Secrets is a separate design, not an extension of
ConfigMap reading.

## Namespaces

**Namespace** — A cluster-wide partition that ConfigMaps, pods and other
namespaced resources live in. The service can create one and list or read them,
but never delete one: deleting a namespace takes everything inside it with it,
irreversibly, so that is a separate design.

**Reserved namespace name** — A name the service refuses to create: `default`
and anything starting with `kube-`. These belong to Kubernetes itself; the list
is fixed, not configurable.

**Terminating namespace** — A namespace whose deletion was requested and which
is still emptying itself. It can no longer be used and its name cannot be
created again until it is gone. Creating it is refused as already existing, but
reported distinctly from a namespace that is simply there. Not to be confused
with a pod that is **Still terminating** — that is a pod-removal outcome during
a drain; this is the state of a namespace.

**Namespace label** — A key/value tag on a namespace, used to identify and
select it. Can be given when the namespace is created, and later added,
overwritten or removed key by key — never replaced wholesale, because other
parties (controllers, operators) put labels there too and a wholesale replace
would silently erase theirs. Labels on a **Reserved namespace name** cannot be
changed through the service, just as such a namespace cannot be created.

**Namespace annotation** — A key/value note on a namespace whose value is
free-form (unlike a label's): any text, including empty. Can be given when the
namespace is created, and later added, overwritten or removed key by key —
never replaced wholesale, for the same reason as labels. Annotations on a
**Reserved namespace name** cannot be changed through the service. A
**Reserved label key** is equally reserved as an annotation key, since
Kubernetes admission acts on some annotations too (e.g. the scheduler's node
selector). Keys under the **Provenance prefix** are the service's own.

**Reserved label key** — A label key whose prefix is `kubernetes.io`, `k8s.io`,
or a subdomain of either. These are not just tags: Kubernetes acts on them (e.g.
the Pod Security level enforced in the namespace), so setting or removing one
would be a way to change a namespace's security posture rather than describe
it. Refused on create and on update alike, and as an annotation key too; the
list is fixed, not configurable, and a refusal names every offending key at once.

**Provenance prefix** — The domain the service's own annotations live under,
set per deployment. It is the team's internal namespace: no caller may set or
remove a label or an annotation key under it (nor under a subdomain of it),
whatever the key's purpose. Refused on create and on every update, and a
refusal names every offending key at once.

**Provenance annotations** — Annotations the service itself puts on a namespace
it creates, relabels or re-annotates: who (the authenticated account), when, and
which request. They belong to the service; no caller can set or remove them. A
convenience pointer back to the audit trail, **not** an audit record: anyone
with direct cluster access can alter them, and they show only the latest write.
"Created" is recorded once and never overwritten; "last modified" moves with
each label or annotation update. See ADR 0002.

A newly created namespace grants the service **nothing** inside it: in
particular its ConfigMaps are unreadable until someone grants that namespace
explicitly. Creating a namespace is not a way to widen what the service can
read.
