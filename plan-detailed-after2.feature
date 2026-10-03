Feature: Devise Organization Strategy
  Scenario: Step 1 - List existing files
    When I list the files in the folder
    Then the output lists "garden-plan.md", "household-budget-2026.csv", "organization.md", and "tax-receipt-2025.txt"
  Scenario: Step 2 - Devise organization strategy
    When I devise an organization strategy based on the listed files
    Then the strategy groups files by category (Gardening, Finance, Tax, Organization)
  Scenario: Step 3 - Save the strategy
    When I write the organization strategy into "organization.md"
    Then "organization.md" contains the devised organization strategy
