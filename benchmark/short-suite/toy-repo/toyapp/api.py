from __future__ import annotations

from dataclasses import dataclass
from time import sleep


@dataclass(frozen=True)
class User:
    user_id: str
    email: str
    plan: str


class APIClient:
    def fetch_user(self, user_id: str) -> User:
        sleep(0.01)
        return User(user_id=user_id, email=f"{user_id}@example.com", plan="free")

    def fetch_account_summary(self, user_id: str) -> dict[str, object]:
        user = self.fetch_user(user_id)
        return {
            "user_id": user.user_id,
            "email": user.email,
            "plan": user.plan,
            "features": ["tickets", "reports"],
        }
