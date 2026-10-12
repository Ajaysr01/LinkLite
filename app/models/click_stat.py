from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Date, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.link import Link


class ClickStat(Base):
    __tablename__ = "click_stats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("links.code", ondelete="CASCADE"),
        nullable=False,
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # Relationships
    link: Mapped["Link"] = relationship("Link", back_populates="click_stats")

    __table_args__ = (
        UniqueConstraint("code", "date", name="uq_click_stats_code_date"),
        Index("idx_click_stats_code_date", "code", "date"),
    )
