# Pinned upstream tools

Spec Relay builds on Robert C. Martin's [UBMode](https://github.com/unclebob/ub-mode). See the [original principles](../vendor/ub-mode/SKILL.md) and [license](../vendor/ub-mode/LICENSE). The companion tools come from [Crapper](https://github.com/unclebob/crapper), [Dryer](https://github.com/unclebob/dryer), and [Mutator](https://github.com/unclebob/mutator).

`upstream.lock.json` records the source revision for each vendored repository. The original licenses remain beside their sources.

The runtime adds structured adapters in `src/ub_execute/quality.py`. The adapters preserve the upstream analysis APIs. They generate versioned JSON, require explicit fresh coverage commands, and keep policy decisions separate from CLI exit codes.

Local source changes:

- Crapper's parser helper requires an installed parser. It does not download during analysis.
- Dryer's parser helper uses the same explicit setup rule.
- Dryer recognizes JavaScript file extensions already supported by the other tools.
- Mutator distinguishes invalid Python syntax, compilation diagnostics, timeouts, and command errors from killed mutants.
- Mutator's runner can inherit the outer check's process group so cancellation reaches its child commands.

The Mutator adapter always requests a complete run for selected files. It does not reuse a prior killed result after tests change. It limits concurrent mutation workers to two by default.

Compilation diagnostics remain conservative heuristics for languages other than Python. Java line-coverage mapping is not implemented in the adapter. An explicit unsupported result prevents a false pass.

Upstream mutation CLI snapshots retain their original summary schema. The adapter's per-site outcomes are authoritative for runtime acceptance. Do not use an upstream CLI exit code as a substitute for the adapter report.
