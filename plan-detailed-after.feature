Feature: Organize workspace files
  Scenario: Step 1 - Read all files
    When I read "garden-plan.md"
    When I read "household-budget-2026.csv"
    When I read "organization.md"
    When I read "tax-receipt-2025.txt"
    Then the contents of "organization.md" cover the files from step 1
  Scenario: Step 2 - Devise and save organization strategy
    When I devise an organization strategy based on the contents of "garden-plan.md", "household-budget-2026.csv", "organization.md", and "tax-receipt-2025.txt" and save it to "organization.md"
    Then "organization.md" contains an organization strategy that names the actual files, groups them in a way that makes sense for what they are, and says what goes where
