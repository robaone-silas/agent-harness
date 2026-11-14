Feature: Slice 3 verification of November 2, 2026 daily failures (issue 37)
  Verify the Slice 1 (Maya index completeness) and Slice 2 (Tomas requirement retention)
  fixes against the exact November 2 fixtures and failure shapes.

  @deterministic @tomas
  Scenario: Plan-time narrowing on the November 2 Tomas plan triggers plan_narrowing warning
    Given the exact November 2 plan for Tomas
    When plan lint runs on it for the Tomas task
    Then lint reports a "plan_narrowing" warning on the compile step
    And the warning detail names the dropped requirement

  @deterministic @tomas
  Scenario: The 2026-10-18 narrowed Tomas plan triggers plan_narrowing warning
    Given the Tomas plan "narrowed" from the 2026-10-18 field shape
    When plan lint runs on it for the Tomas task
    Then lint reports a "plan_narrowing" warning on the compile step

  @deterministic @tomas
  Scenario: A Tomas plan carrying the full requirement into step 3 passes lint without plan_narrowing
    Given a Tomas plan that carries all appliance years into the final step
    When plan lint runs on it for the Tomas task
    Then lint reports no "plan_narrowing" finding

  @deterministic @tomas
  Scenario: Exact November 2 Tomas deliverable preserves all three appliance years
    Given a Tomas workspace whose "oldest-appliance.txt" preserves all appliance years alongside the oldest
    When the deliverable "oldest-appliance.txt" is inspected for completeness
    Then the deliverable contains "2016", "2018", and "2021"
    And the deliverable identifies "Dryer" as the oldest appliance

  @deterministic @maya
  Scenario: Maya phrasing variants for index completeness parse to machine verifiers
    Given the expanded phrasing variants for document index completeness
    Then each variant parses to "indexes"

  @deterministic @maya
  Scenario: Exact November 2 Maya workspace verifies index completeness across all three documents
    Given the exact November 2 workspace for Maya
    When the indexing verifier checks "docs-index.md" for the documents from step 1
    Then verification passes and confirms "api-notes.md", "deploy.md", and "setup.md" are all indexed

  @deterministic @nov2_trace
  Scenario: Trace the exact November 2 artifacts against current harness contracts
    Given the November 2 discovery findings for Tomas
    Then discovery knew all three appliance years
    Given the exact November 2 plan for Tomas
    Then step 1 keeps the full requirement, one year for each appliance
    And step 2 narrows the requirement to the oldest appliance only
    And step 3 triggers a plan_narrowing lint finding
    Given the exact November 2 plan for Maya
    Then step 2 Then is the exact November 2 wording and is flagged as an unparsed completeness clause

  @live @tomas
  Scenario Outline: Tomas oldest appliance live verification retains all three years (trial <trial>)
    Given a fresh copy of the "tomas" fixture for trial <trial>
    When the harness proposes a plan with discovery for the Tomas task
    And the approved plan is executed
    Then the run reports done
    And the deliverable contains "2016", "2018", and "2021"

    Examples:
      | trial |
      | 1     |
      | 2     |
      | 3     |

  @live @maya
  Scenario Outline: Maya docs index live verification carries a machine-checked completeness clause (trial <trial>)
    Given a fresh copy of the "maya" fixture for trial <trial>
    When the harness proposes a plan with discovery for the Maya task
    Then the deliverable step carries a machine-checked completeness clause

    Examples:
      | trial |
      | 1     |
      | 2     |
      | 3     |
