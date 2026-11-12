Feature: Oldest appliance
  Scenario: Step 1 - Read the appliance records
    Intent: know the year of each appliance
    When I read "dryer-manual.txt", "fridge-receipt.txt", and "washer-receipt.txt"
    Then the output shows the year for each appliance
  Scenario: Step 2 - Find the oldest
    Intent: determine which appliance is the oldest
    When I compare the years from step 1
    Then I know the oldest appliance
  Scenario: Step 3 - Write the answer
    Intent: write the answer in a file for the user, with the year for each appliance
    When I write the oldest appliance and its year to oldest-appliance.txt
    Then "oldest-appliance.txt" contains the oldest appliance and its corresponding year
