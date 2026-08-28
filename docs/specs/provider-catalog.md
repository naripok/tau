# provider-catalog

## Purpose

The provider catalog defines which providers and models Tau can offer, how a
provider entry selects a transport, and how the runtime constructs providers
from catalog files and saved provider settings. This spec describes the
current behavior of that subsystem in this fork.

## Requirements

### Requirement: catalog offers only constructible providers

The builtin provider catalog SHALL NOT offer a provider whose transport kind
the runtime cannot construct.

##### Scenario: removed transport leaves the picker

- GIVEN the builtin provider catalog
- WHEN the provider picker lists selectable providers
- THEN every listed provider's transport kind is one the runtime can construct

### Requirement: absent transport kind fails at selection

The runtime SHALL reject configuration that selects an absent transport kind
on each surface that resolves a transport kind. The surfaces are entries in
catalog files and saved provider settings. Selection fails with an
unsupported-kind error before any provider request starts.

##### Scenario: user catalog requests removed transport

- GIVEN a user-authored catalog entry that selects an absent transport kind
- WHEN the runtime selects a provider for that entry
- THEN selection fails with an unsupported-kind error
- AND no provider request is sent

##### Scenario: saved settings request removed transport

- GIVEN saved provider settings that name an absent transport kind
- WHEN the runtime selects a provider from those settings
- THEN selection fails with an unsupported-kind error
- AND no provider request is sent
