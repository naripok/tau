# Tau release process

This fork does not publish to PyPI. Releases are git-based: a tag and a
GitHub Release mark a release, and no workflow uploads package artifacts.

## Version source of truth

The package version lives in `pyproject.toml`:

```toml
[project]
version = "0.1.0"
```

A production release starts by intentionally changing that value.

## How to cut a release

1. Choose the next version number.
2. Update `[project].version` in `pyproject.toml` and any checked-in version
   constants that back `tau --version`.
3. Run the release checks locally, for example:

   ```bash
   uv run pytest
   uv run ruff check .
   uv run mypy
   ```

4. Open a PR with the version bump and release notes.
5. Merge the PR to `main` after checks pass.
6. Tag the merge commit with a tag that matches the package version, for
   example `v0.1.1`, and publish a GitHub Release from that tag.

## What runs on ordinary `main` commits

Ordinary commits merged to `main` run the test-suite workflow only. A version
bump merged without a tag creates no release, so package versions stay
meaningful and each release is easy to audit.
