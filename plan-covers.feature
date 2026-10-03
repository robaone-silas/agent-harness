Feature: Build an index of workspace files
  Scenario: Step 1 - List the files in the workspace
    When I list the files in the workspace
    Then the list is recorded
  Scenario: Step 2 - Read every file
    When I read the full contents of every file from step 1
    Then the contents are recorded
  Scenario: Step 3 - Write the index
    When I write index.md with one entry per file from step 1: the file name and a one-line description of what it contains, using the contents recorded in step 2
    Then "index.md" covers the files from step 1
    Then "index.md" exists
