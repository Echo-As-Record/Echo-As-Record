"""③ attribution: injectable framework and the v1.2 matching/protection slice."""

from ear_attribution.matching import MatchConfig
from ear_attribution.planning import BusinessPlanner
from ear_attribution.service import AttributionService, Planner
from ear_attribution.transactions import CommitJournal

__all__ = ["AttributionService", "BusinessPlanner", "CommitJournal", "MatchConfig", "Planner"]
