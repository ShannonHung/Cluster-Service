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
node (unreachable API server, dead credentials). Propagated as a single error,
because reporting it per node would claim N machines are broken when the real
answer is one.

## Configuration resources

**ConfigMap** — Non-secret configuration stored in a namespace. Identified by
namespace *and* name together: the same name in two namespaces is two different
ConfigMaps, so a ConfigMap is never addressed by name alone.

**ConfigMap listing** — Says *which* ConfigMaps exist and what shape they have
(names, keys, labels) — never a value. Seeing that a ConfigMap exists is a
lower privilege than seeing what it says.

**ConfigMap content** — The values themselves, for one ConfigMap at a time.
A separate, higher privilege than listing. Annotations that carry a copy of the
values (an earlier applied version) are not content: they may disagree with
the current values, so they are left out rather than shown as a second truth.

**Secret** — Deliberately **not** readable through this service. Not "a
ConfigMap with a different kind": its exposure risk is of a different order, so
any future need to read Secrets is a separate design, not an extension of
ConfigMap reading.
