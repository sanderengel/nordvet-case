"""python -m nordvet run, with the four steps replaced by fakes: order, flags, where it stops,
and the exit code. No database or API needed."""

import logging

import pytest

from nordvet import __main__ as cli
from nordvet import pipeline
from nordvet.ingest import load
from nordvet.model import build, export
from nordvet.report import render


@pytest.fixture
def steps(monkeypatch):
    """Replace each step's main() with a fake that records its argv and returns a set code."""
    calls = []
    codes = {"load": 0, "build": 0, "export": 0, "report": 0}

    def fake(name):
        def main(argv):
            calls.append((name, list(argv)))
            code = codes[name]
            if isinstance(code, Exception):
                raise code
            return code
        return main

    for module, name in ((load, "load"), (build, "build"), (export, "export"), (render, "report")):
        monkeypatch.setattr(module, "main", fake(name))
    return calls, codes


@pytest.fixture
def opened(monkeypatch):
    """Record what the run asks the browser to open, instead of opening anything."""
    urls = []

    def fake_open(url):
        urls.append(url)
        return True

    monkeypatch.setattr(pipeline.webbrowser, "open", fake_open)
    return urls


def names(calls):
    return [name for name, _ in calls]


def test_all_steps_run_in_order_and_exit_0(steps):
    calls, _ = steps
    assert pipeline.run_pipeline() == 0
    assert calls == [("load", []), ("build", []), ("export", []), ("report", [])]


def test_full_goes_to_the_load_only_and_verbose_to_every_step(steps):
    calls, _ = steps
    assert cli.main(["run", "--full", "-v"]) == 0
    assert calls == [("load", ["--full", "-v"]), ("build", ["-v"]), ("export", ["-v"]), ("report", ["-v"])]


@pytest.mark.parametrize("failing, code", [("load", 2), ("load", 1), ("build", 2), ("build", 1)])
def test_a_failed_load_or_build_stops_the_run_with_its_code(steps, failing, code):
    calls, codes = steps
    codes[failing] = code
    assert pipeline.run_pipeline() == code
    assert names(calls) == ["load", "build"][: ["load", "build"].index(failing) + 1]


@pytest.mark.parametrize("export_code, report_code, expected", [(2, 0, 2), (0, 2, 2), (1, 0, 1), (1, 2, 2)])
def test_export_and_report_both_run_and_the_worst_code_wins(steps, export_code, report_code, expected):
    calls, codes = steps
    codes["export"], codes["report"] = export_code, report_code
    assert pipeline.run_pipeline() == expected
    assert names(calls) == ["load", "build", "export", "report"]


def test_an_exception_from_a_step_counts_as_a_failed_step(steps):
    calls, codes = steps
    codes["export"] = RuntimeError("boom")
    assert pipeline.run_pipeline() == 2
    assert names(calls) == ["load", "build", "export", "report"]

    calls.clear()
    codes["export"], codes["load"] = 0, RuntimeError("boom")
    assert pipeline.run_pipeline() == 2
    assert names(calls) == ["load"]


def test_the_summary_names_every_step_and_the_skipped_ones(steps, caplog):
    _, codes = steps
    codes["build"] = 2
    with caplog.at_level(logging.INFO, logger="nordvet.run"):
        pipeline.run_pipeline()
    summary = caplog.records[-1].getMessage()
    assert summary.startswith("run failed (exit 2): load 0 (")
    assert "build 2 (" in summary and "export skipped" in summary and "report skipped" in summary


def test_open_opens_the_report_only_when_asked_and_it_was_written(steps, opened):
    assert cli.main(["run"]) == 0
    assert opened == []
    assert cli.main(["run", "--open"]) == 0
    assert opened == [render.DEFAULT_OUT.resolve().as_uri()]


@pytest.mark.parametrize("export_code, report_code, opens", [(2, 0, True), (0, 2, False), (0, 1, False)])
def test_open_follows_the_report_step_not_the_export(steps, opened, export_code, report_code, opens):
    _, codes = steps
    codes["export"], codes["report"] = export_code, report_code
    pipeline.run_pipeline(open_report=True)
    assert bool(opened) is opens


def test_open_never_runs_when_the_build_failed(steps, opened):
    _, codes = steps
    codes["build"] = 2
    assert pipeline.run_pipeline(open_report=True) == 2
    assert opened == []


def test_a_browser_that_cannot_open_does_not_fail_the_run(steps, monkeypatch):
    monkeypatch.setattr(pipeline.webbrowser, "open", lambda url: False)
    assert pipeline.run_pipeline(open_report=True) == 0
