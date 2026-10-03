Feature: Build an index of workspace files
  Scenario: Step 1 - Read and index file contents
    When I read the content of "belize-trip-ideas.txt" and add it to index.md
    Then "index.md" contains the file "belize-trip-ideas.txt" and its content
  Scenario: Step 2 - Read and index file contents
    When I read the content of "furnace-warranty.txt" and add it to index.md
    Then "index.md" contains the file "furnace-warranty.txt" and its content
  Scenario: Step 3 - Read and index file contents
    When I read the content of "garden-invoice.csv" and add it to index.md
    Then "index.md" contains the file "garden-invoice.csv" and its content
  Scenario: Step 4 - Read and index file contents
    When I read the content of "meeting-notes-2026-09-14.txt" and add it to index.md
    Then "index.md" contains the file "meeting-notes-2026-09-14.txt" and its content
  Scenario: Step 5 - Read and index file contents
    When I read the content of "reading-list.md" and add it to index.md
    Then "index.md" contains the file "reading-list.md" and its content
  Scenario: Step 6 - Read and index file contents
    When I read the content of "recipe-chili.txt" and add it to index.md
    Then "index.md" contains the file "recipe-chili.txt" and its content
