## ADDED Requirements

### Requirement: Production-derived local Docker comparison
Acceptance SHALL include at least 20 paired timing rounds using unchanged production log bytes and original questions, with pinned source/model settings and baseline/candidate run identities. Results SHALL report successes, failures, median and P90, and the current experimental 20% median-reduction target separately from observed reduction. The final candidate SHALL be compared with the frozen original baseline; incremental private probes SHALL NOT substitute for that end-to-end API cohort.

#### Scenario: User revises the experimental target
- **WHEN** the target changes from the historical 30% to the current 20%
- **THEN** new acceptance uses 20%, while previous plans, results and failed quality decisions remain preserved and are not relabeled as successful

#### Scenario: A run fails
- **WHEN** either arm fails or times out
- **THEN** its evidence is retained and the pair is not silently excluded to improve the reported speedup

#### Scenario: Failed runs terminate quickly
- **WHEN** a failed analysis terminates sooner than successful analyses
- **THEN** the report includes its duration in a separate all-terminal timing distribution and reports each arm's failure rate, while successful-pair timing is explicitly labeled and no failing cohort passes the timing target

### Requirement: Independent quality verification
After optimization, at least 10 distinct production logs SHALL complete analysis with retained baseline/candidate answers and review of conclusions, citations, attachment coverage and expressed certainty. Material changes or unresolved checks SHALL prevent acceptance.

#### Scenario: Faster but incomplete answer
- **WHEN** a candidate omits a decisive finding or increases certainty beyond the evidence
- **THEN** the case fails quality acceptance regardless of runtime

### Requirement: Durable resumable evidence
Validation SHALL persist input hashes, revision/configuration identifiers, live process/run identities and complete terminal outputs without credentials in Git.

#### Scenario: Observer reconnects
- **WHEN** a monitoring call ends while a run is still live
- **THEN** validation checks that same run rather than launching a duplicate
