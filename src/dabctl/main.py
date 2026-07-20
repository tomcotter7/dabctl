import json
import logging
import re
import subprocess
import sys
import time
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

    try:
        tasks = data["resources"]["jobs"][jobname]["tasks"]
    except KeyError:
        logger.error(f"Job '{jobname}' not found in the bundle summary for target '{target}'.")
        logger.error(f"Available jobs: {list(data['resources']['jobs'].keys())}")
        return

    tasks = {task["task_key"]: {"depends_on": [t["task_key"] for t in task.get("depends_on", [])]} for task in tasks}
    task_keys = build_task_ordering(tasks)

    config.set_current(jobname=jobname, target=target, profile=profile, job_id=job_id, tasks=task_keys)

    summary["job_details"] = config.get_current()

    workspace = data["workspace"]

    # TODO; This fails if the bundle has never been deployed, we should handle that case gracefully.
    result = subprocess.run(
        ["databricks", "workspace", "list", "--profile", profile, workspace["artifact_path"] + "/.internal"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.error(
            f"Failed to list workspace artifacts at {workspace['artifact_path']}/.internal. "
            "This may indicate that the bundle has never been deployed to the target. "
            "Please deploy the bundle first using `dabctl deploy`."
        )
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


POLL_INTERVAL_DEFAULT = 120
POLL_RETRY_DELAY = 120
MAX_POLL_RETRIES = 3
TERMINAL_LIFE_CYCLE_STATES = {"TERMINATED", "INTERNAL_ERROR", "SKIPPED"}


def notify(msg: str) -> None:
    """Fire a macOS desktop notification via osascript."""
    subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "dabctl"'])


def get_run(run_id: str | int) -> dict:
    """Fetch a job run's details as JSON. Raises RuntimeError if the CLI call fails."""
    result = subprocess.run(
        ["databricks", "jobs", "get-run", str(run_id), "--output", "json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "databricks jobs get-run failed")
    return json.loads(result.stdout)


def run_state(run: dict) -> tuple[Optional[str], Optional[str]]:
    """Extract (life_cycle_state, result_state) from a run or task dict.

    Handles both the API 2.2 `status` shape and the older 2.1 `state` shape.
    """
    status = run.get("status")
    if status:
        termination = status.get("termination_details") or {}
        return status.get("state"), termination.get("code")
    state = run.get("state") or {}
    return state.get("life_cycle_state"), state.get("result_state")


def parse_run_id(stdout: str, stderr: str) -> Optional[int]:
    """Pull the run_id out of `databricks bundle run --no-wait` output."""
    for text in (stdout, stderr):
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("run_id"):
            return data["run_id"]

    match = re.search(r"run/(\d+)", f"{stdout or ''}\n{stderr or ''}")
    return int(match.group(1)) if match else None


def format_status_line(run: dict) -> str:
    """Build a one-line summary of a run's overall state and each task's state."""
    life_cycle, result_state = run_state(run)
    overall = result_state or life_cycle or "UNKNOWN"
    task_states = []
    for task in run.get("tasks", []):
        task_life_cycle, task_result = run_state(task)
        task_states.append(f"{task['task_key']}:{task_result or task_life_cycle or '?'}")
    timestamp = time.strftime("%H:%M:%S")
    task_str = " ".join(task_states)
    return f"[{timestamp}] {overall}" + (f" — {task_str}" if task_str else "")


SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_FRAME_DELAY = 0.1


def emit_status(line: str) -> None:
    """Show the current status.

    On a TTY, overwrite the previous line in place;
    otherwise print a fresh line so redirected output stays readable.
    """
    if sys.stdout.isatty():
        print(f"\r\033[2K{line}", end="", flush=True)
    else:
        print(line)


def spin_wait(seconds: float, status: str) -> None:
    """Wait `seconds` while animating a spinner next to `status`.

    On a non-TTY the status is printed once and we just sleep, so redirected
    output gets one line per poll rather than a stream of spinner frames.
    """
    if not sys.stdout.isatty():
        print(status)
        time.sleep(seconds)
        return

    frames = max(1, int(seconds / SPINNER_FRAME_DELAY))
    for i in range(frames):
        print(f"\r\033[2K{SPINNER_FRAMES[i % len(SPINNER_FRAMES)]} {status}", end="", flush=True)
        time.sleep(SPINNER_FRAME_DELAY)


def poll_until_complete(run_id: str | int, interval: int) -> Optional[dict]:
    """Poll a run until it reaches a terminal state.

    Returns the final run dict, or None if polling could not recover (in which
    case the run is likely still active on Databricks).
    """
    failures = 0
    while True:
        try:
            run = get_run(run_id)
        except (RuntimeError, json.JSONDecodeError) as exc:
            failures += 1
            if sys.stdout.isatty():
                print()
            if failures > MAX_POLL_RETRIES:
                logger.error("Lost connection while polling run %s: %s", run_id, exc)
                return None
            logger.warning(
                "Poll failed (%s/%s), retrying in %ss: %s", failures, MAX_POLL_RETRIES, POLL_RETRY_DELAY, exc
            )
            time.sleep(POLL_RETRY_DELAY)
            continue

        failures = 0
        status = format_status_line(run)
        life_cycle, _ = run_state(run)
        if life_cycle in TERMINAL_LIFE_CYCLE_STATES:
            emit_status(status)
            if sys.stdout.isatty():
                print()
            return run
        spin_wait(interval, status)


@app.command()
def run(
    jobname: Optional[str] = None,
    target: Optional[str] = None,
    profile: Optional[str] = None,
    tasks: Annotated[
        Optional[str],
        typer.Option(help="The tasks to run", autocompletion=complete_tasks),
    ] = None,
    params: Annotated[
        Optional[str],
        typer.Option(
            help="The parameters to pass to the run, in the format 'key1=value1,key2=value2'",
        ),
    ] = None,
    poll_interval: Annotated[
        int,
        typer.Option(help="Seconds between run status polls while waiting for completion"),
    ] = POLL_INTERVAL_DEFAULT,
):
    """Run specific tasks from a Databricks bundle job.

    Triggers the run without holding a long-lived connection open, then polls
    for completion. A dropped connection retries the poll rather than losing the
    run, which continues on Databricks regardless.
    """
    if not jobname or not target or not profile:
        config_values = config.get_current()
        jobname = jobname or config_values["jobname"]
        target = target or config_values["target"]
        profile = profile or config_values["profile"]

    command = ["databricks", "bundle", "run", jobname, "--no-wait", "--output", "json"]
    if tasks:
        command = command + ["--only", tasks]

    if params:
        command = command + ["--params", params]

    trigger = subprocess.run(command, capture_output=True, text=True)
    if trigger.returncode != 0:
        logger.error("Failed to trigger run: %s" % trigger.stderr)
        notify(f"Failed to trigger job {jobname}")
        return

    run_id = parse_run_id(trigger.stdout, trigger.stderr)
    if not run_id:
        logger.error(
            "Triggered run but could not determine run_id from output:\n%s\n%s", trigger.stdout, trigger.stderr
        )
        return

    logger.info("Triggered run %s for job %s, polling every %ss...", run_id, jobname, poll_interval)

    final = poll_until_complete(run_id, poll_interval)
    if final is None:
        logger.error(
            "The run is likely still active on Databricks. Resume tracking with: dabctl analyse --run-id %s", run_id
        )
        notify(f"Lost track of run {run_id} — check Databricks")
        return

    _, result_state = run_state(final)
    if result_state == "SUCCESS":
        logger.info("Run %s complete: SUCCESS", run_id)
        notify(f"Run complete for job {jobname}")
    else:
        logger.error("Run %s finished: %s. For details: dabctl analyse --run-id %s", run_id, result_state, run_id)
        notify(f"Run failed for job {jobname}")


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
    run_output = get_run(run_id)
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
