# Packaged resources

`agents/` and `skills/` here are symlinks to the repo's top-level `agents/`
and `skills/` — the single source of truth. They live under the package so
the build (`setuptools`) copies their real content into the wheel, making an
installed `goaloop` self-contained: the orchestrator loads the Runner system
prompt from `resources/agents/goal-runner.md`, and `goaloop install` deploys
`resources/skills/*` and `resources/agents/*` into `~/.claude/`.

Editing the top-level files is all you need; do not edit through the symlinks.
