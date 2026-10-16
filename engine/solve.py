"""One Owl solve. Prints the year-by-year series the page charts.

Launched with Owl's Python; clock sets today to 1 January of the plan's first year.
"""

import json
import sys

import clock  # noqa: F401  must come before Owl: date.today() is the plan's first 1 January

import highs_setup
import numpy as np
import pandas as pd

# Many solves run side by side; one solver thread each keeps the cores from thrashing.
highs_setup.setup(one_thread=True)
from owlplanner.config import readConfig  # noqa: E402


def top_rate(filled, rates):
    rate = 0.0
    for amount, bracket in zip(filled, rates):
        if amount > 1:
            rate = float(bracket)
    if rate > 1:
        rate = rate / 100
    return round(rate * 100)


# HiGHS sometimes fails on a model it can solve once the numbers are scaled differently.
RETRIES = [{}, {"mipScaleOrder": 3}, {"mipScaleOrder": 1}, {"gap": 1e-3}]


def load(case, overrides):
    """Read the case, then apply the overrides one path of the Risk view needs: drawn lifespans,
    a market (a rate series, or one of Owl's market models with a seed), and fixed conversions."""
    plan = readConfig(case, verbose=False)
    if overrides.get("lifespanSeed") is not None:
        # Each person's age at death drawn from Owl's life tables, given their age today.
        from owlplanner.config.plan_bridge import clone
        from owlplanner.data.mortality_tables import sample_lifespans
        rng = np.random.default_rng(int(overrides["lifespanSeed"]))
        ages = [int(plan.year_n[0] - plan.yobs[i]) for i in range(plan.N_i)]
        drawn = [int(sample_lifespans(plan.sexes[i], ages[i], 1, rng, table=plan.mortality_table)[0]) for i in range(plan.N_i)]
        plan = clone(plan, expectancy=drawn, verbose=False)
    if overrides.get("model"):
        model = overrides["model"]
        plan.setReproducible(True, seed=int(model["seed"]))
        extra = {k: v for k, v in model.items() if k not in ("method", "seed", "from", "to")}
        plan.setRates(model["method"], frm=model.get("from"), to=model.get("to"), **extra)
    if overrides.get("rates"):
        frame = pd.DataFrame(overrides["rates"], columns=["S&P 500", "Bonds Baa", "T-Notes", "Inflation"])
        plan.setRates("dataframe", df=frame, n_years=len(frame))
    if overrides.get("horizonYear"):
        # End the plan in this year: the same market path, cut short. Each person's plan ends by
        # then; the returns already chosen for the path are kept for the years that remain.
        from owlplanner.config.plan_bridge import clone
        last = int(overrides["horizonYear"])
        ends = [min(int(plan.expectancy[i]), last - int(plan.yobs[i])) for i in range(plan.N_i)]
        if ends != [int(v) for v in plan.expectancy]:
            returns = plan.tau_kn.T * 100
            # Rebuilt from the case file, which carries no path returns, then given this path's.
            plan = clone(readConfig(case, verbose=False), expectancy=ends, verbose=False)
            frame = pd.DataFrame(returns[: plan.N_n], columns=["S&P 500", "Bonds Baa", "T-Notes", "Inflation"])
            plan.setRates("dataframe", df=frame, n_years=plan.N_n)
    for i, amount in enumerate(overrides.get("fixedRoth") or []):
        if amount is not None:
            plan.myRothX_in[i, 0] = float(amount)
            plan.rothXfixed_in[i, 0] = True
    return plan


def next_state(plan):
    """Balances on 1 January of the second year, in that year's dollars, for the next start."""
    if plan.N_n < 2:
        return None
    taxable = plan.b_ijn[:, 0, 1]
    fraction = getattr(plan, "gain_fraction_in", None)
    if fraction is not None and not np.isnan(fraction[:, 1]).any():
        # Owl's gain fraction is of the stock share only (bonds and cash sit at their basis), so
        # the account's unrealized gain is that fraction times the stock share.
        basis = taxable * (1 - fraction[:, 1] * plan.alpha_ijkn[:, 0, 0, 1])
    else:
        basis = None
    return {
        "taxable": [round(float(v)) for v in taxable],
        "basis": None if basis is None else [round(float(v)) for v in basis],
        "pretax": [round(float(v)) for v in plan.b_ijn[:, 1, 1]],
        "roth": [round(float(v)) for v in plan.b_ijn[:, 2, 1]],
        "conversions": [round(float(v)) for v in plan.x_in[:, 0]],
    }


def main():
    overrides = json.loads(open(sys.argv[3]).read()) if len(sys.argv) > 3 else {}
    for extra in RETRIES:
        plan = load(sys.argv[1], overrides)
        if extra == RETRIES[0]:
            # The market this path lives through, kept even if no plan pays for everything in it:
            # each year's return on stocks, corporate bonds and T-notes, and inflation, in percent.
            with open(sys.argv[2] + ".returns.json", "w") as handle:
                json.dump([[int(plan.year_n[n])] + [round(float(v) * 100, 4) for v in plan.tau_kn[:, n]] for n in range(plan.N_n)], handle)
            if overrides.get("returnsOnly"):
                # Only the market is wanted: the year-by-year re-planning lives it a year at a time.
                return
        options = dict(plan.solverOptions)
        options["solver"] = "HiGHS"
        options.update(extra)
        # A Risk path caps each solve: on a path where the money barely lasts, HiGHS's default method
        # can grind for many minutes before giving up and handing over to Owl's fallback.
        if overrides.get("maxTime"):
            options["maxTime"] = float(overrides["maxTime"])
        # "maxSpending" asks the opposite question: the most this path can spend every year.
        plan.solve(overrides.get("objective") or plan.objective, options)
        if plan.caseStatus == "solved":
            break
        if "Solver error" not in str(plan.solverMessage):
            break
    if plan.caseStatus != "solved":
        raise SystemExit(plan.solverMessage or plan.caseStatus)

    n_years = plan.N_n
    gamma = plan.gamma_n[:n_years]
    taxable = np.sum(plan.b_ijn[:, 0, :n_years], axis=0) / gamma
    pretax = np.sum(plan.b_ijn[:, 1, :n_years], axis=0) / gamma
    roth = np.sum(plan.b_ijn[:, 2, :n_years], axis=0) / gamma
    real_estate = np.asarray(plan.fixed_assets_current_asset_values_n[:n_years], dtype=float) / gamma
    debt = np.asarray(plan.fixed_assets_debt_balances_remaining_n[:n_years], dtype=float) / gamma
    spending = plan.g_n[:n_years] / gamma
    federal = plan.T_n[:n_years] / gamma
    gains = plan.U_n[:n_years] / gamma
    niit = plan.J_n[:n_years] / gamma
    state = plan.st_T_n[:n_years] / gamma
    roth_moved = np.sum(plan.x_in[:, :n_years], axis=0) / gamma
    ss = np.sum(plan.zetaBar_in[:, :n_years], axis=0) / gamma
    insurance = np.asarray(plan.aca_costs_n[:n_years], dtype=float) / gamma
    medicare = np.asarray(plan.medicare_n[:n_years], dtype=float) / gamma

    # Each person's moves in each year, in today's dollars, for the year's to-do list.
    def person(i, n):
        g = gamma[n]
        sold = float(plan.w_ijn[i, 0, n])
        # Owl's gain fraction applies to the stock part of a sale; bonds and cash sit at cost.
        share = float(plan._effective_cap_gain_coef(i, n)) * float(plan.alpha_ijkn[i, 0, 0, n])
        rmd = getattr(plan, "rmd_in", None)
        required = float(rmd[i, n]) if rmd is not None else float(plan.rho_in[i, n] * plan.b_ijn[i, 1, n])
        return {
            "wages": round(float(plan.omega_in[i, n]) / g),
            "save": [round(float(plan.kappa_ijn[i, j, n]) / g) for j in range(plan.N_j)],
            "sell": round(sold / g),
            "gain": round(sold * share / g),
            "invest": round(float(plan.d_in[i, n]) / g),
            "pretaxOut": round(float(plan.w_ijn[i, 1, n]) / g),
            "required": round(required / g),
            "rothOut": round(float(plan.w_ijn[i, 2, n]) / g),
            "hsaOut": round(float(plan.w_ijn[i, 3, n]) / g),
            "convert": round(float(plan.x_in[i, n]) / g),
            "ss": round(float(plan.zetaBar_in[i, n]) / g),
            "pretax": round(float(plan.b_ijn[i, 1, n]) / g),
        }

    rows = []
    for n in range(n_years):
        rows.append({
            "year": int(plan.year_n[n]),
            "taxable": round(float(taxable[n])),
            "pretax": round(float(pretax[n])),
            "roth": round(float(roth[n])),
            "realEstate": round(float(real_estate[n])),
            "debt": round(float(debt[n])),
            "spending": round(float(spending[n])),
            "federal": round(float(federal[n])),
            "gainsTax": round(float(gains[n])),
            "niit": round(float(niit[n])),
            "state": round(float(state[n])),
            "tax": round(float(federal[n] + gains[n] + niit[n] + state[n])),
            "rothMoved": round(float(roth_moved[n])),
            "ss": round(float(ss[n])),
            "insurance": round(float(insurance[n])),
            "medicare": round(float(medicare[n])),
            "bracket": top_rate(plan.f_tn[:, n], plan.theta_tn[:, n]),
            # Prices that year relative to the plan's start, to show that year's own dollars.
            "price": round(float(gamma[n]), 5),
            "people": [person(i, n) for i in range(plan.N_i)],
        })

    summary = {
        "status": plan.caseStatus,
        "spending": round(float(spending[0])),
        "bequest": round(float(plan.bequest)),
        "roth": round(float(np.sum(roth_moved))),
        "federalTax": round(float(np.sum(federal))),
        "ssAges": [round(float(age), 2) for age in plan.ssecAges],
        "solvedAt": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "dollars": "today",
        # The average inflation behind the plan, so the page can turn fixed payments into today's dollars.
        "inflation": round(float((gamma[-1] / gamma[0]) ** (1 / max(1, n_years - 1)) - 1), 5),
        "lifespans": [int(v) for v in plan.expectancy],
        # How settled the answer is: how Owl's passes ended, and the largest gap, over the whole
        # plan in today's dollars, between the costs the plan was solved with and the ones its
        # own income implies.
        "convergence": str(getattr(plan, "convergenceType", "")),
        "residual": round(max([float(v.get("abs_sum", 0)) for v in (getattr(plan, "fixedPointResidual", {}) or {}).values()] or [0])),
        "timeline": rows,
        "next": next_state(plan),
    }
    with open(sys.argv[2], "w") as handle:
        json.dump(summary, handle)


if __name__ == "__main__":
    main()
