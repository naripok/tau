# Spec: Fork Slimming

This change removes runtime behavior the maintainer does not use, and it
keeps exactly one CI workflow, which runs the test suite. The proposal
defines the full removed set, including its structural repository content.
The domains below cover the runtime delta and the CI rule.

## Domain: provider-catalog

### REMOVED Requirements

#### Requirement: mistral-conversations transport

The runtime SHALL NOT construct a provider for the Mistral conversations
transport kind.

The runtime supported a Mistral conversations transport. Catalog entries
selected it, and the runtime constructed a provider for it. Removal follows
from the maintainer's provider set: no Mistral account or model is in use.
This requirement is verified by the following named scenarios under the
ADDED requirements of this domain: removed transport leaves the picker, user
catalog requests removed transport, saved settings request removed transport.

#### Requirement: GitHub Copilot provider support

The system SHALL NOT offer GitHub Copilot as an OAuth login option. The
builtin provider catalog SHALL NOT include a GitHub Copilot provider entry.
The transports SHALL NOT set GitHub Copilot specific request headers.

The system offered GitHub Copilot as an OAuth login option with a catalog
entry, and the transports set a Copilot specific vision header for it.
Removal follows from the maintainer's provider set. The generic device-code
protocol surface stays, because it is a provider-independent extension point.

##### Scenario: copilot login option is gone

- GIVEN the OAuth login options after this change
- WHEN the login options are listed
- THEN no GitHub Copilot option appears

##### Scenario: copilot entry leaves the picker

- GIVEN the builtin provider catalog after this change
- WHEN the provider picker lists selectable providers
- THEN the GitHub Copilot provider entry does not appear

##### Scenario: copilot request headers are gone

- GIVEN a provider request with image input for a provider identified as
  GitHub Copilot over a kept transport
- WHEN the transport builds the request headers
- THEN no GitHub Copilot specific header is set

### ADDED Requirements

#### Requirement: catalog offers only constructible providers

The builtin provider catalog SHALL NOT offer a provider whose transport kind
the runtime cannot construct.

##### Scenario: removed transport leaves the picker

- GIVEN the builtin provider catalog after this change
- WHEN the provider picker lists selectable providers
- THEN every listed provider's transport kind is one the runtime can
  construct
- AND no provider built on the removed Mistral transport appears

#### Requirement: absent transport kind fails at selection

The runtime SHALL reject configuration that selects an absent transport kind
on each surface that resolves a transport kind. The surfaces are entries in
catalog files and saved provider settings. Selection fails with an
unsupported-kind error before any provider request starts.

##### Scenario: user catalog requests removed transport

- GIVEN a user-authored catalog entry that selects the removed Mistral
  transport
- WHEN the runtime selects a provider for that entry
- THEN selection fails with an unsupported-kind error
- AND no provider request is sent

##### Scenario: saved settings request removed transport

- GIVEN saved provider settings that name the removed Mistral transport kind
- WHEN the runtime selects a provider from those settings
- THEN selection fails with an unsupported-kind error
- AND no provider request is sent

## Domain: frontends

### REMOVED Requirements

#### Requirement: RPC frontend mode

The CLI SHALL NOT accept an RPC frontend mode.

The CLI offered a headless JSONL RPC mode. An external frontend drove a
session through it. The maintainer drives sessions through the TUI and the
in-process extension API only, so the mode is dead weight.

##### Scenario: rpc mode is rejected

- GIVEN the CLI
- WHEN the user passes `--mode rpc`
- THEN the CLI rejects the value with a usage error
- AND the error names the modes that remain

## Domain: self-update

### REMOVED Requirements

#### Requirement: tau update command

The CLI SHALL NOT provide an update command.

The CLI shipped an `update` command that ran the self-updater. The
maintainer updates the fork through git, so the command is dead weight.

##### Scenario: update command is unknown

- GIVEN the CLI
- WHEN the user runs `tau update`
- THEN the CLI reports an unknown command

#### Requirement: startup update check

Startup SHALL NOT fetch upstream release metadata, and it SHALL NOT show
update or release-notes notices.

At startup, the CLI fetched upstream release metadata and showed update
notices. The same module showed packaged release-notes notices at startup.
Removal deletes both behaviors and the startup network call the maintainer
does not want. The startup scenarios assert the stricter rule of zero
outbound network requests during startup.

##### Scenario: no startup notice in the TUI

- GIVEN the CLI after this change
- WHEN the user starts a TUI session
- THEN no update or release-notes notice appears before the first user input
- AND no outbound network request occurs during startup

##### Scenario: no startup notice in print mode

- GIVEN the CLI after this change
- WHEN the user starts a print-mode session
- THEN no update or release-notes notice appears in the session output
- AND no outbound network request occurs during startup

## Domain: fork-maintenance

This domain states the CI rule for the repository after this change.

### ADDED Requirements

#### Requirement: exactly one CI workflow remains

The repository SHALL keep exactly one GitHub Actions workflow. That workflow
SHALL run the test suite, and no job in it SHALL reference removed repository
content.

##### Scenario: workflow inventory

- GIVEN the workflow directory after this change
- WHEN the workflow files are listed
- THEN exactly one workflow remains
- AND its steps invoke the project test suite

##### Scenario: no dead job in the workflow

- GIVEN the single remaining workflow after this change
- WHEN each job in the workflow is inspected
- THEN no job references removed repository content as defined in the
  proposal
