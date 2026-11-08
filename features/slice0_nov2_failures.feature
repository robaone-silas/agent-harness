Feature: Slice 0 replication of the November 2, 2026 daily failures (issue 37)
  Replay the exact November 2 failures against the branch code with gemma4:e2b before any fix is built.

  @deterministic @tomas
  Scenario: Plan-time narrowing passes lint when the Intent promises every year but When and Then require one
    Given the Tomas plan "narrowed" from the 2026-10-18 field shape
    When plan lint runs on it for the Tomas task
    Then lint reports no finding that names the dropped years
    And the attested_compile warning is the only signal on the compile step

  @deterministic @tomas
  Scenario: An attested Then accepts a deliverable that shrank to one year
    Given a Tomas workspace whose "appliance-years.txt" holds only "The oldest appliance is the Dryer, installed in 2016."
    When the step 2 Then "\"appliance-years.txt\" contains the year for each appliance and names the oldest" is verified
    Then verification passes on attestation alone

  @deterministic @maya
  Scenario: A docs index plan with a prose completeness Then has no machine check
    Given the Maya plan "prose_index"
    When plan lint runs on it for the Maya task
    Then no Then clause on the deliverable step parses to indexes, accounts_for, or covers

  @deterministic @maya
  Scenario: The exact November 2 Maya plan carries a Then no verifier parses
    Given the exact November 2 plan for Maya
    Then step 2 Then is the exact November 2 wording and parses to no verifier

  @deterministic @tomas
  Scenario: The exact November 2 Tomas plan narrows the requirement at step 2
    Given the November 2 discovery findings for Tomas
    Then discovery knew all three appliance years
    Given the exact November 2 plan for Tomas
    Then step 1 keeps the full requirement, one year for each appliance
    And step 2 narrows the requirement to the oldest appliance only
    And step 3 requires only the dryer and the year 2016

  @live @tomas
  Scenario Outline: Tomas oldest appliance, discovery plan then execution (trial <trial>)
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
  Scenario Outline: Maya docs index, discovery plan carries a machine-checkable completeness check (trial <trial>)
    Given a fresh copy of the "maya" fixture for trial <trial>
    When the harness proposes a plan with discovery for the Maya task
    Then the deliverable step carries a machine-checked completeness clause

    Examples:
      | trial |
      | 1     |
      | 2     |
      | 3     |
