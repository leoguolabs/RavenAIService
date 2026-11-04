## ADDED Requirements

### Requirement: Production-derived local Docker comparison
Acceptance SHALL include at least 20 paired timing rounds using unchanged production log bytes and original questions, with pinned source/model settings and baseline/candidate run identities. Results SHALL report successes, failures, median, P90 and the observed reduction. The objective is to minimize median analysis time subject to unchanged results and confidence, without a fixed percentage threshold. The final candidate SHALL be compared with the frozen original baseline; incremental private probes SHALL NOT substitute for that end-to-end API cohort.

#### Scenario: User revises the experimental target
- **WHEN** the user removes the historical 30% and 20% percentage thresholds
- **THEN** new acceptance seeks the best verified median reduction while retaining the paired-run and quality requirements; previous plans, results and failed quality decisions remain preserved and are not relabeled as successful

#### Scenario: A run fails
- **WHEN** either arm fails or times out
- **THEN** its evidence is retained and the pair is not silently excluded to improve the reported speedup

#### Scenario: Failed runs terminate quickly
- **WHEN** a failed analysis terminates sooner than successful analyses
- **THEN** the report includes its duration in a separate all-terminal timing distribution and reports each arm's failure rate, while successful-pair timing is explicitly labeled and fast failures do not establish a successful optimization

### Requirement: Independent quality verification
After optimization, at least 10 distinct production logs SHALL complete analysis with retained baseline/candidate answers and review of conclusions, citations, attachment coverage and expressed certainty. Material changes or unresolved checks SHALL prevent acceptance.

Reviews SHALL include the project-specific instructions supplied to the Agent as evidence for terminology and documented standard mappings. A documented test-side/code-side board or address mapping SHALL NOT be rejected solely because it was not rediscovered in incident logs. Standard project context SHALL remain distinct from proof of an actual operation, actor, receipt, path binding or deployed binary.

#### Scenario: Faster but incomplete answer
- **WHEN** a candidate omits a decisive finding or increases certainty beyond the evidence
- **THEN** the case fails quality acceptance regardless of runtime

#### Scenario: User permits natural model variation
- **WHEN** independent runs differ in wording, presentation or nondecisive detail under the revised user requirement
- **THEN** review material conclusions, valid evidence and calibrated certainty separately, retain original strict judgments, and report any revised interpretation explicitly rather than treating all textual differences as failure or using shared baseline mistakes as gold answers
- **AND** decisive missed evidence, contradicted factual claims and unsupported causal exclusion still prevent acceptance; baseline variation alone does not turn a demonstrated error into a pass

### Requirement: Durable resumable evidence
Validation SHALL persist input hashes, revision/configuration identifiers, live process/run identities and complete terminal outputs without credentials in Git.

New runs SHALL retain hash-only evidence of configured base/project prompt layers and required Skill/policy files before and after the timed API call. Missing historical before-captures or changed layers SHALL NOT be labeled verified unchanged. Static fingerprints SHALL NOT be described as a complete SDK wire-prompt capture or export private prompt text.

#### Scenario: Observer reconnects
- **WHEN** a monitoring call ends while a run is still live
- **THEN** validation checks that same run rather than launching a duplicate
