# `dabctl`

- Install typer autocompletions with `typer --install-completion zsh` (or bash/fish).
- Place the following in your `.zshrc`:

```bash
eval "$(dabctl --show-completion zsh)"
zstyle ':autocomplete:*' insert-unambiguous no

```

## Installation

- Run `uv sync`
- Create a symlink to the resulting binary (`.venv/bin/dabctl`) to ensure it's on your path.
- Run `dabctl --help` to ensure that it works.

