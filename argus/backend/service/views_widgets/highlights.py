from dataclasses import dataclass
from datetime import datetime, UTC
from uuid import UUID

from flask import abort

from argus.backend.db import ScyllaCluster
from argus.backend.models.view_widgets import WidgetHighlights, WidgetComment


@dataclass
class Highlight:
    view_id: UUID
    index: int
    created_at: datetime
    creator_id: UUID
    content: str
    archived_at: datetime
    comments_count: int

    @classmethod
    def from_db_model(cls, model: WidgetHighlights):
        created_at = model.created_at.replace(tzinfo=UTC).timestamp()
        archived_at = model.archived_at.replace(tzinfo=UTC).timestamp() if model.archived_at else None
        return cls(
            view_id=model.view_id,
            index=model.index,
            created_at=created_at,
            creator_id=model.creator_id,
            content=model.content,
            archived_at=archived_at,
            comments_count=model.comments_count,
        )


@dataclass
class ActionItem(Highlight):
    assignee_id: UUID | None
    completed: bool

    @classmethod
    def from_db_model(cls, model: WidgetHighlights):
        created_at = model.created_at.replace(tzinfo=UTC).timestamp()
        archived_at = model.archived_at.replace(tzinfo=UTC).timestamp() if model.archived_at else None
        return cls(
            view_id=model.view_id,
            index=model.index,
            created_at=created_at,
            creator_id=model.creator_id,
            content=model.content,
            archived_at=archived_at,
            comments_count=model.comments_count,
            assignee_id=model.assignee_id,
            completed=model.completed,
        )


@dataclass
class CommentCreate:
    view_id: UUID
    index: int
    highlight_created_at: float
    content: str


@dataclass
class CommentUpdate:
    view_id: UUID
    index: int
    highlight_created_at: float
    created_at: float
    content: str

@dataclass
class CommentDelete:
    view_id: UUID
    index: int
    highlight_created_at: float
    created_at: float

@dataclass
class Comment:
    view_id: UUID
    index: int
    highlight_created_at: datetime
    created_at: datetime
    creator_id: UUID
    content: str

    @classmethod
    def from_db_model(cls, model: WidgetComment):
        highlight_created_at = model.highlight_at.replace(tzinfo=UTC).timestamp()
        created_at = model.created_at.replace(tzinfo=UTC).timestamp()
        return cls(
            view_id=model.view_id,
            index=model.index,
            highlight_created_at=highlight_created_at,
            created_at=created_at,
            creator_id=model.creator_id,
            content=model.content,
        )


@dataclass
class HighlightCreate:
    view_id: UUID
    index: int
    content: str
    is_task: bool


@dataclass
class HighlightArchive:
    view_id: UUID
    index: int
    created_at: float


@dataclass
class HighlightUpdate:
    view_id: UUID
    index: int
    created_at: float
    content: str


@dataclass
class HighlightSetAssignee:
    view_id: UUID
    index: int
    created_at: float
    assignee_id: str | None = None


@dataclass
class HighlightSetCompleted:
    view_id: UUID
    index: int
    created_at: float
    completed: bool


class HighlightsService:

    def __init__(self) -> None:
        self.cluster = ScyllaCluster.get()

    def create(
            self,
            creator: UUID,
            payload: HighlightCreate,
    ) -> Highlight | ActionItem:
        created_at = datetime.now(UTC)
        highlight = WidgetHighlights(
            view_id=payload.view_id,
            index=payload.index,
            created_at=created_at,
            creator_id=creator,
            content=payload.content,
            completed=None if not payload.is_task else False,
            archived=datetime.fromtimestamp(0, tz=UTC),
            comments_count=0,
        )
        highlight.save()
        if payload.is_task:
            return ActionItem.from_db_model(highlight)
        return Highlight.from_db_model(highlight)

    def archive_highlight(self, payload: HighlightArchive):
        entry = WidgetHighlights.objects(
            view_id=payload.view_id, index=payload.index, created_at=datetime.fromtimestamp(payload.created_at, tz=UTC)
        ).first()
        if entry:
            entry.archived_at = datetime.now(UTC)
            entry.save()

    def unarchive_highlight(self,  payload: HighlightArchive):
        entry = WidgetHighlights.objects(
            view_id=payload.view_id,
            index=payload.index,
            created_at=datetime.fromtimestamp(payload.created_at, tz=UTC)
        ).first()
        if entry:
            entry.archived_at = datetime.fromtimestamp(0, tz=UTC)
            entry.save()

    def update_highlight(self, user_id: UUID, payload: HighlightUpdate) -> Highlight | ActionItem:
        entry = WidgetHighlights.objects(
            view_id=payload.view_id,
            index=payload.index,
            created_at=datetime.fromtimestamp(payload.created_at, tz=UTC)
        ).first()
        if not entry:
            abort(404, description="Highlight not found")
        if entry.creator_id != user_id:
            abort(403, description="Not authorized to update highlight")
        entry.content = payload.content
        entry.save()
        if entry.completed is None:
            return Highlight.from_db_model(entry)
        else:
            return ActionItem.from_db_model(entry)

    def set_assignee(self, payload: HighlightSetAssignee) -> ActionItem:
        entry = WidgetHighlights.objects(
            view_id=payload.view_id,
            index=payload.index,
            created_at=datetime.fromtimestamp(payload.created_at, tz=UTC)
        ).first()
        if not entry or entry.completed is None:
            abort(404, description="ActionItem not found")
        if payload.assignee_id is None:
            entry.assignee_id = None
        else:
            entry.assignee_id = UUID(payload.assignee_id)
        entry.save()
        return ActionItem.from_db_model(entry)

    def set_completed(self, payload: HighlightSetCompleted) -> ActionItem:
        entry = WidgetHighlights.objects(
            view_id=payload.view_id,
            index=payload.index,
            created_at=datetime.fromtimestamp(payload.created_at, tz=UTC)
        ).first()
        if not entry or entry.completed is None:
            abort(404, description="ActionItem not found")
        entry.completed = payload.completed
        entry.save()
        return ActionItem.from_db_model(entry)

    def get_highlights(self, view_id: UUID, index: int) -> tuple[list[Highlight], list[ActionItem]]:
        entries = WidgetHighlights.objects(view_id=view_id, index=index)
        highlights = [Highlight.from_db_model(entry) for entry in entries if entry.completed is None]
        action_items = [ActionItem.from_db_model(entry) for entry in entries if entry.completed is not None]
        return highlights, action_items

    def create_comment(self, creator_id: UUID, payload: CommentCreate) -> Comment:
        highlight_created_at = datetime.fromtimestamp(payload.highlight_created_at, tz=UTC)
        highlight = WidgetHighlights.objects(view_id=payload.view_id, index=payload.index, created_at=highlight_created_at).first()
        if not highlight:
            abort(404, description="Highlight not found")
        created_at = datetime.now(UTC)
        comment = WidgetComment(
            view_id=payload.view_id,
            index=payload.index,
            highlight_at=highlight_created_at,
            created_at=created_at,
            creator_id=creator_id,
            content=payload.content,
        )
        comment.save()
        highlight.comments_count += 1
        highlight.save()
        return Comment.from_db_model(comment)

    def update_comment(self, user_id: UUID, payload: CommentUpdate) -> Comment:
        highlight_created_at = datetime.fromtimestamp(payload.highlight_created_at, tz=UTC)
        created_at = datetime.fromtimestamp(payload.created_at, tz=UTC)
        comment = WidgetComment.objects(
            view_id=payload.view_id,
            index=payload.index,
            highlight_at=highlight_created_at,
            created_at=created_at,
        ).first()
        if not comment:
            abort(404, description="Comment not found")
        if comment.creator_id != user_id:
            abort(403, description="Not authorized to update comment")
        comment.content = payload.content
        comment.save()
        return Comment.from_db_model(comment)

    def delete_comment(self, user_id: UUID, payload: CommentDelete):
        index = int(payload.index)
        highlight_created_at = datetime.fromtimestamp(payload.highlight_created_at, tz=UTC)
        created_at = datetime.fromtimestamp(payload.created_at, tz=UTC)
        comment = WidgetComment.objects(
            view_id=payload.view_id,
            index=index,
            highlight_at=highlight_created_at,
            created_at=created_at,
        ).first()
        if not comment:
            abort(404, description="Comment not found")
        if comment.creator_id != user_id:
            abort(403, description="Not authorized to delete comment")
        comment.delete()
        highlight = WidgetHighlights.objects(view_id=payload.view_id, index=index, created_at=highlight_created_at).first()
        if not highlight:
            abort(404, description="Highlight not found")
        highlight.comments_count -= 1
        highlight.save()

    def get_comments(self, view_id: UUID, index: int, highlight_created_at: float) -> list[Comment]:
        highlight_created_at = datetime.fromtimestamp(highlight_created_at, tz=UTC)
        comments = WidgetComment.objects(view_id=view_id, index=index, highlight_at=highlight_created_at)
        return [Comment.from_db_model(c) for c in comments]