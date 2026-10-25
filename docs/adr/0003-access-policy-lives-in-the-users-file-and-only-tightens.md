# The access policy lives in the users file, and user rules only tighten

Scopes say *which endpoints* a token may call. Some clusters, nodes and
namespaces must not be changed through this service at all — a prod cluster
maintained by another pipeline, control-plane nodes — whoever is asking. That is
a statement about the request's **parameters**, which scopes cannot express.

**Where it lives.** In `USERS_JSON_PATH`, next to the accounts: a top-level
`policies` list for everyone, and a `policies` list per user. One file means one
thing to deploy and review per environment. The file went from a bare list to
`{"policies": [...], "users": [...]}`; the bare list is still read (as "no
policy") so existing deployments keep working unchanged. The whole file —
accounts included — is validated in one pass by one schema, and read by both
repositories through one cached loader, so they can never disagree about it and
a bad account entry is found at load time, not at that user's first login.

**Rules name actions, not URLs.** `node.cordon` covers both the single and the
batch route; a URL pattern would have to list both, and silently stop matching
when a route is renamed. The action list and each action's parameters are fixed
in code (`POLICY_ACTIONS`).

**User rules only tighten.** Per layer, a matching `deny` refuses, and an
`allow` list (if the layer has one for the action) must be matched. The request
passes only when the global layer *and* the user's layer pass. There is no
per-user exemption, admin included: the whole value of a global `deny` is that
it holds for everyone. Someone who must act on a denied cluster does it outside
this service.

**Default open; rules only take permissions away.** With no rule for an
action, or only `deny` rules none of which match, the request passes. This keeps
the policy additive: deploying it with an empty `policies` changes nothing, and
each rule's effect can be read on its own.

**Fail closed.** Everything a typo could turn into a silently inert rule — an
unknown action, a `match` key the action does not have, a bad regex, a duplicate
`id`, an unknown key such as `"polices"` — makes the file invalid, as does a
malformed account (missing field, a plaintext password instead of a bcrypt
hash, a duplicate account). Startup refuses it, and `make check-users` finds
the same problems before a deploy. At runtime an invalid file refuses login and
every policed write (`500 USERS_FILE_INVALID`) until it is fixed. The last good
version is never served in place of a broken one: that would make "I added a
deny" look applied when it is not. Reads are not policed, so a broken file never
takes the read API down for callers already holding a token.

**Full match.** Regexes use `fullmatch`. With `search`, `prod` would also match
`nonprod-1` — a deny that is too broad is annoying, an allow that is too broad
is a hole.

**Where it is checked.** In the router, before `call_kube`, with the values the
request will act on. It is not a middleware (which cannot see a parsed batch
body) and not a dry-run seam: `DRY_RUN_MODE` must leave every decision intact,
and a request-level `drain.dry_run` previews the refusal. A batch reports a refused
node as an ordinary per-node failure with error code `POLICY_DENIED`
(always-200 contract), and never contacts the cluster when every node is
refused.

**No new response shape for batches.** A separate `denied` status and a
`summary.denied` count were tried and dropped in review: they changed the
batch contract, and a client treating `failed == 0` as "all done" would have
read a partly refused batch as a clean one. Counting a denial as a failure
keeps every existing client correct; `error_code` tells the cause apart, as it
already does for `NODE_NOT_FOUND` vs `NODE_NOT_READY`.

**Not done here.** The deploy / command / inventory proxy routes are not
policed: their parameters (pipeline action, command name, host) are
deploy-service's to judge, where its own whitelist already applies.
