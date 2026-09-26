"""Sistema nativo de tickets de soporte técnico."""

from backend.apps.support.models import (
    SupportCategoryEnum,
    SupportTicket,
    TicketMessage,
    TicketPriorityEnum,
    TicketStatusEnum,
)

__all__ = [
    "SupportCategoryEnum",
    "SupportTicket",
    "TicketMessage",
    "TicketPriorityEnum",
    "TicketStatusEnum",
]
