# fork-maintenance

## Purpose

This spec records the repository-level contract that keeps the fork's
continuous integration meaningful: one workflow, running the test suite,
with no references to content that no longer exists.

## Requirements

### Requirement: exactly one CI workflow remains

The repository SHALL keep exactly one GitHub Actions workflow. That workflow
SHALL run the test suite, and no job in it SHALL reference removed repository
content.

##### Scenario: workflow inventory

- GIVEN the workflow directory
- WHEN the workflow files are listed
- THEN exactly one workflow remains
- AND its steps invoke the project test suite

##### Scenario: no dead job in the workflow

- GIVEN the single remaining workflow
- WHEN each job in the workflow is inspected
- THEN no job references removed repository content
