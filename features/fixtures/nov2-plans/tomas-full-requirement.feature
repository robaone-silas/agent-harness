Feature: Appliance age determination
  Scenario: Step 1 - Extract installation years
    Intent: read the installation or purchase year for each appliance
    When I read "dryer-manual.txt", "fridge-receipt.txt", and "washer-receipt.txt"
    Then the output shows the installation year for each appliance
  Scenario: Step 2 - Determine the oldest appliance
    Intent: identify the appliance with the minimum installation year and keep all years
    When I compare the installation years from step 1
    Then the output identifies the dryer as the oldest appliance
  Scenario: Step 3 - Write the result
    Intent: write the oldest appliance and its year to a file, with the year for each appliance
    When I write "oldest-appliance.txt" with the oldest appliance and the year for each appliance
    Then "oldest-appliance.txt" contains the dryer, the oldest year 2016, and the year for each appliance
