## ADDED Requirements

### Requirement: Self-service password change

The workbench SHALL let authenticated users change their own password by providing their current password and confirming a different new password of 6 to 256 characters.

#### Scenario: Successful password change
- **WHEN** the user submits their correct current password and confirms a valid new password
- **THEN** the system SHALL save a hash of the new password and show a localized success message
- **AND** subsequent logins SHALL accept the new password and reject the old password

#### Scenario: Rejected password change
- **WHEN** the session is absent or invalid, the current password is incorrect, or the new password is invalid
- **THEN** the API SHALL reject the change and preserve the stored credential

#### Scenario: Passwords do not match
- **WHEN** the new password and confirmation differ
- **THEN** the workbench SHALL show an error before sending a request

#### Scenario: Leaving the password form
- **WHEN** the user closes settings or navigates to another settings section
- **THEN** the workbench SHALL clear the entered passwords
