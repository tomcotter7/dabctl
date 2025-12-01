import json
import logging
import re
import subprocess
from typing import Optional

import typer
from typing_extensions import Annotated

from . import config

app = typer.Typer()


log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
logging.basicConfig(level=logging.INFO, format=log_format)
logger = logging.getLogger(__name__)


def build_task_ordering(tasks: dict, ordered_task_names: list[str] = []) -> list[str]:
    """Topologically sort tasks based on their dependencies."""
    if len(ordered_task_names) >= len(tasks):
        return ordered_task_names

    if len(ordered_task_names) == 0:
        ordered_task_names = [key for key, value in tasks.items() if len(value["depends_on"]) == 0]
    else:
        ordered_task_names += [
            key
            for key, value in tasks.items()
            if all([dependent in ordered_task_names for dependent in value["depends_on"]])
            and key not in ordered_task_names
        ]

    return build_task_ordering(tasks, ordered_task_names)


@app.command()
def set(jobname: str, target: str, profile: str):
    """Set the current job name in environment variable."""
    current(jobname, target, profile, show_output=False)


@app.command()
def current(
    jobname: Optional[str] = None,
    target: Optional[str] = None,
    profile: Optional[str] = None,
    *,
    show_output: bool = True,
):
    """Get current deployed bundle version numbers."""
    if not jobname or not target or not profile:
        config_values = config.get_current()
        jobname = jobname or config_values["jobname"]
        target = target or config_values["target"]
        profile = profile or config_values["profile"]

    if not jobname or not target or not profile:
        logger.error("jobname, target, and profile must be set. Use 'dabctl set' command first.")
        return

    result = subprocess.run(
        ["databricks", "bundle", "summary", "--output", "json", "--target", target],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.error("Command failed: %s" % result.stderr)
        return

    summary = {}

    data = json.loads(result.stdout)

    tasks = data["resources"]["jobs"][jobname]["tasks"]

    tasks = {task["task_key"]: {"depends_on": [t["task_key"] for t in task.get("depends_on", [])]} for task in tasks}
    task_keys = build_task_ordering(tasks)

    config.set_current(jobname=jobname, target=target, profile=profile, tasks=task_keys)

    summary["task_keys"] = task_keys

    workspace = data["workspace"]

    result = subprocess.run(
        ["databricks", "workspace", "list", "--profile", profile, workspace["artifact_path"] + "/.internal"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.error("Command failed: %s" % result.stderr)
        return

    artifacts = result.stdout

    version_numbers = [re.search(r"\/([^\/]*?-\d+\.\d+\.\d+)-", artifact) for artifact in artifacts.split("\n")]
    vns = [version_number.group(1) for version_number in version_numbers if version_number is not None]

    summary["deployed_bundle_version_numbers"] = vns

    if show_output:
        print(json.dumps(summary, indent=2))


@app.command()
def deploy(
    jobname: Optional[str] = None,
    target: Optional[str] = None,
    profile: Optional[str] = None,
):
    """Deploy a Databricks bundle job to the specified target."""
    if not jobname or not target or not profile:
        config_values = config.get_current()
        jobname = jobname or config_values["jobname"]
        target = target or config_values["target"]
        profile = profile or config_values["profile"]

    result = subprocess.run(
        ["databricks", "bundle", "deploy", "-t", target],
        text=True,
    )
    if result.returncode != 0:
        logger.error("Command failed")
        return

    logger.info("Command succesful")


def complete_tasks(ctx, args, incomplete):
    """Provide tab completion for task names based on the current job configuration."""
    tasks = config.get_current()["tasks"]
    head, sep, tail = incomplete.rpartition(",")
    prefix = (head + sep) if sep else ""
    used = [s.strip() for s in head.split(",") if s]
    last_idx = 0
    if used:
        last_idx = tasks.index(used[-1])
    possible_tasks = [t for t in tasks[last_idx:]]
    matches = [t for t in possible_tasks if t.startswith(tail) and t not in used]

    return [prefix + m for m in matches]


@app.command()
def run(
    jobname: Optional[str] = None,
    target: Optional[str] = None,
    profile: Optional[str] = None,
    tasks: Annotated[
        Optional[str],
        typer.Option(help="The tasks to run", autocompletion=complete_tasks),
    ] = None,
):
    """Run specific tasks from a Databricks bundle job."""
    if not jobname or not target or not profile:
        config_values = config.get_current()
        jobname = jobname or config_values["jobname"]
        target = target or config_values["target"]
        profile = profile or config_values["profile"]

    command = ["databricks", "bundle", "run", jobname]
    if tasks:
        command = command + ["--only", tasks]

    result = subprocess.run(
        command,
        text=True,
    )
    if result.returncode != 0:
        logger.error("Command failed: %s" % result.stderr)
        return

    logger.info("Command succesful: %s" % result.stdout)


def main():
    """Entry point for the dabctl CLI application."""
    app()
