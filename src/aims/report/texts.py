"""The words of the report, in one place. Values from the log are filled in escaped."""

TITLE = "Run report: {scenario}"
SECTION_MARK = "<!-- aims:section {id} -->"

HEADINGS = {
    "summary": "Judge summary",
    "findings": "Findings",
    "observations": "Other observations",
    "plants": "Plants",
    "flags": "Flags",
    "timeline": "Timeline",
    "facts": "Run facts",
    "sources": "Other monitor sources",
    "appendix": "Appendix",
}

PIN_LABELS = {
    "run": "Run",
    "scenario": "Scenario",
    "status": "Status",
    "started": "Started",
    "duration": "Duration",
    "chain": "Hash chain",
    "monitor": "Monitor",
    "score": "Score",
    "labels": "Labels",
    "access": "Access",
    "links": "Links",
    "built": "Built",
}
ACCESS = {
    "local": "local runs directory. Labels are only the ones written here.",
    "operator": "operator. Plants and scores included.",
    "viewer": "viewer. Plants, ground truth and scores left out.",
}
NO_LINKS = "none. Evidence shows as seq N."
LABELS_NOTE = "Labels sit beside the log. They are not in the hash chain."

JUDGE_VIEW = "The judge's view, in its own words."
NO_VERDICT = {
    "running": "none yet, the run is in progress",
    "no_monitor": "none, the run had no monitor",
    "skipped": "skipped",
    "missing": "no verdict",
}
NO_SUMMARY = "No judge summary."
FINDINGS_INTRO = "The judge's view. Importance runs from 1 to 5."
NO_FINDINGS = "The judge found none of its categories."
OBSERVATIONS_INTRO = (
    "Things the judge saw that fit no category. Also signs the scenario or sandbox is broken."
)
NO_OBSERVATIONS = "None."
ANOMALY_KIND = {"behaviour": "Behaviour", "environment": "Environment"}

PLANTS_INTRO = "Ground truth from the run's truth file. The monitor never sees it."
NO_PLANTS = "No agent reached a plant."
PLANTS_ERROR = "The plants could not be read: {error}"
NO_SCORE = "No score of this monitor source yet. {command} shows what each layer caught."
SCORED_BY = "Caught, from score {score}, at importance {importance} or more."

FLAGS_INTRO = {
    False: "Flags of the live monitor.",
    True: "Flags of review {source}.",
}
FLAGS_NOTE = "The judge's review and a person's label sit side by side."
NO_FLAGS = "No flags."
MORE_ROWS = "{rows} more"

TIMELINE_INTRO = (
    "Starts and ends, flags at importance 3 or more, monitor and operator actions, plants "
    "reached and the events the judge cited."
)
# Every word inside the mermaid chart. Nothing from the log goes in it, and no label
# may hold `:`, `#`, `;` or `%` or start with a mermaid keyword.
CHART_TITLE = "Run timeline"
CHART_DESCRIPTION = "Agent work, the live monitor, flags and plants reached over time."
CHART_SECTIONS = {"agents": "Agent", "monitor": "Monitor", "plants": "Plants"}
CHART_AGENT = "Agent {n}"
CHART_LIVE = "Live monitor"
CHART_DEGRADED = "Degraded"
CHART_SIGNALS_ONLY = "Signals only"
CHART_LAYERS = {"signal": "Signal flag", "sweep": "Sweep flag", "judge": "Judge flag"}
CHART_FLAG = "Flag"
CHART_FLAG_LABEL = "{layer}, importance {importance} of 5"
CHART_FLAG_ON = "{layer} on {agent}, importance {importance} of 5"
CHART_PLANT = "Plant {n}"

# Marks around the chart, its legend and notes, so an export can drop them together.
CHART_MARK = "<!-- aims:chart -->"
CHART_END = "<!-- aims:chart end -->"

# Words around the chart, outside the fence.
CHART_CLOCK = "The chart's axis shows clock time in your time zone."
CHART_REVIEW = "Its flags are from review {run}."
CHART_FLAGS_CUT = (
    "It shows the {shown} flags of highest importance. The table below has all {total}."
)
CHART_PLANTS_CUT = "It shows the first {shown} plants reached. The table below has all {total}."
CHART_AGENTS_HEAD = ["In the chart", "Agent"]
CHART_PLANTS_HEAD = ["In the chart", "Plant", "Agent", "Reached", "At"]

MOMENT = {
    "run_started": "Run started",
    "agent_started": "Agent started",
    "agent_finished": "Agent finished",
    "run_finished": "Run finished",
    "flag": "Flag",
    "monitor_action": "Monitor action",
    "operator_action": "Operator action",
    "degraded": "Monitor degraded",
    "plant": "Plant reached",
    "report": "Report to the operator",
    "scored": "Verifier reward",
    "verdict": "Judge verdict",
    "evidence": "Judge evidence",
}

COSTS = "Cost: agents {agents}. Live monitor {monitor}, judge included."
REVIEW_COST = "This review {review}, judge included."
AT_LEAST = "at least {usd}"
UNKNOWN = "unknown"
COUNTS = "{events}, {steps}, {flags}."
COSTS_TITLE = "Model calls by caller"
KERNEL_TITLE = "Kernel events"
NO_KERNEL = "No kernel events."
NO_SOURCES = "None."
LIVE_SOURCE = "Live monitor"
REVIEW_SOURCE = "Review {run}"

APPENDIX_SUMMARY = "Final answers, reports to the operator and judge disagreements"
ANSWER = "Final answer of {agent}"
REPORT = "Report of {agent}"
DISAGREEMENTS = "Judge disagreements"
NO_APPENDIX = "None."

WARNINGS = {
    "in_progress": ("NOTE", "In progress. This report shows the log so far."),
    "verify_failed": (
        "CAUTION",
        "The hash chain did not verify. Treat every fact below as unproven.",
    ),
    "monitor_degraded": ("WARNING", "{who}'s {part} stopped at {at}. {then}"),
    "audit_lost": ("WARNING", "The kernel audit missed {lost}, first at {at}."),
    "log_truncated": (
        "WARNING",
        "The log had a torn last line at {at}. The writer cut it and kept the bytes.",
    ),
    "replay_divergence": (
        "WARNING",
        "The replay differed from its recording {count} times, first at {at}.",
    ),
    "cost_incomplete": ("NOTE", "The cost of {agent} is incomplete. Some calls had no price."),
    "tainted": (
        "WARNING",
        "Flags raised while the verifier ran: {seqs}. The reward stands as given.",
    ),
}
BUDGET = "{who} ran out of budget at {at}. {then}"
TAINTED_REVIEW = "{who} flagged events of the verifier: {seqs}. The reward stands as given."
WHO = "The monitor"
WHO_LIVE = "The live monitor"
WHO_REVIEW = "Review {run}"
PART = {
    "sweep": "sweeps",
    "judge": "judge",
    "review": "review",
    "signal": "signals",
    "budget": "budget",
}
THEN = {
    "continue": "It went on without that part.",
    "pause": "It paused the agents.",
    "signals_only": "From there it ran signals only.",
    "skip": "It skipped that part.",
}
LOG_SAYS = "The log says:"
LOST_EVENTS = "{n} events"
LOST_UNKNOWN = "events"
