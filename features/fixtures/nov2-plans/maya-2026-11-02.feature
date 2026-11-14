Feature: Document Indexing
  Scenario: Step 1 - List workspace documents
    Intent: know exactly which documents the index must cover
    When I list the files in the workspace
    Then the output lists api-notes.md, deploy.md, and setup.md
  Scenario: Step 2 - Create the document index
    Intent: create docs-index.md with one entry per document: the exact filename and a one-line description of what the document is about, without changing the source documents
    When I read api-notes.md, deploy.md, and setup.md and write docs-index.md
    Then "docs-index.md" has one entry for api-notes.md and one entry for deploy.md and one entry for setup.md, where each entry contains the exact filename and a one-line description of what the document is about
