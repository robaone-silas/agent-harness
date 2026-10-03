Feature: File Organization by Category
  Scenario: Step 1 - Create necessary directories
    When I create the directory "Planning" and "Finance" and "Documents"
    Then "Planning" exists
    Then "Finance" exists
    Then "Documents" exists
  Scenario: Step 2 - Move garden plan
    When I move "garden-plan.md" into "Planning/"
    Then "Planning/garden-plan.md" exists
  Scenario: Step 3 - Move household budget
    When I move "household-budget-2026.csv" into "Finance/"
    Then "Finance/household-budget-2026.csv" exists
  Scenario: Step 4 - Move tax receipt
    When I move "tax-receipt-2025.txt" into "Documents/"
    Then "Documents/tax-receipt-2025.txt" exists
