"""Run one of Owl's own stochastic tools on a case, unchanged, and write its result as JSON.

    owl_tools.py <case.toml> <result.json> <progress.json> frontier
    owl_tools.py <case.toml> <result.json> <progress.json> regret <person index>

frontier: Owl's stochastic spending optimization (stresstests.run_stochastic_spending) over
          every start year of real history.
regret:   Owl's Roth conversion regret sweep (stresstests.run_conversion_regret_sweep) over the
          same years, with its own grid, summarized by summarize_conversion_regret.

Progress is written to <progress.json> as {"phase", "done", "total"} while the tool runs.
Launched with Owl's Python; clock sets today to 1 January of the plan's first year.
"""

import json
import sys

import clock  # noqa: F401  must come before Owl
import highs_setup

import numpy as np

highs_setup.setup(one_thread=False)

from owlplanner import stresstests
from owlplanner.config import readConfig


def plain(value):
    """Owl's results as JSON: arrays to lists, numpy numbers to Python ones, NaN to None."""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, np.ndarray):
        return plain(value.tolist())
    if isinstance(value, (np.floating, float)):
        return None if np.isnan(value) else float(value)
    if isinstance(value, np.integer):
        return int(value)
    return value


class Progress:
    """Owl's progress hook, written to a file the server reads."""

    def __init__(self, path, phase):
        self.path, self.phase = path, phase

    def write(self, done, total):
        with open(self.path, "w") as handle:
            json.dump({"phase": self.phase, "done": int(done), "total": int(total)}, handle)

    def start(self):
        self.write(0, 0)

    def show(self, done, total):
        self.write(done, total)

    def finish(self):
        pass


def main():
    case, out, progress, tool = sys.argv[1:5]
    plan = readConfig(case, verbose=False)
    options = dict(plan.solverOptions)
    options["solver"] = "HiGHS"
    first, last = 1928, 2025
    if tool == "frontier":
        result = stresstests.run_stochastic_spending(
            plan, options, "historical", ystart=first, yend=last, progcall=Progress(progress, "paths"))
        result.pop("engine", None)
    elif tool == "regret":
        person = int(sys.argv[5])
        hook = Progress(progress, "")

        def on_scenario(phase, completed, total, partial):
            hook.phase = phase
            hook.write(completed, total)

        raw = stresstests.run_conversion_regret_sweep(
            plan, plan.objective, options, None, first, last, person=person, on_scenario=on_scenario)
        result = {"summary": stresstests.summarize_conversion_regret(raw), "sweep": raw}
    else:
        raise SystemExit(f"unknown tool {tool}")
    with open(out, "w") as handle:
        json.dump(plain(result), handle)


if __name__ == "__main__":
    main()
