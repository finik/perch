"""Run Owl as if today is 1 January of the plan's first year.

Owl has no start-year setting: it calls date.today() and opens the plan on 1 January of that
year. Importing this module before Owl replaces that clock. Owl's source is not changed.
The year comes from OWL_START_YEAR, which the server sets from the scenario.
"""

import datetime
import os

YEAR = int(os.environ.get("OWL_START_YEAR", datetime.date.today().year + 1))


class Date(datetime.date):
    @classmethod
    def today(cls):
        return cls(YEAR, 1, 1)


datetime.date = Date
