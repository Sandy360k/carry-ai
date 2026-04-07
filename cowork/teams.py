"""
carry-ai/cowork/teams.py -- Team Management & Task Coordination
================================================================

Lightweight team and task system. Teams coordinate multiple users,
tasks track work items, and a simple cron scheduler handles recurring
prompts.

All data persisted to config/teams.json on USB.
"""

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger("carry-ai.cowork.teams")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Task:
    """A work item within a team."""
    task_id: str = ""
    team_id: str = ""
    description: str = ""
    assigned_to: str = ""          # User alias
    status: str = "pending"        # pending | in_progress | completed | blocked
    priority: int = 2              # 1=high, 2=medium, 3=low
    created_by: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float = 0.0
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Team:
    """A named team with members and tasks."""
    team_id: str = ""
    name: str = ""
    members: list = field(default_factory=list)    # User aliases
    description: str = ""
    status: str = "active"         # active | archived
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Team":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class CronJob:
    """A recurring scheduled task."""
    cron_id: str = ""
    schedule: str = ""             # Simplified: "daily", "hourly", "weekly", or cron expr
    prompt: str = ""               # Agent prompt to execute
    team_id: str = ""              # Optional team scope
    enabled: bool = True
    last_run_at: float = 0.0
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CronJob":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def is_due(self) -> bool:
        """Check if this job should run based on schedule and last run."""
        if not self.enabled:
            return False
        now = time.time()
        elapsed = now - self.last_run_at

        schedule_map = {
            "hourly": 3600,
            "daily": 86400,
            "weekly": 604800,
        }
        interval = schedule_map.get(self.schedule.lower())
        if interval:
            return elapsed >= interval

        # Basic cron: not implemented yet, always returns False for complex exprs
        return False


# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------

class TeamRegistry:
    """
    Manages teams and their members.

    Usage:
        reg = TeamRegistry("config/teams.json")
        reg.load()

        team = reg.create_team("backend", members=["alice", "bob"])
        reg.add_member(team.team_id, "charlie")

        reg.save()
    """

    def __init__(self, storage_path: str = None):
        if storage_path is None:
            project_root = Path(__file__).resolve().parent.parent
            storage_path = str(project_root / "config" / "teams.json")
        self.storage_path = storage_path
        self._teams: dict[str, Team] = {}
        self._tasks: dict[str, Task] = {}
        self._crons: dict[str, CronJob] = {}

    # -- Persistence -------------------------------------------------------

    def load(self):
        if not os.path.isfile(self.storage_path):
            return
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._teams = {k: Team.from_dict(v) for k, v in data.get("teams", {}).items()}
            self._tasks = {k: Task.from_dict(v) for k, v in data.get("tasks", {}).items()}
            self._crons = {k: CronJob.from_dict(v) for k, v in data.get("crons", {}).items()}
            logger.info("Loaded %d teams, %d tasks, %d crons",
                        len(self._teams), len(self._tasks), len(self._crons))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load teams data: %s", e)

    def save(self):
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        data = {
            "teams": {k: v.to_dict() for k, v in self._teams.items()},
            "tasks": {k: v.to_dict() for k, v in self._tasks.items()},
            "crons": {k: v.to_dict() for k, v in self._crons.items()},
        }
        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    # -- Teams -------------------------------------------------------------

    def create_team(self, name: str, members: list = None,
                    description: str = "") -> Team:
        team = Team(
            team_id=str(uuid.uuid4())[:8],
            name=name,
            members=members or [],
            description=description,
            created_at=time.time(),
        )
        self._teams[team.team_id] = team
        self.save()
        logger.info("Created team: %s (%s)", name, team.team_id)
        return team

    def get_team(self, team_id: str) -> Optional[Team]:
        return self._teams.get(team_id)

    def find_team(self, name: str) -> Optional[Team]:
        """Find team by name (case-insensitive)."""
        for t in self._teams.values():
            if t.name.lower() == name.lower():
                return t
        return None

    def add_member(self, team_id: str, member: str) -> bool:
        team = self._teams.get(team_id)
        if team and member not in team.members:
            team.members.append(member)
            self.save()
            return True
        return False

    def remove_member(self, team_id: str, member: str) -> bool:
        team = self._teams.get(team_id)
        if team and member in team.members:
            team.members.remove(member)
            self.save()
            return True
        return False

    def archive_team(self, team_id: str) -> bool:
        team = self._teams.get(team_id)
        if team:
            team.status = "archived"
            self.save()
            return True
        return False

    def list_teams(self, active_only: bool = True) -> list[Team]:
        teams = list(self._teams.values())
        if active_only:
            teams = [t for t in teams if t.status == "active"]
        return teams

    # -- Tasks -------------------------------------------------------------

    def create_task(self, team_id: str, description: str,
                    assigned_to: str = "", priority: int = 2,
                    created_by: str = "") -> Task:
        task = Task(
            task_id=str(uuid.uuid4())[:8],
            team_id=team_id,
            description=description,
            assigned_to=assigned_to,
            priority=priority,
            created_by=created_by,
            created_at=time.time(),
            updated_at=time.time(),
        )
        self._tasks[task.task_id] = task
        self.save()
        return task

    def update_task(self, task_id: str, **kwargs) -> Optional[Task]:
        task = self._tasks.get(task_id)
        if not task:
            return None
        for key, val in kwargs.items():
            if hasattr(task, key):
                setattr(task, key, val)
        task.updated_at = time.time()
        if kwargs.get("status") == "completed":
            task.completed_at = time.time()
        self.save()
        return task

    def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list_tasks(self, team_id: str = None, status: str = None,
                   assigned_to: str = None) -> list[Task]:
        tasks = list(self._tasks.values())
        if team_id:
            tasks = [t for t in tasks if t.team_id == team_id]
        if status:
            tasks = [t for t in tasks if t.status == status]
        if assigned_to:
            tasks = [t for t in tasks if t.assigned_to == assigned_to]
        tasks.sort(key=lambda t: (t.priority, -t.created_at))
        return tasks

    def delete_task(self, task_id: str) -> bool:
        if task_id in self._tasks:
            del self._tasks[task_id]
            self.save()
            return True
        return False

    # -- Cron jobs ---------------------------------------------------------

    def create_cron(self, schedule: str, prompt: str,
                    team_id: str = "") -> CronJob:
        cron = CronJob(
            cron_id=str(uuid.uuid4())[:8],
            schedule=schedule,
            prompt=prompt,
            team_id=team_id,
            created_at=time.time(),
        )
        self._crons[cron.cron_id] = cron
        self.save()
        return cron

    def get_due_crons(self) -> list[CronJob]:
        """Return cron jobs that are due for execution."""
        return [c for c in self._crons.values() if c.is_due()]

    def mark_cron_run(self, cron_id: str):
        cron = self._crons.get(cron_id)
        if cron:
            cron.last_run_at = time.time()
            self.save()

    def list_crons(self) -> list[CronJob]:
        return list(self._crons.values())

    def delete_cron(self, cron_id: str) -> bool:
        if cron_id in self._crons:
            del self._crons[cron_id]
            self.save()
            return True
        return False

    # -- Summary -----------------------------------------------------------

    def summary(self) -> dict:
        """Return overview stats."""
        active_teams = [t for t in self._teams.values() if t.status == "active"]
        pending_tasks = [t for t in self._tasks.values() if t.status in ("pending", "in_progress")]
        return {
            "teams": len(active_teams),
            "total_tasks": len(self._tasks),
            "pending_tasks": len(pending_tasks),
            "cron_jobs": len(self._crons),
            "members": list(set(
                m for t in active_teams for m in t.members
            )),
        }
