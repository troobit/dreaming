# Instruction set

Run snapshots read this directory from the configured repository's default branch.
`config.example.toml` documents the external configuration. `lenses.toml` contains
the initial versioned lenses; domain, project and workflow restrictions are optional.
`roles/` defines the five narrow roles and `schemas/` defines their JSON outputs.

`RoleSet` reads a run's snapshot and bounds inputs against the smallest configured
fallback context, including prompt, schema and tool-name overhead and reserved
output space. Reviewers receive summaries and evidence identifiers; omitted
summaries carry counts. Readers must split oversized source parts rather than
cutting their evidence. Replayers receive only the historical opening prompt and
the instruction under replay. Update a lens's version when changing its behaviour.
