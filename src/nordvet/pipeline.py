"""The whole pipeline in one command: python -m nordvet run [--full] [-v] [--open].

Four steps, each the same command that can also be run on its own:
  1. load    (nordvet.ingest.load)    the API into raw, then staging;
  2. build   (nordvet.model.build)    the marts views, then the build checks;
  3. export  (nordvet.model.export)   the Parquet files for Power BI, in exports/;
  4. report  (nordvet.report.render)  the no-show page, reports/no-shows.html.

If the load or the build fails, the run stops there: both outputs would show stale or unchecked
data. The export and the report do not depend on each other (the report reads the database, not
the Parquet files), so after a good build both run even if one of them fails; a Power BI problem
never costs the morning report, and the other way round.

--full goes to the load (ignore earlier runs and load everything); -v goes to every step.
--open opens the report in the default browser once the report step has passed (for a demo or a
run by hand); never when it failed, so a stale page is not shown by mistake. A scheduled run
leaves it off: there is no one at the screen.

Exit codes: 0 when every step passed; otherwise the highest code among the steps that ran, so 1
for a configuration problem and 2 for a failed step, the same codes as each step on its own.
"""

from __future__ import annotations

import logging
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass

from nordvet.ingest import load
from nordvet.model import build, export
from nordvet.report import render

log = logging.getLogger("nordvet.run")

EXIT_OK, EXIT_FAILED = 0, 2


@dataclass(frozen=True)
class StepResult:
    name: str
    code: int
    seconds: float


def _step(name: str, main: Callable[[list[str]], int], argv: list[str]) -> StepResult:
    """Run one step's main(); an exception that escapes it counts as a failed step (exit 2)."""
    log.info("step %s: start", name)
    t0 = time.monotonic()
    try:
        code = main(argv)
    except Exception as exc:
        log.error("step %s raised %s: %s", name, type(exc).__name__, exc)
        log.debug("traceback", exc_info=True)
        code = EXIT_FAILED
    result = StepResult(name, code, time.monotonic() - t0)
    log.info("step %s: exit %d (%.1fs)", name, result.code, result.seconds)
    return result


def run_pipeline(full: bool = False, verbose: bool = False, open_report: bool = False) -> int:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    flags = ["-v"] if verbose else []
    results: list[StepResult] = []

    for name, main, extra in (
        ("load", load.main, ["--full"] if full else []),
        ("build", build.main, []),
    ):
        results.append(_step(name, main, extra + flags))
        if results[-1].code != EXIT_OK:
            return _finish(results)

    for name, main in (("export", export.main), ("report", render.main)):
        results.append(_step(name, main, list(flags)))
    if open_report and results[-1].code == EXIT_OK:
        _open(render.DEFAULT_OUT)
    return _finish(results)


def _open(path) -> None:
    """Open the report in the default browser. A browser that cannot open does not fail the run."""
    if webbrowser.open(path.resolve().as_uri()):
        log.info("opened %s", path)
    else:
        log.warning("could not open a browser; the report is at %s", path)


def _finish(results: list[StepResult]) -> int:
    done = {r.name: r for r in results}
    parts = []
    for name in ("load", "build", "export", "report"):
        r = done.get(name)
        parts.append(f"{name} {r.code} ({r.seconds:.1f}s)" if r else f"{name} skipped")
    code = max(r.code for r in results)
    if code == EXIT_OK:
        log.info("run done: %s", ", ".join(parts))
    else:
        log.error("run failed (exit %d): %s", code, ", ".join(parts))
    return code
