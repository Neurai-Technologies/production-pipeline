# Contributing

- `just setup` once per clone from a normal shell, again after moving it.
- The working agreement is the agent file for your tool (CLAUDE.md,
  AGENTS.md or GEMINI.md); humans follow the same rules.
- `just ci` must pass; never bypass a hook. Conventional commits, no AI
  attribution trailers, one concern per commit; `main` moves only by
  fast-forward from a green `ci`.
- A human reviews and edits every AI-authored change before it is committed.
- External contributions are accepted only under a signed agreement that
  assigns the rights to Neurai Technologies Private Limited. The repository
  is public; that is not an invitation to contribute.
- Maintainers review an outside pull request in a separate clone without
  `just setup`: in a clone with hooks, the next commit or push would run
  that branch's scripts through the hooks.
