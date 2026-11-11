"""In-memory store for tests and local development without a database."""

import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.ports.google_oauth import ProfileLocation
from app.ports.store import (
    Account,
    GoogleConnection,
    Location,
    SelectedProfile,
    StoredSummary,
    WaitlistSignup,
)


@dataclass
class AccountRow:
    id: str
    google_sub: str
    email: str
    selected_location_id: str | None = None


@dataclass
class LocationRow:
    id: str
    account_id: str
    profile: ProfileLocation
    removed: bool = False


@dataclass
class SessionRow:
    account_id: str
    expires_at: datetime
    revoked_at: datetime | None = None


class MemoryStore:
    def __init__(self, fail: bool = False) -> None:
        self.connections: list[GoogleConnection] = []
        self.signups: dict[tuple[str, str], WaitlistSignup] = {}
        self.events: list[tuple[str, str | None, str | None, int | None]] = []
        self.accounts: dict[str, AccountRow] = {}  # by google_sub
        self.locations: list[LocationRow] = []
        self.sessions: dict[str, SessionRow] = {}  # by token hash, never the token
        # What the worker would write, by (google_sub, Google location name).
        self.summaries: dict[tuple[str, str], StoredSummary] = {}
        self._fail = fail

    def _check(self) -> None:
        if self._fail:
            raise RuntimeError("fallo simulado")

    async def add_signup(self, signup: WaitlistSignup) -> None:
        self._check()
        self.signups.setdefault((signup.channel, signup.contact), signup)

    async def add_google_connection(self, connection: GoogleConnection) -> None:
        self._check()
        self.connections.append(connection)

    async def record_event(
        self,
        kind: str,
        source: str | None = None,
        place_id: str | None = None,
        score: int | None = None,
    ) -> None:
        self._check()
        self.events.append((kind, source, place_id, score))

    def _account(self, account_id: str) -> AccountRow:
        return next(a for a in self.accounts.values() if a.id == account_id)

    async def upsert_account(self, google_sub: str, email: str) -> str:
        self._check()
        row = self.accounts.setdefault(google_sub, AccountRow(str(uuid.uuid4()), google_sub, email))
        row.email = email
        return row.id

    async def sync_locations(
        self, account_id: str, locations: Sequence[ProfileLocation], now: datetime
    ) -> None:
        self._check()
        stale = {r.profile.location: r for r in self.locations if r.account_id == account_id}
        for profile in locations:
            row = stale.pop(profile.location, None)
            if row is None:
                row = LocationRow(f"loc_{secrets.token_hex(6)}", account_id, profile)
                self.locations.append(row)
            row.profile = profile
            row.removed = False
        account = self._account(account_id)
        for row in stale.values():
            row.removed = True
            if account.selected_location_id == row.id:
                account.selected_location_id = None

    async def create_session(
        self, account_id: str, token_hash: str, now: datetime, expires_at: datetime
    ) -> None:
        self._check()
        self.sessions[token_hash] = SessionRow(account_id, expires_at)

    async def account_for_session(self, token_hash: str, now: datetime) -> Account | None:
        self._check()
        session = self.sessions.get(token_hash)
        if session is None or session.revoked_at is not None or session.expires_at <= now:
            return None
        row = self._account(session.account_id)
        return Account(row.id, row.email, row.selected_location_id)

    async def revoke_session(self, token_hash: str, now: datetime) -> None:
        self._check()
        session = self.sessions.get(token_hash)
        if session is not None and session.revoked_at is None:
            session.revoked_at = now

    async def list_locations(self, account_id: str) -> list[Location]:
        self._check()
        found = [
            Location(r.id, r.profile.title, r.profile.place_id or None, r.profile.verified)
            for r in self.locations
            if r.account_id == account_id and not r.removed
        ]
        return sorted(found, key=lambda loc: (loc.title, loc.id))

    async def select_location(self, account_id: str, location_id: str) -> bool:
        self._check()
        owned = any(
            r.id == location_id and r.account_id == account_id and not r.removed
            for r in self.locations
        )
        if owned:
            self._account(account_id).selected_location_id = location_id
        return owned

    def put_summary(self, google_sub: str, location: str, summary: StoredSummary) -> None:
        """Stand-in for the worker: the API itself never writes a summary."""
        self.summaries[(google_sub, location)] = summary

    async def selected_profile(self, account_id: str) -> SelectedProfile | None:
        self._check()
        account = self._account(account_id)
        row = next(
            (
                r
                for r in self.locations
                if r.id == account.selected_location_id
                and r.account_id == account_id
                and not r.removed
            ),
            None,
        )
        if row is None:
            return None
        key = (account.google_sub, row.profile.location)
        return SelectedProfile(account.google_sub, row.profile.location, self.summaries.get(key))
