# Independent Dataflows Package Design

## Goal

Turn the repository's market-data adapters into an independently installable
Python subproject without changing the `czsc-trader` command interface or any
research behavior. The package remains in this repository today but can be
moved to its own repository later without changing its public imports.

## Repository Layout

```text
packages/
└── dataflows/
    ├── pyproject.toml
    ├── README.md
    ├── src/
    │   └── dataflows/
    │       └── *.py
    └── tests/
src/
└── czsc_trader/
```

`dataflows` is a top-level Python package, not part of the
`czsc_trader` namespace. The root project consumes it through a local path
dependency. No runtime Python files remain in a repository-root `dataflows/`
directory.

## Package Boundary

The `dataflows` package owns market resolution, Tushare access, bar formatting,
and indicator helpers. It must not import `czsc_trader`. The application owns
repository discovery, command-line behavior, raw-data persistence, validation,
and research workflows.

Application code imports adapters through stable names such as
`dataflows.tushare_etf`. This import path will remain valid if the package is
later published privately or moved to another Git repository.

## Dependencies and Installation

`packages/dataflows/pyproject.toml` declares only dependencies needed by the
adapter library. The root `pyproject.toml` declares the independent distribution
as a required dependency. A documented, cross-device bootstrap command installs
the local subproject and root project together; it must not embed an absolute
checkout path in project metadata. The installed `czsc-trader` entry point must
work outside the checkout without relying on the repository root in `PYTHONPATH`.

## Configuration

The ignored credential file moves from `dataflows/.env` to the repository-root
`.env`; its content must never be printed or committed. `.env.example` also
moves to the repository root. The application passes the exact repository-local
credential path to the adapter package. A process-level `TUSHARE_TOKEN` retains
precedence.

Package code accepts an explicit credential path and otherwise uses environment
variables. It does not assume that it is running from this repository.

## Documentation and Compatibility

The former `dataflows/README.md` becomes package documentation at
`packages/dataflows/README.md`. Dataflow-specific requirement notes are folded
into the subproject metadata. Root and handoff documentation explain the
package boundary and the standard installation command.

The public `czsc-trader` command tree, raw-data schema, frozen strategies,
experiment archives, and research conclusions remain unchanged.

## Verification

Verification covers:

- installing the root project and its local `dataflows` dependency;
- importing `dataflows` from outside the repository;
- running `czsc-trader data prepare` without `PYTHONPATH`;
- the existing CLI end-to-end suite;
- package compilation and dependency integrity;
- research archive validation;
- confirmation that `.env` contents and tracked raw data are unchanged.
