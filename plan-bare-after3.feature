Feature: Organize files by category
  Scenario: Step 1 - List existing files
    When I list the files in the current directory
    Then the output lists "garden-plan.md", "household-budget-2026.csv", "organization.md", and "tax-receipt-2025.txt"
  Scenario: Step 2 - Create category folders
    When I create the folders "Finance", "Garden", "Organization", and "Taxes"
    Then "Finance" exists
    Then "Garden" exists
    Then "Organization" exists
    Then "Taxes" exists
  Scenario: Step 3 - Move files to categories
    When I run: mv garden-plan.md Garden/
    When I run: mv household-budget-2026.csv Finance/
    When I run: mv organization.md Organization/
    When I run: mv tax-receipt-2025.txt Taxes/
    Then "Garden/garden-plan.md" exists
    Then "Finance/household-budget-2026.csv" exists
    Then "Organization/organization.md" exists
    Then "Taxes/tax-receipt-2025.txt" exists
