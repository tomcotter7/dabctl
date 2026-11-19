import json
import logging
import re
import shutil
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

POLL_INTERVAL_DEFAULT = 120
POLL_RETRY_DELAY = 120
MAX_POLL_RETRIES = 3
TERMINAL_LIFE_CYCLE_STATES = {"TERMINATED", "INTERNAL_ERROR", "SKIPPED"}
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_FRAME_DELAY = 0.1
ANSI_ESCAPE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


def strip_ansi(s: str | None) -> str | None:
    """Remove ANSI escape sequences from a string."""
    return None if s is None else ANSI_ESCAPE.sub("", s)


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


def notify(msg: str) -> None:
    """Fire a macOS desktop notification via osascript."""
    # pass msg via argv so quotes in it can't break the AppleScript
    subprocess.run(
        ["osascript", "-e", "on run argv", "-e", 'display notification (item 1 of argv) with title "dabctl"', "-e", "end run", msg]
    )


def get_run(run_id: str | int, profile: Optional[str] = None) -> dict:
    """Fetch a job run's details as JSON. Raises RuntimeError if the CLI call fails."""
    profile = profile or config.get_current().get("profile")
    result = subprocess.run(
        ["databricks", "jobs", "get-run", str(run_id), "--output", "json", "--profile", profile],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "databricks jobs get-run failed")
    return json.loads(result.stdout)


def run_state(run: dict) -> tuple[str | None, str | None, str | None]:
    """Extract (life_cycle_state, result_state) from a run or task dict."""
    # https://docs.databricks.com/api/workspace/jobs/getrun
    status = run.get("status", {})
    termination = status.get("termination_details", {})
    return status.get("state"), termination.get("code"), termination.get("message")


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

    match = re.search(r"runs?/(\d+)", f"{stdout or ''}\n{stderr or ''}")
    return int(match.group(1)) if match else None


def format_status_line(run: dict) -> str:
    """Build a one-line summary of a run's overall state and each task's state."""
    current_state, termination_details, _ = run_state(run)
    overall = termination_details or current_state or "UNKNOWN"
    task_states = []
    for task in run.get("tasks", []):
        task_current_state, task_termination_details, _ = run_state(task)
        if task_termination_details != "DISABLED":
            task_states.append(f"{task['task_key']}:{task_termination_details or task_current_state or '?'}")
    timestamp = time.strftime("%H:%M:%S")
    task_str = " ".join(task_states)
    return f"[{timestamp}] {overall}" + (f" — {task_str}" if task_str else "")


def fit_to_width(line: str) -> str:
    """Truncate a line to the terminal width."""
    width = shutil.get_terminal_size(fallback=(80, 24)).columns
    return line if len(line) <= width else line[: max(0, width - 5)] + "..."


def emit_status(line: str) -> None:
    """Show the current status."""
    # A TTY is an interactive terminal.
    if sys.stdout.isatty():
        # \r - carriage return
        # \033[2K - \033 - ESC, [ - START ANSI SEQUENCE, 2K - Erase in line (2 = WHOLE LINE)
        print(f"\r\033[2K{fit_to_width(line)}", end="", flush=True)
    else:
        print(line)


def spin_wait(seconds: float, status: str) -> None:
    """Wait `seconds` while animating a spinner next to `status`."""
    if not sys.stdout.isatty():
        print(status)
        time.sleep(seconds)
        return

    frames = max(1, int(seconds / SPINNER_FRAME_DELAY))
    for i in range(frames):
        frame = f"{SPINNER_FRAMES[i % len(SPINNER_FRAMES)]} {status}"
        print(f"\r\033[2K{fit_to_width(frame)}", end="", flush=True)
        time.sleep(SPINNER_FRAME_DELAY)


def poll_until_complete(run_id: str | int, max_interval: int) -> dict | None:
    """Poll a run until it reaches a terminal state.

    interval increases over time to avoid hammering the API.
    """
    current_interval = 1
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
        current_state, _, _ = run_state(run)
        if current_state in TERMINAL_LIFE_CYCLE_STATES:
            emit_status(status)
            if sys.stdout.isatty():
                print()
            return run
        spin_wait(current_interval, status)
        current_interval = min(current_interval * 1.5, max_interval)


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
        job = data["resources"]["jobs"][jobname]
        tasks = job["tasks"]
    except KeyError:
        logger.error(f"Job '{jobname}' not found in the bundle summary for target '{target}'.")
        logger.error(f"Available jobs: {list(data['resources']['jobs'].keys())}")
        return

    if not job_id:
        # The summary holds the deployed name (with any "[dev user]" prefix), so we can filter server-side.
        result = subprocess.run(
            ["databricks", "jobs", "list", "--output", "json", "--name", job["name"]],
            capture_output=True,
            text=True,
            check=True,
        )
        print("Got no `job_id` set, looking up job ID for job name '%s'..." % job["name"])

        jobs = json.loads(result.stdout)
        matches = [(j.get("job_id"), j.get("settings", {}).get("name", "")) for j in jobs]
        if not matches:
            logger.error(f"No deployed job named '{job['name']}'. Deploy it first using `dabctl deploy`.")
            return
        if len(matches) > 1:
            print("No `job_id` set, we've found multiple possibilities, which of these is your job ID?")
            print(json.dumps(matches, indent=2))
            job_id_idx = int(input(f"Press 1 - {len(matches)}: "))
            job_id = matches[job_id_idx - 1][0]
        else:
            job_id = matches[0][0]

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


@app.command()
def reattach(
    run_id: str,
    jobname: Optional[str] = None,
    target: Optional[str] = None,
    profile: Optional[str] = None,
):
    """Resume tracking an existing run until it completes."""
    if not jobname or not target or not profile:
        config_values = config.get_current()
        jobname = jobname or config_values["jobname"]
        target = target or config_values["target"]
        profile = profile or config_values["profile"]

    logger.info("Reattaching to run %s for job %s...", run_id, jobname)
    final = poll_until_complete(run_id, POLL_INTERVAL_DEFAULT)
    if final is None:
        logger.error(
            "The run is likely still active on Databricks. Resume tracking with: dabctl analyse --run-id %s", run_id
        )
        notify(f"Lost track of run {run_id} — check Databricks")
        return
    _, termination_details, state_msg = run_state(final)
    if termination_details == "SUCCESS":
        logger.info("Run %s complete: SUCCESS", run_id)
        notify(f"Run complete for job {jobname}")
    else:
        message = f"Run {run_id} finished: {termination_details}."
        if state_msg:
            message += f" See state message: {state_msg}."
        message += f" For details:\ndabctl analyse --run-id {run_id}"
        logger.error(message)
        notify(f"Run failed for job {jobname} with error {termination_details}.")


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
    no_poll: Annotated[
        bool,
        typer.Option(help="Do not poll for completion, just trigger the run and exit"),
    ] = False,
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
    command = command + ["--only", tasks] if tasks else command
    command = command + ["--params", params] if params else command

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
        notify(f"Triggered job {jobname} but lost its run ID — check Databricks")
        return

    if no_poll:
        print(json.dumps({"run_id": run_id}, indent=2))
        return

    logger.info("Triggered run %s for job %s, polling every %ss...", run_id, jobname, poll_interval)
    final = poll_until_complete(run_id, poll_interval)
    if final is None:
        logger.error(
            "The run is likely still active on Databricks. Resume tracking with: dabctl analyse --run-id %s", run_id
        )
        notify(f"Lost track of run {run_id} — check Databricks")
        return

    _, termination_details, state_msg = run_state(final)
    if termination_details == "SUCCESS":
        logger.info("Run %s complete: SUCCESS", run_id)
        notify(f"Run complete for job {jobname}")
    else:
        message = f"Run {run_id} finished: {termination_details}."
        if state_msg:
            message += f" See state message: {state_msg}."
        message += f" For details:\ndabctl analyse --run-id {run_id}"
        logger.error(message)
        notify(f"Run failed for job {jobname} with error {termination_details}.")


@app.command()
def analyse(
    jobname: Optional[str] = None,
    target: Optional[str] = None,
    profile: Optional[str] = None,
    job_id: Optional[str] = None,
    run_id: Optional[str] = None,
    *,
    agentic_mode: bool = False,
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

        if error and error_trace and show_errors and not agentic_mode:
            print(f"===========Task: {task_key} ({task_run_id})===========")
            print(error)
            print(error_trace)
        state = data.get("metadata", {}).get("state", {}).get("result_state")
        state_msg = data.get("metadata", {}).get("state", {}).get("state_message")

        if state != "DISABLED":
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

    if agentic_mode:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return

    print(f"A short summary of the run: {run_id}")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print("For more details on the individual tasks, use `databricks jobs get-run-output <task_id> --output json`")


def main():
    """Entry point for the dabctl CLI application."""
    app()
