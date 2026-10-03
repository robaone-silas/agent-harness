Feature: Organize files by category
  Scenario: Step 1 - List existing files
    When I list the files in the current directory
    Then the list of files contains "garden-plan.md", "household-budget-2026.csv", "organization.md", and "tax-receipt-2025.txt"
  Scenario: Step 2 - Create category structure
    When I write the following content to organization.md
    Then the file "organization.md" contains the text "Categories: Finance, Planning, Admin"
  Scenario: Step 3 - Organize finance files
    When I move "household-budget-2026.csv" and "tax-receipt-2025.txt" into a new file named "Finance"
    Then the file "Finance" exists
    Then the file "Finance" contains the content of "household-budget-2026.csv" and "tax-receipt-2025.txt"
  Scenario: Step 4 - Organize planning files
    When I move "garden-plan.md" into a new file named "Planning"
    Then the file "Planning" exists
    Then the file "Planning" contains the content of "garden-plan.md"
  Scenario: Step 5 - Verify final organization
    When I list the files in the current directory
    Then the list of files contains "Finance", "Planning", "Admin"
