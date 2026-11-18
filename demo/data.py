"""Seed data for the demo portal. All people and companies are fictional.

``behavior`` lets the demo reproduce real-world failure modes on purpose:

* ``normal``  - always works
* ``slow``    - the profile page takes longer than the automation timeout
* ``flaky``   - the first two loads return HTTP 503, then the page works (shows retries)
* ``blocked`` - the site answers with HTTP 429 and an "unusual activity" page (automation must stop)
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Person:
    id: int
    name: str
    headline: str
    company: str
    location: str
    about: str
    recent_activity: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    behavior: str = "normal"
    private: bool = False  # served under /private/, which robots.txt disallows


PEOPLE: list[Person] = [
    Person(
        1,
        "Priya Raman",
        "Head of Operations",
        "Bluefin Logistics",
        "Rotterdam, Netherlands",
        "Leads a 40-person operations team running cross-dock warehouses. Focused on cutting manual "
        "reporting so planners can spend more time on exceptions.",
        [
            "Wrote about moving weekly warehouse reports from spreadsheets to dashboards",
            "Spoke on a panel about scaling cross-dock operations",
        ],
        ["Operations management", "Lean", "Warehouse planning"],
    ),
    Person(
        2,
        "Marcus Oyelaran",
        "VP of Engineering",
        "Tidewater Health",
        "Lagos, Nigeria",
        "Builds patient-scheduling software used by regional clinics. Cares about reliable releases "
        "and small, autonomous teams.",
        ["Shared his team's approach to on-call rotations"],
        ["Engineering leadership", "Python", "Distributed systems"],
    ),
    Person(
        3,
        "Elena Varga",
        "Data Platform Lead",
        "Kestrel Energy",
        "Budapest, Hungary",
        "Runs the data platform that forecasts demand for a wind and solar portfolio.",
        ["Posted a guide to data quality checks for sensor pipelines"],
        ["Data engineering", "Forecasting", "SQL"],
    ),
    Person(
        4,
        "Tomás Ferreira",
        "Procurement Manager",
        "Atlas Fixtures",
        "Porto, Portugal",
        "Manages supplier onboarding for a furniture manufacturer.",
        [],
        ["Procurement", "Supplier management"],
        behavior="slow",
    ),
    Person(
        5,
        "Hana Sato",
        "Customer Success Director",
        "Meridian Labs",
        "Osaka, Japan",
        "Leads customer success for a lab-automation company.",
        ["Wrote about renewal playbooks"],
        ["Customer success"],
        behavior="blocked",
    ),
    Person(
        6,
        "Daniel Kim",
        "Finance Operations Lead",
        "Harbor & Pine",
        "Vancouver, Canada",
        "Owns month-end close and finance reporting for a retail group.",
        ["Shared a checklist for faster month-end close"],
        ["Financial reporting", "Automation"],
        behavior="flaky",
    ),
    Person(
        7,
        "Sofia Lindqvist",
        "Chief of Staff",
        "Polar Robotics",
        "Gothenburg, Sweden",
        "Private profile used to show that the automation respects robots.txt.",
        [],
        [],
        private=True,
    ),
]

PEOPLE_BY_ID = {p.id: p for p in PEOPLE}
