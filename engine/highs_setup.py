"""How Perch runs HiGHS under Owl. Imported before Owl, by every script that solves.

- One solver thread per solve when asked: many solves run side by side.
- A fallback for a pure LP that ends with no verdict: HiGHS's default path (presolve, then the
  dual simplex) can stop with "Unknown", "Solve error" or "Not Set" on a model that has no
  feasible point. Such an LP is solved again with the interior-point method without presolve,
  which settles it either way (https://github.com/ERGO-Code/HiGHS/issues/3418,
  https://github.com/mdlacasse/Owl/pull/184). An LP that solves, or ends with a feasible point,
  is untouched; so are integer models.
"""

import highspy

_Highs = highspy.Highs
_VERDICT = ("Optimal", "Infeasible")


def setup(one_thread: bool) -> None:
    class PerchHighs(_Highs):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if one_thread:
                self.setOptionValue("threads", 1)

        def run(self):
            status = super().run()
            lp = self.getLp()
            if any(int(v) for v in (lp.integrality_ or [])):
                return status
            if self.modelStatusToString(self.getModelStatus()) in _VERDICT:
                return status
            if self.getInfoValue("primal_solution_status")[1] == highspy.kSolutionStatusFeasible:
                return status
            solver, presolve = self.getOptionValue("solver")[1], self.getOptionValue("presolve")[1]
            self.clearSolver()
            self.setOptionValue("solver", "ipm")
            self.setOptionValue("presolve", "off")
            status = super().run()
            self.setOptionValue("solver", solver)
            self.setOptionValue("presolve", presolve)
            return status

    highspy.Highs = PerchHighs
