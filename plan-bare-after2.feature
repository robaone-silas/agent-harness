Feature: Organize files by category
  Scenario: Step 1 - List existing files
    When I list the files in the current folder
    Then the output lists "garden-plan.md", "household-budget-2026.csv", "organization.md", and "tax-receipt-2025.txt"
  Scenario: Step 2 - Create category folders
    When I create the folder "Garden"
    When I create the folder "Finance"
    When I create the folder "Taxes"
    When I create the folder "Organization"
    Then "Garden" exists
    Then "Finance" exists
    Then "Taxes" exists
    Then "Organization" exists
  Scenario: Step 3 - Move garden file
    When I run: mv garden-plan.md Garden/
    Then "Garden/garden-plan.md" exists
  Scenario: Step 4 - Move finance file
    When I run: mv household-budget-2026.csv Finance/
    Then "Finance/household-budget-2026.csv" exists
  Scenario: Step 5 - Move tax file
    When I run: mv tax-receipt-2025.txt Taxes/
    Then "Taxes/tax-receipt-2025.txt" exists
  Scenario: Step 6 - Verify final organization
    When I list the files in the current folder
    Then the output lists "Garden", "Finance", "Taxes", and "Organization"
    Then "Garden/garden-plan.md" exists
    Then "Finance/household-budget-2026.csv" exists
    Then "Taxes/tax-receipt-2025.txt" exists
    Then "Organization/organization.md" exists
