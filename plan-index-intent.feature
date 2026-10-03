Feature: Build file index
  Scenario: Step 1 - Read all files to gather content
    Intent: read each file to gather content for the index
    When I read the content of "belize-trip-ideas.txt", "furnace-warranty.txt", "garden-invoice.csv", "meeting-notes-2026-09-14.txt", "reading-list.md", and "recipe-chili.txt"
    Then the content of the files is available for indexing
  Scenario: Step 2 - Create the index file
    Intent: create index.md listing every file with its name and a one-line description of what it actually contains
    When I create "index.md" listing all files with their names and one-line descriptions based on their content
    Then "index.md" contains entries for "belize-trip-ideas.txt", "furnace-warranty.txt", "garden-invoice.csv", "meeting-notes-2026-09-14.txt", "reading-list.md", and "recipe-chili.txt"
