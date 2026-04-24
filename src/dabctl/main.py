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
    job_id: Optional[str] = None,
    *,
    show_output: bool = True,
):
    """Get current deployed bundle version numbers."""
    if not jobname or not target or not profile or not job_id:
        config_values = config.get_current()
        jobname = jobname or config_values.get("jobname")
        target = target or config_values.get("target")
        profile = profile or config_values.get("profile")
        job_id = job_id or config_values.get("job_id")

    if not jobname or not target or not profile:
        logger.error("jobname, target, and profile must be set. Use 'dabctl set' command first.")
        return

    if not job_id:
        result = subprocess.run(
            ["databricks", "jobs", "list", "--output", "json"],
            capture_output=True,
            text=True,
            check=True,
        )

        jobs = json.loads(result.stdout)
        matches = [(j.get("job_id"), jn) for j in jobs if jobname in (jn := j.get("settings", {}).get("name", ""))]
        if len(matches) > 1:
            print("No `job_id` set, we've found multiple possibilities, which of these is your job ID?")
            print(json.dumps(matches, indent=2))
            job_id_idx = int(input(f"Press 1 - {len(matches)}: "))
            job_id = matches[job_id_idx - 1][0]
        else:
            job_id = matches[0][0]

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

    config.set_current(jobname=jobname, target=target, profile=profile, job_id=job_id, tasks=task_keys)

    summary["job_details"] = config.get_current()

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


ANSI_ESCAPE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


def strip_ansi(s: str | None) -> str | None:
    """Remove ANSI escape sequences from a string."""
    return None if s is None else ANSI_ESCAPE.sub("", s)


@app.command()
def analyse(
    jobname: Optional[str] = None,
    target: Optional[str] = None,
    profile: Optional[str] = None,
    job_id: Optional[str] = None,
    run_id: Optional[str] = None,
    *,
    show_errors: bool = False,
):
    """Analyse the results of a run, defaults to the most recent run if not provided."""
    if not jobname or not target or not profile:
        config_values = config.get_current()
        jobname = jobname or config_values["jobname"]
        target = target or config_values["target"]
        profile = profile or config_values["profile"]
        job_id = job_id or config_values["job_id"]

    if not run_id:
        command = f"databricks jobs list-runs --job-id {job_id} --output json --completed-only | jq '.[0]'"
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        run_id = json.loads(result.stdout)["run_id"]

    summary = {}
    command = ["databricks", "jobs", "get-run", str(run_id), "--output", "json"]
    result = subprocess.run(command, capture_output=True, text=True)
    run_output = json.loads(result.stdout)
    summary["status"] = run_output.get("status")
    run_tasks = run_output.get("tasks")

    tasks_results = []

    for task in run_tasks:
        task_run_id = task["run_id"]
        task_key = task["task_key"]
        command = ["databricks", "jobs", "get-run-output", str(task_run_id), "--output", "json"]
        result = subprocess.run(command, capture_output=True, text=True)
        data = json.loads(result.stdout)
        error = strip_ansi(data.get("error"))
        error_trace = strip_ansi(data.get("error_trace"))

        if error and error_trace and show_errors:
            print(f"===========Task: {task_key} ({task_run_id})===========")
            print(error)
            print(error_trace)
        state = data.get("metadata", {}).get("state", {}).get("result_state")
        state_msg = data.get("metadata", {}).get("state", {}).get("state_message")

        tasks_results.append(
            {
                "task_run_id": task_run_id,
                "task_key": task_key,
                "state": state,
                "state_msg": state_msg,
                "error": error,
                "error_trace": error_trace,
            }
        )

    summary["tasks"] = tasks_results

    print(f"A short summary of the run: {run_id}")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print("For more details on the individual tasks, use `databricks jobs get-run-output <task_id> --output json`")


def main():
    """Entry point for the dabctl CLI application."""
    app()
