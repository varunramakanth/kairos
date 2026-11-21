"""Users of the deployment (ADR-018): who may sign in, their role and their spend cap."""

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncEngine

from kairos_domain.errors import KairosError
from kairos_domain.users import LOCAL_USER, Role, User

_COLUMNS = (
    "id, email, role, daily_cap_usd::float AS daily_cap_usd, email_verified, disabled, created_at"
)


class UserExistsError(KairosError):
    """Someone with this email is already a user."""


def _user(row: RowMapping) -> User:
    return User.model_validate(dict(row))


class UserStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def get(self, user_id: str) -> User | None:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(f"SELECT {_COLUMNS} FROM users WHERE id = :id"),  # noqa: S608
                        {"id": user_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _user(row) if row else None

    async def list_users(self) -> list[User]:
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                text(f"SELECT {_COLUMNS} FROM users ORDER BY created_at"),  # noqa: S608
            )
            return [_user(r) for r in rows.mappings()]

    async def admins(self) -> list[User]:
        """Admins who are not disabled: who system alerts (eval regressions) go to."""
        return [u for u in await self.list_users() if u.role is Role.ADMIN and not u.disabled]

    async def create(
        self, user_id: str, email: str, *, role: Role, daily_cap_usd: float | None
    ) -> User:
        async with self._engine.begin() as connection:
            taken = (
                await connection.execute(
                    text("SELECT 1 FROM users WHERE lower(email) = lower(:email)"),
                    {"email": email},
                )
            ).first()
            if taken is not None:
                raise UserExistsError(f"{email} is already a user")
            row = (
                (
                    await connection.execute(
                        text(
                            "INSERT INTO users (id, email, role, daily_cap_usd) "  # noqa: S608
                            f"VALUES (:id, :email, :role, :cap) RETURNING {_COLUMNS}"
                        ),
                        {"id": user_id, "email": email, "role": role.value, "cap": daily_cap_usd},
                    )
                )
                .mappings()
                .one()
            )
        return _user(row)

    async def update(
        self,
        user_id: str,
        *,
        daily_cap_usd: float | None = None,
        clear_cap: bool = False,
        disabled: bool | None = None,
        email_verified: bool | None = None,
    ) -> User | None:
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "UPDATE users SET "  # noqa: S608  # constant SQL
                            "daily_cap_usd = CASE WHEN :clear THEN NULL "
                            "  ELSE COALESCE(CAST(:cap AS numeric), daily_cap_usd) END, "
                            "disabled = COALESCE(CAST(:disabled AS boolean), disabled), "
                            "email_verified = COALESCE(CAST(:verified AS boolean), email_verified) "
                            f"WHERE id = :id RETURNING {_COLUMNS}"
                        ),
                        {
                            "id": user_id,
                            "cap": daily_cap_usd,
                            "clear": clear_cap,
                            "disabled": disabled,
                            "verified": email_verified,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _user(row) if row else None

    async def delete(self, user_id: str) -> bool:
        """Remove a user and, through ``ON DELETE CASCADE``, everything they own."""
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text("DELETE FROM users WHERE id = :id AND role <> 'admin'"), {"id": user_id}
            )
        return result.rowcount == 1

    async def unbound_owner(self) -> User | None:
        """The ``local`` admin, if no Cognito sign-in has claimed it yet."""
        return await self.get(LOCAL_USER)

    async def bind_owner(self, sub: str, email: str) -> User | None:
        """Rename ``local`` to the owner's Cognito ``sub``. Every owned row follows
        through ``ON UPDATE CASCADE``. ``None`` if ``local`` was already claimed."""
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "UPDATE users SET id = :sub, email = :email, email_verified = true "  # noqa: S608
                            f"WHERE id = :local RETURNING {_COLUMNS}"
                        ),
                        {"sub": sub, "email": email, "local": LOCAL_USER},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _user(row) if row else None

    async def set_local_email(self, email: str) -> None:
        """Give the ``local`` admin the configured address, so its notifications have a
        recipient when sign-in is off."""
        async with self._engine.begin() as connection:
            await connection.execute(
                text("UPDATE users SET email = :email WHERE id = :local AND email IS NULL"),
                {"email": email, "local": LOCAL_USER},
            )
