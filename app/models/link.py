from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.click_stat import ClickStat


class Link(Base):
    __tablename__ = "links"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(
        String(32),
        unique=True,
        index=True,
        nullable=False,
    )
    long_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    click_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # Relationships
    click_stats: Mapped[list["ClickStat"]] = relationship(
        "ClickStat",
        back_populates="link",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        # Partial index for fast lookups on active links
        Index(
            "idx_links_code_active",
            "code",
            postgresql_where=(is_deleted.is_(False)),
        ),
        # Partial index on expiration for cleanup worker queries
        Index(
            "idx_links_expires_at",
            "expires_at",
            postgresql_where=(expires_at.is_not(None)),
        ),
    )

    def is_expired(self, now: datetime | None = None) -> bool:
        """Determines if the link has exceeded its configured expiration timestamp."""
        if not self.expires_at:
            return False
        if now is None:
            now = datetime.now(self.expires_at.tzinfo)
        return now >= self.expires_at
