# ADR 0005: moving the approval decision off the device

## Status

**Proposed.** Nothing here is implemented, and this ADR decides nothing on its own — it reopens a
question three previous ADRs each closed with the same sentence and then deferred, and states the
case for answering it rather than deferring it a fourth time.

Reopened as Phase 4.3 of the self-approval review, the phase that follows the four that hardened
what is on the device: enrollment (0), the step-up default (1), session provenance (2), and the
rest of the settings surface (3). Those shipped. This is the one the review names as *"the change
that stops needing them"*, and its own summary of why:

> Everything above is defense in depth on a machine where the adversary has code execution; that is
> the change that stops needing it.

Prior mentions, all of them one line long, all of them deferring:

- [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md), *Out of scope*: "moving the
  approval decision off the device entirely (a paired-phone push is the stronger long-term answer
  and deserves its own issue, not a line in this one)."
- [ADR 0003](0003-separated-installs-only.md), *Out of scope*: "**Moving the approval decision off
  the device.** Still the stronger long-term answer, still its own issue."
- The self-approval review, Phase 4.3.

This ADR is that issue's content. It exists because "its own issue" was written three times and the
issue was not opened, and because the work since ADR 0003 has changed what the question is worth
asking about: the local-mode gate is now as strong as a same-machine gate can be made, which is
exactly the position from which its ceiling is measurable.

## Context

### The threat model local mode actually adopted

ADR 0003 settled it, and every phase since has been reasoned inside it: **the adversary is an agent
with code execution on this machine, running as the signed-in user.** Not a remote attacker, not a
second human at the keyboard. That is the party PrivacyFence governs, which is what makes it
different from most access-control software — the thing being restrained is also the thing running
next to the restraint.

Privilege separation (ADR 0003) puts `authority/` — the policy file, the passkey store, the audit
log and its HMAC key — under a service account, `0700`, out of that party's reach. That is real, and
it is what makes everything built on top of it worth building. But a uid boundary governs file
access, and the decision itself is not a file access. It is an HTTP request to a loopback port that
the agent can reach as easily as a browser can.

### What Phases 0–3 bought, precisely

Each closed a real path, and each is worth keeping regardless of what this ADR proposes:

| | What it closed | What it rests on |
|---|---|---|
| 0 | A session could enroll a passkey of its own and then satisfy every step-up check with it | A fresh assertion with a credential already on file, or, for the first, a companion confirmation |
| 1 | `require_passkey` was off by default on the installs ADR 0003 makes separated | `paths.is_bundled()` |
| 2 | Three ways to a session were interchangeable; one was audited and two were silent | A session's provenance, and the companion's ability to attest one |
| 3 | `org_config_upload` and `toggle_connector` sat outside the sensitive-action net | Route-level classification, enforced at startup |
| 4.1 | A bridge-proposed auto-accept rule could be confirmed by the session that asked for it | The same two checks the Settings page already applies |

Read down the right-hand column and one thing recurs. Phases 0, 2 and, through provenance, 4.1 all
rest on **the companion being distinguishable from the agent**, and `security-and-compliance.md`
already says, in its own words, that it is not:

> **What this is not: authentication of the companion.** Companion and agent share an OS user, and
> the companion's own channel lives under `handoff_dir()`, which `paths.py` states outright is
> "deliberately *not* a security boundary" — so a local process running as that user can bind that
> address before the companion does and answer the daemon's call-back itself.

That is an honest statement of a real limit, and the bound it offers is a real bound: winning the
race means starting before the companion at login and staying there, which breaks the connector
OAuth flows sharing that address and leaves a human staring at a missing tray icon. It raises the
cost and makes the attempt visible. It is not a boundary, and no amount of further work on the same
side of the uid line turns it into one.

### The ceiling

State it plainly, because the value of this ADR is in the sentence and not in anything it proposes:

> On a machine where the adversary has code execution as the signed-in user, every proof the human
> can give is a proof the adversary can be present for.

A passkey assertion is the strongest thing in the current design, and it is the clearest example.
The authenticator proves a human touched a sensor. It does not prove *which* request that human
believed they were touching it for. The binding between the two — `webauthn_stepup.
decision_fingerprint`, which ties the assertion to an approval id, a result and a choice — is
computed and checked by the daemon and rendered for the human by a browser on the same machine. The
service account owns the check. Nothing owns the rendering. An adversary with code execution as the
user does not need to defeat the assertion; it needs the human to be looking at a different sentence
than the one being signed, which is a UI problem on a machine it controls.

Nothing in Phases 0–4 claims otherwise, and nothing in them is weakened by saying so. They are
defense in depth, correctly built and correctly described. Depth is not a boundary either.

### What org mode already demonstrates

Org mode is not a different product here; it is the same daemon with the decision already somewhere
else. The agent runs on the employee's laptop, the daemon runs on a server the employee has no
account on, and the approval is rendered by a surface behind an IdP. `authority/` is not merely
`0700` from the agent — it is on a different machine.

That is why ADR 0002 decision 6 could state the integrity guarantee for org mode without hedging,
and why every hedge this review has been closing is local mode's. The two modes are not two threat
models. They are one threat model with the decision on different sides of a network boundary.

Local mode cannot have a server. The question this ADR reopens is whether it can have the boundary
anyway.

## The decision to be made

**Should local mode's approving decision be rendered and answered on a device the agent does not
run on?**

Not "should there be a phone app". The load-bearing property is narrower and worth naming
separately from any product that might carry it:

> **The text the human reads, and the act by which they answer it, are produced and consumed
> somewhere the governed agent cannot execute code.**

Anything with that property qualifies. Anything without it — however good — is another layer on the
same side of the line.

## Options

### A. Paired-phone push (ADR 0002's own suggestion)

The daemon renders the approval to a push notification on a phone the human paired once. The phone
displays the summary and returns a signed decision over a channel the laptop does not mediate.

- **Has the property.** The agent has no code execution on the phone, and the rendering it would
  have to corrupt happens there.
- **Costs a server.** Push requires APNs/FCM, which requires a service PrivacyFence would have to
  run — a standing operational and privacy commitment for a product whose local mode's entire pitch
  is that nothing leaves the machine.
- **And it collides with what an approval has to say.** This is the objection to take most
  seriously, because it is not an implementation detail. A decision the human can actually make
  needs the content of the card: who the recipient is, which file, what the message says. A push
  that says "1 pending" is a notification, not an approval. So the phone has to be shown the thing
  PrivacyFence exists to stop leaving the machine, and with A that content crosses a third party's
  push infrastructure to get there. `docs/approval-list-ui-ux.md`'s own Notifications section
  already meets a smaller version of this and answers it by *disclosure* rather than by
  restriction — `detailed` may carry the approval summary, and the doc says outright that operators
  should treat it as sensitive content reaching the OS notification surface. That is a defensible
  answer for a notification. It is a much harder one for a channel that must carry the summary by
  construction. End-to-end encryption between daemon and paired device, with the push service
  carrying ciphertext and a wake signal only, is the shape of the answer; it is also more design
  than the rest of option A put together.
- **Pairing is the hard part**, and it is the same problem one layer down: a pairing performed from
  the laptop is a pairing the agent can be present for. It has to be bootstrapped from a channel the
  agent does not control, or accept that its first moment is unprotected.
- **Offline is a real state**, not an edge case. A phone that is dead, absent or off-network must
  fail closed, and a product that fails closed on a commute is a product people turn off.

### B. Local network second device, no push service

The same, minus the push service: the second device reaches the daemon over the LAN, paired out of
band (a QR code the daemon renders and the phone's camera reads, which is a channel a local process
cannot forge without controlling the screen — and if it controls the screen, option A has the same
problem).

- **Has the property**, on the same reasoning as A.
- **No server, no third party, nothing leaves the network.** Which dissolves A's hardest problem
  rather than solving it: the card's content reaches the second device over the same LAN the
  laptop is already on, so "the human must see enough to decide" stops trading against "nothing
  leaves the machine" in any sense a user would object to. Much better fit for local mode's own
  promise, and the reason this is the recommendation.
- **Discovery and reachability are the cost.** Two devices on one network is an assumption that
  breaks on guest wifi, on client isolation, and on a laptop tethered to the same phone.
- **A dead phone is still a dead gate**, same as A.

### C. A hardware authenticator that displays the decision

A security key with a display, which renders the transaction text itself and signs what it showed.
The property holds by construction — the rendering happens on the key.

- **Exactly the right shape, and almost nobody has one.** Transaction confirmation is specified
  (WebAuthn's `txAuthSimple` extension was removed from the spec, and its successors are not widely
  implemented); platform authenticators — Touch ID, Windows Hello — cannot do it, and they are what
  `step_up.require_passkey` actually runs against today.
- Worth stating as the reference shape the other options approximate, not as a shippable answer.

### D. Do nothing further; keep deepening the local defense

The honest baseline, and not a straw man: Phases 0–4 measurably raised the cost, the remaining
attacks are loud, and a second device is a real usability tax paid by every user against an attacker
most of them will never have.

- **The cost of choosing it is a documentation cost, and it comes due immediately.** The product's
  central claim — ADR 0002 decision 6's "the agent cannot approve its own request" — would keep
  being true only in the bounded, cost-raising sense this ADR's *Ceiling* section describes.
  Choosing D means saying so where the claim is made, in the same voice ADR 0003 used when it
  refused to ship two postures under one name.

## Recommendation

**Pursue B, with A as a later addition rather than a prerequisite; do not treat C as shippable; and
if any of this slips, take D explicitly and write the sentence D costs.**

B first because it has the property, needs no service PrivacyFence must run, and does not ask local
mode to start sending anything anywhere — which is the objection that would otherwise have to be
answered before the security argument even gets heard. A is a strictly better experience and a
strictly larger commitment; it is the right second step and the wrong first one.

What would have to be true before this could be Accepted rather than Proposed, in the order it
should be found out:

1. **The pairing bootstrap.** Until there is an answer to "what channel pairs the second device that
   the agent cannot be present for", nothing else matters, because the whole design inherits
   whatever that moment is worth. A QR code rendered on the laptop's own screen is the obvious
   candidate and needs its own adversarial pass, not an assumption.
2. **The fail-closed behavior.** What a pending approval does when the second device is unreachable,
   and what the fallback is, and whether that fallback is a way back to exactly the local decision
   this replaces. If it is, the design is decorative.
3. **What crosses the wire, and who can read it.** Decided up front rather than discovered. For B
   the honest answer is "the card, over the LAN, encrypted to the paired device"; for A it is the
   same plus a push service that must never be able to read it.
4. **Whether `step_up` survives alongside it, or is replaced by it.** Two gates that both ask for a
   human are two habituation surfaces; the plausible answer is that the second device *is* the
   step-up for the scopes it covers, and `require_passkey` stays for the installs that do not pair
   one.

## Consequences

**If B (or A) is adopted:**

- ADR 0002 decision 6's integrity guarantee becomes true of local mode in the same unqualified sense
  it is already true of org mode, and `security-and-compliance.md` loses the hedges this review has
  spent four phases making accurate rather than removing.
- The companion's load-bearing role shrinks to what it can actually carry. It stays the thing that
  runs where a human is, and stops being the thing that stands in for proof that one is there.
- Phases 0–3 do not become dead code. An install with no paired device keeps exactly what it has
  today, which is the bounded guarantee, honestly described.

**If D is chosen instead:**

- Say it in the ADR that supersedes this one, and say it in `security-and-compliance.md` and in
  ADR 0002 decision 6's own text, at the same volume the claim is made. The failure this whole
  review exists to prevent is a product that claims a guarantee it cannot keep; a deferral written
  down once in an *Out of scope* bullet and never reflected where users read the claim is how that
  happens by accident.

**Either way, this ADR stops the fourth deferral.** The question has an answer in this file now,
even if the answer turns out to be D.

## Out of scope

- **Org mode**, which already has the property and needs nothing from this.
- **Any change to Phases 0–4.** They are the right work regardless of which option wins, and every
  one of them is what an install with no second device falls back to.
- **A specific transport, framing or pairing protocol.** This ADR argues that the property is worth
  paying for and that B is the cheapest way to get it. What it is built out of belongs to whatever
  supersedes this.

## Verification

Not applicable while this is *Proposed*. The ADR that accepts an option carries its own
verification; the one test that belongs to *this* file is the one that decides whether the option
was ever really taken:

- An adversary with code execution as the signed-in user, given the approval id and a live session,
  cannot produce a released write — **without relying on any process that shares that OS user being
  told apart from it.** Everything shipped today fails this test by design, and says so. Anything
  proposed to answer this ADR that also fails it has not answered it.

## Related

- [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) — the trust boundary this would
  move, and decision 6's integrity claim; its *Out of scope* is the first deferral
- [ADR 0003](0003-separated-installs-only.md) — the uid boundary this sits above, and the second
  deferral
- `docs/security-and-compliance.md` — "A session is not a human", "Enrolling a passkey is itself
  gated", "A confirmation dialog is not always a second step": the three places the current bound is
  stated in the product's own words
- `docs/approval-list-ui-ux.md`, "Notifications" — the detail levels, and the smaller version of
  the same "how much may the second surface say" question this would have to answer at full size
