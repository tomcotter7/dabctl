# `dabctl`

`dabctl` is essentially a wrapper around the `databricks` CLI to make interacting with DABs easier. The CLI is made more intuitive, has autocomplete for tasks within the DAB, and can be given to an LLM to allow for automated access to the DAB.

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

## Claude Code skill

`skills/dabctl-autopilot/` is a Claude Code skill that drives `dabctl` autonomously: deploy, run, diagnose failures from task tracebacks, fix code/config/dependency issues, and iterate until the run is green. It retries capacity failures (no GPU) without spending its fix budget, and stops rather than touching data or permissions.

Skills load from `~/.claude/skills/`, but you use `dabctl` from inside your bundle project rather than from this repo, so install it as a personal skill via symlink:

```bash
ln -sfn "$PWD/skills/dabctl-autopilot" ~/.claude/skills/dabctl-autopilot
```

### Run it in auto mode

Run the skill in Claude Code's **auto mode**. The loop runs unattended for minutes to hours, so a
permission prompt in the middle stops it until you come back. Auto mode removes those prompts: a
classifier reviews each action instead of you. It also keeps Claude working instead of stopping to
ask questions.

Press `Shift+Tab` to cycle modes. The status bar must show `⏵⏵ auto mode on`. In manual mode it shows
`⏸ manual mode on`.

The skill's `allowed-tools` frontmatter pre-approves `dabctl` and file edits, but that grant covers
only the turn that invokes the skill. Auto mode is what keeps a long sweep moving.

## Usage
- Move to the project where the Databricks Asset Bundle is defined.
- Use `dabctl set <your_bundle_name> <your_target_env> <your_databricks_profile>`
- Now, you can use the commands specified in `dabctl --help` to view, deploy and run the asset bundle.
