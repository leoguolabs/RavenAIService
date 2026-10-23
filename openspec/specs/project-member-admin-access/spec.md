# project-member-admin-access Specification

## Purpose

Allow non-global-admin users who are members of enabled project repositories to access a constrained project-member admin console, scoped to only the projects they belong to, with backend authorization enforced independently of the frontend.

## Requirements

### Requirement: Project members authenticate as constrained admin principals
The system SHALL allow an authenticated active user who belongs to at least one enabled project repository to access the admin console as a project-member admin. The admin identity response SHALL distinguish global admins from project-member admins and SHALL include the allowed admin navigation keys and allowed project identifiers for project-member admins. A user with no enabled project membership SHALL NOT be admitted to the admin console unless the user is a global admin.

#### Scenario: Project member receives constrained admin identity
- **WHEN** an active user with role `user` and membership in enabled project `alpha` calls `GET /admin/auth/me` with a valid user bearer token
- **THEN** the response has status 200
- **AND** the response identifies `access_level` as `project_member`
- **AND** the response includes only project admin navigation keys and the user's allowed project identifiers

#### Scenario: Global admin receives full admin identity
- **WHEN** a legacy admin token or a user token for `role == "admin"` calls `GET /admin/auth/me`
- **THEN** the response has status 200
- **AND** the response identifies `access_level` as `global_admin`
- **AND** the response includes all admin navigation keys

#### Scenario: User without project membership is rejected
- **WHEN** an active non-admin user with no enabled project memberships calls `GET /admin/auth/me`
- **THEN** the response has status 403

#### Scenario: Unauthenticated admin identity request is rejected
- **WHEN** `GET /admin/auth/me` is called without a bearer token
- **THEN** the response has status 401

### Requirement: Project-member admin console shows only project surfaces
The admin frontend SHALL hide every global admin navigation item for project-member admins. Project-member admins SHALL see only the project repository area and project-specific Skill/settings surfaces for projects they belong to. The `/admin` entry route SHALL send project-member admins to `/admin/project-repos` rather than a global settings page.

#### Scenario: Project member sees only project navigation
- **WHEN** a project-member admin opens the admin console
- **THEN** the visible admin navigation contains the project repository entry
- **AND** the visible admin navigation does not contain prompts, users, releases, agent Skills, model settings, or metrics

#### Scenario: Project member enters from the workbench user menu
- **WHEN** an authenticated project member opens the bottom-left workbench user menu
- **THEN** the menu checks the current user's admin identity and shows the admin console entry
- **AND** selecting the entry sends the user to their scoped project repository area
- **AND** a previously stored legacy admin session does not replace the current user's scope

#### Scenario: Membership changes apply without signing in again
- **WHEN** a signed-in user is added to an enabled project or loses their last enabled project membership
- **AND** the user opens the workbench user menu again
- **THEN** the admin entry reflects the current backend authorization without requiring another login

#### Scenario: Ordinary user does not see the admin entry
- **WHEN** a non-admin user with no enabled project memberships opens the workbench user menu
- **THEN** the admin console entry is hidden

#### Scenario: Project member admin home redirects to projects
- **WHEN** a project-member admin navigates to `/admin`
- **THEN** the frontend routes the user to `/admin/project-repos`

#### Scenario: Direct global admin route navigation is blocked
- **WHEN** a project-member admin directly navigates to `/admin/users`
- **THEN** the frontend SHALL NOT render the users management page
- **AND** any corresponding backend request is rejected as forbidden

### Requirement: Backend authorization enforces project scope independently of the frontend
Every backend admin endpoint that admits project-member admins SHALL verify the requested project resource against the current user's project memberships. Hidden frontend controls SHALL NOT be the only enforcement mechanism. Non-project admin endpoints SHALL require global admin authorization.

#### Scenario: Member accesses own project resource
- **WHEN** a project-member admin requests an allowed admin endpoint for a project they belong to
- **THEN** the endpoint processes the request according to that endpoint's project-member permissions

#### Scenario: Member cannot access another project resource
- **WHEN** a project-member admin requests an allowed project admin endpoint for a project they do not belong to
- **THEN** the response has status 404

#### Scenario: Member cannot access global admin endpoint
- **WHEN** a project-member admin requests a global-only admin endpoint such as `/admin/users`, `/admin/prompts/config`, `/admin/agents`, `/admin/model-settings`, `/admin/metrics/overview`, or `/admin/releases`
- **THEN** the response has status 403
