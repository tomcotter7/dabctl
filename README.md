# `dabctl`

Whilst DABs are typically meant to be run as complete pipelines, I've found it useful to be able to run a subset of tasks associated with a DAB. This package allows for that with an intuitive CLI (w/ autocomplete!)

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

## Usage
- Move to the project where the Databricks Asset Bundle is defined.
- Use `dabctl set <your_bundle_name> <your_target_env> <your_databricks_profile>`
- Now, you can use the commands specified in `dabctl --help` to view, deploy and run the asset bundle.
