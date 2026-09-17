<!-- pair-contract: urn:data-flow-graph:schema:3 -->
<!--
Canonical markdown template for a data-flow lifecycle narration.
One markdown = one lifecycle = one graph file sharing the same basename,
differing only by extension (e.g. upload-ingest.json + upload-ingest.md).
Where the pair lives is decided per project — this template imposes no
location or organization beyond the pairing itself.

Line 1 above is the contract stamp: KEEP it verbatim as the first line of
every filled copy (it pairs with the JSON's root "version" field). Only
guidance comments like the one you are reading get deleted.

Section order is fixed. Sections may be empty only where explicitly allowed;
do not invent new top-level sections. HTML comments like this one are guidance
and must be deleted from the filled-in copy.

Conventions:
- Title MUST match the filename stem: `# <Stem>. <Lifecycle name>` (prefix any
  numbering scheme only if the project's chosen organization uses one).
- Every narrative element cites its graph coordinates (node IDs, seq numbers,
  outcome values) so prose joins back to the JSON deterministically.
- Failure = handled error surfaced to the caller (graph `outcome: "error"`).
  Exception = unhandled escape or mid-flight crash (ties to `dead-end` nodes).
  Anomaly = design-level finding: verified-absent paths, nonsensical or
  prematurely terminating paths (`outcome: "anomaly"`). Keep the three apart.
-->

# <Stem>. <Lifecycle name>

<One-to-three sentence summary of what this lifecycle accomplishes end-to-end.>

- **Entry point:** <method + URL / server function / route loader — with file reference>
- **Trigger:** <the user action or system event that starts the request-response cycle>
- **Termination:** <where the lifecycle ends: sink node(s), dead-end(s), and/or return-to-initiator>
- **Response:**
  - **Success:** <what the caller receives on the happy path>
  - **Failure:** <what the caller receives when a handled error occurs>
  - **Exception:** <what happens when an unhandled escape or crash interrupts the flow>

## Participants

<!-- One row per node in the JSON graph, same order as nodes[].
     Group: integer per the project's documented group legend.
     Role: initiator | intermediary | sink | dead-end.
     Symbol: code identifier from the node's `symbol` field; em-dash if none.
     What: the node's `description`. Location: owning file path. -->

| Node ID | Group | Role | Symbol | What | Location |
| --- | --- | --- | --- | --- | --- |
| `<NODE_ID>` | <int> | <role> | `<symbol>` | <description> | <file path> |

## Sequence

<!--
Happy path only — errors and anomalies get their own sections below.
One numbered step per narrative beat, ordered by seq. Each step opens with the
seq value(s) it corresponds to in the JSON graph: `(seq N)`, `(seq N–M)` for a
range, `(seq N, M)` for scattered steps. Parallel interchangeable steps share
a seq — say so. Cite exact files, functions, types, schemas per hop.
-->

1. (seq <N>) <Step description citing files/symbols/schemas.>
2. (seq <N>) <Step description.>

## Error paths

<!--
One H3 per distinct failure mode (handled errors, `outcome: "error"`).
Each subsection opens with a *Covers* line listing the graph links it describes.
If this lifecycle has no handled-error paths, keep the section with "None."
-->

### <Failure mode name>

_Covers:_ seq <N>, `<SOURCE>` → `<TARGET>` (outcome: error)

<Trigger condition, where it is raised (file:symbol), how it surfaces to the caller/user.>

### <Another failure mode>

_Covers:_ seq <N>

<Describe.>

## Anomalies

<!--
One H3 per anomaly (`outcome: "anomaly"` or `dead-end` role): expected-but-absent
paths, present-but-nonsensical paths, premature terminations. Each must cite code
evidence and explain why it is interesting. If none, keep the section with "None."
-->

### <Anomaly name>

_Covers:_ seq <N>, `<SOURCE>` → `<TARGET>` (outcome: anomaly)

<What was expected or found, code evidence (file:symbol), why it is interesting.>

### <Another anomaly>

_Covers:_ seq <N>

<Describe.>

## Verification

<!--
How the graph structure was confirmed against reality. One bullet per unit of
evidence, shaped as `<path-or-symbol> — <what was confirmed>`. Include tests
that pin behavior, source files read, and any dynamic checks run.
-->

- `<path:symbol>` — <what was confirmed here>
- `<test file>` — <behavior pinned by this test>

## Serialization notes

<!--
Judgment calls ONLY — role assignments, seq ordering decisions, kind mapping
choices. Keep it lean; rationale and findings belong in Anomalies, not here.
Group under short H3s when there is more than one cluster of decisions.
If no judgment calls were needed, keep the section with "None."
-->

### <Decision cluster name>

- <Decision and its justification.>

## References

<!--
Citations in three buckets, in this order. Use relative links for repo files.
-->

### Specs and decisions

- [ADR-XXXX](<path>) — <relevance>
- [<doc>](<path>) §<section> — <relevance>

### Related lifecycles

- [<NN>. <name>](<NN>-<slug>.md) — <what it shares with this lifecycle>

### Code

- `<path:symbol>` — <role in this lifecycle>
